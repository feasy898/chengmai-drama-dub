# -*- coding: utf-8 -*-
"""M15 指标体系基类 —— CallLedger 会话账本（契约只增不改名）。

M14 队列每步尝试落一行 ``step_metrics`` 到 metrics.db，
:func:`pipeline.queue.ledger_from_metrics_db` 将其水合为
:class:`CallLedger` 子类实例。M15 的 :func:`run` 消费账本以计算
六项成本/质量指标（T21 定案，数值如实不做阈值门禁）。

子类化纪律：只增不改名。
"""

from __future__ import annotations

from typing import Any


class CallLedger:
    """单次队列会话的记账容器（M14 metrics.db 的一批 step_metrics 水合）。

    Attributes:
        stages: ``{module[lang]: wall_s}``，逐步累计耗时（含失败重试）。
        calls: 每尝试一行的清单
            ``[{service, op, seconds, ok, note}]``。
    """

    def __init__(self) -> None:
        self.stages: dict[str, float] = {}
        self.calls: list[dict[str, Any]] = []

    @property
    def wall_total(self) -> float:
        """本次运行全阶段墙钟累计（秒，round 3 位）。"""
        return round(sum(self.stages.values()), 3)
