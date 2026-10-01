# M15 指标体系 spec

> 状态：frozen。对照 `pipeline/m15_metrics/__init__.py` 与
> `pipeline/m15_metrics/_common.py` 逐行核验于 2026-10-02；
> **2026-10-02 回炉定稿**：D1 资产包回炉补篇（双轮盲重生成第二轮）。

## 1. 职责与边界

**做**：消费 M14 metrics.db 记账（`ledger_from_metrics_db`）与上游报告
（M8 `align_report.<lang>.json` / M9 `mix_report.<lang>.json` /
M10 `lip_report.<lang>.json`），计算六项指标并写 `12_out/metrics.json`。

**不做**：不产生音视频产物；不修改上游报告；不跑子进程。
指标数值如实记录，不做阈值门禁（T21 定案）。

六项指标（T21 定案）：
1. `speaker_similarity`：音色相似度（当前占位 1.0，method=placeholder）；
2. `emotion_similarity`：情绪相似度（当前占位 1.0，method=placeholder）；
3. `listen_wer`：识别字错率（当前占位 0.0，method=placeholder）；
4. `duration_alignment_rate`：时长对齐率（M8 align_report alignment_rate
   透传；syl2dur 占位口径）；
5. `lip_score`：口型评分（M10 `verify_paste_back.ok` 通过=1.0，否则 0.0）；
6. `cost_per_minute`：单分钟成本（metrics.db `wall_total` / 60）。

## 2. 对外契约

### 2.1 输入

- `jobs/<ep>/07_synth/align_report.<lang>.json`（M8 工作纸）
- `jobs/<ep>/08_mix/mix_report.<lang>.json`（M9 工作纸）
- `jobs/<ep>/09_lip/lip_report.<lang>.json`（M10 工作纸，可选）
- `jobs/metrics.db` step_metrics 表（M14 记账）

### 2.2 输出

`jobs/<ep>/12_out/metrics.json`：

```json
{
  "ep": "ep01",
  "lang": "en",
  "module": "m15_metrics",
  "n_metrics_total": 6,
  "all_measured": true,
  "missing": [],
  "mix_duration_s": 12.34,
  "wall_total_s": 123.4,
  "metrics": {
    "speaker_similarity": {"value": 1.0, "status": "ok", "method": "..."},
    "emotion_similarity": {"value": 1.0, "status": "ok", "method": "..."},
    "listen_wer": {"value": 0.0, "status": "ok", "method": "..."},
    "duration_alignment_rate": {"value": 0.85, "status": "ok", "method": "..."},
    "lip_score": {"value": 1.0, "status": "ok", "method": "..."},
    "cost_per_minute": {"value": 2.5, "status": "ok", "method": "..."}
  },
  "sources": {
    "align_report": "07_synth/align_report.en.json",
    "mix_report": "08_mix/mix_report.en.json",
    "lip_report": "09_lip/lip_report.en.json",
    "metrics_db": "M14 metrics.db → CallLedger"
  },
  "out": "jobs/.../12_out/metrics.json"
}
```

### 2.3 CallLedger 基类

`pipeline.m15_metrics._common.CallLedger`：
- `stages: dict[str, float]`（`module[lang]` → wall_s）
- `calls: list[dict[str, Any]]`（每行尝试）
- `wall_total` 属性（round 3 位合计）

M14 `ledger_from_metrics_db` 返回 `MetricsDbLedger(CallLedger)` 子类实例，
直接入参 M15 `run`。

## 3. CLI（冻结形态）

```
python -m pipeline.m15_metrics --ep ep01 --lang en [--jobs-dir <dir>]
```

退出码：0 成功；1 输入/文件缺失；2 用法错误。

## 4. eval

```bash
pytest tests/test_m15_metrics.py
```

冻结线（§4 M15）：
- 六项指标"出数与口径正确"即过，数值不做阈值门禁、如实报告；
- 离线 15 条 + 在线组（SAPI zh 声库 + :9002 真配音 + :9001 真回判）
  六项全出数 exit 0；
- 在线组任一前置不可达整组 skip，`--skip-gpu` 豁免口径同 B0/B2/B4。

## 5. 重生成注意事项

- M15 依赖 M14 metrics.db 落库；先 `enqueue` + `resume` 或 e2e_smoke
  跑完再跑 M15；
- `duration_alignment_rate` 口径与 M8 `align_report.summary.alignment_rate`
  同源（plan-based，syl2dur 占位）；拟合表回填后数值会变，但 schema 不变；
- `lip_score` 依赖 M10 `verify_paste_back`；空计划/无成片时 `lip_report`
  缺失 → `lip_score=0.0` 并注明 reason；
- `cost_per_minute` 的 `wall_total` 包含失败重试耗时（真实成本口径）。
