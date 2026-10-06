"""M12 合规模块 eval（规划 §4 M12；全离线零 GPU，ffmpeg/PIL 系统依赖）。

冻结 eval 命令::

    pytest tests/test_m12.py

覆盖面（与 2026-10-06 真机链验证同口径）：
  ① 载荷比特：16-bit（C7 ``audio_wm.bits``）确定性派生 + 非法载荷显错；
  ② audmark 嵌入/检测回环：合成音轨嵌入后精确检出（bit_acc=1.0 / match）；
  ③ 负控：未嵌水印音轨检测不误报（match=False）；
  ④ 有损重编码鲁棒：AAC 128k 重编码后仍精确检出（交付链音轨口径）；
  ⑤ CLI 全链（显式标识 → 音频水印 → C8 报告）：最小 ep 工作区真跑
     ``python -m pipeline.m12_compliance``，断言 C8 四项标识状态
     （explicit/implicit/audio_wm=ok，c2pa=pending 如实）、ffprobe 隐式元数据位、
     ``audio_wm_report.json`` 检测报告、成片双流与时长守恒。

口径注记：
  - audmark 扰码按帧序锁定时间轴，时间平移/切片后不可检属域界（交付件检测
    口径不涉及；见 pipeline/_audmark_wm.py 模块注记）；
  - ④ 以 128k 为下界口径（交付 mux 用 192k，留更大裕量）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from pipeline._audmark_wm import (
    AudmarkError,
    bits_hex,
    detect_file,
    embed_file,
    payload_bits,
)
from pipeline import contracts as C
from pipeline.config import load_pipeline_config
from pipeline.m1_ingest import FFMPEG, FFPROBE
from pipeline.m12_compliance import main as m12_main

LANG = "en"
EP = "ep12t"
DUR = 4.0


# ---------------------------------------------------------------------------
# ① 载荷比特
# ---------------------------------------------------------------------------

def test_payload_bits_deterministic_16bit():
    b1, b2 = payload_bits("ep12t-en"), payload_bits("ep12t-en")
    assert len(b1) == 16 and (b1 == b2).all()
    assert set(np.asarray(b1).tolist()) <= {0, 1}
    assert bits_hex(b1) == bits_hex(b2)
    assert bits_hex(payload_bits("ep12t-en")) != bits_hex(payload_bits("other"))


def test_payload_bits_rejects_empty():
    with pytest.raises(AudmarkError):
        payload_bits("")


# ---------------------------------------------------------------------------
# ②③ 回环 + 负控（合成音轨：谐波簇 + 低噪，确定性）
# ---------------------------------------------------------------------------

def _synth_wav(dst: Path, dur: float = DUR, sr: int = 48000) -> Path:
    rng = np.random.default_rng(42)
    t = np.arange(int(dur * sr)) / sr
    tone = sum(np.sin(2 * np.pi * (220 * (1 + 0.2 * np.sin(2 * np.pi * 0.5 * t)) * k) * t) / k
               for k in (1, 2, 3, 4, 5))
    x = 0.3 * tone + 0.01 * rng.standard_normal(len(t))
    x = (x / np.abs(x).max() * 0.5).astype(np.float32)
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                    "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-",
                    "-c:a", "pcm_s16le", str(dst)],
                   input=x.tobytes(), check=True, timeout=300)
    return dst


def test_embed_detect_roundtrip(tmp_path: Path):
    pay = f"{EP}-{LANG}"
    wav = _synth_wav(tmp_path / "src.wav")
    wm_wav = tmp_path / "wm.wav"
    info = embed_file(wav, wm_wav, pay)
    assert info["bits"] == 16 and info["engine"] == "audmark"
    assert wm_wav.is_file() and info["snr_db"] > 10.0  # 嵌入不可闻度量（实测 ~20dB）
    det = detect_file(wm_wav, pay)
    assert det["match"] is True and det["bit_acc"] == 1.0
    assert det["expected_hex"] == info["expected_hex"] == det["detected_hex"]


def test_detect_negative_control_no_false_positive(tmp_path: Path):
    pay = f"{EP}-{LANG}"
    wav = _synth_wav(tmp_path / "plain.wav")
    det = detect_file(wav, pay)
    assert det["match"] is False  # 未嵌音频不得误报精确匹配


def test_aac_transcode_survival(tmp_path: Path):
    pay = f"{EP}-{LANG}"
    wm_wav = embed_file(_synth_wav(tmp_path / "src.wav"), tmp_path / "wm.wav", pay)["out"]
    m4a = tmp_path / "wm_128k.m4a"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", wm_wav,
                    "-c:a", "aac", "-b:a", "128k", str(m4a)],
                   check=True, timeout=300)
    det = detect_file(m4a, pay)
    assert det["match"] is True and det["bit_acc"] == 1.0


# ---------------------------------------------------------------------------
# ⑤ CLI 全链（最小 ep 工作区：C7 labels + 成片 → 显式+元数据+音频水印+C8）
# ---------------------------------------------------------------------------

def _labels() -> dict:
    return {
        "service_provider": "澄迈短剧出海测试",
        "content_id": f"{EP}-m12-eval",
        "standard": "GB45438-2025",
        "explicit": {"text": "本内容由AI生成",
                     "video": "12_out（片头 3s 文字提示，M12 overlay）",
                     "audio_announce": False},
        "implicit": {"metadata_field": "XMP:aiGeneratedContent",
                     "value": f"{EP}-m12-eval|澄迈短剧出海测试"},
        "c2pa": "pending（T11/M12 未收口）",
        "audio_wm": {"engine": "audmark", "payload": f"{EP}-m12-eval", "bits": 16},
    }


def _make_src_mp4(dst: Path) -> Path:
    """最小成片（lavfi 纯色 + 正弦×宽带底噪音轨，双流；小分辨率省时）。

    底噪不可省：乘性水印需要宿主带内能量（见 pipeline/_audmark_wm.py 域界注记），
    真实链 M9 输出为宽带语音+背景，此处以低电平噪声等效。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=0x101820:size=320x568:rate=12:duration={DUR}",
         "-f", "lavfi", "-i", f"sine=frequency=330:duration={DUR}",
         "-f", "lavfi", "-i", f"anoisesrc=color=white:amplitude=0.02:duration={DUR}:seed=42",
         "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first[a]",
         "-map", "0:v", "-map", "[a]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
         "-crf", "28", "-c:a", "aac", "-b:a", "96k", str(dst)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    assert r.returncode == 0, r.stderr[-500:]
    return dst


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / EP
    (root / "11_labels").mkdir(parents=True)
    (root / "11_labels" / "labels.json").write_text(
        json.dumps(_labels(), ensure_ascii=False, indent=1), encoding="utf-8")
    _make_src_mp4(root / "12_out" / f"{EP}.{LANG}.mp4")
    return root


