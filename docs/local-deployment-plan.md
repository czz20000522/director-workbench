# 导演工作台 · 本地复刻与目录建设方案

> 用途：把这套「导演工作台 + ComfyUI + 本地语音/音乐/视觉引擎」完整复刻到另一台 Windows 电脑。
> 本文只写**事实和可执行步骤**：每个目录做什么、每个文件放哪里、用什么命令建、怎么验。
> 本文以本机 `D:\AI本地拓展坞\MiniMax H3` 为示例根；换机器时把 `$ROOT` 换成你的根即可，结构保持一致。

---

## 1. 三个根，先分清

| 根 | 示例值 | 归谁管 | 删软件会不会丢 |
|---|---|---|---|
| **引擎根** `$ROOT\ComfyUI_windows_portable` | `D:\AI本地拓展坞\MiniMax H3\ComfyUI_windows_portable` | 外部 ComfyUI 便携版 | 重装 ComfyUI 会整体替换 |
| **共享资产根** `$ROOT\ComfyUI-Shared` | `...\ComfyUI-Shared` | 模型、工具、引擎环境 | 跟随引擎根，不跟工作台 |
| **工作台数据根** `$ROOT\director-data` | `...\director-data` | 数据库、配置、运行状态 | **必须保住**，与 ComfyUI 无关 |
| **作品临时区** `$ROOT\临时作品内容` | `...\临时作品内容` | 未归档的作品工作区（可删） | 删作品即清 |
| **作品归档区** `$ROOT\作品` | `...\作品` | 归档成片（不可删） | 永久保留 |
| **素材库** `$ROOT\素材库` | `...\素材库` | 参考图/音色/配乐（不可删） | 永久保留 |

原则：**引擎可以重装，作品与素材不能丢**。所以模型不放进 ComfyUI 本体目录，作品不放进引擎目录，配置与数据库不放进代码目录。

```
$ROOT\
├─ ComfyUI_windows_portable\        # ComfyUI 便携版（引擎）
├─ ComfyUI-Shared\
│  ├─ models\                       # 所有模型权重（见 §5.3）
│  └─ tools\                        # 外部引擎环境（IndexTTS / AuK / …）
├─ director-workbench\              # 工作台源代码 + dist（见 §7）
├─ director-data\                   # 工作台数据根（不能删）
│  ├─ config\                       # storage.json、库配置、云端密钥
│  ├─ runtime\director-workbench\   # SQLite、队列状态、删除计划
│  ├─ reference-library\            # 缩略图、外部方法参考、inbox
│  └─ logs\
├─ 临时作品内容\<作品名>\            # 每部作品的工作区（未归档）
├─ 作品\<日期>_<作品名>\             # 归档成片
└─ 素材库\{参考图,音色,配乐,清晰化,剧本}\  # 归档素材与成稿
```

---

## 2. 硬件与软件要求

- **显卡**：NVIDIA，8GB 显存可跑 H3 量化版（本机 RTX 4060 Ti 8GB），16GB 以上更稳。
- **内存**：32GB 起（文本编码器要卸载到内存）。
- **磁盘**：模型约 55–60GB + 作品/素材另算；建议 D 盘 SSD，路径尽量短。
- **系统**：Windows 10/11 x64；路径**允许中文**，但不要放在同步盘（OneDrive）里。

| 软件 | 版本/位置 | 用途 |
|---|---|---|
| NVIDIA 驱动 | 支持 CUDA 12.6 | 引擎运行 |
| ComfyUI 便携版 | 自带 `python_embeded` | 出图/出片/配乐 |
| Python | 3.13（工作台 `.venv` 用） | 后端 |
| Node.js / npm | 20+ | 构建前端 `dist` |
| FFmpeg | 见 §6.5，无需单独装 | 换音轨、清晰化、抽帧 |
| Ollama（可选） | `qwen2.5vl:7b` | 画面观察 |
| Git（可选） | — | 拉代码 |

---

## 3. 首次建设：建目录

以 PowerShell 为例（管理员不必需，普通用户即可）：

