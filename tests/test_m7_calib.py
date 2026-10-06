"""M7 闭环时长校准验收（D2 修复，全离线零 GPU）。

D2 根因：M8 est（文本占位模型）与合成引擎实测存在 10–20% 双向系统偏差，
``duration_factor`` 线性缩放假设不完全成立——计划层"预测落窗"≠"落盘落窗"。
M7 修复：整集合成后按**实测**闭环校准（窗口口径与 M15 dur_align 一致）：

  ① atempo 微调窗内可达即收口（零调用、确定性）；
  ② 不可达按实测反馈修正 duration_factor 重合成（C5 域 [0.5,2.0]；
     首轮比例修正、次轮割线内插——不假设全局线性）；
  ③ 域界内不可解如实记 unresolved（保留产物、不删句、不伪造落窗）。

覆盖：
- 纯函数：atempo 可达域求解（超长/过短/压线取整/不可达）、df 提案
  （单点比例 / 两点割线 / 响应死区回落 / 域界钳制 / 无实质变化停）；
- 窗口解析：C4 budget 优先，C4 缺行按 C2 窗×ratio 自算（M15 同口径）；
- 桩服务端到端：五句各异响应曲线（比例/亚线性/不可解）——首轮即落窗不
  动计划、atempo 收口、1 轮 df 收敛、2 轮割线+域界钳制收敛、域界内不可解
  如实 unresolved；C5 校准行回写（df/atempo=最终执行值、expect_dur=实测）
  且未校准行逐字节不动；无后缀副本刷新为镜像；tts_calls 记账准确。

桩服务不占 GPU、不触碰真实 :9002（服务不可达时本文件照常全绿）。
"""

from __future__ import annotations

import io
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pipeline.m7_tts import (
    ST_ATEMPO_FIXED,
    ST_DF_CALIBRATED,
    ST_IN_WINDOW,
    ST_UNRESOLVED,
    _wav_duration,
    atempo_fix_for,
    df_propose,
    main,
    resolve_windows,
    synth_episode,
)

SR = 16000
A_LO, A_HI = 0.9, 1.1
DF_LO, DF_HI = 0.5, 2.0


