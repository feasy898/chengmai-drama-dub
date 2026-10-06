# 权重来源台账（weights provenance）

> 定位：全组件模型权重的真名对照、校验和与获取方式单一入口。
> REGENERATE §2.2 ③ 原只载获取策略（ModelScope 主路/HF 兜底）未载对照与命令——本文件补齐该缺口。
> 中性名纪律不变：真名只登记于 `docs/`（gate_b0 `SCOPE_EXCLUDES=("docs/","requirements.txt")` 豁免），
> 仓内其余公开文本仍一律中性名。
>
> **来源**：windev-01 销毁前抢救的 `archive/gate-ledger-20261004`（2026-10-06 回传，
> 本地留档 windev-salvage/`yjng_ledger_dump2.txt`；其 SHA256SUMS.txt 对 14 件台账文件做 sha256 锚定）。
> 已与仓内 `docs/*_deps.md` 四篇交叉核对：六件 sha256 与台账逐字节一致，
> `lip_service_deps.md` 仅 1 处差异（原稿含真实宿主 IP，仓内版脱敏为 203.0.113.41）——两源互证成立。

## 1. 全组件权重对照表

| 中性名（服务/模块） | 真实权重 ID（ModelScope 与 HF 同 ID 仓） | 引擎/检出 commit | 权重 sha256-16 | 精度 | 许可 | 仓内登记处 |
|---|---|---|---|---|---|---|
| asr-core（:9001/M4 转写） | `Qwen/Qwen3-ASR-1.7B` | — | `2db53c7d81bd9b8c` | fp16 | Apache-2.0 | docs/gpu_asr_align_deps.md |
| align-core（:9001/M4 字级对齐） | `Qwen/Qwen3-ForcedAligner-0.6B-hf`（备选非 -hf 版 `ALIGN_ID_ALT`） | — | `00568245ceca5af1` | fp16 | Apache-2.0；11 语种无阿语 | docs/gpu_asr_align_deps.md |
| emo-tag（:9001/M4 情绪+事件） | `FunAudioLLM/SenseVoiceSmall`（funasr 1.4.16 推理） | — | `833ca2dcfdf8ec91` | — | FunASR 模型许可（可商用，保留署名） | docs/gpu_asr_align_deps.md |
| dub-tts（:9002/M7 主力） | `IndexTeam/IndexTTS-2.5` | 引擎仓 `index-tts`@`ee40fa7` | 未登记（HF snapshot 见 GPU 机 `~/.cache/huggingface/hub/models--IndexTeam--IndexTTS-2.5`） | **fp32 定案**（Volta 无 bf16，`use_bf16=False`） | bilibili 模型使用协议（保留版权声明与协议副本） | docs/tts_service_deps.md §1/§4 |
| dub-tts-base（:9002 备选链2） | 同仓上一代引擎权重 | — | — | fp16 | — | 权重未部署，链上如实 skip |
| alt-tts-a（:9002 备选A） | `Qwen/Qwen3-TTS-1.7B-CustomVoice` | — | — | — | Apache-2.0 | 权重未部署，链上如实 skip |
| alt-tts-b（:9002 备选B） | `openbmb/VoxCPM2` | 分发 `voxcpm` 0.0.post1+`gf772e498a` | — | fp32 | Apache-2.0 | docs/tts_service_deps.md §1 |
| lip-fast（:9003/M10 口型） | `github.com/TMElyralab/MuseTalk` 1.5 + `musetalkV15/unet.pth` 官方权重；附属：sd-vae / whisper-tiny / dwpose（`dw-ll_ucoco_384.pth`）/ face-parse-bisent / face-detection 0.2.2（S³FD 权重内置） | `0a89dec`（`feat: update download_weights.bat (#372)`） | 未登记 | fp16（`use_float16`） | MIT | docs/lip_service_deps.md §1 |
| lip-pro（:9003 pro 档） | LatentSync 1.6 | TBD-D1 | — | — | — | 权重未部署，服务 mode=pro 恒 503，models.yaml fallback 降级 |
| mt-core（:9004/M6 本地翻译） | `tencent/Hy-MT2-1.8B` | — | 未登记（TBD-T12） | fp16+sdpa | Apache-2.0 | docs/mt_service_deps.md §1 |
| mt-api（M6 兜底后端） | OpenAI 兼容 chat.completions（任意供应商） | — | — | — | — | `MT_API_BASE`/`MT_API_KEY`/`MT_API_MODEL` env 注入，key 不落盘 |

