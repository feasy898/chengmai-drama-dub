# 冻结契约（C1–C8）

> 本文件为契约索引； authoritative schema 为 `pipeline/contracts.py`（pydantic 模型）。

## C1 镜头表
`pipeline/contracts.py` → `Shot`

## C2 逐句事实表
`pipeline/contracts.py` → `Utterance`

## C3 角色卡 / 术语
`pipeline/contracts.py` → `Character` / `CastBook`

## C4 译文候选
`pipeline/contracts.py` → `Translation` / `Candidate`

## C5 合成计划
`pipeline/contracts.py` → `SynthPlanItem`

## C6 口型分流
`pipeline/contracts.py` → `LipItem`

## C7 标签种子
`pipeline/contracts.py` → `Labels`

文件：`11_labels/labels.json`

字段：
- `service_provider`
- `content_id`
- `standard`
- `explicit.text`
- `explicit.video`
- `explicit.audio_announce`
- `implicit.metadata_field`
- `implicit.value`
- `c2pa`
- `audio_wm`

## C8 合规报告
`pipeline/contracts.py` → `ComplianceReport` / `Finding` / `LabelStatus`

文件：`12_out/compliance_report.json`（M12 模块生成）

生成器：`pipeline.m12_compliance`

单一来源：`pipeline.review_server.build_compliance`（M12 与 M13 复用同一函数）

### 字段
| 字段 | 类型 | 说明 |
|---|---|---|
| `market` | str | 目标市场（取自 channels config） |
| `ep` | str | 集 ID |
| `findings` | list[Finding] | 规则引擎发现（M12 接入前为空） |
| `label_status` | LabelStatus | 四项标识落实状态 |
| `human_review` | list[str] | 已审校 utt_id 清单 |
| `generator` | str | 生成器署名 |

### label_status 四项
| 键 | 语义 | 当前口径 |
|---|---|---|
| `explicit` | 片头 3s 显式水印 | M12 写入后 ok，否则 pending |
| `implicit` | 隐式元数据 AI 标识 | ffprobe 实测 C7 implicit.value |
| `c2pa` | C2PA 嵌入 | pending（未实现） |
| `audio_wm` | 音频水印 | pending（未实现） |