```powershell
$ROOT = 'D:\AI本地拓展坞\MiniMax H3'

# 引擎与共享资产
New-Item -ItemType Directory -Force -Path "$ROOT\ComfyUI-Shared\models" | Out-Null
foreach ($d in 'diffusion_models','text_encoders','vae','loras','upscale_models','checkpoints') {
  New-Item -ItemType Directory -Force -Path "$ROOT\ComfyUI-Shared\models\$d" | Out-Null
}
New-Item -ItemType Directory -Force -Path "$ROOT\ComfyUI-Shared\tools" | Out-Null

# 工作台数据根
foreach ($d in 'config','runtime','reference-library','logs') {
  New-Item -ItemType Directory -Force -Path "$ROOT\director-data\$d" | Out-Null
}

# 作品与素材
New-Item -ItemType Directory -Force -Path "$ROOT\临时作品内容" | Out-Null
New-Item -ItemType Directory -Force -Path "$ROOT\作品" | Out-Null
foreach ($d in '参考图','音色','配乐','清晰化','剧本') {
  New-Item -ItemType Directory -Force -Path "$ROOT\素材库\$d" | Out-Null
}
```

> 这些目录**必须**是绝对路径、不能互相嵌套、不能是符号链接/junction——`storage_layout.py` 会拒绝。

---

## 4. 配置：`storage.json`

路径：`$ROOT\director-data\config\storage.json`（**不进 Git**）。完整模板：

```json
{
  "schema_version": 1,
  "roots": {
    "input": "D:/AI本地拓展坞/MiniMax H3/ComfyUI_windows_portable/ComfyUI/input",
    "output": "D:/AI本地拓展坞/MiniMax H3/ComfyUI_windows_portable/ComfyUI/output",
    "workspaces": "D:/AI本地拓展坞/MiniMax H3/临时作品内容",
    "private": "D:/AI本地拓展坞/MiniMax H3/director-data/private",
    "assets": "D:/AI本地拓展坞/MiniMax H3/素材库",
    "works_archive": "D:/AI本地拓展坞/MiniMax H3/作品"
  },
  "legacy_assets": []
}
```

| 字段 | 含义 |
|---|---|
| `input` / `output` | ComfyUI 的输入/输出目录，生成的候选默认落在这里 |
| `workspaces` | 每部作品的工作区根（`临时作品内容`）；删除作品只清这里 |
| `private` | 单用户模式下不使用，保留占位 |
| `assets` | 素材库根：`素材库/参考图|音色|配乐`，归档写入 |
| `works_archive` | 作品归档根：`作品/<日期>_<作品名>/`，归档成片写入 |
| `legacy_assets` | 旧路径映射：`[{"source": "...", "destination": "..."}]`，迁移时用（§11） |

校验规则（写错会直接启动失败）：
1. `schema_version` 必须是 `1`；`roots` 必须含 `input/output/workspaces/private`，`assets/works_archive` 可省略（省略时回退默认）。
2. 所有根：绝对、已规范化、不含 `..`、不是链接/junction。
3. 任意两个根不能相同或互相嵌套。

---

## 5. ComfyUI 与模型

### 5.1 放置与启动

- 便携版解压到 `$ROOT\ComfyUI_windows_portable`（内有 `python_embeded`、`ComfyUI`）。
- 工作台按需启动它：`python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --offline --reserve-vram 1.0`，端口 **8188**。
- 只打开工作台、不点生成时，ComfyUI 保持关闭；第一次提交任务时后端自动拉起（冷启动约 13 秒）。

### 5.2 模型映射：`extra_model_paths.yaml`

路径：`$ROOT\ComfyUI_windows_portable\ComfyUI\extra_model_paths.yaml`。作用：模型放在 `ComfyUI-Shared`，升级/重装 ComfyUI 不会动到 30GB 权重。

```yaml
minimax_h3:
    base_path: D:/AI本地拓展坞/MiniMax H3/ComfyUI-Shared/models
    diffusion_models: diffusion_models
    unet: diffusion_models
    text_encoders: text_encoders
    clip: text_encoders
    vae: vae
    loras: loras
    upscale_models: upscale_models
    checkpoints: checkpoints
```

### 5.3 模型清单（本机实测体积）

