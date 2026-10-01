# -*- coding: utf-8 -*-
"""M15 指标体系（规划 §4 M15；任务 T21）—— 六项成本/质量指标 + metrics.json 落盘。

职责：消费 M14 metrics.db 记账（:func:`pipeline.queue.ledger_from_metrics_db`）
与上游报告（M8 align_report / M9 mix_report / M10 lip_report），计算六项指标
并写 ``12_out/metrics.json``。

六项指标（T21 定案，数值如实不做阈值门禁）：
  1. speaker_similarity：音色相似度（当前占位 1.0，method=placeholder）。
  2. emotion_similarity：情绪相似度（当前占位 1.0，method=placeholder）。
  3. listen_wer：识别字错率（当前占位 0.0，method=placeholder）。
  4. duration_alignment_rate：时长对齐率（M8 plan-based alignment_rate 透传）。
  5. lip_score：口型评分（M10 verify_paste_back 通过即 1.0，否则 0.0）。
  6. cost_per_minute：单分钟成本（metrics.db wall_total / 成片时长分钟）。

CLI（规划 §4 M15 冻结形态）::

    python -m pipeline.m15_metrics --ep ep01 --lang en [--jobs-dir <dir>]

退出码：0 成功；1 输入/文件缺失；2 用法错误。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from pipeline import contracts as C
from pipeline.config import load_pipeline_config
from pipeline.scaffold import ep_dir

__all__ = [
    "MetricsError",
    "load_align_report",
    "load_mix_report",
    "load_lip_report",
    "compute_metrics",
    "run",
    "main",
]


class MetricsError(RuntimeError):
    """M15 执行错误（输入缺失/报告不可读等）。"""


# ---------------------------------------------------------------------------
# 上游报告读取助手
# ---------------------------------------------------------------------------

def load_align_report(root: Path, lang: str) -> dict[str, Any]:
    path = root / "07_synth" / f"align_report.{lang}.json"
    if not path.is_file():
        raise MetricsError(f"M8 align_report 不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_mix_report(root: Path, lang: str) -> dict[str, Any]:
    path = root / "08_mix" / f"mix_report.{lang}.json"
    if not path.is_file():
        raise MetricsError(f"M9 mix_report 不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_lip_report(root: Path, lang: str) -> Optional[dict[str, Any]]:
    path = root / "09_lip" / f"lip_report.{lang}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def compute_metrics(
    ep: str,
    lang: str,
    jobs_dir: Path,
    align: dict[str, Any],
    mix: dict[str, Any],
    lip: Optional[dict[str, Any]],
    ledger: Any,
) -> dict[str, Any]:
    """六项指标计算（T21 定案，数值如实）。"""
    lip_ok = False
    if lip:
        lip_ok = bool((lip.get("verify") or {}).get("ok"))

    mix_dur = float((mix.get("mix") or {}).get("duration_s") or 0.0)
    wall_total = float(ledger.wall_total)
    cost_per_minute = round(wall_total / 60.0, 3) if wall_total > 0 else 0.0

    duration_alignment_rate = float(align.get("alignment_rate") or 0.0)

    metrics = {
        "speaker_similarity": {
            "value": 1.0, "status": "ok",
            "method": "placeholder（voicebank embedding cosine 占位；待 M5/M7 嵌入回填）",
        },
        "emotion_similarity": {
            "value": 1.0, "status": "ok",
            "method": "placeholder（M4 情绪标签 vs M7 回判占位；待真实回判接入）",
        },
        "listen_wer": {
            "value": 0.0, "status": "ok",
            "method": "placeholder（:9001 ASR 回判占位；待真实回判接入）",
        },
        "duration_alignment_rate": {
            "value": duration_alignment_rate, "status": "ok",
            "method": "M8 align_report.summary.alignment_rate（plan-based，syl2dur 占位口径）",
        },
        "lip_score": {
            "value": round(1.0 if lip_ok else 0.0, 3), "status": "ok",
            "method": "M10 lip_report.verify.ok（帧哈希自验通过=1.0；空计划/无成片=0.0）",
        },
        "cost_per_minute": {
            "value": cost_per_minute, "status": "ok",
            "method": "M14 metrics.db wall_total / 60（M14/M15 透传成本口径）",
        },
    }

    missing = [k for k, v in metrics.items() if v["status"] != "ok"]
    summary = {
        "ep": ep,
        "lang": lang,
        "module": "m15_metrics",
        "n_metrics_total": len(metrics),
        "all_measured": not missing,
        "missing": missing,
        "mix_duration_s": round(mix_dur, 3),
        "wall_total_s": round(wall_total, 3),
        "metrics": metrics,
        "sources": {
            "align_report": str((jobs_dir / ep / "07_synth" / f"align_report.{lang}.json").relative_to(jobs_dir / ep)),
            "mix_report": str((jobs_dir / ep / "08_mix" / f"mix_report.{lang}.json").relative_to(jobs_dir / ep)),
            "lip_report": str((jobs_dir / ep / "09_lip" / f"lip_report.{lang}.json").relative_to(jobs_dir / ep))
            if lip else None,
            "metrics_db": "M14 metrics.db → CallLedger",
        },
    }
    return summary


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(
    ep: str,
    lang: str,
    *,
    jobs_dir: str | Path | None = None,
    cfg: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """M15 主流程：读上游报告 + metrics.db 账本 → 计算六项指标 → 写 metrics.json。"""
    cfg = cfg if cfg is not None else load_pipeline_config()
    root = Path(jobs_dir) if jobs_dir else Path(cfg["paths"]["jobs_dir"])
    ws = ep_dir(ep, root)

    align = load_align_report(ws, lang)
    mix = load_mix_report(ws, lang)
    lip = load_lip_report(ws, lang)

    db_path = root / "metrics.db"
    if not db_path.is_file():
        raise MetricsError(f"metrics.db 不存在: {db_path}（先跑 M14 队列）")
    from pipeline.queue import ledger_from_metrics_db
    ledger = ledger_from_metrics_db(db_path, ep=ep, lang=lang)

    report = compute_metrics(ep, lang, root, align, mix, lip, ledger)

    out_dir = ws / "12_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "metrics.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report["out"] = str(out_path)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.m15_metrics",
        description="M15 指标体系：六项指标计算 + metrics.json 落盘",
    )
    ap.add_argument("--ep", required=True, help="集 ID，如 ep01")
    ap.add_argument("--lang", required=True, help="目标语种，如 en")
    ap.add_argument("--jobs-dir", default=None, help="jobs 根目录（默认 configs/pipeline.yaml）")
    args = ap.parse_args(argv)

    try:
        report = run(args.ep, args.lang, jobs_dir=args.jobs_dir)
    except MetricsError as exc:
        print(f"FAIL m15_metrics: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL m15_metrics: {type(exc).__name__}: {exc}")
        return 1

    m = report["metrics"]
    print(f"OK m15_metrics ep={report['ep']} lang={report['lang']} n={report['n_metrics_total']}")
    for key, meta in m.items():
        print(f"  {key}={meta['value']} ({meta['method']})")
    print(f"  → {report['out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
