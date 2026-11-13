import { useEffect, useMemo, useRef, useState } from 'react';
import { Bookmark, ImageIcon, LoaderCircle, Paintbrush, Scaling, Sparkles, Wand2, X } from 'lucide-react';
import { mediaUrl, request } from './api/client';
import InpaintDialog from './InpaintDialog';
import LiquifyDialog from './LiquifyDialog';
import PromptWriter, { type PromptApply } from './PromptWriter';
import ReferenceSlotField, { invalidateMaterialIndex, lookupMaterial, type ReferenceRole, type SlotValue } from './ReferenceSlotField';
import { notifyLibraryChanged } from './ReferenceLibrary';
import TranslateButton from './TranslateButton';
import StyleBlockPicker, { readStyleBlock } from './StyleBlockPicker';
import ProgressBar, { type TaskProgress } from './TaskProgress';

/** 图生视频的「首帧工作台」。
 *
 * 一段镜头能不能用，八成取决于首帧。所以这里不把「上传一张图」当唯一入口：
 * 先用素材库的 脸型/造型/服装/姿势 拼出一张，看着不对就带着这张继续改，
 * 改到满意再交给下面的图生视频。改图的做法是把当前图塞回「底图」槽——
 * 提示词里它会被写成「参考图1是当前画面，以它为准做修改」，模型就不会重新编一个人。
 */

type Profile = 'general' | 'adult-realism' | 'adult-anime';
type Aspect = 'portrait' | 'portrait-hd' | 'landscape' | 'square';

const ROLES: ReferenceRole[] = ['脸型', '造型', '服装', '姿势'];
/** 第五个槽：一张整图，长相/造型/服装/姿势都在里面，不用再选上面四类。 */
const OWN_ROLE: ReferenceRole = '自有素材';
const SLOT_ROLES: ReferenceRole[] = [...ROLES, OWN_ROLE];
const ROLE_HINTS: Record<string, string> = {
  '脸型': '只决定长相：默认只列「脸型·发型」库里的正面脸图',
  '造型': '只决定发型：默认只列同一库里的「造型参考·」那批；也可以不选图，直接写「丸子头」',
  '服装': '决定穿什么：默认只列「服饰·写实」；不想穿就点「不穿衣服」，或自己写一句',
  '姿势': '会走 ControlNet 结构控制：深度图锁身体轮廓（更准），线稿锁外轮廓（关节圆圈可能被画进衣服）',
  '自有素材': '一张整图就够：任意分类都能选，也能上传本地图片。选了它就不用再选上面四类；画幅尽量和方案一致，否则开始画面会被拉伸。',
};
// note 只放比例，pro 说清这档的实际取舍（都来自本机实测，不是估算）。
const ASPECTS: { value: Aspect; label: string; width: number; height: number; note: string; pro: string }[] = [
  { value: 'portrait', label: '竖屏', width: 768, height: 1344, note: '9:16',
    pro: '最快的一档（约 15 秒），全身构图稳，适合口播、人物展示和批量出图' },
  { value: 'portrait-hd', label: '竖屏高清', width: 1024, height: 1536, note: '2:3',
    pro: '细节最足（五官、发丝、织物纹理明显更清楚），代价是多等约 6 成时间（约 23 秒）' },
  { value: 'landscape', label: '横屏', width: 1344, height: 768, note: '16:9',
    pro: '宽画幅，双人对话、场景交代和影院比例都用它；人物在画面里会更小' },
  { value: 'square', label: '方图', width: 1024, height: 1024, note: '1:1',
    pro: '头像、封面和社媒方图；人物居中最好看，上下留白少' },
];
/** 文字优先：提示词里写到同类属性，就会盖掉对应参考图的观感（实测：写「酒红缎面
 *  吊带长裙」时，服装参考图里的象牙白针织裙完全不出现）。
 *  这里只做提醒，不改写、不拦提交——是否覆盖由用户自己决定。 */
const OVERRIDE_RULES: { role: ReferenceRole; label: string; words: string[] }[] = [
  { role: '服装', label: '服装', words: [
    '裙', '裤', '衬衫', '衬衣', '上衣', '外套', '西装', '卫衣', '吊带', '开衫', '大衣', '风衣',
    '旗袍', '泳装', '内衣', '毛衣', '背心', '棉麻', '针织', '缎面', '雪纺', '牛仔', '皮革', '真丝', '丝质',
  ] },
  { role: '造型', label: '发型', words: [
    '长发', '短发', '卷发', '直发', '马尾', '丸子头', '寸头', '刘海', '盘发', '双马尾', '波波头', '大波浪',
  ] },
];

const PROFILES: { value: Profile; label: string; note: string }[] = [
  { value: 'general', label: '常规', note: '不挂 LoRA' },
  { value: 'adult-realism', label: '写实·成人向', note: '真人化 + 去审查' },
  { value: 'adult-anime', label: '二次元·成人向', note: '动漫一致性 + 去审查' },
];
const DEFAULT_PROMPT = '全身，浅灰纯色背景，影棚柔光，写实摄影，人物自然站立看向镜头。';
const MAX_HISTORY = 8;
const HISTORY_KEY = 'director-workbench:first-frame-history:v1';

