# M10 按镜头分流口型 spec

> 状态：frozen。对照 `pipeline/m10_lipsync.py` 逐行核验于 2026-10-02；
> **2026-10-02 回炉定稿**：D1 资产包回炉补篇（双轮盲重生成第二轮），
> 与 `pipeline/queue.py` M10 artifact 槽位（`09_lip/done/{ep}.{lang}.lip.mp4`）
> 对齐。

## 1. 职责与边界

**做**：按 C6 计划逐窗调 GPU 口型服务（:9003）→ 回贴合成 → 帧哈希自验。
**只写** `09_lip/` 一层（`lip_plan.<lang>.jsonl`、`work/`、`done/*.lip.mp4`、
`lip_report.<lang>.json`）；跨层只读 M5/C2/M9 产物，交换只经冻结契约。

分流规则（C6）：
- 正脸近景（`frontal=true & closeup=true`）且 `lip_eligible` 才入口型；
- 台词最长前 `lip.pro_top_ratio`（默认 10%）升级 `lip-pro`，`--pro-shots`
  显式清单同样升级；
- 其余合格镜头 `lip-fast`；不合格镜头不出计划 = 原画。

## 2. 对外契约

### 2.1 服务接口

- `LipClient` 默认环境变量 `M10_LIP_URL`，回退 `http://127.0.0.1:9003`；
  超时 `600s`、retries=2×2s。
- `POST /v1/lip`（multipart：video mp4 + audio wav + mode/utt_id/shot_id/dry_run）；
  服务响应必须包含 `video_b64` / `n_frames` / `fps` / `sha256`。
- 执行降级链：`lip-pro` → `lip-pro-1.5` → `lip-fast`（models.yaml
  `components.<engine>.fallback` 冻结镜像）；链尾 `lip-fast` 失败 = 硬错误。

### 2.2 帧域与抽段

- 时间口径：C6 `window` 为全片绝对秒；帧号映射 `f0=round(window[0]×fps)`、
  `f1=round(window[1]×fps)`，越界夹取，空窗报错。
- 视频段抽帧：`ffmpeg -ss f0/fps -to f1/fps -c:v libx264 -crf 12 -pix_fmt yuv420p`；
- 配音段：`-ar 16000 -ac 1 -c:a pcm_s16le`。

### 2.3 回贴合成与自验

- 回贴：源视频 raw yuv420p 逐帧解码 → 窗内帧替换为服务产出帧 →
  无损 x264 `qp=0` 重封装 → `09_lip/done/{ep}.{lang}.lip.mp4`；
  源有音轨时 `-c:a copy` 合成。
- 帧哈希自验 `verify_paste_back`：
  ① 帧数一致；② 非口型帧 raw yuv sha256 逐帧全等；③ 口型窗帧全不同；
  ④ 嘴区像素平均绝对差 ≥ `MIN_MOUTH_MAD=3.0`。任一不过 → `ok=False`
  并附逐项计数，不出假交付。

### 2.4 产物命名

- 计划：`09_lip/lip_plan.<lang>.jsonl` + 无后缀副本 `lip_plan.jsonl`；
- 回贴成片：`09_lip/done/{ep}.{lang}.lip.mp4`；
- 报告：`09_lip/lip_report.<lang>.json`。

## 3. CLI（冻结形态）

```
python -m pipeline.m10_lipsync --ep ep01 --lang en [--pro-shots s0007,s0012]
    [--source <mp4>] [--audio <wav>] [--url ...] [--jobs-dir <dir>]
    [--no-verify] [--work-dir <dir>]
```

退出码：0 成功（含自验通过）；1 输入/服务/自验错误；2 用法错误。

## 4. eval

```bash
pytest tests/test_m10_lipsync.py
```

冻结线（§4 M10）：
- C6 分流纯逻辑：正脸近景镜头表硬门槛 + face.frontal/closeup/!overlap +
  台词最长前 10% 与 `--pro-shots` 升 lip-pro、执行降级链落到 lip-fast；
- 回贴合成位精确性：零替换输出逐帧字节等同源、窗外改动/窗内漏替换两类
  缺陷可检出；
- 服务真跑（需 :9003）：4s 正脸近景样本非口型帧字节级不变 + 口型窗帧全异
  + 嘴区变化达标；
- CLI 真实子进程 exit 0。

## 5. 重生成注意事项

- M10 artifact 槽位 = `09_lip/done/{ep}.{lang}.lip.mp4`（M14 跳过判据）；
- `verify_paste_back` 是冻结验收口径，生产路径内置，不单独写测试桩；
- 无口型窗（全部原画）时不出合成成片，报告 `verify.ok=true` 且 note 注明；
- M10 依赖 M9 产物（dubbed wav / 12_out 成片）；编排顺序见 REGENERATE §3。
