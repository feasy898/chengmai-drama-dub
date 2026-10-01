# M14 任务队列与编排 spec

> 状态：frozen。对照 `pipeline/queue.py` 与 `pipeline/cli.py` 逐行核验于 2026-10-02；
> **2026-10-02 回炉定稿**：D1 资产包回炉补篇（双轮盲重生成第二轮）。

## 1. 职责与边界

职责与口径：
1. **ep×lang 粒度作业**：一个 job = `(ep, lang)` 上以 `to_step` 收尾的步骤
   子图。任务（task）粒度按规划冻结表 `tasks(ep, module, state, deps,
   artifact)` 落库，增补 `lang` 列：`lang=''` 为 ep 级共享步骤（m1–m5
   一类），多语种作业共享同一行 —— 第二个语种入队时已完成共享步骤直接跳过。
2. **四态机**：`pending(待跑) → running(进行) → done(完成) | failed(失败)`。
   作业四态同枚举。失败步在 resume 时重试（断点续跑语义：修好即续）。
3. **断点续跑**：状态为 done 且产物在位（存在且非空）的任务跳过不重跑；
   产物被清理则降级回 pending 重跑。进程被杀时停在 running 的任务，下次
   resume 按 `run_id` 判定为陈旧 → 收回 pending 从断点重跑（单写者 MVP：
   同一批 jobs.db 同时只应有一个队列进程在跑，见下方"并发口径"）。
4. **依赖未满足拒绝执行**：任务的依赖步任一非 done（失败/被拒绝连带）→
   本步拒绝执行（note 记原因，不产生子进程），作业判 failed。
5. **步骤图与服务依赖声明（单一来源）**：`DEFAULT_GRAPH` 声明每步的依赖
   （deps）、产物（artifact 模板）与**外部服务依赖**（`SERVICE_DEPS`，
   如 `asr-align@9001`）。M13 审校台的单句重生成（m6→m8→m7→m9）与导出
   回路（m11→m9）按规划与本图同源 —— 通过 `subgraph` / `service_deps`
   导入复用，不另行声明第二份。步骤执行器为数据驱动（`StepSpec.cmd`
   argv 模板），可用 `--graph <json>` 换整张图（eval 用微型桩图验收，
   零 GPU/零真模型）。
6. **metrics.db 记账 → M15 CallLedger**：每步每次尝试落一行
   `step_metrics`（墙钟/退出码/声明服务），`ledger_from_metrics_db`
   把账目水合成真实的 `pipeline.m15_metrics._common.CallLedger`
   （子类只增不改名），可直接喂给 M15 成本指标 `cost.compute` ——
   全链路逐步耗时的"透传"路径由此闭环（M15 cost 模块 docstring 预留口径）。

并发口径：MVP 单写者——jobs.db/metrics.db 的写入方只有队列进程自身（步骤
子进程只写各自工作区产物）；多作业可各自 enqueue 后分别 resume。步骤在子
进程内串行执行（依赖序；GPU 服务侧 lip/tts 存在显存互斥，串行即正确性），
"本地进程池"的并行面 = 多作业各自独立进程。

SQL 纪律：全部外部输入经 `?` 参数绑定；DDL 为静态常量，无拼接。

## 2. 对外契约

### 2.1 步骤图默认值（`DEFAULT_GRAPH`）

| 步骤 | scope | deps | artifact | services |
|---|---|---|---|---|
| m1 | ep |  | `01_media/video_1080x1920_25fps.mp4` |  |
| m3 | ep | m1 | `04_dial/vocals.wav` |  |
| m4 | ep | m3 | `04_dial/asr.jsonl` | asr-align@9001 |
| m2 | ep | m1,m4 | `03_ocr/ocr_merged.jsonl` |  |
| m5 | ep | m2,m4 | `04_dial/utterances.jsonl` |  |
| m6 | lang | m5 | `06_mt/translations.jsonl` | mt@9004 |
| m8 | lang | m6 | `07_synth/synth_plan.{lang}.jsonl` |  |
| m7 | lang | m8 | `07_synth/wavs` | tts@9002 |
| m11 | lang | m6 | `10_subs/tgt.{lang}.ass` |  |
| m9 | lang | m7,m11 | `12_out/{ep}.{lang}.mp4` |  |
| m10 | lang | m9 | `09_lip/done/{ep}.{lang}.lip.mp4` | lip@9003 |
| m12 | lang | m10 | `12_out/compliance_report.json` |  |
| m15 | lang | m12 | `12_out/metrics.json` |  |

产物模板中 `{ep}` / `{lang}` 由渲染器替换；目录型产物以"内含非空文件"
判定位。

### 2.2 跳过判据

断点续跑跳过条件 = 任务 state=`done` 且 artifact 存在且非空。
M10 槽位已修正为回贴成片（`09_lip/done/{ep}.{lang}.lip.mp4`），
与 `pipeline/m10_lipsync.py` 真实交付物一致。

## 3. CLI（冻结形态）

```
python -m pipeline.cli init <ep> [--jobs-dir <dir>]
python -m pipeline.cli validate <kind> <path>
python -m pipeline.cli run <ep> --langs <langs> --to <step> [--input <src>] [--param K=V]...
python -m pipeline.cli enqueue <ep> --langs <langs> --to <step> [--input <src>] [--param K=V]...
python -m pipeline.cli status [--job <id>] [--ep <ep>] [--lang <lang>] [--json]
python -m pipeline.cli resume --job <id>... | --ep <ep> [--lang <lang>] | --all
```

退出码：0 成功；1 校验/文件/执行失败；2 未实现/用法错误。

## 4. eval

```bash
pytest tests/test_m14_queue.py
```

冻结线（§4 M14）：
- 步骤图 DDL + `validate_graph` 无环/scope 单调 + `subgraph` 拓扑序；
- `enqueue` + `resume` 断点语义：done 且产物在位跳过、done 但产物缺失
  降级重跑、陈旧 running 收回 pending；
- `default_graph` 中 M10 artifact 槽位 = `09_lip/done/{ep}.{lang}.lip.mp4`；
- `ledger_from_metrics_db` 产出 `stages` / `calls` / `wall_total` 且可直接
  喂给 M15 `CallLedger`；
- CLI 六子命令真实子进程 exit 0。

## 5. 重生成注意事项

- M14 步骤图只增不改名（`validate_graph` 强校验依赖已知 + 无环）；
- `--graph <json>` 可换整张图，但替换图必须过 `validate_graph`；
- metrics.db 与 jobs.db 同根，单写者 MVP 不引入分布式锁；
- e2e_smoke.sh 与 `pipeline/queue.py` DEFAULT_GRAPH 同序校验点：
  两处头部互注"同序校验点"，改一处必查另一处。
