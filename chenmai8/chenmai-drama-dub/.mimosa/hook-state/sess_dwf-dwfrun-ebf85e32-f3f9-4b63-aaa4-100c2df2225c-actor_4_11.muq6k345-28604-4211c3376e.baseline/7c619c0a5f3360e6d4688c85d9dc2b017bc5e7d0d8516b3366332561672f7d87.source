"""素材获取与生成（M1 前置）。

CLI:
  python -m pipeline.material_fetch --help
  python -m pipeline.material_fetch --ep ep01 --source <path_or_url>
  python -m pipeline.material_fetch --ep ep01 --fetch-all
  python -m pipeline.material_fetch --ep ep01 --only-missing
  python -m pipeline.material_fetch --list [--ep ep01]
  python -m pipeline.material_fetch --ep ep01 --synthetic

默认走真实素材 fetch；fetch 失败或无源时回退到 _generate_synthetic()。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont


def _check_ffmpeg() -> str:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("未找到 ffmpeg（请确认已安装并在 PATH）")
    return "ffmpeg"


def _ensure_portrait() -> Path:
    """Return path to portrait image, generating a fallback if tests/fixtures missing."""
    root = Path.cwd()
    portrait = root / "tests" / "fixtures" / "portrait_pd.jpg"
    if portrait.is_file():
        return portrait
    portrait.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1080, 1920), (30, 30, 40))
    d = ImageDraw.Draw(img)
    for y in range(1920):
        color = int(30 + (y / 1920) * 60)
        d.line([(0, y), (1079, y)], fill=(color, color, color + 10))
    img.save(portrait, quality=85)
    return portrait


def _generate_synthetic(ep: str, clip_dst: str | Path) -> dict:
    """合成素材备用（抽离自 scripts/e2e_smoke.sh 素材生成 heredoc）。

    返回 material layout dict（与 tmp/e2e_material.json 同构）。
    """
    root = Path.cwd()
    clip = Path(clip_dst)
    clip.parent.mkdir(parents=True, exist_ok=True)
    tmp = root / "tmp"
    tmp.mkdir(exist_ok=True)

    SENTS = [
        ("你到底想怎么样", "A", 0),
        ("把话说清楚", "B", -2),
        ("三年了", "A", -1),
    ]
    FONT = Path(r"C:\Windows\Fonts\msyh.ttc")
    if FONT.is_file():
        font = ImageFont.truetype(str(FONT), 64)
    else:
        font = ImageFont.load_default()

    def trim_tail(data: np.ndarray, sr: int, thr: float = 0.004, margin_s: float = 0.06):
        x = np.asarray(data).squeeze()
        nz = np.nonzero(np.abs(x) > thr)[0]
        if not nz.size:
            raise RuntimeError("整段静音")
        a = max(0, int(nz[0] - margin_s * sr))
        b = min(len(x), int(nz[-1] + margin_s * sr))
        return x[a:b]

    voices = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -AssemblyName System.Speech;"
         "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
         "$s.GetInstalledVoices()|ForEach-Object{$_.VoiceInfo.Name+'|'+$_.VoiceInfo.Culture.Name}"],
        capture_output=True, text=True, timeout=60).stdout
    zh_voice = next((ln.split("|")[0] for ln in voices.splitlines()
                     if "|zh" in ln.lower()), None)
    if not zh_voice:
        raise RuntimeError(f"本机无 zh SAPI 声库:\n{voices}")

    wavs = []
    for i, (text, spk, rate) in enumerate(SENTS):
        raw = tmp / f"e2e_sapi_{i}.wav"
        ok48 = tmp / f"e2e_sapi_{i}_48k.wav"
        ps = ("$ErrorActionPreference='Stop';"
              "Add-Type -AssemblyName System.Speech;"
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              f"$s.SelectVoice('{zh_voice}');$s.Rate={rate};"
              "$s.SetOutputToWaveFile('" + str(raw).replace('\\', '/') + "');"
              f"$s.Speak('{text}');$s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=120,
                       capture_output=True, text=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                        "-ar", "48000", "-ac", "1", str(ok48)], check=True, timeout=120)
        wavs.append((ok48, spk))

    cursor, layout = 3.2, []
    for i, ((w, spk), (text, _, _)) in enumerate(zip(wavs, SENTS)):
        data, _sr = sf.read(str(w), dtype="float32")
        data = trim_tail(data, _sr)
        trim_w = w.with_name(w.stem + "_trim.wav")
        sf.write(str(trim_w), data, _sr, subtype="PCM_16")
        wavs[i] = (trim_w, spk)
        dur = len(data) / _sr
        layout.append({"text": text, "speaker": spk, "start": round(cursor, 3),
                       "end": round(cursor + dur, 3)})
        cursor += dur + 1.0
    total = round(cursor + 1.0 - 1.0, 3)
    n_frames = int(round(total * 25))

    sr = 48000
    track = np.zeros((int(round(total * sr)), 2), dtype=np.float32)
    for (w, _), item in zip(wavs, layout):
        data, _sr = sf.read(str(w), dtype="float32")
        data = np.asarray(data).squeeze()
        i0 = int(item["start"] * sr)
        seg = data[: max(0, len(track) - i0)] * 0.85
        track[i0:i0 + len(seg), 0] += seg
        track[i0:i0 + len(seg), 1] += seg
    peak = float(np.abs(track).max())
    if peak > 0.99:
        track *= 0.99 / peak
    audio_wav = tmp / "e2e_audio_48k.wav"
    sf.write(str(audio_wav), track, sr, subtype="PCM_16")

    portrait_path = _ensure_portrait()
    portrait = Image.open(portrait_path).convert("RGB")
    scale = 1920 / portrait.height
    pw = int(portrait.width * scale)
    portrait = portrait.resize((pw, 1920), Image.LANCZOS)
    x0 = max(0, (pw - 1080) // 2)
    base_img = portrait.crop((x0, 0, x0 + 1080, 1920))

    sub_imgs = {}
    for i, item in enumerate(layout):
        img = base_img.copy()
        d = ImageDraw.Draw(img)
        bbox = d.textbbox((0, 0), item["text"], font=font, stroke_width=6)
        tw = bbox[2] - bbox[0]
        d.text(((1080 - tw) / 2 - bbox[0], 1560 - bbox[1]), item["text"],
               font=font, fill=(255, 255, 255), stroke_width=6, stroke_fill=(0, 0, 0))
        sub_imgs[i] = np.asarray(img)[:, :, ::-1].copy()
    none_img = np.asarray(base_img)[:, :, ::-1].copy()

    video_only = tmp / "e2e_video.mp4"
    vw = cv2.VideoWriter(str(video_only), cv2.VideoWriter_fourcc(*"mp4v"), 25, (1080, 1920))
    if not vw.isOpened():
        raise RuntimeError("cv2.VideoWriter 打开失败（mp4v 后端缺失？）")
    for f in range(n_frames):
        t = f / 25.0
        state = none_img
        for i, item in enumerate(layout):
            if item["start"] - 0.02 <= t <= item["end"] + 0.02:
                state = sub_imgs[i]
        vw.write(state)
    vw.release()

    ffmpeg = _check_ffmpeg()
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video_only),
                    "-i", str(audio_wav), "-c:v", "libx264", "-preset", "fast",
                    "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                    "-shortest", str(clip)], check=True, timeout=600)
    video_only.unlink(missing_ok=True)

    layout_doc = {
        "ep": ep,
        "clip": str(clip),
        "duration_s": total,
        "fps": 25,
        "n_frames": n_frames,
        "sentences": layout,
    }
    (tmp / "e2e_material.json").write_text(
        json.dumps(layout_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return layout_doc


def _is_private_host(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return True
    host_lower = host.lower()
    if host_lower in ("localhost", "127.0.0.1", "::1"):
        return True
    if host_lower.startswith("10."):
        return True
    if host_lower.startswith("172."):
        try:
            second = int(host_lower.split(".")[1])
            if 16 <= second <= 31:
                return True
        except ValueError:
            pass
    if host_lower.startswith("192.168."):
        return True
    if host_lower.startswith("169.254."):
        return True
    if host_lower.startswith("fd") or host_lower.startswith("fc"):
        return True
    return False


def fetch_real(ep: str, source: str, clip_dst: str | Path) -> dict:
    """从真实源获取素材（本地路径复制或 http/https 下载）。"""
    clip = Path(clip_dst)
    clip.parent.mkdir(parents=True, exist_ok=True)

    if source.startswith("http://") or source.startswith("https://"):
        if _is_private_host(source):
            raise ValueError(f"拒绝私有/保留地址: {source}")
        import urllib.request
        tmp_path = clip.with_suffix(".download.tmp")
        with urllib.request.urlopen(source, timeout=120) as resp, open(tmp_path, "wb") as f:
            f.write(resp.read())
        shutil.move(str(tmp_path), str(clip))
    else:
        src_path = Path(source)
        if not src_path.is_file():
            raise FileNotFoundError(f"素材源不存在: {src_path}")
        shutil.copy2(src_path, clip)

    ffmpeg = _check_ffmpeg()
    probe = subprocess.run(
        [ffmpeg, "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(clip)],
        capture_output=True, text=True, timeout=300)
    if probe.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {probe.stderr[-500:]}")
    probe_doc = json.loads(probe.stdout)
    duration = float(probe_doc.get("format", {}).get("duration", 0.0))
    return {
        "ep": ep,
        "clip": str(clip),
        "duration_s": round(duration, 3),
        "source": source,
        "method": "fetch",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.material_fetch",
        description="素材获取与生成（真实素材 fetch / 合成备用）",
    )
    parser.add_argument("--ep", help="集 ID，如 ep01")
    parser.add_argument("--source", help="真实素材源（本地路径或 http/https URL）")
    parser.add_argument("--out", help="输出素材路径（默认 <cwd>/clips/<ep>_raw.mp4）")
    parser.add_argument("--fetch-all", action="store_true", help="全量 fetch 模式")
    parser.add_argument("--only-missing", action="store_true", help="仅处理缺失素材")
    parser.add_argument("--list", action="store_true", help="列出素材状态")
    parser.add_argument("--synthetic", action="store_true", help="强制合成（不走 fetch）")

    args = parser.parse_args(argv)

    if args.list:
        if not args.ep:
            print("用法: --list --ep ep01", file=sys.stderr)
            return 2
        clip = Path.cwd() / "clips" / f"{args.ep}_raw.mp4"
        status = "exists" if clip.is_file() else "missing"
        print(json.dumps({"ep": args.ep, "clip": str(clip), "status": status}, ensure_ascii=False, indent=2))
        return 0

    if not args.ep:
        parser.print_help()
        return 2

    clip_dst = args.out or str(Path.cwd() / "clips" / f"{args.ep}_raw.mp4")

    if args.synthetic:
        info = _generate_synthetic(args.ep, clip_dst)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if args.only_missing and Path(clip_dst).is_file():
        print(json.dumps({"ep": args.ep, "clip": clip_dst, "status": "exists", "skipped": True}, ensure_ascii=False, indent=2))
        return 0

    if args.source:
        try:
            info = fetch_real(args.ep, args.source, clip_dst)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"WARN fetch 失败: {exc}，回退合成", file=sys.stderr)
            info = _generate_synthetic(args.ep, clip_dst)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0

    # Default: no source provided -> fallback to synthetic
    print("INFO 未提供素材源，回退合成", file=sys.stderr)
    info = _generate_synthetic(args.ep, clip_dst)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
