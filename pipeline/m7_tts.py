"""M7 TTS 整集合成运行器（任务 T20 增补）—— C5 计划 → :9002 逐句合成。

背景：M7 的服务端（gpu-services/tts）与单句客户端（pipeline/tts_client.py）
T12 已交付，但"整集合成"此前只作为 scripts/e2e_smoke.sh 的内联段存在，
任务队列（pipeline/queue.py，M14）的默认步骤图需要可执行的模块 CLI ——
本模块把该内联段按原口径落成模块（行为忠实移植，含 atempo 微调）。

口径（与 e2e_smoke.sh 内联段一致）：
- 读 C5 ``07_synth/synth_plan.<lang>.jsonl``（缺则回退无后缀副本）；
- ``keep_original=true`` 句跳过（哭/尖叫等保留原声，不合成）；
- 逐句 ``TtsClient.synth``：voice_ref=角色音色参考、emo_ref=原片人声
  （音色与情绪参考分离）、emo_alpha/duration_factor/engine 均取 C5 行；
- ``atempo`` 偏离 1.0（>1e-3）时 ffmpeg 一次微调回写（M8 决策的执行位）；
- 一句都不可合成（C5 全 keep_original）→ exit 1（不伪造产出）。

闭环时长校准（D2 修复，任务 A 路定案）：
  M8 计划层的 est（文本占位模型）与合成引擎实测存在 10–20% 双向系统偏差
  （``duration_factor`` 线性缩放假设不完全成立——实测响应单调但非线性），
  计划层"预测落窗"不等于"落盘落窗"。M7 在整集合成后按**实测**闭环校准
  （窗口口径与 M15 dur_align 完全一致：C4 ``budget.lo/hi``，缺行按 C2 窗×
  ratio 自算；原句 ±10% 产品契约不在本层重定义）：

  ① 微调兜底（零调用）：对每句落盘 wav 实测时长，若落在 atempo 微调窗
    （configs/pipeline.yaml ``m8.atempo_window``，默认 [0.9,1.1]，T14 听感
    口径）内可达窗，ffmpeg 一次变速不变调直接收口——确定性、不占 GPU；
  ② 实测反馈重合成（迭代 1–2 轮收敛）：微调不可达的句子，按实测响应修正
    ``duration_factor``（C5 契约域 [0.5,2.0]）重合成——首轮比例修正
    （d∝df 一阶近似），次轮起用过最近两个实测点的割线内插（不假设全局
    线性）；提案无实质变化（步长 <0.02）或已到域界即停，不空耗调用；
  ③ 每轮重合成后再走 ①，最后一轮结果由收尾 ① 兜底；
  ④ 仍不可解的句子如实记 ``unresolved``（保留最后一次可用产物，不伪造、
    不删句），对齐率统计由 M15 按实测如实计入未命中。

  校准发生的行回写 C5（``duration_factor``/``atempo`` 为最终执行值，
  ``expect_dur``=最终实测落盘时长——C5 从"计划"对齐为"已执行口径"，
  计划层预测口径仍见 ``align_report.<lang>.json``），并写工作纸
  ``07_synth/tts_calib.<lang>.json``（逐句修正历史/状态/调用数，非冻结契约）。

CLI::

    python -m pipeline.m7_tts --ep ep01 --lang en [--jobs-dir <dir>] [--url ...]

在线依赖：tts 服务（默认 :9002，env M7_TTS_URL 可覆盖）；服务不可达时
本模块如实失败（不静默降级、不伪造产物）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: 闭环校准默认参数（C5/C4 契约域内；configs/pipeline.yaml ``m7`` 段可覆盖）
_DEFAULT_CALIB_ROUNDS = 2            # df 重合成轮数上限（迭代 1–2 轮收敛）
_DEFAULT_ATEMPO_WINDOW = (0.9, 1.1)  # 与 M8 T14 微调窗同源（缺省回落 m8 段）
_MIN_DF_STEP = 0.02                  # df 提案最小实质变化（低于即视为收敛/到界）

#: 校准行状态（tts_calib 报告口径）
ST_IN_WINDOW = "in-window"        # 首轮合成即落窗（计划参数未动）
ST_ATEMPO_FIXED = "atempo-fixed"  # 仅 atempo 微调收口（零重合成）
ST_DF_CALIBRATED = "df-calibrated"  # 实测反馈重合成后落窗（含收尾微调）
ST_UNRESOLVED = "unresolved"      # 域界内不可解，如实未命中
ST_NO_WINDOW = "no-window"        # 无 C2 句窗且无 C4 预算，无法定窗（不校准）


def _wav_duration(path: str | Path) -> float:
    """wav 实测时长（秒，3 位小数）——与 m15_metrics._common.wav_duration 同口径。"""
    import soundfile as sf

    info = sf.info(str(path))
    return round(info.frames / float(info.samplerate), 3)


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


# ---------------------------------------------------------------------------
# 闭环校准纯函数（eval 可离线测）
# ---------------------------------------------------------------------------

def atempo_fix_for(
    meas: float,
    lo: float,
    hi: float,
    *,
    a_lo: float,
    a_hi: float,
    target: float,
) -> Optional[float]:
    """当前实测 ``meas`` → 能落窗 ``[lo,hi]`` 的 atempo（∈[a_lo,a_hi]）或 None。

    可达域推导：落窗要求 ``lo ≤ meas/a ≤ hi``，即 ``a ∈ [meas/hi, meas/lo]``，
    与微调窗 ``[a_lo,a_hi]`` 有交才可解。优先取「瞄向窗中心」的值（离窗心
    近、对采样抖动余量最大）；3 位小数取整后压线的极端改取可行域中点
    （中点必然严格在窗内）。已落窗返回 1.0（不动）。
    """
    if lo <= meas <= hi:
        return 1.0
    if meas <= 0 or hi <= 0 or lo <= 0 or target <= 0:
        return None
    f_lo = max(a_lo, meas / hi)
    f_hi = min(a_hi, meas / lo)
    if f_lo > f_hi:
        return None
    a = _clamp(meas / target, f_lo, f_hi)
    if not lo <= round(meas / a, 3) <= hi:  # 取整压线 → 可行域中点兜底
        a = (f_lo + f_hi) / 2.0
    a = round(a, 3)
    return a if lo <= meas / a <= hi else None


def df_propose(
    history: list[tuple[float, float]],
    target: float,
    *,
    df_lo: float,
    df_hi: float,
    min_step: float = _MIN_DF_STEP,
) -> Optional[float]:
    """实测响应历史 ``[(df, raw_meas)]`` → 修正 duration_factor（C5 域内）或 None。

    - 单实测点：比例修正 ``f = f₁ × target/d₁``（d∝df 一阶近似）；
    - ≥2 点：过最近两点的**割线内插**（实测响应非线性——引擎在 df→1 附近
      响应趋平、低 df 段变陡，比例外推会系统性打偏）；d₁≈d₂（响应死区）
      回落比例式。
    - 域界钳制 ``[df_lo,df_hi]`` 后与当前 df 差 < ``min_step`` 视为无实质
      变化（已到域界/已收敛）→ None，调用方停止重合成、不空耗调用。
    """
    if not history or target <= 0:
        return None
    f2, d2 = history[-1]
    if d2 <= 0:
        return None
    if len(history) >= 2:
        f1, d1 = history[-2]
        if abs(d2 - d1) > 1e-3 and abs(f2 - f1) > 1e-6:
            f = f2 + (target - d2) * (f2 - f1) / (d2 - d1)
        else:  # 响应死区：割线退化 → 比例式
            f = f2 * target / d2
    else:
        f = f2 * target / d2
    f = round(_clamp(f, df_lo, df_hi), 3)
    if abs(f - f2) < min_step:
        return None
    return f


# ---------------------------------------------------------------------------
# 目标窗解析（与 M15 dur_align 同口径：C4 budget.lo/hi → C2 窗×ratio 兜底）
# ---------------------------------------------------------------------------

def resolve_windows(
    rows: list[Any],
    ws: Path,
    lang: str,
    *,
    cfg: Optional[dict[str, Any]] = None,
) -> dict[str, Optional[tuple[float, float]]]:
    """C5 行 → (lo, hi) 目标窗；无 C4 行且无 C2 句窗的行记 None（不校准）。

    口径与 ``pipeline.m15_metrics.dur_align`` 一致：优先 C4
    ``translations[lang].budget.lo/hi``；缺行按 pipeline.yaml
    ``budget.lo_ratio/hi_ratio`` 从 C2 句窗自算（原句 ±10% 产品契约）。
    """
    from pipeline import contracts as C
    from pipeline.config import load_pipeline_config
    from pipeline.m6_translate import budget_for

    cfg = cfg if cfg is not None else load_pipeline_config()
    budget_cfg = cfg.get("budget") or {}
    lo_ratio = float(budget_cfg.get("lo_ratio", 0.9))
    hi_ratio = float(budget_cfg.get("hi_ratio", 1.1))

    utt_path = ws / "04_dial" / "utterances.jsonl"
    utts = (
        {u.utt_id: u for u in C.load_jsonl(utt_path, C.UtteranceTable).root}
        if utt_path.is_file() else {}
    )
    budgets: dict[str, Any] = {}
    trans_path = ws / "06_mt" / "translations.jsonl"
    if trans_path.is_file():
        for t in C.load_jsonl(trans_path, C.TranslationTable).root:
            if t.tgt == lang:
                budgets[t.utt_id] = t.budget

    out: dict[str, Optional[tuple[float, float]]] = {}
    for it in rows:
        b = budgets.get(it.utt_id)
        if b is not None:
            out[it.utt_id] = (float(b.lo), float(b.hi))
        elif it.utt_id in utts:
            bb = budget_for(utts[it.utt_id], lo_ratio, hi_ratio)
            out[it.utt_id] = (float(bb.lo), float(bb.hi))
        else:
            out[it.utt_id] = None
    return out


# ---------------------------------------------------------------------------
# 校准行内状态
# ---------------------------------------------------------------------------

@dataclass
class _CalibState:
    """单句校准状态（工作纸数据源；只在整集合成的单次运行内存活）。"""

    item: Any                                        # C.SynthPlanItem
    window: Optional[tuple[float, float]]            # (lo, hi)；None=无窗
    atempo_total: float = 1.0   # 落盘文件相对引擎 raw 的累计 atempo（含计划值）
    history: list[tuple[float, float]] = field(default_factory=list)
    #   ↑ (duration_factor, 原始实测时长) 时序表（raw=未叠 atempo 的引擎输出）
    rounds: int = 0                                  # 已发生的 df 重合成次数
    atempo_applied: Optional[float] = None           # 校准施加的 atempo（非计划值）
    status: str = ""                                 # 收口后定值（ST_* 常量）
    meas_final: Optional[float] = None               # 最终落盘实测时长
    df_exhausted: bool = False                       # 提案无实质变化/域界/失败 → 不再重合成

    @property
    def calibrated(self) -> bool:
        """是否发生过校准干预（C5 回写判据；首轮即落窗的行不动计划）。"""
        return self.rounds > 0 or self.atempo_applied is not None


def _apply_atempo(out: Path, a: float, *, ffmpeg: str) -> float:
    """ffmpeg atempo 变速不变调回写（M8/M7 既有口径），返回落盘实测时长。"""
    tmp = out.with_name(out.stem + ".atempo.wav")
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(out),
         "-af", f"atempo={a:.4f}", str(tmp)],
        check=True, timeout=300)
    tmp.replace(out)
    return _wav_duration(out)


# ---------------------------------------------------------------------------
# 整集合成 + 闭环校准
# ---------------------------------------------------------------------------

def synth_episode(ep: str, lang: str, *, jobs_dir: str | Path,
                  url: Optional[str] = None) -> dict:
    """C5 → :9002 整集合成 + 闭环时长校准。

    返回 ``{"n", "skipped", "wavs", "tts_calls", "calib"}``（前三键为既有
    口径；``calib`` 为校准摘要，与 tts_calib 报告同源）。
    """
    from pipeline import contracts as C
    from pipeline.config import load_pipeline_config
    from pipeline.m1_ingest import FFMPEG
    from pipeline.tts_client import DURATION_FACTOR_RANGE, TtsClient, TtsError

    cfg = load_pipeline_config()
    if jobs_dir:
        root = Path(jobs_dir)
    else:
        root = Path(cfg["paths"]["jobs_dir"])
    ws = root / ep
    plan_path = ws / "07_synth" / f"synth_plan.{lang}.jsonl"
    if not plan_path.is_file():
        plan_path = ws / "07_synth" / "synth_plan.jsonl"
    if not plan_path.is_file():
        raise FileNotFoundError(
            f"C5 不存在: {ws/'07_synth'/f'synth_plan.{lang}.jsonl'}（先跑 M8）")
    rows = C.load_jsonl(plan_path, C.SynthPlanTable).root

    m7cfg = cfg.get("m7") or {}
    calib_rounds = max(0, int(m7cfg.get("calib_rounds", _DEFAULT_CALIB_ROUNDS)))
    atempo_win = (m7cfg.get("atempo_window")
                  or (cfg.get("m8") or {}).get("atempo_window")
                  or list(_DEFAULT_ATEMPO_WINDOW))
    a_lo, a_hi = float(atempo_win[0]), float(atempo_win[1])
    df_lo, df_hi = DURATION_FACTOR_RANGE  # C5 契约域（contracts.SynthPlanItem 同源）

    cli = TtsClient(url) if url else TtsClient()
    windows = resolve_windows(rows, ws, lang, cfg=cfg)

    tts_calls = 0
    n = 0
    skipped = 0
    wavs: list[str] = []
    states: dict[str, _CalibState] = {}

    # ---- 首轮：按计划逐句合成（既有口径原样执行，含计划 atempo） ----
    for r in rows:
        if r.keep_original:
            skipped += 1
            print(f"skip {r.utt_id} (keep_original)")
            continue
        out = ws / r.out
        res = cli.synth(
            r.text, str(ws / r.voice_ref),
            emo_ref=str(ws / r.emo_ref) if r.emo_ref else None,
            lang=lang, emo_alpha=float(r.emo_alpha),
            duration_factor=float(r.duration_factor),
            engine=r.engine or "auto", out=str(out), utt_id=r.utt_id)
        tts_calls += 1
        if r.atempo and abs(float(r.atempo) - 1.0) > 1e-3:
            _apply_atempo(out, float(r.atempo), ffmpeg=FFMPEG)
        meas = _wav_duration(out)
        planned_atempo = float(r.atempo or 1.0)
        st = _CalibState(item=r, window=windows.get(r.utt_id),
                         atempo_total=planned_atempo)
        # 历史点记**原始**引擎输出（atempo>1 使落盘变短，raw = 落盘 × 计划
        # atempo）——df 修正只对引擎自身的响应建模，atempo 是叠加的确定性变换
        st.history.append((float(r.duration_factor), round(meas * planned_atempo, 3)))
        st.meas_final = meas
        states[r.utt_id] = st
        n += 1
        wavs.append(str(out))
        print(f"synth {r.utt_id} engine={res['engine']} "
              f"dur={res['duration_s']}s df={r.duration_factor} "
              f"atempo={r.atempo} expect={r.expect_dur}s")

    # ---- 闭环校准（D2）：实测 → atempo 兜底 / df 反馈重合成（迭代收敛） ----
    def _sweep(tag: str) -> None:
        """逐句核对落盘实测：落窗即收口；atempo 可达即微调收口（零调用）。"""
        for utt_id, st in states.items():
            if st.status or st.window is None:
                if st.window is None and not st.status:
                    st.status = ST_NO_WINDOW
                continue
            lo, hi = st.window
            out = ws / st.item.out
            meas = _wav_duration(out)
            if lo <= meas <= hi:
                st.status = ST_IN_WINDOW if st.rounds == 0 else ST_DF_CALIBRATED
                st.meas_final = meas
                print(f"calib[{tag}] {utt_id} meas={meas}s 窗[{lo},{hi}] "
                      f"→ {st.status}")
                continue
            target = _clamp(round((lo + hi) / 2, 3), lo, hi)
            a = atempo_fix_for(meas, lo, hi, a_lo=a_lo, a_hi=a_hi, target=target)
            total = round(st.atempo_total * a, 3) if a is not None else None
            if a is not None and a_lo <= total <= a_hi:
                # 复合总量仍在听感带内才施加（首轮文件已含计划 atempo，重合成
                # 后从 raw 起算）——C5 回写记录复合值，可复现落盘变换
                meas2 = _apply_atempo(out, a, ffmpeg=FFMPEG)
                st.atempo_applied = a
                st.atempo_total = total
                st.item.atempo = total
                st.meas_final = meas2
                if lo <= meas2 <= hi:
                    st.status = ST_ATEMPO_FIXED if st.rounds == 0 else ST_DF_CALIBRATED
                    print(f"calib[{tag}] {utt_id} meas={meas}s 窗[{lo},{hi}] → "
                          f"atempo={a} 实测={meas2}s → {st.status}")
                else:
                    # ffmpeg atempo 实际压缩比与请求值存在 ~1% 级偏差（分块/
                    # 取整），带界瞄准可能仍出窗——不收口不虚报，留给 df 轮/
                    # final 兜底如实计未命中
                    print(f"calib[{tag}] {utt_id} atempo={a} 后实测={meas2}s "
                          f"仍出窗（带界 {total} 已到，不虚报收口）")
            elif a is not None:
                print(f"calib[{tag}] {utt_id} atempo={a} 需复合总量 {total} "
                      f"超听感带 [{a_lo},{a_hi}]，不施加——留待 df 轮/兜底")

    for rnd in range(1, calib_rounds + 1):
        _sweep(f"round{rnd}")
        open_states = [st for st in states.values()
                       if not st.status and st.window is not None
                       and not st.df_exhausted]
        if not open_states:
            break
        for st in open_states:
            lo, hi = st.window  # type: ignore[misc]
            target = _clamp((lo + hi) / 2, lo, hi)  # 瞄窗心：对抖动余量最大
            f = df_propose(st.history, target, df_lo=df_lo, df_hi=df_hi)
            if f is None:
                st.df_exhausted = True  # 已到域界/无实质变化——不空耗调用
                print(f"calib[round{rnd}] {st.item.utt_id} df 提案无实质变化"
                      f"（域界收敛），停止重合成")
                continue
            out = ws / st.item.out
            try:
                res = cli.synth(
                    st.item.text, str(ws / st.item.voice_ref),
                    emo_ref=str(ws / st.item.emo_ref) if st.item.emo_ref else None,
                    lang=lang, emo_alpha=float(st.item.emo_alpha),
                    duration_factor=float(f),
                    engine=st.item.engine or "auto", out=str(out),
                    utt_id=st.item.utt_id)
            except TtsError as exc:  # 单句重合成失败：保留最后可用产物，如实记账
                st.df_exhausted = True
                print(f"calib[round{rnd}] {st.item.utt_id} 重合成失败: {exc} "
                      "——保留上一产物，停止该句重合成")
                continue
            tts_calls += 1
            st.rounds += 1
            st.item.duration_factor = float(f)
            st.item.atempo = 1.0  # 重合成=原始引擎输出，atempo 交由下一轮收口
            st.atempo_total = 1.0  # 文件已换为 fresh raw——累计 atempo 归一
            meas = _wav_duration(out)
            st.history.append((float(f), meas))
            st.meas_final = meas
            print(f"calib[round{rnd}] {st.item.utt_id} df={f} 实测={meas}s "
                  f"窗[{lo},{hi}] engine={res['engine']}")
    _sweep("final")

    # ---- 收尾：仍未收口的句子如实记 unresolved（保留最后产物，不删句） ----
    for st in states.values():
        if st.window is None:
            st.status = st.status or ST_NO_WINDOW
        elif not st.status:
            st.status = ST_UNRESOLVED
            lo, hi = st.window
            meas = st.meas_final if st.meas_final is not None else _wav_duration(ws / st.item.out)
            print(f"WARN {st.item.utt_id} 校准未收口: meas={meas}s 窗[{lo},{hi}] "
                  f"history={st.history} → unresolved")

    # ---- C5 回写：校准行的最终执行参数/实测时长（计划层预测口径不变） ----
    calibrated = [st for st in states.values() if st.calibrated]
    main_path = ws / "07_synth" / f"synth_plan.{lang}.jsonl"
    copy_path = ws / "07_synth" / "synth_plan.jsonl"
    if calibrated:
        # expect_dur：计划层是 est×df/atempo 预测；校准行回写为最终实测落盘
        # 时长（C5 从「计划」对齐为「已执行」口径——报告 items.meas_final 同源）
        for st in calibrated:
            if st.meas_final is not None:
                st.item.expect_dur = round(float(st.meas_final), 3)
        by_utt = {st.item.utt_id: st.item for st in states.values()}
        items = [by_utt.get(r.utt_id, r) for r in rows]
        C.upsert_jsonl(main_path, items, container=C.SynthPlanTable,
                       key_field="utt_id")
        if copy_path.is_file():  # 无后缀副本刷新为当前语种镜像（M8 同口径）
            C.dump_jsonl(copy_path, C.SynthPlanTable.model_validate(
                [line for line in C.load_jsonl(main_path, C.SynthPlanTable).root]))

    # ---- 工作纸：tts_calib.<lang>.json（非冻结契约） ----
    def _status_of(st: _CalibState) -> str:
        return st.status or ST_UNRESOLVED

    calib_summary = {
        "n_synth": n,
        "n_in_window": sum(1 for s in states.values() if s.status == ST_IN_WINDOW),
        "n_atempo_fixed": sum(1 for s in states.values() if s.status == ST_ATEMPO_FIXED),
        "n_df_calibrated": sum(1 for s in states.values() if s.status == ST_DF_CALIBRATED),
        "n_unresolved": sum(1 for s in states.values() if s.status == ST_UNRESOLVED),
        "n_no_window": sum(1 for s in states.values() if s.status == ST_NO_WINDOW),
        "unresolved": [s.item.utt_id for s in states.values()
                       if s.status == ST_UNRESOLVED],
    }
    report = {
        "ep": ep,
        "lang": lang,
        "module": "m7_tts",
        "generated_by": "m7",
        "policy": {
            "calib_rounds": calib_rounds,
            "atempo_window": [a_lo, a_hi],
            "df_range": [df_lo, df_hi],
            "min_df_step": _MIN_DF_STEP,
            "window_semantics": ("C4 budget.lo/hi（原句窗 ±10%；C4 缺行按 C2 窗"
                                 "×ratio 自算）——与 m15_metrics.dur_align 同口径"),
        },
        "tts_calls": tts_calls,
        "summary": calib_summary,
        "items": [
            {
                "utt_id": st.item.utt_id,
                "status": _status_of(st),
                "window": list(st.window) if st.window else None,
                "history": [[f, d] for f, d in st.history],
                "duration_factor_final": st.item.duration_factor,
                "atempo_final": st.item.atempo,
                "atempo_applied": st.atempo_applied,
                "meas_final": st.meas_final,
                "n_resynth": st.rounds,
                "out": st.item.out,
            }
            for st in states.values()
        ],
        "notes": (
            "tts_calib.<lang>.json 为 M7 闭环校准工作纸（非冻结契约）：首轮按 C5 "
            "计划合成后按落盘实测闭环——atempo 微调窗内可达即收口（零调用），"
            "不可达按实测反馈修正 duration_factor 重合成（割线内插，迭代 "
            f"{calib_rounds} 轮），域界内不可解如实记 unresolved；校准行已回写 "
            "C5（df/atempo=最终执行值，expect_dur=最终实测），计划层预测口径见 "
            "align_report.<lang>.json（M8 工作纸），两者差异即「计划 vs 落盘」。"
        ),
    }
    report_path = ws / "07_synth" / f"tts_calib.{lang}.json"
    _write_json_atomic(report_path, report)

    return {"n": n, "skipped": skipped, "wavs": wavs, "tts_calls": tts_calls,
            "calib": calib_summary, "report_json": str(report_path)}


def _write_json_atomic(path: Path, payload: Any) -> None:
    """单文档 JSON 原子写（与 M6/M8 同口径：tmp + os.replace）。"""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n",
    )
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.m7_tts",
        description="M7 TTS 整集合成（C5 计划 → :9002 逐句合成 + 闭环时长校准）")
    ap.add_argument("--ep", required=True, help="集 ID，如 ep01")
    ap.add_argument("--lang", required=True, help="目标语种（en/es/ar）")
    ap.add_argument("--jobs-dir", default=None,
                    help="jobs 根目录（默认 configs/pipeline.yaml）")
    ap.add_argument("--url", default=None,
                    help="tts 服务地址（默认 M7_TTS_URL 或 http://127.0.0.1:9002）")
    args = ap.parse_args(argv)
    try:
        res = synth_episode(args.ep, args.lang, jobs_dir=args.jobs_dir,
                            url=args.url)
    except Exception as exc:  # 如实失败：报错到 stderr，退出码 1
        print(f"FAIL m7_tts {args.ep}/{args.lang}: {exc}", file=sys.stderr)
        return 1
    calib = res["calib"]
    print(f"OK m7_tts {args.ep}/{args.lang} 合成 {res['n']} 句"
          f"（keep_original 跳过 {res['skipped']}） tts_calls={res['tts_calls']}")
    print(f"  校准: in_window={calib['n_in_window']} "
          f"atempo_fixed={calib['n_atempo_fixed']} "
          f"df_calibrated={calib['n_df_calibrated']} "
          f"unresolved={calib['n_unresolved']}"
          + (f" no_window={calib['n_no_window']}" if calib["n_no_window"] else ""))
    if calib["unresolved"]:
        print(f"  WARN 未落窗（域界内不可解，如实保留）: {calib['unresolved']}")
    print(f"  校准工作纸: {res['report_json']}")

    # 实测入窗率门（D2）：与 M8 计划层同一条通过线（pipeline.yaml
    # m8.min_align_rate_mvp），口径换成**合成后实测**——计划层假绿在本模块
    # 被如实拦下。无窗源（no-window）句不计入分母（无法定窗，无从判定）。
    from pipeline.config import load_pipeline_config

    cfg = load_pipeline_config()
    min_rate = float(((cfg.get("m8") or {}).get("min_align_rate_mvp", 0.70)))
    n_window = (calib["n_in_window"] + calib["n_atempo_fixed"]
                + calib["n_df_calibrated"] + calib["n_unresolved"])
    if n_window > 0:
        rate = (calib["n_in_window"] + calib["n_atempo_fixed"]
                + calib["n_df_calibrated"]) / n_window
        print(f"  实测入窗率={rate:.3f} (min {min_rate:.2f}，"
              f"分母 {n_window} 句，no-window 不计)")
        if rate < min_rate:
            print(f"FAIL m7_tts: 合成后实测入窗率 {rate:.3f} 低于通过线 "
                  f"{min_rate:.3f}（详见 {res['report_json']}）")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