type FrameHistoryEntry = {
  id: string; path: string; prompt: string; seed: number;
  profile: Profile; aspect: Aspect; createdAt: number;
};

const PROFILE_VALUES: Profile[] = PROFILES.map(item => item.value);
const ASPECT_VALUES: Aspect[] = ASPECTS.map(item => item.value);

/** localStorage 里的东西可能来自旧版本，也可能被手改过：逐字段收口，坏的整条丢掉。 */
function normalizeEntry(item: unknown): FrameHistoryEntry | null {
  if (!item || typeof item !== 'object') return null;
  const raw = item as Record<string, unknown>;
  const path = typeof raw.path === 'string' ? raw.path.trim() : '';
  if (!path) return null;
  const seed = typeof raw.seed === 'number' && Number.isFinite(raw.seed) && raw.seed > 0 ? Math.floor(raw.seed) : 0;
  return {
    id: typeof raw.id === 'string' && raw.id ? raw.id : `${path}#${seed}`,
    path, seed,
    profile: PROFILE_VALUES.includes(raw.profile as Profile) ? raw.profile as Profile : 'general',
    aspect: ASPECT_VALUES.includes(raw.aspect as Aspect) ? raw.aspect as Aspect : 'portrait',
    prompt: typeof raw.prompt === 'string' ? raw.prompt : '',
    createdAt: typeof raw.createdAt === 'number' ? raw.createdAt : 0,
  };
}

function loadHistory(): FrameHistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed.map(normalizeEntry).filter((item): item is FrameHistoryEntry => item !== null)
      .slice(0, MAX_HISTORY);
  } catch { return []; }
}

type Task = { id: string; status: string; error?: string; progress?: TaskProgress; result?: unknown };

let sequence = 0;
function nextSeed(): number {
  if (sequence === 0) sequence = Math.floor(Math.random() * 1_000_000);
  sequence = (sequence + 7919) % 4294967295;
  return sequence;
}

function readOutput(result: unknown): string {
  let value = result;
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return ''; }
  }
  if (!value || typeof value !== 'object') return '';
  const record = value as Record<string, unknown>;
  for (const key of ['output', 'native_output', 'published_native_output']) {
    if (typeof record[key] === 'string' && record[key]) return record[key] as string;
  }
  return '';
}

const statusLabels: Record<string, string> = {
  scheduler_waiting: '等待调度', batch_waiting: '批次等待', preparing: '准备中', submitting: '提交中',
  queued: '排队中', running: '生成中', succeeded: '已完成', failed: '生成失败', stopped: '已停止',
  needs_reconcile: '需要核对结果',
};

