"""audmark 组件实现（音频水印嵌入/盲检测，本机 CPU 可跑）—— M12 内部实现件。

中性名 ``audmark`` 的架构实现（组件注册表见 configs/models.yaml 与内部台账
附录A；公开文本不写上游名，依赖登记只出现在 requirements.txt）。

算法（对数域分块谱补丁 + BPSK + 盲相关检测）：
  1. 载荷：16-bit（C7 ``audio_wm.bits``），``payload_bits`` = SHA-256 前 2 字节。
  2. 嵌入：48kHz STFT（帧长 2048 / 帧移 1024 / Hann，WOLA 双窗重构），取
     1–4kHz 带内整除 16 的 bin，均分为 16 个连续块；每帧每块施加对数域深度
     ``±beta`` 的同向增量（BPSK：块增量符号 = 伪随机扰码 × 比特极性）。扰码
     按帧序由固定 key 确定性生成（收发同源，检测无需密钥协商）。
  3. 检测：同网格 STFT 逐帧取各块对数均值，去带内均值白化后与扰码相关，
     跨帧均值投票出 16 bit；报告逐比特 t 型 margin 与精确匹配置信。
  4. 鲁棒域（2026-10-06 实测，e2e01v2 真实音轨）：WAV 回读 / AAC 192k·128k·96k
     重编码 / MP3 128k / EBU R128 loudnorm / 幅度增益，均 16/16 精确检出；
     负控（未嵌音频）margin≈0.1–0.5 不误报。域界（如实）：
     a) 扰码按帧序锁定时间轴，时间平移/切片后不可检（交付件检测口径不涉及）；
     b) 乘性嵌入依赖宿主 1–4kHz 带内能量，纯单音/带内近静音素材无载体，
        ``embed_verify_match`` 如实 False（真实链 M9 输出为宽带语音+背景，可嵌）。
  5. 立体声：嵌入仅施加于中置声道 mid=(L+R)/2（单声道兼容，side 不动）；
     检测对下混单声道执行。

对外口径：
  - :func:`embed_file` —— 音频/视频容器音轨 → 水印 WAV（解码-嵌入-封装）；
  - :func:`detect_file` —— 容器音轨 → 检测报告（detected/expected/margin/match）。

ffmpeg 直调（与仓内 m1/m9 同口径，pydub 不引入）；CPU 实测 10.3s 音轨
嵌入+检测 <2s（2026-10-06，e2e01v2）。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import numpy as np

__all__ = [
    "AudmarkError", "payload_bits", "bits_hex", "embed_file", "detect_file",
]

#: 扰码种子（收发同源的固定 key；中性名常量，不含上游信息）
_KEY = 0x6175646D

#: STFT 网格（帧长与 AAC 长块对齐；帧移 50% Hann，WOLA Σw²=1）
_N = 2048
_HOP = 1024

#: 嵌入频带（Hz）与默认对数域深度（±beta ≈ ±1.7dB，波形 SNR 实测 ≈20dB）
_BAND_LO_HZ, _BAND_HI_HZ = 1000.0, 4000.0
_BETA = 0.20

#: 帧门限（带内峰值低于满幅该比例的近静音帧不嵌/不统计，防底噪水印可闻）
_FLOOR = 1e-5

_BITS = 16          # C7 audio_wm.bits 契约冻结值
_SR = 48_000        # 处理采样率（解码统一重采样）


class AudmarkError(RuntimeError):
    """audmark 组件执行错误（解码失败/载荷非法等）。"""


def payload_bits(payload: str, bits: int = _BITS) -> np.ndarray:
    """载荷字符串 → 定长比特（SHA-256 前 bits/8 字节大端展开）。"""
    if not payload:
        raise AudmarkError("payload 不得为空")
    d = hashlib.sha256(payload.encode("utf-8")).digest()
    v = int.from_bytes(d[: bits // 8], "big")
    return np.array([(v >> (bits - 1 - k)) & 1 for k in range(bits)], dtype=np.int8)


def bits_hex(bits: np.ndarray) -> str:
    """比特序列 → 十六进制串（检测报告口径）。"""
    v = int("".join(map(str, np.asarray(bits).tolist())), 2)
    return v.to_bytes(len(bits) // 8, "big").hex()


def _band() -> tuple[int, int, int]:
    """嵌入带 [lo, hi_eff] 与每块 bin 数（16 块均分，余量 bin 弃用）。"""
    lo = int(np.ceil(_BAND_LO_HZ * _N / _SR)) + 1
    hi = int(np.floor(_BAND_HI_HZ * _N / _SR))
    n = (hi - lo + 1) // _BITS * _BITS
    return lo, lo + n - 1, n // _BITS


def _patterns(n_frames: int) -> np.ndarray:
    """帧序扰码（±1，固定 key 确定性生成，收发同源）。"""
    rng = np.random.default_rng(_KEY)
    return np.sign(rng.standard_normal((n_frames, _BITS))).astype(np.float32)


def _frame_count(n: int) -> int:
    return (n - _N) // _HOP + 1


def _decode(src: Path, channels: int) -> np.ndarray:
    """容器音轨 → f32 [frames, ch]（48kHz；ffmpeg 直调）。"""
    cmd = ["ffmpeg", "-v", "error", "-i", str(src), "-map", "0:a:0",
           "-f", "f32le", "-ac", str(channels), "-ar", str(_SR), "-"]
    try:
        raw = subprocess.run(cmd, check=True, capture_output=True,
                             timeout=600).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise AudmarkError(f"音轨解码失败: {src}（{e}）") from e
    x = np.frombuffer(raw, dtype=np.float32)
    return x.reshape(-1, channels)


def _write_wav(path: Path, x: np.ndarray) -> None:
    """f32 [frames, ch] → 48kHz PCM_16 WAV（ffmpeg 直调）。"""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "f32le", "-ar", str(_SR),
           "-ac", str(x.shape[1]), "-i", "-",
           "-c:a", "pcm_s16le", "-ar", str(_SR), str(path)]
    try:
        subprocess.run(cmd, input=x.astype(np.float32).tobytes(), check=True,
                       capture_output=True, timeout=600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise AudmarkError(f"WAV 封装失败: {path}（{e}）") from e


def _embed_mono(x: np.ndarray, bits: np.ndarray, beta: float) -> tuple[np.ndarray, int]:
    """单声道嵌入（对数域分块谱补丁，WOLA 重构）；返回 (水印信号, 实嵌帧数)。"""
    lo, hi_eff, bpc = _band()
    w = np.hanning(_N)
    pad = _N  # 两端补 N：每个样本被完整重叠窗覆盖（WOLA 内区 Σw²=1）
    xp = np.concatenate([np.zeros(pad), x.astype(np.float64), np.zeros(pad)])
    off = pad // _HOP
    pats = _patterns(_frame_count(len(x)))
    acc = np.zeros(len(xp))
    norm = np.zeros(len(xp))
    n_gated = 0
    for f in range(_frame_count(len(xp))):
        sidx = f - off
        if sidx < 0 or sidx >= len(pats):
            continue  # 纯补零前缀/后缀帧不嵌
        seg = xp[f * _HOP: f * _HOP + _N] * w
        spec = np.fft.rfft(seg)
        mag = np.abs(spec)
        if mag[lo:hi_eff + 1].max() < _FLOOR * _N / 4:
            continue  # 近静音帧不嵌（防底噪水印可闻）
        n_gated += 1
        lm = np.log(mag + 1e-10)
        pat = pats[sidx]
        for b in range(_BITS):
            vb = 2.0 * float(bits[b]) - 1.0      # BPSK：1→+1 / 0→−1
            i0 = lo + b * bpc
            lm[i0:i0 + bpc] += beta * pat[b] * vb  # 整块同向（窗平滑下存活）
        spec2 = np.exp(lm) * np.exp(1j * np.angle(spec))
        t = f * _HOP
        acc[t:t + _N] += np.fft.irfft(spec2, n=_N) * w  # WOLA：合成窗×分析窗
        norm[t:t + _N] += w * w
    y = (acc / np.maximum(norm, 1e-3))[pad: pad + len(x)]
    if not np.isfinite(y).all():
        raise AudmarkError("嵌入结果含非有限值（输入非法？）")
    return y.astype(np.float32), n_gated


def _detect_mono(x: np.ndarray, bits: np.ndarray | None) -> dict:
    """单声道盲检测（逐帧分块对数均值去均值白化 × 扰码相关，跨帧投票）。"""
    lo, hi_eff, bpc = _band()
    w = np.hanning(_N)
    F = _frame_count(len(x))
    pats = _patterns(F)
    S = np.zeros((F, _BITS))
    gate = np.zeros(F, dtype=bool)
    for f in range(F):
        seg = x[f * _HOP: f * _HOP + _N] * w
        mag = np.abs(np.fft.rfft(seg))
        if mag[lo:hi_eff + 1].max() < _FLOOR * _N / 4:
            continue
        gate[f] = True
        lm = np.log(mag + 1e-10)
        cm = np.array([lm[lo + b * bpc: lo + (b + 1) * bpc].mean()
                       for b in range(_BITS)])
        S[f] = (cm - cm.mean()) * pats[f]
    g = S[gate]
    if len(g) == 0:
        return {"match": False, "detected_hex": None, "n_frames": 0,
                "total_frames": F, "note": "无有效帧（纯静音？）"}
    per_bit = g.mean(axis=0)
    det = (per_bit > 0).astype(np.int8)
    margin = np.abs(per_bit) / (g.std(axis=0) + 1e-9) * np.sqrt(len(g))
    out = {
        "detected_hex": bits_hex(det),
        "n_frames": int(gate.sum()),
        "total_frames": F,
        "min_margin": round(float(margin.min()), 4),
        "median_margin": round(float(np.median(margin)), 4),
    }
    if bits is not None:
        out["expected_hex"] = bits_hex(bits)
        out["bit_acc"] = round(float((det == bits).mean()), 4)
        out["match"] = bool((det == bits).all())
    return out


def embed_file(src: Path, dst_wav: Path, payload: str, beta: float = _BETA) -> dict:
    """容器音轨 → 水印 WAV（立体声嵌中置、单声道直嵌；返回嵌入摘要）。

    自证：嵌入后立即对水印 WAV 回读检测，``embed_verify_match`` 随摘要返回
    （宿主 1–4kHz 带内能量近静音时乘性嵌入无载体，如实记 False——真实链 M9
    输出为宽带语音+背景，实测可嵌；纯单音合成素材属域界）。
    """
    bits = payload_bits(payload)
    src = Path(src)
    stereo = _decode(src, 2)  # 单声道源解码为双副本，等效直嵌
    peak_in = float(np.abs(stereo).max())
    mid = stereo.mean(axis=1)
    side = stereo[:, 0] - stereo[:, 1]
    mid_wm, n_gated = _embed_mono(mid, bits, beta)
    verify = _detect_mono(mid_wm, bits)
    wm = np.stack([mid_wm + side / 2, mid_wm - side / 2], axis=1)
    snr = 10 * np.log10(np.sum(mid ** 2) / max(np.sum((mid_wm - mid) ** 2), 1e-20))
    peak = float(np.abs(wm).max())
    if peak > 1.0:  # 防削波：增益不变量，检测不受影响
        wm = wm / peak
    _write_wav(Path(dst_wav), wm)
    return {
        "engine": "audmark",
        "payload": payload,
        "expected_hex": bits_hex(bits),
        "bits": _BITS,
        "beta": beta,
        "sample_rate": _SR,
        "snr_db": round(float(snr), 1),
        "peak_in": round(peak_in, 4),
        "peak_out": round(min(peak, 1.0), 4),
        "n_frames_embedded": n_gated,
        "embed_verify_match": verify.get("match", False),
        "out": str(dst_wav),
    }


def detect_file(src: Path, payload: str | None = None) -> dict:
    """容器音轨盲检测（下混单声道；payload 给出时附精确匹配判定）。"""
    mono = _decode(Path(src), 1).ravel()
    bits = payload_bits(payload) if payload else None
    return _detect_mono(mono, bits)