| 文件 | 放置目录 | 体积 | 用途 |
|---|---|---|---|
| `minimax_h3_fl2va_pruned-UD-Q3_K_XL.gguf` | `models/diffusion_models` | 9.0 GB | H3 首尾帧/音频驱动视频主模型（GGUF 量化） |
| `qwen-image-2.1-UC-Q4_K_M.gguf` | `models/diffusion_models` | 4.3 GB | Qwen-Image 2.1 文生图 |
| `Qwen-Image-2.1-Turbo-AD-Q4_K.gguf` | `models/diffusion_models` | 4.0 GB | Qwen-Image 2.1 Turbo 出图 |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `models/text_encoders` | 15 GB | H3 文本编码器 |
| `qwen3vl_8b_int8_convrot.safetensors` | `models/text_encoders` | 8.8 GB | Qwen-Image 文本编码器 |
| `minimax_h3_video_vae_fp16.safetensors` | `models/vae` | 4.9 GB | H3 视频 VAE |
| `minimax_h3_audio_vae_fp32.safetensors` | `models/vae` | 578 MB | H3 原生音频 VAE |
| `qwen_image_2.1_vae_bf16.safetensors` | `models/vae` | 645 MB | Qwen-Image VAE |
| `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | `models/loras` | 1.9 GB | H3 8 步 Turbo LoRA |
| `qwen-image-2.1-uncensored-lora.safetensors` | `models/loras` | 33 MB | Qwen-Image 分区 LoRA（按需） |
| `RealESRGAN_x2plus.pth` | `models/upscale_models` | 64 MB | 2× 清晰化 |
| `ace_step_1.5_turbo_aio.safetensors` | `models/checkpoints` | 9.4 GB | ACE-Step 1.5 配乐（AIO 版） |

> 自定义节点：ComfyUI 需带 **GGUF 加载节点**（ComfyUI-GGUF）；ACE-Step 用 ComfyUI 原生 `TextEncodeAceStepAudio1.5` 等节点。

---

## 6. 本地引擎

### 6.1 IndexTTS-2.5（配音 / 锚点配音 / 对白镜头的克隆引擎）

- 代码：`$ROOT\ComfyUI-Shared\tools\index-tts`（自带 `.venv`，Python 3.11 + torch cu128）。
- 权重：`$ROOT\ComfyUI-Shared\models\IndexTTS-2.5`（含 `hf_cache\w2v-bert-2.0`、`campplus_cn_common.bin`、`bigvgan\`、`qwen0.6bemo4-merge`）。
- **两处非 ASCII 路径补丁**（目录名含中文时必须打，`uv sync` 会还原）：
  1. `site-packages\_editable_impl_indextts.pth` 在 GBK 控制台会让 Python 初始化崩溃 → 改名成 `.disabled`。
  2. `site-packages\wetext\constants.py` 的 `load_fst()` 先把 `.fst` 拷到 ASCII 临时目录再加载（kaldifst 打不开中文路径）。
- 没有随机编号（种子），同样参数不保证复现。

### 6.2 VoxCPM2（默认配音引擎，可文字造音色）

- 目录：`D:\AI本地拓展坞\VoxCPM`（默认取 `$ROOT` 的同级目录 `..\VoxCPM`；可用环境变量 `DIRECTOR_VOXCPM_ROOT` 覆盖）。
- 自带 `.venv`（torch 2.14+cu126）与 `pretrained_models\VoxCPM2`（约 4.7GB：`model.safetensors`、`config.json`、`audiovae.pth`）。
- 输出 48kHz；有随机编号；情绪只支持文字描述。

### 6.3 配乐（ACE-Step 1.5）

- 只需上面的 `ace_step_1.5_turbo_aio.safetensors` 放进 `models/checkpoints`，并在 `extra_model_paths.yaml` 里映射 `checkpoints`（§5.2 已含）。

### 6.4 画面观察（可选）

- 安装 Ollama，拉取 `qwen2.5vl:7b`；服务端口 **11434**。
- 工作台所有回环请求都禁用系统代理（`trust_env=False`），否则 127.0.0.1 会被代理成 502。

### 6.5 FFmpeg（清晰化 / 换音轨 / 抽帧）

解析顺序：`DIRECTOR_FFMPEG` → `ComfyUI-Shared\tools\index-tts\ffmpeg.exe` → PATH → Python 包 `imageio-ffmpeg` 自带版本。本机走最后一种，无需单独安装。

---

## 7. 工作台本体

```powershell
$ROOT = 'D:\AI本地拓展坞\MiniMax H3'
cd "$ROOT\director-workbench"

