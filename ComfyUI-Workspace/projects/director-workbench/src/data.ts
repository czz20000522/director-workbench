export type Kind = 'video' | 'audio' | 'image';
export type Variant = 'A' | 'B';
export type StageBackend = 'ComfyUI 工作流' | '本地脚本' | '混合流程';
export type PipelineStage = {
  id: string;
  order: number;
  title: string;
  purpose: string;
  backend: StageBackend;
  status: '已接入' | '待接入' | '可调试';
  inputs: string[];
  outputs: string[];
  recipe: string;
  generation_mode?: 'text_to_video' | 'image_to_video';
  note: string;
  inputSpecs?: PipelineInput[];
  outputSpecs?: PipelineOutput[];
  execution?: PipelineExecution;
};
export type PipelineValueKind = '音频' | '图片' | '视频' | '文本' | '参数' | '时间轴' | '工作流' | '脚本';
export type PipelineInputControl = 'select' | 'multi-select' | 'boolean' | 'number' | 'text' | 'freeform' | 'asset' | 'time-range';
export type PipelineInput = { id: string; label: string; kind: PipelineValueKind; required: boolean; description: string; example?: string; control?: PipelineInputControl; options?: string[]; min?: number; max?: number; step?: number; flag?: string; flagMode?: 'value' | 'presence'; serialize?: 'value' | 'raw-args'; multiple?: boolean; allowEmpty?: boolean };
export type PipelineOutput = { id: string; label: string; kind: PipelineValueKind; description: string; state: '已存在' | '待生成' | '待接入' };
export type PipelineExecution = { mode: 'comfyui' | 'script' | 'hybrid'; label: string; references: string[]; state: '已验证' | '可调试' | '待接入' };
export type Asset = {
  id: string; kind: Kind; title: string; subtitle: string; start?: number; end?: number;
  ready: boolean; image?: string; lastImage?: string; sources?: Partial<Record<Variant, string>>;
  intent: string; prompt: string; origin: string; specs: string; workflow: string;
};

export const labels: Record<Kind, string> = { video: '视频', audio: '音频', image: '图片' };
export const clock = (seconds: number) => {
  const rounded = Math.round(seconds);
  return `${Math.floor(rounded / 60).toString().padStart(2, '0')}:${(rounded % 60).toString().padStart(2, '0')}`;
};
export const range = (asset: Asset) => asset.start === undefined ? '独立图片' : `${clock(asset.start)} — ${clock(asset.end!)}`;

// Offline fallback only: the live project manifest is always preferred.
export const pipelineStages: PipelineStage[] = [];
export const assets: Asset[] = [
  { id: 'asset-video', kind: 'video', title: '视频资源', subtitle: '等待项目清单', ready: false, intent: '从当前项目导入或登记视频素材。', prompt: '', origin: '离线占位', specs: '', workflow: '' },
  { id: 'asset-audio', kind: 'audio', title: '音频资源', subtitle: '等待项目清单', ready: false, intent: '从当前项目导入或登记音频素材。', prompt: '', origin: '离线占位', specs: '', workflow: '' },
  { id: 'asset-image', kind: 'image', title: '图片资源', subtitle: '等待项目清单', ready: false, intent: '从当前项目导入或登记图片素材。', prompt: '', origin: '离线占位', specs: '', workflow: '' },
];
