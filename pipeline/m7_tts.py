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

CLI::

    python -m pipeline.m7_tts --ep ep01 --lang en [--jobs-dir <dir>] [--url ...]

在线依赖：tts 服务（默认 :9002，env M7_TTS_URL 可覆盖）；服务不可达时
本模块如实失败（不静默降级、不伪造产物）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path
from typing import Optional


def _wav_duration(path: Path) -> Optional[float]:
    """PCM wav 时长（秒）；非 wave 可读格式返回 None（校正跳过，不猜）。"""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, OSError):
        return None


def _post_correction(actual: float, lo: float, hi: float,
                     atempo_window: tuple[float, float]) -> tuple[str, float]:
    """D2 实测校正决策（M8 est 预测与 TTS 实测存在系统偏差的执行层闭环）。

    - actual 已落窗 → ("none", 1.0)
    - 窗外但所需变速在 atempo 微调窗（默认 [0.9,1.1]，T14 口径）内
      → ("atempo", need)（变速不变调，听感微调，零额外 TTS 调用）
    - 窗外且 atempo 不够 → ("resynth", target/actual)（df 修正系数：新 df=原df×系数，
      交 TTS 服务 duration_factor 通道重合成一次，0.5–2.0 服务范围内）
    窗口定义（原句 ±10%）由 M8/C5 冻结，本函数不改窗、不放宽。
    """
    if lo <= actual <= hi:
        return ("none", 1.0)
    target = hi if actual > hi else lo
    need = actual / target  # >1=加速缩短, <1=放慢加长
    a_lo, a_hi = atempo_window
    if a_lo <= need <= a_hi:
        return ("atempo", need)
    return ("resynth", target / actual)


#: TTS 服务 duration_factor 通道范围（tts_client C5 契约，0.5–2.0）
_DF_RANGE = (0.5, 2.0)


def synth_episode(ep: str, lang: str, *, jobs_dir: str | Path,
                  url: Optional[str] = None) -> dict:
    """C5 → :9002 整集合合成。返回 ``{"n": 合成句数, "skipped": 数, "wavs": [...]}``。"""
    from pipeline import contracts as C
    from pipeline.config import load_pipeline_config
    from pipeline.m1_ingest import FFMPEG
    from pipeline.tts_client import TtsClient

    if jobs_dir:
        root = Path(jobs_dir)
    else:
        root = Path(load_pipeline_config()["paths"]["jobs_dir"])
    ws = root / ep
    plan_path = ws / "07_synth" / f"synth_plan.{lang}.jsonl"
    if not plan_path.is_file():
        plan_path = ws / "07_synth" / "synth_plan.jsonl"
    if not plan_path.is_file():
        raise FileNotFoundError(
            f"C5 不存在: {ws/'07_synth'/f'synth_plan.{lang}.jsonl'}（先跑 M8）")
    rows = C.load_jsonl(plan_path, C.SynthPlanTable).root

    # D2 校正所需的句窗（M8 落的 align_report；缺失则不校正，行为回退旧口径）
    cfg_all = load_pipeline_config()
    atempo_window = tuple(cfg_all.get("m8", {}).get("atempo_window", (0.9, 1.1)))
    windows: dict[str, tuple[float, float]] = {}
    report_path = ws / "07_synth" / f"align_report.{lang}.json"
    if report_path.is_file():
        rep = json.loads(report_path.read_text(encoding="utf-8"))
        for it in rep.get("items", []):
            win = it.get("window")
            if it.get("utt_id") and isinstance(win, list) and len(win) == 2:
                windows[it["utt_id"]] = (float(win[0]), float(win[1]))
    else:
        print(f"note: align_report 缺失（{report_path.name}），跳过 D2 实测校正")

    def _apply_atempo(out: Path, factor: float) -> None:
        tmp = out.with_name(out.stem + ".fix.wav")
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", str(out),
             "-af", f"atempo={factor:.4f}", str(tmp)],
            check=True, timeout=300)
        tmp.replace(out)

    cli = TtsClient(url) if url else TtsClient()
    n = 0
    skipped = 0
    wavs: list[str] = []
    for r in rows:
        if r.keep_original:
            skipped += 1
            print(f"skip {r.utt_id} (keep_original)")
            continue
        out = ws / r.out
        df = float(r.duration_factor)
        res = cli.synth(
            r.text, str(ws / r.voice_ref),
            emo_ref=str(ws / r.emo_ref) if r.emo_ref else None,
            lang=lang, emo_alpha=float(r.emo_alpha),
            duration_factor=df,
            engine=r.engine or "auto", out=str(out), utt_id=r.utt_id)
        if r.atempo and abs(float(r.atempo) - 1.0) > 1e-3:
            _apply_atempo(out, float(r.atempo))
        # ---- D2 实测校正（两级：atempo 微调 / df 修正重合成一次）----
        win = windows.get(r.utt_id)
        postfix = "in-window"
        if win is not None:
            lo, hi = win
            actual = _wav_duration(out)
            if actual is None:
                postfix = "dur-unknown(不校正)"
            else:
                act, val = _post_correction(actual, lo, hi, atempo_window)
                if act == "atempo":
                    _apply_atempo(out, val)
                    fixed = _wav_duration(out) or actual / val
                    postfix = f"postfix-atempo={val:.3f} {actual:.2f}->{fixed:.2f}s"
                elif act == "resynth":
                    df2 = min(max(df * val, _DF_RANGE[0]), _DF_RANGE[1])
                    res = cli.synth(
                        r.text, str(ws / r.voice_ref),
                        emo_ref=str(ws / r.emo_ref) if r.emo_ref else None,
                        lang=lang, emo_alpha=float(r.emo_alpha),
                        duration_factor=df2,
                        engine=r.engine or "auto", out=str(out), utt_id=r.utt_id)
                    actual2 = _wav_duration(out)
                    if actual2 is not None and not (lo <= actual2 <= hi):
                        need2 = actual2 / (hi if actual2 > hi else lo)
                        a_lo2, a_hi2 = atempo_window
                        if a_lo2 <= need2 <= a_hi2:
                            _apply_atempo(out, need2)
                            actual2 = _wav_duration(out) or actual2 / need2
                    postfix = (f"postfix-resynth df {df:.3f}->{df2:.3f} "
                               f"{actual:.2f}->{actual2 if actual2 is not None else -1:.2f}s")
                    df = df2
                else:
                    postfix = "in-window"
        n += 1
        wavs.append(str(out))
        print(f"synth {r.utt_id} engine={res['engine']} "
              f"dur={res['duration_s']}s df={df} "
              f"atempo={r.atempo} expect={r.expect_dur}s [{postfix}]")
    if n <= 0:
        raise RuntimeError("C5 无可合成句（全 keep_original？）——不出假交付")
    return {"n": n, "skipped": skipped, "wavs": wavs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.m7_tts",
        description="M7 TTS 整集合成（C5 计划 → :9002 逐句合成 + atempo 微调）")
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
    print(f"OK m7_tts {args.ep}/{args.lang} 合成 {res['n']} 句"
          f"（keep_original 跳过 {res['skipped']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