# 后端环境（Python 3.13）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # 或 uv pip install -r requirements.txt

# 前端构建（产物 dist/ 会被后端直接服务）
npm install
npm run build
```

启动入口（**不要**手工 `uvicorn`，否则数据根会退回默认目录）：

```powershell
# 单用户模式，仅本机
powershell -ExecutionPolicy Bypass -File .\start_local.ps1

# 幂等启动 + 可选开浏览器；ComfyUI 仍按需启动
powershell -ExecutionPolicy Bypass -File .\start_services.ps1 [-NoBrowser] [-WithComfy]
```

`start_local.ps1` 会导出：
`DIRECTOR_DATA_ROOT=$ROOT\director-data`、`DIRECTOR_COMFY_ROOT=$ROOT`、`DIRECTOR_COMFY_URL=http://127.0.0.1:8188`、`PYTHONUTF8=1`，然后监听 **4100**。

可选环境变量：`DIRECTOR_VOXCPM_ROOT`、`DIRECTOR_FFMPEG`、`DIRECTOR_LLAMA_ROOT`、`DIRECTOR_TASK_EXECUTION_TIMEOUT_SECONDS`。

---

## 8. 数据规则（复刻时必须一致）

### 8.1 术语

界面上叫**随机编号**（内部字段仍是 `seed`）。同一个随机编号 + 同样参数＝可复现；换一个就是另一次抽签。

### 8.2 临时 vs 归档

- **临时作品内容**：每部作品的工作区；删作品 = 工作区、任务行、快照、以及登记在 ComfyUI 输出目录里的**本作产物**一并清除。
- **作品 / 素材库**：归档区，删除作品永远不碰（`delete_files` 有硬保护）。
- 归档是**移动**，不是复制；命名统一 `日期_标题`，同名自动 `-2`。

