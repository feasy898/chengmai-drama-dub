# 资产包 manifest

> 更新时间：2026-10-02

## 已冻结

| 模块 | 标题 | spec | 备注 |
|---|---|---|---|
| M1 | 摄取与预处理 | [m1-ingest.md](specs/m1-ingest.md) | T1 冻结批 |
| M2 | OCR 字幕抽取 | [m2-ocr.md](specs/m2-ocr.md) |  |
| M3 | 人声/背景分离 | [m3-separate.md](specs/m3-separate.md) |  |
| M4 | ASR 识别对齐 | — | 实现已入库（`pipeline/m4_asr.py`） |
| M5 | 说话人区分与角色绑定 | [m5-diar.md](specs/m5-diar.md) |  |
| M6 | 整集上下文翻译 | [m6-translate.md](specs/m6-translate.md) |  |
| M7 | 情绪迁移合成 | [m7-tts.md](specs/m7-tts.md) | T12 冻结批 |
| M8 | 时长对齐 | [m8-align.md](specs/m8-align.md) | T14 定案 |
| M9 | 混音 + 成片合成 | [m9-mix.md](specs/m9-mix.md) | T15 定案 |
| M10 | 按镜头分流口型 | [m10-lipsync.md](specs/m10-lipsync.md) | D1 回炉补篇 2026-10-02 |
| M11 | 字幕擦除 + 目标语渲染 | [m11-subs.md](specs/m11-subs.md) | T8 冻结批 |
| M13 | 审校台 Web | [m13-review.md](specs/m13-review.md) | D1 回炉补篇 2026-10-02 |
| M14 | 任务队列与编排 | [m14-queue.md](specs/m14-queue.md) | D1 回炉补篇 2026-10-02；M10 artifact 槽位已修正；全局拓扑同序校验点见 pipeline/queue.py DEFAULT_GRAPH（m10-m15 段） |
| M15 | 指标体系 | [m15-metrics.md](specs/m15-metrics.md) | D1 回炉补篇 2026-10-02；CallLedger 落库 |

## 未开工 / planned

| 模块 | 标题 | spec | blocker |
|---|---|---|---|
| M12 | 合规标识（显式/C2PA/音频水印） | — | D4 模块入库前不补 spec；当前 e2e_smoke.sh 内为 M12 最小替身（PIL overlay） |

> 勘误注：M10/M13/M14/M15 代码已入库（见 pipeline/），本表"未开工"仅表示
> 资产包文档尚未回炉；代码状态以 pipeline/ 实际文件为准。