def test_m12_cli_full_chain(workspace: Path):
    jobs_root = workspace.parent
    rc = m12_main(["--ep", EP, "--lang", LANG, "--jobs-dir", str(jobs_root)])
    assert rc == 0

    # C8 报告：四项标识状态（c2pa 如实 pending；audio_wm 检测过线才 ok）
    cfg = load_pipeline_config()
    comp_path = workspace / "12_out" / "compliance_report.json"
    comp = json.loads(comp_path.read_text(encoding="utf-8"))
    C.ComplianceReport.model_validate(comp)  # C8 契约强校验
    assert comp["label_status"]["explicit"] == "ok"
    assert comp["label_status"]["implicit"] == "ok"
    assert comp["label_status"]["audio_wm"] == "ok"
    assert comp["label_status"]["c2pa"] == "pending"

    # ② 隐式元数据：ffprobe 回读与 C7 精确一致
    mp4 = workspace / "12_out" / f"{EP}.{LANG}.mp4"
    tags = json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-show_format", "-of", "json", str(mp4)],
        check=True, capture_output=True, text=True, timeout=300).stdout
    )["format"]["tags"]
    assert tags.get("XMP:aiGeneratedContent") == _labels()["implicit"]["value"]

    # ③ 音频水印：M12 检测器对最终成片（AAC 音轨）精确检出
    wm_rep = json.loads((workspace / "12_out" / "audio_wm_report.json")
                        .read_text(encoding="utf-8"))
    assert wm_rep["embed"]["payload"] == f"{EP}-m12-eval"
    assert wm_rep["embed"]["embed_verify_match"] is True  # 嵌入侧自证
    assert wm_rep["detect_final"]["match"] is True
    assert wm_rep["detect_final"]["bit_acc"] == 1.0
    det = detect_file(mp4, f"{EP}-m12-eval")  # 独立二次复核
    assert det["match"] is True

    # ① 显式标识在位 + 成片双流/时长守恒（片头 3s overlay 后时长不变）
    probe = json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(mp4)],
        check=True, capture_output=True, text=True, timeout=300).stdout)
    kinds = {s["codec_type"] for s in probe["streams"]}
    assert kinds == {"video", "audio"}
    assert abs(float(probe["format"]["duration"]) - DUR) < 0.2


def test_m12_requires_source_video(tmp_path: Path):
    (tmp_path / EP / "11_labels").mkdir(parents=True)
    (tmp_path / EP / "11_labels" / "labels.json").write_text(
        json.dumps(_labels(), ensure_ascii=False), encoding="utf-8")
    from pipeline.m12_compliance import apply_explicit_label
    with pytest.raises(FileNotFoundError):
        apply_explicit_label(tmp_path / EP, EP, LANG)
