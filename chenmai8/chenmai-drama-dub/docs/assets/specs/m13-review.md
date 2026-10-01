# M13 审校台 Web spec

> 状态：frozen。对照 `pipeline/review_server.py` 逐行核验于 2026-10-02；
> **2026-10-02 回炉定稿**：D1 资产包回炉补篇（双轮盲重生成第二轮）。

## 1. 职责与边界

定位：内网人工审校工作台（无鉴权，规划口径），消费已产出的上游契约
（C2 逐句事实表 / C4 译文候选 / C5 合成计划 / 08_mix 音轨 / 12_out 成片），
四组功能（对应任务 T19 验收面）：

1. 逐句表格 `GET /api/tasks/{tid}/lines`：C4 译文与候选
   (text/syl/est_dur/q) + 时长预算 (C4.budget lo/hi) + **实测时长**
   （07_synth/wavs 逐文件 soundfile 实测，落窗判定）+ 审校状态机列；
2. 单句重生成回路 `PUT .../translation`（改译文，回写 C4）→
   `POST .../regenerate`：M8 单句重对齐（复算 C5 计划行）→ M7 重合成
   （:9002 经 `M7_TTS_URL` 探测，**隧道不可达时 mock 占位音频生成并在
   状态机/响应中如实注明**，mock 波形以译文哈希为种子、文本变则波形变）
   → M9 重混（整集 ducking+响度+成片）→ 片段预览（句窗切片 wav / 成片
   mp4 切段）；另有 `POST .../retranslate` 走 M6 单句重翻（术语注入）；
3. 术语表管理 `06_mt/terms_seed.json` CRUD（M6 术语种子同源格式
   `{"源术语": {"语种": "译法"}}`），有效术语 = C3 terms[lang] ∪ 种子
   （复用 `m6.build_glossary`，M6 上下文注入同一实现）；
4. 导出：MP4（12_out 成片，缺则触发 M9 compose）+ 字幕 SRT
   （C2 句窗 × C4 chosen）+ 合规报告 C8 `compliance.<lang>.json`
   （findings 由 M12 规则引擎产出，未接入前留空并如实注明）。

状态机（SQLite `lines.state`，逐句）:

    idle → edited（改译/重翻）→ aligned（M8）→ synthesized（M7）
         → ready（M9）；任一步失败 → error（detail 留痕）。
    每步转移写 `regens` 流水（stage/from_state/to_state/ok/detail）。
    注：`LINE_STATES` 元组含 `mixed`（历史预留），但审校台单句重生成回路
    （`regenerate`）实际从 `synthesized` 直接进入 `ready`，不经过 `mixed`。

## 2. API（冻结形态）

### 2.1 请求体模型

- `TaskCreate`：`ep` / `lang`（默认 en）
- `TranslationEdit`：`text` / `chosen_idx`
- `RetranslateReq`：`backend`（默认 mock）/ `n_candidates`（1-5）
- `RegenerateReq`：`stages`（默认 ["m8","m7","m9"]）/ `tts`（auto|force-mock）
  / `compose`（默认 true）
- `GlossaryUpsert`：`tgt`
- `ExportReq`：`mp4` / `srt` / `compliance`（默认 true）

### 2.2 路由清单

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | 创建/打开任务 |
| GET | `/api/tasks` | 任务清单 |
| GET | `/api/tasks/{task_id}` | 任务详情 + regens 计数 |
| GET | `/api/tasks/{task_id}/lines` | 逐句表格 |
| GET | `/api/tasks/{task_id}/lines/{utt_id}` | 单句详情 |
| PUT | `/api/tasks/{task_id}/lines/{utt_id}/translation` | 改译文/换候选 |
| POST | `/api/tasks/{task_id}/lines/{utt_id}/retranslate` | M6 单句重翻 |
| POST | `/api/tasks/{task_id}/lines/{utt_id}/regenerate` | M8→M7→M9 重生成 |
| GET | `/api/tasks/{task_id}/lines/{utt_id}/preview` | 句窗 wav 预览 |
| GET | `/api/tasks/{task_id}/lines/{utt_id}/clip` | 成片句窗 mp4 切段 |
| GET | `/api/tasks/{task_id}/video` | 成片下载 |
| POST | `/api/tasks/{task_id}/export` | MP4/SRT/C8 打包导出 |
| GET | `/api/tasks/{task_id}/export/files` | 导出文件清单 |
| GET | `/api/tasks/{task_id}/glossary` | 术语表（C3 ∪ seed） |
| PUT | `/api/tasks/{task_id}/glossary/{src}` | 术语 upsert |
| DELETE | `/api/tasks/{task_id}/glossary/{src}` | 术语删除 |
| GET | `/api/tasks/{task_id}/files/{name}` | 12_out 白名单下载 |

### 2.3 并发与安全

- SQLite 单连接 + 线程锁串行化 DB 访问，WAL + busy_timeout；
- 重生成按 `(ep,lang)` 任务级锁串行（同集重混互斥，异集并行）；
- SQL 一律参数绑定（`?` 占位），ep 经 `pipeline.scaffold.ep_dir`
  白名单校验，文件下载仅限 `12_out` 白名单文件名。

## 3. CLI（冻结形态）

```
python -m pipeline.review_server --jobs jobs --port 8080
python -m pipeline.review_server --jobs jobs --db <path> --host 0.0.0.0
```

## 4. eval

```bash
pytest tests/test_review_console.py
```

冻结线（§4 M13）：
- httpx ASGI 端到端：创建任务 → 改一句 → 重生成状态机 → 导出 200；
- 离线路径全绿；GPU 真合成用例在 `:9002` 不可达时整组 skip 并注明
  skip-gpu 归因。

## 5. 重生成注意事项

- 单句重生成的 M9 与导出 MP4 共用同一任务级锁（同写 08_mix/12_out）；
- mock 占位音频以译文哈希为种子，文本变则波形变——支撑"导出片段该句
  变化、其余句不变"的可检验口径；
- M12 未接入前 `build_compliance` 的 findings 与 explicit/c2pa/audio_wm
  如实标 pending，不伪造 ok。