def _tiny_wav_bytes(dur_s: float = 0.5, sr: int = SR) -> bytes:
    n = max(1, int(sr * dur_s))
    t = np.linspace(0.0, dur_s, n, endpoint=False)
    wav = (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype("float32")
    buf = io.BytesIO()
    sf.write(buf, wav, sr, format="WAV")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 纯函数：atempo 可达域求解
# ---------------------------------------------------------------------------

class TestAtempoFixFor:
    def test_in_window_returns_unity(self):
        assert atempo_fix_for(2.0, 1.8, 2.2, a_lo=A_LO, a_hi=A_HI, target=2.0) == 1.0

    def test_too_long_reachable_aims_window_center(self):
        # 真实基线数字（dd01 句5）：2.264s 窗 [1.804,2.206] → atempo 收口
        a = atempo_fix_for(2.264, 1.804, 2.206, a_lo=A_LO, a_hi=A_HI, target=2.005)
        assert a == 1.1 and 1.804 <= 2.264 / a <= 2.206

    def test_too_short_reachable(self):
        # 过短但 atempo 可达：aim 0.85 被域下界 0.9 钳住 → 1.7/0.9=1.889 落窗
        a = atempo_fix_for(1.7, 1.8, 2.2, a_lo=A_LO, a_hi=A_HI, target=2.0)
        assert a == pytest.approx(0.9) and 1.8 <= 1.7 / a <= 2.2

    def test_too_long_unreachable(self):
        # 真实基线数字（dd01 句3）：3.855s 窗 [2.606,3.185]，atempo 不可达
        assert atempo_fix_for(3.855, 2.606, 3.185, a_lo=A_LO, a_hi=A_HI,
                              target=2.895) is None

    def test_too_short_unreachable(self):
        assert atempo_fix_for(1.5, 1.8, 2.2, a_lo=A_LO, a_hi=A_HI,
                              target=2.0) is None

    def test_rounding_pinch_falls_back_to_midpoint(self):
        # 瞄窗心的值经 3 位取整后压出窗 → 回落可行域中点（必在窗内）
        a = atempo_fix_for(1.98, 1.8, 1.98, a_lo=A_LO, a_hi=A_HI, target=1.89)
        assert a is not None and 1.8 <= 1.98 / a <= 1.98


# ---------------------------------------------------------------------------
# 纯函数：df 实测反馈提案
# ---------------------------------------------------------------------------

class TestDfPropose:
    def test_single_point_proportional(self):
        # 真实基线数字（dd01 句3）：(1.287, 3.855) → 目标 2.895
        f = df_propose([(1.287, 3.855)], 2.895, df_lo=DF_LO, df_hi=DF_HI)
        assert f == pytest.approx(0.967)

    def test_two_points_secant(self):
        # 亚线性响应（d ∝ √df）：比例外推会打偏，割线内插修正
        hist = [(1.0, 3.0), (0.667, round(3.0 * math.sqrt(0.667), 3))]
        f = df_propose(hist, 2.0, df_lo=DF_LO, df_hi=DF_HI)
        assert f is not None and DF_LO <= f <= DF_HI

    def test_clamped_to_domain_returns_none_when_no_step(self):
        # (1.0,5.0)→(0.5,2.5)：割线解 0.4 被钳到 0.5，步长 0 → 无实质变化
        assert df_propose([(1.0, 5.0), (0.5, 2.5)], 2.0,
                          df_lo=DF_LO, df_hi=DF_HI) is None

    def test_dead_zone_falls_back_proportional(self):
        # d₁≈d₂（响应死区）：割线退化 → 比例式（0.9×1.4/2.0005≈0.63）
        f = df_propose([(1.0, 2.0), (0.9, 2.0005)], 1.4, df_lo=DF_LO, df_hi=DF_HI)
        assert f == pytest.approx(0.63)

    def test_empty_history_and_bad_meas(self):
        assert df_propose([], 1.0, df_lo=DF_LO, df_hi=DF_HI) is None
        assert df_propose([(1.0, 0.0)], 1.0, df_lo=DF_LO, df_hi=DF_HI) is None


# ---------------------------------------------------------------------------
# 窗口解析（M15 dur_align 同口径）
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


class TestResolveWindows:
    def test_c4_first_c2_fallback(self, tmp_path: Path):
        _write_jsonl(tmp_path / "06_mt" / "translations.jsonl", [{
            "utt_id": "u1", "tgt": "en",
            "budget": {"orig_dur": 2.0, "lo": 1.8, "hi": 2.2},
            "candidates": [],
        }])
        _write_jsonl(tmp_path / "04_dial" / "utterances.jsonl", [{
            "utt_id": "u2", "shot_id": "s1", "start": 1.0, "end": 3.0,
            "lang": "zh", "text": "你好",
        }])
        rows = [type("R", (), {"utt_id": "u1"})(),
                type("R", (), {"utt_id": "u2"})(),
                type("R", (), {"utt_id": "u9"})()]
        win = resolve_windows(rows, tmp_path, "en")
        assert win["u1"] == (1.8, 2.2)          # C4 budget 优先
        assert win["u2"] == (1.8, 2.2)          # C2 缺 C4 → 窗×[0.9,1.1]
        assert win["u9"] is None                # 两缺 → 无法定窗（不校准）


# ---------------------------------------------------------------------------
# 桩服务端到端：synth_episode 闭环校准
# ---------------------------------------------------------------------------

#: 桩引擎响应模型：dur = base[utt] × df**exp[utt]（确定性、单调、可控非线性；
#: exp<1 模拟引擎在 df→1 附近响应趋平的真实行为——比例外推打偏，逼出割线轮）
BASES = {"u1": 2.0, "u2": 2.0, "u3": 2.5, "u4": 3.0, "u5": 5.0}
EXPS = {"u1": 1.0, "u2": 1.0, "u3": 1.0, "u4": 0.5, "u5": 0.5}
#: 目标窗（C4 budget 口径；orig 落窗内）
BUDGETS = {
    "u1": (2.0, 1.8, 2.2),   # 首轮即落窗（计划参数不动）
    "u2": (1.8, 1.7, 1.9),   # 超长但 atempo 可达 → 零调用收口
    "u3": (2.1, 2.0, 2.2),   # atempo 不可达（2.5/2.2=1.136>1.1）→ 1 轮 df 收敛
    "u4": (2.0, 1.8, 2.2),   # 亚线性响应 → 比例轮打偏 → 割线+域界钳制 2 轮收敛
    "u5": (2.0, 1.8, 2.2),   # 域界内不可解 → unresolved（保留产物）
}


class _CalibStubHandler(BaseHTTPRequestHandler):
    """桩 :9002：按 BASES×df**EXPS 回固定时长 wav——断言闭环收敛行为。"""

    calls: list = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))

        def field(name: str) -> str:
            i = body.find(f'name="{name}"'.encode())
            if i < 0:
                return ""
            j = body.find(b"\r\n\r\n", i)
            k = body.find(b"\r\n--", j + 4)
            return body[j + 4:k].decode()

        utt = field("utt_id")
        df = float(field("duration_factor") or 1.0)
        self.calls.append({"utt_id": utt, "df": df})
        dur = round(BASES[utt] * (df ** EXPS[utt]), 3)
        raw = _tiny_wav_bytes(dur_s=dur)
        payload = {
            "engine": "dub-tts", "chain": ["dub-tts"],
            "attempts": [{"engine": "dub-tts", "ok": True}],
            "duration_s": dur, "sr": SR,
            "wav_b64": __import__("base64").b64encode(raw).decode(),
            "request": {"emo_ref_used": False},
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *a: object) -> None:  # 静默
        pass