## 2. 获取命令示例（按台账策略重构，**未实测**——原获取动作 2026-09 在 GPU 机完成，
##    台账未逐字登记 CLI 形态；实际以 `gpu/setup_asr_venv.sh`、`gpu/setup_mt_service.sh` 内实现为准）

```bash
# 公共前置（REGENERATE §6.1 实测坑）：hf-mirror 不代理 Xet CAS（401），
# /resolve 302 到的 HF CDN 本机路由超时频发 → 一律禁用 Xet
export HF_HUB_DISABLE_XET=1

# 主路：ModelScope 国内直连（同 ID 仓，实测 11.8MB/s）
modelscope download --model Qwen/Qwen3-ASR-1.7B              --local_dir /data/xdng/models/asr-core
modelscope download --model Qwen/Qwen3-ForcedAligner-0.6B-hf --local_dir /data/xdng/models/align-core
modelscope download --model FunAudioLLM/SenseVoiceSmall      --local_dir /data/xdng/models/emo-tag
modelscope download --model tencent/Hy-MT2-1.8B              --local_dir /data/xdng/models/mt-core

# 兜底：HF 镜像（同 ID 仓）
HF_ENDPOINT=https://hf-mirror.com huggingface-cli download <同 ID> --local-dir <同目录>
```

- 部署落位：`/data/xdng/models/<中性名>/`（dub-tts 11 文件、alt-tts-b 9 文件、lip-fast 含
  `sd-vae/`、`whisper/`、`dwpose/`、`face-parse-bisent/` 与引擎检出 `/data/xdng/smoke/repos/`）。
- 服务运行期同样固定 `HF_HUB_DISABLE_XET=1`（权重全在本地目录，防 from_pretrained 意外联网踩 Xet）。

## 3. 模型 ID 注入（GPU 机本地文件，不入公开仓）

`/data/xdng/etc/model_ids.env`（运行时拼接构造动态加载，真名不进公开仓代码）：

```bash
ASR_DIR=/data/xdng/models/asr-core
ALIGN_ID=Qwen/Qwen3-ForcedAligner-0.6B-hf
ALIGN_ID_ALT=Qwen/Qwen3-ForcedAligner-0.6B
EMO_ID=FunAudioLLM/SenseVoiceSmall
EMO_PKG=funasr
ALIGN_SRC=modelscope    # modelscope（主路，国内直连）| hf（镜像兜底）
EMO_SRC=modelscope
MT_CORE_ID=tencent/Hy-MT2-1.8B   # T12 批增补
```

## 4. 核对记录（2026-10-06 回填当日实测）

| 台账文件 | 台账 SHA256SUMS.txt 锚 | 仓内 docs/ 实测 sha256 | 结论 |
|---|---|---|---|
| gpu_asr_align_deps.md | `946ff6ef979daa79…` | 同 | 逐字节一致 |
| tts_service_deps.md | `c6a36789f06264a8…` | 同 | 逐字节一致 |
| mt_service_deps.md | `60cc9a6a4e9d1602…` | 同 | 逐字节一致 |
| b1_contract_notes.md | `4f3a2c880b7fe8a3…` | 同 | 逐字节一致 |
| m9_mix_notes.md | `6ca2b3dc2df15239…` | 同 | 逐字节一致 |
| setup_windev.md | `532325ef1ba48103…` | 同 | 逐字节一致 |
| lip_service_deps.md | `e203b5fc727f4280…` | `d9732291f107df35…` | 仅 1 处差异：宿主 IP 脱敏（36.139.118.235→203.0.113.41），事实面一致 |

- sha256-16 列仅 asr_align 三模型在台账有登记；其余组件「未登记」即台账无载，**不得**臆造补齐。
- alt-tts-b 经服务懒加载的合成链路台账自注「未实测」（GPU-1 有并行租户占卡）；dub-tts-base /
  alt-tts-a 权重未部署，volta_status 维持 pending（纪律：未实测不写 ok）。
