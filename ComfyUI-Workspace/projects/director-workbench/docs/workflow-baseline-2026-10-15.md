# 三类生成基线（2026-10-15）

## 结论

工作台继续复用现有任务、候选和片段浏览系统，默认基线如下：

| 入口 | 默认基线 | 本机配置 | 选择理由 |
| --- | --- | --- | --- |
| 文生图 | Qwen Image 2.1 int8 | 6.76GB diffusion + 8.71GB Qwen VL + BF16 VAE | 已有官方 ComfyUI 节点与模型，25 步质量档在 16GB 显存上可以完成；提示词、中文和构图控制优先 |
| 文生视频 | MiniMax H3 纯文本原生音画 | 现有 H3 int8、VAE、文本编码器 | 已通过工作台真实 GPU 小样，约 1MP 原生画布，兼顾清晰度和声音 |
| 图生视频 | MiniMax H3 首帧 / 首尾帧 | 现有 H3 图像条件工作流 | 与已有片段、候选版本和首尾帧输入兼容，工作台已有真实任务链路 |

Qwen Image 2.1 的纯文字图工作流沿用官方模板的核心节点：`UNETLoader`、`CLIPLoader(type=qwen_image)`、`TextEncodeQwenImage21`、`QwenImage21Cache`、`KSampler(euler/simple, 25 steps, CFG 1)`、`VAEDecode` 和 `SaveImage`。提示词增强模型没有纳入首版基线，因此不额外下载约 8.8GB 的增强器，也不会让一次简单生图变成两次推理。

## 社区候选与取舍

- [ComfyUI-WanVideoWrapper](https://github.com/kijai/ComfyUI-WanVideoWrapper)（约 6.7k stars）：包含 Wan 2.2 5B T2V/I2V 示例，适合作为下一阶段统一视频工作流参考。官方 Wan2.2 TI2V-5B 页面建议单卡至少约 24GB，本机 RTX 5070 Ti 只有 16GB；当前缺少完整 5B 权重，因此本轮不下载、不替换已验证的 H3。
- [ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)（约 4.2k stars）：两阶段草稿→空间/时间放大→精修和 4K/8K tiled workflow 很值得借鉴，但 README 要求 32GB+ VRAM 与 100GB+ 模型/缓存，不适合作为本机首个实际基线。
- [ComfyUI-HunyuanVideoWrapper](https://github.com/kijai/ComfyUI-HunyuanVideoWrapper)（约 2.6k stars）：官方 HunyuanVideo 720p/129 帧约需 60GB GPU，544p 也约需 45GB，明确排除本机部署。
- [Comfy-Org/workflow_templates](https://github.com/Comfy-Org/workflow_templates)：官方 Qwen Image 2.1 模板与 H3/Wan/LTX 模板作为节点和输入契约来源。

## 小样证据

在正式 ComfyUI（8188，输入/输出仍由 storage.json 指向 E 盘）上，用同一英文提示词和 seed 42 做了 768×768 隔离样片：

- Qwen Image 2.1：25 步，成功，约 26 秒；输出保存在本机 research 目录。
- Krea 2 Turbo：8 步，成功，约 6 秒；输出保存在本机 research 目录。

Qwen 样片的主体、光照和材质更集中，适合作为需要后续图生视频的首帧质量档；Krea 样片更快、画面更偏风格化，因此保留其构造器作为后续“快速预览”候选，但不再作为工作台文字生图默认基线。上述样片是引擎验证证据，正式作品仍须从工作台提交并登记任务回执。

## 页面简化原则

快速生成页默认只显示三个入口：文生图、文生视频、图生视频。复用工作流模板、固定种子、执行方案和其他高级参数后置到折叠区域；用户仍可从已有片段载入提示词、修改后重新生成，所有结果继续进入原素材库、候选版本和片段时间线。

