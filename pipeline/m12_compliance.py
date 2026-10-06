"""M12 合规模块（规划 §4 M12；任务 T11/T21）—— 显式水印 + 隐式元数据 + 音频水印 + C8 报告。

职责
====
1. 显式 AI 标识：片头 3s 文字提示（PIL 预渲染 PNG + ffmpeg overlay）。
2. 音频水印（C7 ``audio_wm``，audmark 组件）：成片音轨嵌入 16-bit 载荷，
   回读检测精确匹配后才将 C8 ``label_status.audio_wm`` 记 ``ok``（检测不过
   如实记 ``failed``，不虚报）；嵌入/检测实现在 :mod:`pipeline._audmark_wm`。
   （隐式元数据位由 M9/封口步依 C7 写入，本模块 mux 时重封同值。）
3. C8 合规报告生成：复用 ``review_server.build_compliance`` 作为单一来源。

CLI
===
    python -m pipeline.m12_compliance --ep ep01 --lang en [--jobs-dir jobs] [--out 12_out/compliance_report.json]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def apply_explicit_label(
    root: Path,
    ep: str,
    lang: str,
) -> Path:
    """片头 3s 显式 AI 标识（PIL 预渲染 PNG + ffmpeg overlay）。

    返回被修改的成片路径 ``12_out/<ep>.<lang>.mp4``。
    """
    from pipeline.config import load_pipeline_config
    from pipeline.m1_ingest import FFMPEG

    out12 = root / "12_out" / f"{ep}.{lang}.mp4"
    if not out12.is_file():
        raise FileNotFoundError(f"成片不存在: {out12}（M9 未跑？）")

    labels = json.loads(
        (out12.parents[1] / "11_labels" / "labels.json").read_text(encoding="utf-8")
    )
    field, value = labels["implicit"]["metadata_field"], labels["implicit"]["value"]

    from PIL import Image, ImageDraw, ImageFont
    import subprocess
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="m12_lbl_"))
    font_candidates = [
        Path(r"C:\Windows\Fonts\msyh.ttc"),                     # windev
        Path("/usr/share/fonts/google-noto-vf/NotoSans-VF.ttf"),  # anolis 系统字体
        Path("/home/anuser/.local/share/fonts/NotoSansCJK-Thin.ttc"),
        # Debian/Ubuntu noto-cjk 包（Linux 全链实测补充，2026-10-06）
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
    ]
    font_path = next((p for p in font_candidates if p.is_file()), None)
    if font_path is None:
        raise FileNotFoundError(f"无可用的 CJK 字体: {[str(p) for p in font_candidates]}")
    font = ImageFont.truetype(str(font_path), 84)
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    bb = probe.textbbox((0, 0), "本内容由AI生成", font=font, stroke_width=6)
    img = Image.new("RGBA", (bb[2] - bb[0], bb[3] - bb[1]), (0, 0, 0, 0))
    ImageDraw.Draw(img).text(
        (-bb[0], -bb[1]), "本内容由AI生成", font=font,
        fill=(255, 255, 255, 255), stroke_width=6, stroke_fill=(0, 0, 0, 255),
    )
    png = tmp / "ai_label.png"
    img.save(png)

    fc = "[0:v][1:v]overlay=(W-w)/2:64:enable='lte(t,3)'[v]"
    tmp_out = out12.with_name(out12.name + ".lbl.tmp.mp4")
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(out12), "-i", str(png),
         "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-c:a", "copy",
         "-movflags", "+faststart+use_metadata_tags",
         "-metadata", f"{field}={value}", str(tmp_out)],
        check=True, timeout=1200,
    )
    tmp_out.replace(out12)
    return out12


def apply_audio_watermark(root: Path, ep: str, lang: str) -> dict:
    """成片音轨嵌入 C7 音频水印（audmark）+ 回读检测。

    视频 流 copy（不动显式标识帧），音频重编码 AAC 192k，C7 隐式元数据位
    同步重封。检测报告落 ``12_out/audio_wm_report.json``（embed 摘要 +
    对最终成片的 detect 实测），返回同一报告 dict（含 ``match``）。
    """
    from pipeline.config import load_pipeline_config
    from pipeline.m1_ingest import FFMPEG
    from pipeline._audmark_wm import detect_file, embed_file

    out12 = root / "12_out" / f"{ep}.{lang}.mp4"
    if not out12.is_file():
        raise FileNotFoundError(f"成片不存在: {out12}（M9 未跑？）")

    labels = json.loads(
        (out12.parents[1] / "11_labels" / "labels.json").read_text(encoding="utf-8")
    )
    field, value = labels["implicit"]["metadata_field"], labels["implicit"]["value"]
    aw = labels.get("audio_wm") or {}
    payload = str(aw.get("payload") or labels["content_id"])
    engine = str(aw.get("engine") or "audmark")

    import subprocess
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="m12_wm_"))
    wm_wav = tmp / "wm_audio.wav"
    embed_info = embed_file(out12, wm_wav, payload)
    embed_info["engine"] = engine  # C7 登记口径回写

    # 视频 copy + 水印音轨 + 隐式元数据重封（与 M9/封口步同一 C7 字段同值）
    tmp_out = out12.with_name(out12.name + ".wm.tmp.mp4")
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(out12), "-i", str(wm_wav),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
         "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart+use_metadata_tags",
         "-metadata", f"{field}={value}", str(tmp_out)],
        check=True, timeout=1200,
    )
    tmp_out.replace(out12)

    # 对最终成片（AAC 音轨）回读检测——过线才记 ok，如实不虚报
    report = {
        "ep": ep, "lang": lang, "carrier": str(out12),
        "embed": embed_info,
        "detect_final": detect_file(out12, payload),
        "standard": labels.get("standard", "GB45438-2025"),
    }
    out_json = out12.parent / "audio_wm_report.json"
    out_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    from pipeline.config import load_pipeline_config

    ap = argparse.ArgumentParser(
        prog="python -m pipeline.m12_compliance",
        description="M12 合规模块（显式水印 + C8 报告生成）",
    )
    ap.add_argument("--ep", required=True, help="集 ID（如 ep01）")
    ap.add_argument("--lang", required=True, help="目标语种（如 en）")
    ap.add_argument("--jobs-dir", default=None, help="jobs 根目录（默认 configs/pipeline.yaml paths.jobs_dir）")
    ap.add_argument("--configs-dir", default=None, help="configs 目录（默认 configs/）")
    ap.add_argument("--out", default="12_out/compliance_report.json", help="合规报告输出路径（相对 ep 工作区）")
    args = ap.parse_args(argv)

    cfg = load_pipeline_config(args.configs_dir)
    # --jobs-dir 显式优先（与其余模块一致；缺省回落 configs/pipeline.yaml）
    jobs_root = Path(args.jobs_dir) if args.jobs_dir else Path(cfg["paths"]["jobs_dir"])
    root = jobs_root / args.ep

    apply_explicit_label(root, args.ep, args.lang)

    wm_report = apply_audio_watermark(root, args.ep, args.lang)
    audio_wm_state = "ok" if wm_report["detect_final"].get("match") else "failed"

    from pipeline.review_server import build_compliance
    report = build_compliance(
        root, args.ep, args.lang, cfg, reviewed=[],
        explicit_state="ok",
        audio_wm_state=audio_wm_state,
    )

    out_path = root / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n",
    )
    os.replace(str(tmp), str(out_path))
    det = wm_report["detect_final"]
    print(
        f"OK m12_compliance {out_path}"
        f" audio_wm={audio_wm_state}"
        f" hex={det.get('detected_hex')}/{det.get('expected_hex')}"
        f" margin(med)={det.get('median_margin')}"
    )
    if audio_wm_state != "ok":
        print(f"WARN audio_wm 检测未精确匹配，C8 如实记 {audio_wm_state}（见 {out_path.parent / 'audio_wm_report.json'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