export default function FirstFrameStudio({ projectId, disabled, defaultAspect, currentFrame, onChange, onPromptChange, restoreFrom }: {
  projectId: string; disabled?: boolean;
  /** 所选制作方案的画幅。首帧和方案对不上，视频会先裁一刀，所以这里跟着方案走。 */
  defaultAspect?: '16:9' | '9:16' | '1:1';
  /** 外层当前的开始画面：有图时选「自有素材」不再自动顶掉它。 */
  currentFrame?: string;
  onChange: (path: string) => void;
  /** 图生视频里这个框就是视频提示词：把内容同步给外层，外层不再重复渲染一个输入框。 */
  onPromptChange?: (text: string) => void;
  /** 「编辑重生成」从首页带回来的那一版：提示词、画幅、种子和参考槽一起还原。 */
  restoreFrom?: {
    prompt?: string;
    references?: { role: string; material_id?: string; label?: string; path?: string }[];
    seed?: number;
    aspect?: '16:9' | '9:16' | '1:1';
  };
}) {
  const [slots, setSlots] = useState<SlotValue[]>([]);
  const [base, setBase] = useState('');
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);
  // 用户自己动过提示词之后，选素材就不再清空它——只有预填的那句会被清掉。
  const [promptTouched, setPromptTouched] = useState(false);
  // 图生视频把这个框当视频提示词用：初始预填、选素材清空、恢复历史都要同步给外层。
  useEffect(() => { onPromptChange?.(prompt); }, [prompt, onPromptChange]);
  const [profile, setProfile] = useState<Profile>('general');
  const [aspect, setAspect] = useState<Aspect>(defaultAspect === '16:9' ? 'landscape' : defaultAspect === '1:1' ? 'square' : 'portrait');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [progress, setProgress] = useState<TaskProgress | null>(null);
  const [frame, setFrame] = useState('');
  const [history, setHistory] = useState<FrameHistoryEntry[]>(loadHistory);
  // 刷新后内部 frame 是空的，但历史还在。默认选中最新那张，否则并排区一片空白，
  // 用户会以为图丢了。只补内部状态，不自动把旧图同步成开始画面。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (frame || currentFrame || !history.length) return;
    setFrame(history[0].path);
  }, []);
  // 首页「编辑重生成」带回来的那一版：提示词、画幅、种子、参考槽一起还原，
  // 否则点进来是个空表单，还得凭记忆重选一遍素材。
  useEffect(() => {
    if (!restoreFrom) return;
    if (restoreFrom.prompt) { setPrompt(restoreFrom.prompt); setPromptTouched(true); }
    if (typeof restoreFrom.seed === 'number') setSeed(restoreFrom.seed);
    if (restoreFrom.aspect) {
      setAspect(restoreFrom.aspect === '16:9' ? 'landscape' : restoreFrom.aspect === '1:1' ? 'square' : 'portrait');
    }
    const refs = restoreFrom.references || [];
    if (!refs.length) return;
    void (async () => {
      const next: SlotValue[] = [];
      for (const item of refs) {
        const role = item.role as ReferenceRole;
        if (!role) continue;
        if (item.material_id) {
          const found = await lookupMaterial(item.material_id);
          if (found) { next.push({ role, material_id: found.id, title: found.title, thumb: found.thumb }); continue; }
        }
        // 底图模式没有 material_id，只有当时冻结的副本：把它放回底图位。
        if (item.path) { setBase(item.path); continue; }
        // 素材已经被撤掉：退成文字槽，别把「丸子头」这类设定弄丢。
        if (item.label) next.push({ role, material_id: '', custom: item.label, title: item.label, thumb: '' });
      }
      if (next.length) setSlots(next);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // 切回历史时锁住当年那个种子，才能真的复现同一张；null 表示每次随机。
  const [seed, setSeed] = useState<number | null>(null);
  const [inpaintOpen, setInpaintOpen] = useState(false);
  // 液化塑形：先把脸型/身形定下来，再决定要不要补质感（精修）。
  const [liquifyOpen, setLiquifyOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveTitle, setSaveTitle] = useState('');
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveNote, setSaveNote] = useState<{ text: string; error?: boolean } | null>(null);
  // 推入清晰化：提交 → 排队 → 处理 → 自动归档进清晰化库，一条龙做完。
  const [enhanceBusy, setEnhanceBusy] = useState(false);
  const [enhanceNote, setEnhanceNote] = useState<{ text: string; error?: boolean } | null>(null);
  const [error, setError] = useState('');
  // 风格锁：只拼进送模型的那一份，输入框里的提示词保持原样。
  const [styleBlock, setStyleBlock] = useState(readStyleBlock);
  // 当前提示词是从收藏里哪条填进来的：生成成功后要把产物回写到它身上。
  const [recipeId, setRecipeId] = useState('');

  useEffect(() => {
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch { /* 隐私模式下写不进去，不影响本次会话 */ }
  }, [history]);

  useEffect(() => {
    if (!defaultAspect) return;
    setAspect(defaultAspect === '16:9' ? 'landscape' : defaultAspect === '1:1' ? 'square' : 'portrait');
  }, [defaultAspect]);

  const size = useMemo(() => ASPECTS.find(item => item.value === aspect) || ASPECTS[0], [aspect]);
  // 「自有素材」是整图路径：它会顶掉上面四类槽，而不是和它们叠加。
  // 四个槽仍然留在状态里（不清空），清掉自有素材就自动恢复，免得手滑丢配置。
  const ownSlot = slots.find(slot => slot.role === OWN_ROLE);
  const activeSlots = ownSlot ? [ownSlot] : slots.filter(slot => slot.role !== OWN_ROLE);
  const materialSlots = activeSlots.filter(slot => slot.material_id);
  // 带姿势时 Union 参考编码只有 3 个位置。现在的槽位结构刚好卡在上限，
  // 这里只把余量写出来，避免以后加槽位时又变成"填了但没进模型"。
  const semanticCount = materialSlots.filter(slot => slot.role !== '姿势').length;
  // 当前提示词里有没有会盖掉参考图的属性词。只提示，不拦。
  const overrideHints = (prompt || '')
    ? OVERRIDE_RULES.filter(rule =>
        rule.words.some(word => prompt.includes(word)) &&
        materialSlots.some(slot => slot.role === rule.role))
    : [];
  const textSlots = activeSlots.filter(slot => !slot.material_id && slot.custom);
  const filled = activeSlots;
  // 姿势槽只是"再给模型看一张图"，不是骨架约束；没有构图文字时它常被脸型图带成头肩特写。
  const poseSlot = activeSlots.find(slot => slot.role === '姿势' && (slot.material_id || slot.custom));
  // 上一次被「自有素材」自动设成开始画面的那一条：换一张自有素材时要跟着换，
  // 但用户自己生成/上传的开始画面不许被顶掉。
  const autoAdopted = useRef<{ materialId: string; path: string } | null>(null);

  // 只选了一张素材时，直接拿它当首帧，不必先跑一次首帧生成：
  // 「选个脸型 + 写动作」就能出片，服装/姿势交给提示词。
  const useSlotAsFrame = async (slot: SlotValue, why = '') => {
    if (!projectId || !slot.material_id) return;
    setBusy(true); setError(''); setStatus('正在登记素材…');
    try {
      const staged = await request<{ path: string }>(
        `/api/reference-library/materials/${encodeURIComponent(slot.material_id)}/stage`,
        { json: { project_id: projectId } },
      );
      setFrame(staged.path);
      onChange(staged.path);
      autoAdopted.current = { materialId: slot.material_id, path: staged.path };
      setStatus(why || `已直接用「${slot.title}」作首帧，可以直接生成视频。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登记素材失败');
      setStatus('');
    } finally { setBusy(false); }
  };

  /** 自有素材是「一张就够」的路径：还没有开始画面时自动把它设成开始画面，
   *  这样选一张、写一句就能直接出视频，不用再点一次「用作首帧」。 */
  const shouldAdoptOwn = (next: SlotValue) => {
    if (!next.material_id || base) return false;
    const current = currentFrame || frame;
    if (!current) return true;
    return autoAdopted.current?.path === current;
  };

  const setSlot = (role: ReferenceRole, next: SlotValue | undefined) => {
    setSlots(current => [...current.filter(slot => slot.role !== role), ...(next ? [next] : [])]);
    // 选好素材后，画面描述应当空着让你写镜头；预填的整段话会和新选的脸/服装打架。
    if (next && !promptTouched) setPrompt('');
    if (role === OWN_ROLE) {
      if (next && shouldAdoptOwn(next)) {
        void useSlotAsFrame(next, `已把「${next.title}」设为开始画面：写清动作和镜头就能生成视频。`);
        return;
      }
      // 清掉自有素材时，如果开始画面正是它自动设的，一起撤掉：
      // 否则换回脸型路线后，视频还会拿这张旧图当第一帧，很难发现。
      const adopted = autoAdopted.current;
      if (!next && adopted && (currentFrame === adopted.path || frame === adopted.path)) {
        autoAdopted.current = null;
        setFrame('');
        onChange('');
        setStatus('已清掉自有素材，开始画面也一起撤掉了。');
      }
    }
  };

  /** 从词库「填入」一条配方：除了文本，把画质档/画幅/种子/素材槽也还原。
   *  只认工作台里真有的档位，库里存着旧值也不会把界面带到一个不存在的选项上。 */
  const applyRecipe = async (extra: PromptApply) => {
    if (extra.profile && PROFILES.some(item => item.value === extra.profile)) setProfile(extra.profile as Profile);
    if (extra.aspect && ASPECTS.some(item => item.value === extra.aspect)) setAspect(extra.aspect as Aspect);
    if (extra.seed !== undefined) setSeed(extra.seed ?? null);
    const wanted = (extra.materials || []).filter(entry => SLOT_ROLES.some(role => role === entry.role));
    if (!wanted.length) return;
    const restored = new Map<ReferenceRole, SlotValue>();
    for (const entry of wanted) {
      const role = entry.role as ReferenceRole;
      if (entry.material_id) {
        const found = await lookupMaterial(entry.material_id);
        if (found) {
          restored.set(role, { role, material_id: found.id, title: found.title, thumb: found.thumb });
          continue;
        }
      }
      // 素材已经从库里撤掉：退成文字槽，至少别把「丸子头」这类设定弄丢。
      if (entry.label) restored.set(role, { role, material_id: '', custom: entry.label, title: entry.label, thumb: '' });
    }
    if (!restored.size) return;
    setSlots(current => [...current.filter(slot => !restored.has(slot.role)), ...restored.values()]);
    setStatus(`已按配方回填 ${restored.size} 个素材槽`);
  };

  const useAsBase = (path: string) => {
    setBase(path);
    setPrompt('');
    setError('');
    setStatus('底图已锁定：写下要改的地方，再生成一次。');
  };

  /** 点历史不只换图：提示词、分区、画幅、种子一起回到那一版，否则「切回去」只是看，复现不了。 */
  /** 只把这张设成当前首帧，不动你正在写的提示词——并排比较时最常用。 */
  const pickFrame = (entry: FrameHistoryEntry) => {
    setFrame(entry.path);
    onChange(entry.path);
    setError('');
    setStatus('已把这一张设为开始画面；提示词保持你现在的写法。');
  };

  const restore = (entry: FrameHistoryEntry) => {
    setFrame(entry.path);
    onChange(entry.path);
    setError('');
    if (!entry.seed) {
      // 局部重绘结果没有文生图种子，它的 prompt 是「改什么」而不是整张画面的描述，
      // 套回去会把你现在写的画面描述盖掉，所以只切图。
      setStatus('已切回这一版；它没有文生图种子，提示词保持你现在的写法。');
      return;
    }
    setPrompt(entry.prompt);
    setPromptTouched(Boolean(entry.prompt));
    setProfile(entry.profile);
    setAspect(entry.aspect);
    setSeed(entry.seed);
    setStatus(`已切回这一版：提示词、分区、画幅、种子 ${entry.seed} 都还原了。`);
  };

  /** 存进素材库的「首帧图参考」：下次在 图生视频 的开始画面里可以直接选它。 */
  const saveToLibrary = async () => {
    if (!frame) return;
    setSaveBusy(true); setSaveNote(null);
    try {
      const data = await request<{ material: { title: string; category: string } }>('/api/reference-library/first-frames', {
        json: { path: frame, title: saveTitle.trim() || defaultTitle() },
      });
      invalidateMaterialIndex();
      notifyLibraryChanged();
      setSaveOpen(false);
      setSaveNote({ text: `已存入素材库「${data.material.category}」：${data.material.title}` });
      setStatus('这张首帧已存进素材库，下次可以直接选作开始画面。');
    } catch (reason) {
      setSaveNote({ text: reason instanceof Error ? reason.message : '存入素材库失败', error: true });
    } finally { setSaveBusy(false); }
  };

  const defaultTitle = () => {
    const head = prompt.trim().replace(/\s+/g, ' ').slice(0, 16);
    return head || `首帧 ${new Date().toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}`;
  };

  /** 推入清晰化：对当前首帧直接提交 2× 超分任务，跑完自动归档进清晰化库。
   *
   * 引擎是全局队列，这里只管提交和等结果；排队多久取决于前面还有几条任务。
   */
  const pushToEnhance = async () => {
    if (!frame || !projectId || enhanceBusy) return;
    setEnhanceBusy(true); setEnhanceNote(null);
    const statusLabelFor: Record<string, string> = {
      scheduler_waiting: '等待调度', queued: '引擎排队中', running: '正在处理', preparing: '准备中',
    };
    try {
      const task = await request<Task>(`/api/projects/${projectId}/enhancement-tasks`, {
        json: { source: frame, idempotency_key: `enhance-frame-${crypto.randomUUID()}` },
      });
      let done = false;
      for (let attempt = 0; attempt < 200 && !done; attempt += 1) {
        const current = await request<Task>(`/api/tasks/${encodeURIComponent(task.id)}`);
        setStatus(statusLabelFor[current.status] || `清晰化：${current.status}`);
        if (['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(current.status)) {
          if (current.status !== 'succeeded') throw new Error(current.error || `清晰化${current.status === 'stopped' ? '已停止' : '未完成'}`);
          done = true;
        } else {
          await new Promise(resolve => setTimeout(resolve, 2000));
        }
      }
      if (!done) throw new Error('清晰化超时未完成，可到「清晰化」工具里查看这条任务');
      const archived = await request<{ path: string }>(`/api/projects/${projectId}/enhancements/${encodeURIComponent(task.id)}/archive`, {
        json: {},
      });
      const name = String(archived.path).split(/[\\/]/).pop();
      setEnhanceNote({ text: `已推入清晰化库：${name}（原图保留，库里的文件是 2× 版本）` });
      setStatus('清晰化完成，成品已进清晰化库。');
    } catch (reason) {
      setEnhanceNote({ text: reason instanceof Error ? reason.message : '推入清晰化未完成', error: true });
      setStatus('');
    } finally { setEnhanceBusy(false); }
  };

  const submit = async () => {
    if (!projectId) { setError('单步项目还没准备好'); return; }
    if (!prompt.trim() && !textSlots.length) { setError('请写一句画面描述，或至少写清要改什么'); return; }
    if (!materialSlots.length && !base && !textSlots.length) { setError('至少选一个素材，或先有一张底图'); return; }
    setBusy(true); setError(''); setProgress(null); setStatus('提交首帧任务…');
    const usedSeed = seed ?? nextSeed();
    // 不选图的槽（「不穿衣服」「丸子头」）没有参考图，只能写进提示词里；
    // 有参考图或底图时走参考图模式，否则就是一次纯文字生图。
    const customPrefix = textSlots.map(slot => `${slot.role}：${slot.custom}`).join('；');
    const fullPrompt = [customPrefix, prompt.trim()].filter(Boolean).join('。');
    const mode = materialSlots.length || base ? 'reference' : 'text';
    try {
      const payload = {
        mode,
        prompt: fullPrompt,
        creative_profile: profile,
        ...(mode === 'reference' ? {
          // 自有素材是整图：后端按「底图」写提示词（参考图N是当前画面，以它为准）。
          references: materialSlots.map(slot => ({
            role: slot.role === OWN_ROLE ? '底图' : slot.role,
            material_id: slot.material_id,
          })),
          ...(base ? { reference_image: base } : {}),
        } : {}),
        width: size.width, height: size.height, steps: 8,
        seed: usedSeed, idempotency_key: `firstframe-${crypto.randomUUID()}`,
        // 风格锁：后端只把它拼进 prompt_for_model，request.prompt 仍是上面这段原文。
        ...(styleBlock ? { style_block: styleBlock } : {}),
      };
      const created = await request<Task & { task?: Task }>(`/api/projects/${projectId}/keyframe-tasks`, { json: payload });
      const taskId = created.id || created.task?.id;
      if (!taskId) throw new Error('任务未返回编号');
      let output = '';
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const detail = await request<Task & { task?: Task }>(`/api/tasks/${encodeURIComponent(taskId)}`);
        const current = detail.task || detail;
        setStatus(statusLabels[current.status] || current.status);
        setProgress(current.progress || null);
        if (['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(current.status)) {
          if (current.status !== 'succeeded') throw new Error(current.error || `首帧生成${statusLabels[current.status] || current.status}`);
          output = readOutput(current.result);
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      if (!output) throw new Error('任务成功但没有拿到图片路径');
      setFrame(output);
      setHistory(current => [{
        id: crypto.randomUUID(), path: output, prompt: prompt.trim(), seed: usedSeed,
        profile, aspect, createdAt: Date.now(),
      }, ...current.filter(item => item.path !== output)].slice(0, MAX_HISTORY));
      onChange(output);
      // 这条词是从收藏里取的：把产物记回去，下次打开收藏就知道它出过什么。
      if (recipeId) {
        const usedId = recipeId;
        setRecipeId('');
        void request(`/api/prompt-library/${encodeURIComponent(usedId)}/use`, {
          json: { result: output, task_id: taskId },
        }).catch(() => undefined);
      }
      setStatus(base ? '已按底图改出新版本，可继续改或直接用作开始画面' : '已生成，可继续改或直接用作开始画面');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '首帧生成失败');
      setStatus('');
    } finally { setBusy(false); }
  };

  return <div className="ffs">
    <p className="sf-fine ffs-lede">选素材拼一张首帧，不满意就带着它继续改；改到满意再拿去生成视频。</p>

    <fieldset className="ffs-slots" data-own={ownSlot ? 'true' : undefined} disabled={disabled || busy}>
      <legend>参考素材 {ownSlot ? '· 自有素材（整图）' : '· 每类最多一张'}{filled.length ? ` · 已选 ${filled.length}` : ''}</legend>
      {ROLES.map(role => <ReferenceSlotField
        key={role} role={role} hint={ROLE_HINTS[role]} disabled={disabled || busy || Boolean(ownSlot)}
        value={slots.find(slot => slot.role === role)}
        onChange={next => setSlot(role, next)}
      />)}
      <ReferenceSlotField
        role={OWN_ROLE} hint={ROLE_HINTS[OWN_ROLE]} disabled={disabled || busy}
        projectId={projectId}
        value={ownSlot}
        onChange={next => setSlot(OWN_ROLE, next)}
      />
    </fieldset>

    {poseSlot && <p className="sf-fine">
      姿势走 Union 参考编码，除姿势图外最多再带 3 张参考图（当前 {semanticCount}/3）。超了会直接报错，不会悄悄丢掉。
    </p>}

    {ownSlot && <p className="sf-fine ffs-own-note">
      自有素材已启用：这张图本身就定了长相、造型、服装和姿势，上面四类这次不参与（清掉它就恢复）。
      写清动作和镜头就能直接生成视频；想让它变成另一张图，再点「生成首帧」。
      横屏方案请选横图、竖屏方案选竖图——比例不一致时开始画面会被拉伸。
    </p>}

    {materialSlots.length === 1 && !(autoAdopted.current?.path && autoAdopted.current.path === (currentFrame || frame)) && <div className="ffs-direct">
      <button type="button" className="sf-secondary" disabled={disabled || busy} onClick={() => void useSlotAsFrame(materialSlots[0])}>
        直接用「{materialSlots[0].title}」作首帧
      </button>
      <small className="sf-fine">不必先生成一张图：这张会直接成为视频的开始画面，其余造型/姿势/服装写在下面的提示词里。多选素材时请先生成首帧，让模型把它们合成一张。</small>
    </div>}

    {base && <div className="ffs-base">
      <span className="ffs-base-label">底图</span>
      <img src={mediaUrl(base)} alt="" />
      <span className="ffs-base-note">这次生成会以它为准做修改</span>
      <button type="button" className="sf-slot-clear" aria-label="取消底图" onClick={() => setBase('')}><X size={12} /></button>
    </div>}

    <div className="sf-field"><span className="qt-field-head">画面描述 / 改动要求
      {poseSlot && <button type="button" className="sf-inline-link" disabled={disabled || busy}
        onClick={() => {
          const clause = '全身构图，人物头顶到脚完整入镜，镜头拉远，不要裁成头像或半身特写。';
          const next = prompt.includes('全身构图') ? prompt : `${prompt.trim()}${prompt.trim() ? ' ' : ''}${clause}`;
          setPrompt(next); setPromptTouched(true); onPromptChange?.(next);
        }}>补一句全身构图</button>}
      <TranslateButton text={prompt} disabled={disabled || busy}
        onTranslated={next => { setPrompt(next); setPromptTouched(true); onPromptChange?.(next); }} />
      <StyleBlockPicker value={styleBlock} onChange={setStyleBlock} disabled={disabled || busy} />
    </span>
      <textarea rows={4} value={prompt} disabled={disabled || busy}
        onChange={event => { setPrompt(event.target.value); setPromptTouched(true); onPromptChange?.(event.target.value); }}
        placeholder="选好素材后这里就空出来了：写清画面、动作、光线。服装的结构细节（蝴蝶结、泡泡袖、开衩）要自己补一句，素材标签里通常没有。" />
      {poseSlot && (poseSlot.people || 1) >= 2 && <p className="ffs-override-note">
        这张姿势图里有 <strong>{poseSlot.people} 个人</strong>。只写一个人的描述时，模型很容易把两个人合成一个、或者凭空多画一个——
        请把第二个人的动作和位置也写出来。
        <button type="button" className="sf-inline-link" disabled={disabled || busy}
          onClick={() => {
            const clause = '画面里一共两个人，两人的身体朝向和相互位置要和参考图一致，不要合并成一个人，也不要多画人。';
            if (prompt.includes('一共两个人')) return;
            const next = `${prompt.trim()}${prompt.trim() ? ' ' : ''}${clause}`;
            setPrompt(next); setPromptTouched(true); onPromptChange?.(next);
          }}>补一句双人约束</button>
      </p>}
      {overrideHints.length > 0 && <p className="ffs-override-note">
        这段描述里的{overrideHints.map(rule => rule.label).join('、')}会盖掉对应参考图——模型是文字优先，写到什么就按文字重画什么。
        想用参考图里的样子，把这类词删掉；只想改颜色又要保留版型，改用「继续改这张 → 局部重绘」。
      </p>}
      {poseSlot && <small className="sf-fine">姿势图会作为结构控制图：深度图最稳，线稿偶尔会把关节圆圈带进画面。构图仍建议写清「全身、坐地、双手后撑、镜头拉远」，模型对场景和服装的理解会更准。</small>}
      <PromptWriter disabled={disabled || busy} prompt={prompt}
        context={{
          project_id: projectId, profile, aspect, seed,
          materials: materialSlots.map(slot => ({ role: slot.role, label: slot.title, material_id: slot.material_id })),
          result: frame || base || undefined,
        }}
        onUse={(text, extra) => {
          setPrompt(text); setPromptTouched(true);
          if (extra?.itemId) setRecipeId(extra.itemId);
          if (extra) void applyRecipe(extra);
        }} />
    </div>

    <div className="sf-field"><span>画幅</span>
      <div className="clarity-levels" role="group" aria-label="首帧画幅">
        {ASPECTS.map(option => <button key={option.value} type="button" aria-pressed={aspect === option.value} disabled={disabled || busy} onClick={() => setAspect(option.value)} title={option.pro}>
          <strong>{option.label}</strong><small>{option.note}</small>
        </button>)}
      </div>
      <small className="sf-fine">{size.width}×{size.height} · {size.pro}</small>
    </div>

    <div className="sf-field"><span>创作分区</span>
      <div className="clarity-levels" role="group" aria-label="创作分区">
        {PROFILES.map(option => <button key={option.value} type="button" aria-pressed={profile === option.value} disabled={disabled || busy} onClick={() => setProfile(option.value)}>
          <strong>{option.label}</strong><small>{option.note}</small>
        </button>)}
      </div>
      <small className="sf-fine">成人向分区会在主 LoRA 之后再叠一层去审查 LoRA（强度 0.7）。叠得太满会连材质一起推糊，所以没有拉满。</small>
    </div>

    {error && <p className="sf-error" role="alert">{error}</p>}
    {status && <p className="qt-status">{busy && <LoaderCircle size={14} className="sf-spin" />}{status}</p>}
    <ProgressBar progress={progress} />

    <div className="ffs-actions">
      <button type="button" className="sf-primary" disabled={disabled || busy} onClick={() => void submit()}>
        {busy ? '生成中…' : frame ? '再生成一张' : '生成首帧'}
      </button>
      {frame && <button type="button" className="sf-secondary" disabled={disabled || busy} onClick={() => setInpaintOpen(true)}>
        <Paintbrush size={14} /> 局部重绘
      </button>}
      {frame && <button type="button" className="sf-secondary" disabled={disabled || busy} onClick={() => useAsBase(frame)}>
        <Wand2 size={14} /> 继续改这张
      </button>}
    </div>

    {seed !== null && <p className="sf-fine ffs-seed-line">种子 <code>{seed}</code> 已锁定（切回历史时带进来的），同样的提示词再生成一次会得到那一版。
      <button type="button" className="sf-inline-link" disabled={disabled || busy}
        onClick={() => { setSeed(null); setStatus('已改回每次随机种子。'); }}>换随机</button>
    </p>}

    {/* 刷新后 frame 可能是空的，但历史里还有图：这时也要把并排区显示出来。 */}
    {(frame || history.length > 0) && <div className="ffs-frame">
      <div className="ffs-frame-head">
        <span><ImageIcon size={13} /> 首帧{history.length > 1 ? ` · ${history.length} 张` : ''}</span>
        <span className="ffs-frame-actions">
          <button type="button" className="sf-inline-link" onClick={() => onChange(frame)}>重新用作开始画面</button>
          <button type="button" className="sf-inline-link" disabled={disabled || busy}
            onClick={() => { setSaveOpen(open => !open); setSaveNote(null); setSaveTitle(defaultTitle()); }}>
            <Bookmark size={12} /> 存入素材库
          </button>
          <button type="button" className="sf-inline-link" disabled={disabled || busy}
            onClick={() => setLiquifyOpen(true)}>
            <Scaling size={12} /> 液化塑形
          </button>
          <button type="button" className="sf-inline-link" disabled={disabled || busy || enhanceBusy} title="2× 超分，跑完自动存进清晰化库"
            onClick={() => void pushToEnhance()}>
            <Wand2 size={12} /> {enhanceBusy ? '清晰化中…' : '推入清晰化'}
          </button>
          <button type="button" className="sf-inline-link" disabled={disabled || busy}
            onClick={() => { setHistory([]); setStatus('已清空生成记录；当前这张仍保留为开始画面。'); }}>
            清空记录
          </button>
        </span>
      </div>
      {/* 同一页里连续生成的每一版都留在这儿并排摆着，点哪张就把它设成开始画面。
          以前这里只显示最新一张、历史缩成 52×68 的小方块，看起来就像被覆盖了。 */}
      {history.length ? <div className="ffs-grid">
        {history.map(entry => <div key={entry.id} className={entry.path === frame ? 'ffs-cell is-current' : 'ffs-cell'}>
          <button type="button" className="ffs-cell-pick" disabled={disabled || busy}
            title={entry.prompt || '（没有记录提示词）'}
            onClick={() => pickFrame(entry)}>
            <img src={mediaUrl(entry.path)} alt="" loading="lazy" />
            {entry.path === frame && <em className="ffs-cell-badge">当前</em>}
          </button>
          <span className="ffs-cell-meta">{(entry.prompt || '（无提示词）').slice(0, 22) || '（无提示词）'}</span>
          <button type="button" className="sf-inline-link" disabled={disabled || busy}
            onClick={() => restore(entry)}>切回这一版</button>
        </div>)}
      </div> : <img src={mediaUrl(frame)} alt="首帧" />}
      {saveOpen && <div className="ffs-save">
        <label><span>存进「首帧图参考」的名字</span>
          <input value={saveTitle} maxLength={48} disabled={saveBusy} placeholder={defaultTitle()}
            onChange={event => setSaveTitle(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') void saveToLibrary(); }} />
        </label>
        <button type="button" className="sf-primary" disabled={saveBusy} onClick={() => void saveToLibrary()}>
          {saveBusy ? '保存中…' : '保存'}
        </button>
        <button type="button" className="sf-ghost" disabled={saveBusy} onClick={() => setSaveOpen(false)}>取消</button>
      </div>}
      {saveNote && <p className={saveNote.error ? 'sf-error' : 'sf-fine'} role={saveNote.error ? 'alert' : 'status'}>{saveNote.text}</p>}
      {enhanceNote && <p className={enhanceNote.error ? 'sf-error' : 'sf-fine'} role={enhanceNote.error ? 'alert' : 'status'}>{enhanceNote.text}</p>}
      <p className="sf-fine">这张已经自动设为下面的开始画面。想再改就点「继续改这张」，它会成为参考图1。</p>
    </div>}

    {!frame && !busy && <p className="sf-fine ffs-tip"><Sparkles size={12} /> 提示：只想把已有图片改成别的样子，可以先在下面用「上传图片」设成开始画面，再选好素材后点生成。</p>}
    {inpaintOpen && frame && <InpaintDialog projectId={projectId} imagePath={frame} profile={profile}
      onClose={() => setInpaintOpen(false)}
      onDone={(path, text) => {
        setInpaintOpen(false);
        setFrame(path);
        setHistory(current => [{
          id: crypto.randomUUID(), path, prompt: text, seed: 0, profile, aspect, createdAt: Date.now(),
        }, ...current.filter(item => item.path !== path)].slice(0, MAX_HISTORY));
        onChange(path);
        setStatus('已按蒙版重绘；没涂到的区域保持原样。');
      }} />}
    {liquifyOpen && frame && <LiquifyDialog projectId={projectId} imagePath={frame}
      onClose={() => setLiquifyOpen(false)}
      onDone={(path, count, refine) => {
        setFrame(path);
        setHistory(current => [{
          id: crypto.randomUUID(), path, prompt: `液化塑形（${count} 个调整中心）`, seed: 0, profile, aspect, createdAt: Date.now(),
        }, ...current.filter(item => item.path !== path)].slice(0, MAX_HISTORY));
        onChange(path);
        setLiquifyOpen(false);
        setStatus('液化已保存为新图；原图还在，历史里可以切回去。');
        if (refine) setInpaintOpen(true);
      }} />}
  </div>;
}