@pytest.fixture()
def stub_url():
    _CalibStubHandler.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CalibStubHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def jobs(tmp_path: Path) -> Path:
    """最小合法 jobs：C4 预算窗 + C5 计划 + 音色参考（无 C2——C4 全在）。"""
    ws = tmp_path / "jobs" / "epc"
    _write_jsonl(ws / "06_mt" / "translations.jsonl", [
        {"utt_id": u, "tgt": "en",
         "budget": {"orig_dur": o, "lo": lo, "hi": hi}, "candidates": []}
        for u, (o, lo, hi) in BUDGETS.items()
    ])
    ref = ws / "05_cast" / "voicebank" / "ref.wav"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_bytes(_tiny_wav_bytes(0.5))
    _write_jsonl(ws / "07_synth" / "synth_plan.en.jsonl", [
        {"utt_id": u, "engine": "dub-tts",
         "voice_ref": "05_cast/voicebank/ref.wav", "emo_ref": None,
         "emo_alpha": 0.7, "duration_factor": 1.0, "atempo": 1.0,
         "text": f"line {u}", "out": f"07_synth/wavs/{u}.wav",
         "expect_dur": o, "keep_original": False}
        for u, (o, _, _) in BUDGETS.items()
    ])
    # 无后缀副本（M8 会在标准链路里生成镜像；此处预置以验证刷新）
    (ws / "07_synth" / "synth_plan.jsonl").write_text(
        (ws / "07_synth" / "synth_plan.en.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8")
    return ws


def _plan_rows(p: Path) -> dict[str, dict]:
    return {json.loads(l)["utt_id"]: json.loads(l)
            for l in p.read_text(encoding="utf-8").splitlines() if l.strip()}


class TestSynthEpisodeClosedLoop:
    def test_convergence_and_bookkeeping(self, jobs: Path, stub_url: str):
        res = synth_episode("epc", "en", jobs_dir=jobs.parent, url=stub_url)

        # 调用记账：首轮 5 + u3/u4/u5 各 1（比例轮） + u4 割线轮 1 = 9
        assert res["tts_calls"] == 9
        assert res["n"] == 5 and res["skipped"] == 0
        assert len(_CalibStubHandler.calls) == 9

        # 逐句落窗实测（M15 同口径 soundfile）
        ws = jobs
        win = {u: (lo, hi) for u, (_, lo, hi) in BUDGETS.items()}
        meas = {u: _wav_duration(ws / f"07_synth/wavs/{u}.wav") for u in BUDGETS}
        for u in ("u1", "u2", "u3", "u4"):
            assert win[u][0] <= meas[u] <= win[u][1], f"{u}: {meas[u]} ∉ {win[u]}"
        # u5 域界内不可解：产物保留（不删句/不伪造），实测如实出窗
        assert not (win["u5"][0] <= meas["u5"] <= win["u5"][1])

        # 工作纸：状态与逐句修正历史
        report = json.loads((ws / "07_synth" / "tts_calib.en.json").read_text("utf-8"))
        st = {it["utt_id"]: it for it in report["items"]}
        assert st["u1"]["status"] == ST_IN_WINDOW
        assert st["u2"]["status"] == ST_ATEMPO_FIXED and st["u2"]["n_resynth"] == 0
        assert st["u2"]["atempo_applied"] == pytest.approx(1.1)  # 瞄窗心 1.111 被域上界钳制
        assert st["u3"]["status"] == ST_DF_CALIBRATED and st["u3"]["n_resynth"] == 1
        assert st["u4"]["status"] == ST_DF_CALIBRATED and st["u4"]["n_resynth"] == 2
        assert st["u5"]["status"] == ST_UNRESOLVED
        assert st["u4"]["history"][0] == [1.0, 3.0]
        assert len(st["u4"]["history"]) == 3  # 计划点 + 两轮重合成实测点
        assert report["summary"]["n_unresolved"] == 1
        assert report["summary"]["unresolved"] == ["u5"]
        assert report["tts_calls"] == 9

        # C5 回写：校准行=最终执行参数 + 实测 expect_dur；未校准行不动
        rows = _plan_rows(ws / "07_synth" / "synth_plan.en.jsonl")
        assert rows["u1"]["duration_factor"] == 1.0 and rows["u1"]["atempo"] == 1.0
        assert rows["u1"]["expect_dur"] == 2.0  # 首轮落窗：计划层预测口径保持
        assert rows["u2"]["atempo"] == pytest.approx(1.1)
        assert rows["u3"]["duration_factor"] == pytest.approx(0.84)
        assert rows["u4"]["duration_factor"] == pytest.approx(0.5)  # 割线解 0.395 被钳域界
        assert rows["u5"]["duration_factor"] == pytest.approx(0.5)
        for u in ("u2", "u3", "u4", "u5"):
            assert rows[u]["expect_dur"] == pytest.approx(meas[u], abs=1e-3)
        # 无后缀副本刷新为镜像（M8 同口径）
        assert _plan_rows(ws / "07_synth" / "synth_plan.jsonl") == rows

    def test_all_in_window_no_rewrite(self, jobs: Path, stub_url: str, monkeypatch):
        # 全部落窗（df=1.0 直落窗心，默认轮数）→ 零校准干预、C5 逐字节不动
        before = (jobs / "07_synth" / "synth_plan.en.jsonl").read_bytes()
        for u, (o, _, _) in BUDGETS.items():
            BASES[u] = o
        try:
            res = synth_episode("epc", "en", jobs_dir=jobs.parent, url=stub_url)
        finally:
            BASES.update({"u1": 2.0, "u2": 2.0, "u3": 2.5, "u4": 3.0, "u5": 5.0})
        assert res["tts_calls"] == 5
        assert all(s["status"] == ST_IN_WINDOW for s in
                   json.loads((jobs / "07_synth" / "tts_calib.en.json")
                              .read_text("utf-8"))["items"])
        assert (jobs / "07_synth" / "synth_plan.en.jsonl").read_bytes() == before

    def test_calib_rounds_zero_atempo_only(self, jobs: Path, stub_url: str, monkeypatch):
        # calib_rounds=0 → 零重合成轮（调用=首轮）；atempo 兜底仍属执行职责
        monkeypatch.setattr("pipeline.m7_tts._DEFAULT_CALIB_ROUNDS", 0)
        res = synth_episode("epc", "en", jobs_dir=jobs.parent, url=stub_url)
        assert res["tts_calls"] == 5
        rows = _plan_rows(jobs / "07_synth" / "synth_plan.en.jsonl")
        assert all(r["duration_factor"] == 1.0 for r in rows.values())
        assert rows["u2"]["atempo"] == pytest.approx(1.1)  # 零调用收口仍生效
        assert rows["u3"]["atempo"] == 1.0  # atempo 不可达 → 无 df 轮 → unresolved
        st = {it["utt_id"]: it["status"] for it in
              json.loads((jobs / "07_synth" / "tts_calib.en.json")
                         .read_text("utf-8"))["items"]}
        assert st["u1"] == ST_IN_WINDOW and st["u2"] == ST_ATEMPO_FIXED
        assert st["u3"] == ST_UNRESOLVED and st["u5"] == ST_UNRESOLVED

    def test_plan_atempo_overflow_blocks_atempo_goes_df(self, jobs: Path, stub_url: str):
        # 计划 atempo=1.05 已施加到首轮文件；校准还需 1.1 → 复合 1.155 超听感带
        # → 不施加（零调用），转 df 实测反馈路收敛——raw 历史=落盘×计划atempo
        plan = jobs / "07_synth" / "synth_plan.en.jsonl"
        rows = _plan_rows(plan)
        rows["u1"]["atempo"] = 1.05
        _write_jsonl(plan, [rows[u] for u in rows])
        BASES["u1"] = 2.4  # 首轮落盘 2.4/1.05=2.286 出窗(hi=2.2)
        try:
            res = synth_episode("epc", "en", jobs_dir=jobs.parent, url=stub_url)
        finally:
            BASES["u1"] = 2.0
        out_rows = _plan_rows(plan)
        assert out_rows["u1"]["duration_factor"] == pytest.approx(0.833)  # 2.0/2.4
        assert out_rows["u1"]["atempo"] == 1.0            # 重合成 raw 直落窗，无 atempo
        meas = _wav_duration(jobs / "07_synth/wavs/u1.wav")
        assert 1.8 <= meas <= 2.2 and out_rows["u1"]["expect_dur"] == pytest.approx(meas, abs=1e-3)
        report = json.loads((jobs / "07_synth" / "tts_calib.en.json").read_text("utf-8"))
        st1 = {it["utt_id"]: it for it in report["items"]}["u1"]
        assert st1["status"] == ST_DF_CALIBRATED
        assert st1["history"][0] == [1.0, 2.4]            # raw=落盘×计划atempo
        assert st1["n_resynth"] == 1

    def test_atempo_underdelivery_never_claimed(self, tmp_path: Path, stub_url: str,
                                                monkeypatch):
        # ffmpeg atempo 实际压缩比与请求值存在 ~1% 级偏差（真实生产在带界瞄准时
        # 出现过：请求 1.1 → 实测 1.095）。模拟欠交付：施加后实测仍出窗 →
        # 不得记收口（不虚报），复合总量超带不再叠加以免超带 → 如实 unresolved。
        monkeypatch.setattr("pipeline.m7_tts._DEFAULT_CALIB_ROUNDS", 0)

        def _underdeliver(out: Path, a: float, *, ffmpeg: str) -> float:
            import soundfile as _sf
            d = _wav_duration(out)
            new_d = round(d / (a * 0.995), 6)  # 实际提速比请求低 0.5%
            t = np.linspace(0.0, new_d, int(SR * new_d), endpoint=False)
            tmp = out.with_name(out.stem + ".atempo.wav")
            _sf.write(str(tmp), (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype("float32"), SR)
            tmp.replace(out)
            return _wav_duration(out)

        monkeypatch.setattr("pipeline.m7_tts._apply_atempo", _underdeliver)
        BASES.update({"x3": 2.53})  # 出窗且带界可达（2.53/2.305≈1.0976→a=1.1 可解）
        EXPS.update({"x3": 1.0})
        BUDGETS.update({"x3": (2.096, 1.886, 2.305)})
        try:
            ws = tmp_path / "jobs" / "epx"
            _write_jsonl(ws / "06_mt" / "translations.jsonl", [
                {"utt_id": "x3", "tgt": "en",
                 "budget": {"orig_dur": 2.096, "lo": 1.886, "hi": 2.305},
                 "candidates": []}])
            ref = ws / "05_cast" / "voicebank" / "ref.wav"
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_bytes(_tiny_wav_bytes(0.5))
            _write_jsonl(ws / "07_synth" / "synth_plan.en.jsonl", [
                {"utt_id": "x3", "engine": "dub-tts",
                 "voice_ref": "05_cast/voicebank/ref.wav", "emo_ref": None,
                 "emo_alpha": 0.7, "duration_factor": 1.0, "atempo": 1.0,
                 "text": "line x3", "out": "07_synth/wavs/x3.wav",
                 "expect_dur": 2.096, "keep_original": False}])
            synth_episode("epx", "en", jobs_dir=ws.parent, url=stub_url)
            meas = _wav_duration(ws / "07_synth/wavs/x3.wav")
            assert meas > 2.305  # 欠交付后仍出窗——m15 口径如实未命中
            rows = _plan_rows(ws / "07_synth" / "synth_plan.en.jsonl")
            assert rows["x3"]["atempo"] == pytest.approx(1.1)   # 施加过=如实记录
            assert rows["x3"]["duration_factor"] == 1.0
            assert rows["x3"]["expect_dur"] == pytest.approx(meas, abs=1e-3)
            report = json.loads((ws / "07_synth" / "tts_calib.en.json").read_text("utf-8"))
            st3 = {it["utt_id"]: it for it in report["items"]}["x3"]
            assert st3["status"] == ST_UNRESOLVED               # 不虚报收口
            assert st3["atempo_applied"] == pytest.approx(1.1)
        finally:
            BASES.pop("x3"), EXPS.pop("x3"), BUDGETS.pop("x3")


# ---------------------------------------------------------------------------
# CLI 实测率门（D2）：与 M8 同一条通过线，口径换成合成后实测
# ---------------------------------------------------------------------------

class TestMainMeasuredRateGate:
    """main() 诚实 exit：实测入窗率 <0.70 → 1（计划层假绿在此被拦下）。"""

    def test_fail_below_line(self, tmp_path: Path, stub_url: str, monkeypatch):
        BASES.update({"x1": 5.0, "x2": 5.0})   # 域界内不可解（同 u5 曲线）
        EXPS.update({"x1": 0.5, "x2": 0.5})
        BUDGETS.update({"x1": (2.0, 1.8, 2.2), "x2": (2.0, 1.8, 2.2)})
        try:
            ws = tmp_path / "jobs" / "epg"
            _write_jsonl(ws / "06_mt" / "translations.jsonl", [
                {"utt_id": u, "tgt": "en",
                 "budget": {"orig_dur": 2.0, "lo": 1.8, "hi": 2.2}, "candidates": []}
                for u in ("x1", "x2")])
            ref = ws / "05_cast" / "voicebank" / "ref.wav"
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_bytes(_tiny_wav_bytes(0.5))
            _write_jsonl(ws / "07_synth" / "synth_plan.en.jsonl", [
                {"utt_id": u, "engine": "dub-tts",
                 "voice_ref": "05_cast/voicebank/ref.wav", "emo_ref": None,
                 "emo_alpha": 0.7, "duration_factor": 1.0, "atempo": 1.0,
                 "text": f"line {u}", "out": f"07_synth/wavs/{u}.wav",
                 "expect_dur": 2.0, "keep_original": False}
                for u in ("x1", "x2")])
            rc = main(["--ep", "epg", "--lang", "en",
                       "--jobs-dir", str(tmp_path / "jobs"), "--url", stub_url])
            assert rc == 1  # 0/2 = 0.0 < 0.70
        finally:
            BASES.pop("x1"), BASES.pop("x2")
            EXPS.pop("x1"), EXPS.pop("x2")
            BUDGETS.pop("x1"), BUDGETS.pop("x2")

    def test_pass_at_line(self, tmp_path: Path, stub_url: str):
        BASES.update({"x1": 2.0, "x2": 2.0})   # df=1.0 直落窗心
        EXPS.update({"x1": 1.0, "x2": 1.0})
        BUDGETS.update({"x1": (2.0, 1.8, 2.2), "x2": (2.0, 1.8, 2.2)})
        try:
            ws = tmp_path / "jobs" / "epg"
            _write_jsonl(ws / "06_mt" / "translations.jsonl", [
                {"utt_id": u, "tgt": "en",
                 "budget": {"orig_dur": 2.0, "lo": 1.8, "hi": 2.2}, "candidates": []}
                for u in ("x1", "x2")])
            ref = ws / "05_cast" / "voicebank" / "ref.wav"
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_bytes(_tiny_wav_bytes(0.5))
            _write_jsonl(ws / "07_synth" / "synth_plan.en.jsonl", [
                {"utt_id": u, "engine": "dub-tts",
                 "voice_ref": "05_cast/voicebank/ref.wav", "emo_ref": None,
                 "emo_alpha": 0.7, "duration_factor": 1.0, "atempo": 1.0,
                 "text": f"line {u}", "out": f"07_synth/wavs/{u}.wav",
                 "expect_dur": 2.0, "keep_original": False}
                for u in ("x1", "x2")])
            rc = main(["--ep", "epg", "--lang", "en",
                       "--jobs-dir", str(tmp_path / "jobs"), "--url", stub_url])
            assert rc == 0  # 2/2 = 1.0 ≥ 0.70
        finally:
            BASES.pop("x1"), BASES.pop("x2")
            EXPS.pop("x1"), EXPS.pop("x2")
            BUDGETS.pop("x1"), BUDGETS.pop("x2")