| 归档项 | 去处 | 命名 |
|---|---|---|
| 参考图 | `素材库\参考图\` | `2026-10-25_标题.png` |
| 音色样本 | `素材库\音色\` | `2026-10-25_标题.wav` |
| 配乐候选 | `素材库\配乐\` | `2026-10-25_标题.mp3` |
| 成片/视频 | `作品\<日期>_<作品名>\` | `2026-10-25_<标题>.mp4` |
| 清晰化成品 | `素材库\清晰化\` | `2026-10-25_<原标题> · 清晰化.png/mp4`（勾「覆盖原素材」才删原件） |
| 剧本（归档后） | `素材库\剧本\` | `2026-10-25_<标题>.md`（草稿只在本机，不进任何库） |

删除预览里的归档清单一律**默认不勾**；不勾的随删除清除。

### 8.3 配置与库文件（都在数据根，不进 Git）

| 文件 | 内容 |
|---|---|
| `config/storage.json` | 存储根（§4） |
| `config/reference-library.json` | 参考图库/提示词库索引（文档、图片目录） |
| `config/preset-library.json` | 音色库/配乐库卡片（内置卡在代码里，同名保存会覆盖） |
| `config/creative-settings.json` | 命名设定库（配乐设定/配音设定） |
| `config/vision-provider.json` | 云端视觉来源（DeepSeek key，可空） |
| `runtime/director-workbench/` | SQLite 任务库、队列状态、删除计划 |

### 8.4 两个「只在你点按钮时才落盘」的库

- **清晰化库**：2× 结果点「存入清晰化库」才复制进 `素材库\清晰化`；勾「覆盖原素材」才把该素材登记改指向清晰化文件并删除原件（只删工作区/ComfyUI 输入输出里的文件）。首页右栏有只读列表，作品里在 后期 → 清晰化 操作。
- **剧本库**：首页左栏入口；草稿存在 `director-data/config/script-library.json`，**不进提示词库也不进素材库**；点「归档到剧本库」才写成 `素材库\剧本\日期_标题.md`。支持导入 .md/.txt 与导出 .md。

---

## 9. 验收入口（启动后逐条 curl）

```powershell
$B = 'http://127.0.0.1:4100'
Invoke-RestMethod "$B/api/status"                 # 数据根、引擎状态
Invoke-RestMethod "$B/api/keyframe-capability"    # 文生图节点/模型
Invoke-RestMethod "$B/api/speech-capability"      # 三个声音引擎（voxcpm-2 默认）
Invoke-RestMethod "$B/api/dubbing-capability"     # 锚点配音（引擎/音色来源/情绪能力）
Invoke-RestMethod "$B/api/audio-swap-capability"  # 换音轨（FFmpeg）
Invoke-RestMethod "$B/api/music-capability"       # ACE-Step
Invoke-RestMethod "$B/api/vlm-capability"         # 画面观察（本机/云端来源）
Invoke-RestMethod "$B/api/preset-library"         # 音色库/配乐库
```

预期：`available` 为 `true`；缺文件时 `missing` 会逐个点名，**不会偷偷换引擎**。

---

## 10. 测试与回归

```powershell
cd "$ROOT\director-workbench"
powershell -ExecutionPolicy Bypass -File .\run_tests.ps1 -Suite core      # 后端核心
powershell -ExecutionPolicy Bypass -File .\run_tests.ps1 -Suite browser   # 浏览器驱动（需 Edge + node_modules）
powershell -ExecutionPolicy Bypass -File .\run_tests.ps1 -Check           # 环境自检
```

已知「环境性」失败（不是代码坏了）：缺 AuK 环境、私有化/safe-delete 技能、`src/App.tsx` 不存在、`contracts/h3-multiref-api.json` 不存在、依赖 ffmpeg 的少数用例。核心组通过不等于全量通过。

---

## 11. 迁移已有数据到新目录

1. 停后端。
2. 移动目录（同盘用 `Move-Item`，跨盘先复制再删）。
3. 改 `storage.json` 的对应根。
4. **只给旧工作区**加 `legacy_assets` 映射（旧绝对引用仍能解析）：
   ```json
   "legacy_assets": [{ "source": "D:/.../director-data/workspaces", "destination": "D:/.../临时作品内容" }]
   ```
   不要把归档区（`作品/`、`素材库/`）映射走。
5. 启动后检查：作品列表、单步任务、素材缩略图、`/media-file?path=workspaces/...` 返回 200。

---

## 12. 常见问题

| 现象 | 原因/处理 |
|---|---|
| 页面能开，但删除预览 `file_count: 0` | 没用 `start_local.ps1` 启动，存储根退回了默认数据根 |
| 中文路径下 Python 启动即崩 / kaldifst 报错 | 没打 §6.1 的两处补丁 |
| 控制台输出乱码或 `.ps1` 解析失败 | PowerShell 5.1 把无 BOM UTF-8 当 GBK；用 `-EncodedCommand` 或加 `PYTHONUTF8=1` |
| 本机服务连 127.0.0.1 反而 502 | 系统代理拦截回环；客户端要 `trust_env=False` / `--noproxy "*"` |
| 4100/8188/11434 被占用 | 先停旧进程；后台任务被杀时可能残留，`netstat -ano` 找 PID |
| 生成时显存不足 | 先让其它模型任务结束；H3 用 0.4MP/8 步档位 |
| 长路径报 AccessDenied（测试） | 保持 `.t` 运行时目录、别用系统 TEMP 深路径 |

---

## 13. 复刻清单（照着打勾）

- [ ] 建 §3 的目录
- [ ] 写 §4 的 `storage.json` 并通过启动校验
- [ ] 解压 ComfyUI 便携版；写 §5.2 的 `extra_model_paths.yaml`
- [ ] 按 §5.3 放齐模型；装 GGUF 节点
- [ ] 装 IndexTTS 并打两处补丁；放 VoxCPM2；放 ACE-Step checkpoint
- [ ] （可选）Ollama + `qwen2.5vl:7b`
- [ ] 建 `.venv`、装 requirements、`npm run build`
- [ ] `start_services.ps1` 启动；跑 §9 的验收 curl
- [ ] 跑 `run_tests.ps1 -Suite core`，确认只有已知环境性失败
- [ ] 真生成一次：文生图 → 文生视频 → 配音 → 配乐 → 换音轨 → 归档 → 删除作品（确认归档保留）
