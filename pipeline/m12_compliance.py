"""M12 合规模块（规划 §4 M12；任务 T11/T21）—— 显式水印 + C8 报告生成。

职责
====
1. 显式 AI 标识：片头 3s 文字提示（PIL 预渲染 PNG + ffmpeg overlay）。
2. C8 合规报告生成：复用 ``review_server.build_compliance`` 作为单一来源。

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

    from pipeline.review_server import build_compliance
    report = build_compliance(
        root, args.ep, args.lang, cfg, reviewed=[],
        explicit_state="ok",
    )

    out_path = root / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n",
    )
    os.replace(str(tmp), str(out_path))
    print(f"OK m12_compliance {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
