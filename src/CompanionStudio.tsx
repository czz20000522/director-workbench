import { useEffect, useId, useRef, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { mediaUrl, request } from './api/client';
import { notifyPresetLibraryChanged, rememberSample } from './timbreSamples';

// 图生视频页内嵌的精简版配套制作：只留"写一句话就能出东西"的必要输入，
// 其余参数走固定默认值；产物带 parent_task_id，所以能看出属于哪条视频。
type Kind = 'music' | 'timbre';
type Task = { id: string; status: string; error?: string; result?: { output?: string; audio_output?: string } };

const terminals = ['succeeded', 'failed', 'stopped', 'needs_reconcile'];
const labels: Record<string, string> = {
  scheduler_waiting: '排队中', preparing: '准备中', submitting: '提交中', queued: '排队中',
  running: '正在生成', succeeded: '已生成', failed: '生成失败', stopped: '已停止', needs_reconcile: '待核对',
};

function randomSeed(): number {
  return Math.floor(Math.random() * 4294967295);
}

export default function CompanionStudio({ projectId, parentTaskId, duration, kind }: {
  projectId: string; parentTaskId: string; duration: number; kind: Kind;
}) {
  const [text, setText] = useState('');
  const [line, setLine] = useState('');
  const [busy, setBusy] = useState(false);
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState('');
  const [cardName, setCardName] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedNote, setSavedNote] = useState('');
  const cardId = useId();
  const timer = useRef<number | undefined>(undefined);
  // 提交时的音色描述才是这次结果的身份证：生成后用户可能继续改输入框，
  // 用小样索引按描述回挂时要认准提交那一刻的原文。
  const submitted = useRef<{ description: string; line: string } | null>(null);

  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);

  function poll(id: string) {
    timer.current = window.setTimeout(async () => {
      try {
        const next = await request<Task>(`/api/projects/${projectId}/tasks/${encodeURIComponent(id)}`);
        setTask(next);
        if (!terminals.includes(next.status)) { poll(id); return; }
        if (next.status !== 'succeeded') setError(next.error || `任务状态：${next.status}`);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : '无法读取任务状态');
      }
    }, 2000);
  }

  async function submit() {
    const value = text.trim();
    if (!value) { setError(kind === 'music' ? '先写风格标签' : '先写音色描述'); return; }
    setBusy(true); setError(''); setTask(null); setSavedNote('');
    submitted.current = { description: value, line: line.trim() };
    try {
      const body = kind === 'music'
        ? { tags: value, lyrics: '', duration_seconds: Math.max(4, Math.round(duration)), bpm: 90,
            keyscale: 'A minor', timesignature: '4', language: 'unknown', seed: randomSeed(), steps: 8,
            cfg: 1.0, shift: 3.0, output_format: 'mp3', quality: 'V0', generate_audio_codes: false,
            encode_cfg_scale: 2.0, temperature: 0.85, top_p: 0.9, top_k: 0, min_p: 0,
            parent_task_id: parentTaskId, idempotency_key: `music-${crypto.randomUUID()}` }
        : { mode: 'design', engine: 'voxcpm-2', text: line.trim() || '这是一段音色试听，用来判断质感。',
            voice_description: value, parent_task_id: parentTaskId,
            idempotency_key: `speech-${crypto.randomUUID()}` };
      const created = await request<Task>(`/api/projects/${projectId}/${kind === 'music' ? 'music-tasks' : 'speech-tasks'}`, { json: body });
      setTask(created);
      if (!terminals.includes(created.status)) poll(created.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '提交失败');
    } finally { setBusy(false); }
  }

  const output = task?.result?.output || task?.result?.audio_output || '';
  const running = Boolean(task && !terminals.includes(task.status));

  // 生成成功后把小样按音色描述挂到音色库：描述没改 → 覆盖那张卡的小样；
  // 描述改了 → 记成另一条音色。这样回库里可直接试听，不必重新生成。
  useEffect(() => {
    if (kind !== 'timbre' || task?.status !== 'succeeded' || !output) return;
    const source = submitted.current;
    if (!source?.description) return;
    rememberSample({ path: output, description: source.description, text: source.line, source: 'companion' });
  }, [kind, task?.status, output]);

  async function saveCard() {
    const name = cardName.trim();
    if (!name || !submitted.current?.description) return;
    setSaving(true); setError(''); setSavedNote('');
    try {
      await request(`/api/preset-library/timbre/${encodeURIComponent(name)}`, {
        init: { method: 'PUT' },
        json: { params: { engine: 'voxcpm-2', voice_description: submitted.current.description,
          text: submitted.current.line || '这是一段音色试听，用来判断质感。' } },
      });
      notifyPresetLibraryChanged();
      setSavedNote(`已存入音色库：${name}`);
      setCardName('');
    } catch (reason) {
      setError(reason instanceof Error ? `存入音色库失败：${reason.message}` : '存入音色库未完成');
    } finally { setSaving(false); }
  }

  return <div className="qt-companion">
    <div className="qt-companion-head">
      <strong>{kind === 'music' ? '配乐' : '音色设计'}</strong>
      <small>属于上面这条视频 · 精简版</small>
    </div>
    <label className="sf-field">
      <span>{kind === 'music' ? '风格标签' : '音色描述'}</span>
      <input value={text} maxLength={300} disabled={busy || running} onChange={event => setText(event.target.value)}
        placeholder={kind === 'music' ? '悬疑氛围，弦乐铺底与低频脉冲，克制，无人声' : '四十岁上下的男性，音色低沉干净，语速平稳'} />
    </label>
    {kind === 'timbre' && <label className="sf-field">
      <span>试听台词</span>
      <input value={line} maxLength={200} disabled={busy || running} onChange={event => setLine(event.target.value)}
        placeholder="留空就用一句通用台词试听" />
    </label>}
    <div className="qt-companion-actions">
      <button className="button primary compact" type="button" disabled={busy || running || !text.trim()} onClick={() => void submit()}>
        {busy ? '提交中…' : running ? (labels[task?.status || ''] || '生成中') : kind === 'music' ? `生成 ${Math.max(4, Math.round(duration))} 秒配乐` : '生成音色样本'}
      </button>
      {running && <LoaderCircle size={14} className="sf-spin" />}
    </div>
    {output && <div className="qt-companion-result">
      <audio controls preload="metadata" src={mediaUrl(output)} aria-label={kind === 'music' ? '配乐试听' : '音色试听'} />
      <a className="button secondary compact" href={mediaUrl(output)} download={`${kind === 'music' ? '配乐' : '音色'}-${task?.id.slice(0, 8)}.mp3`}>下载</a>
    </div>}
    {kind === 'timbre' && output && <div className="qt-companion-save">
      <div className="qt-companion-save-head">
        <label htmlFor={cardId}>把设计的音色存进音色库</label>
        <input id={cardId} value={cardName} maxLength={40}
          placeholder="卡片名称，例如：纪录片旁白" onChange={event => setCardName(event.target.value)} />
        <button className="button secondary compact" type="button" disabled={saving || !cardName.trim()}
          onClick={() => void saveCard()}>{saving ? '存入中…' : '存入音色库'}</button>
      </div>
      <small>{savedNote ? `${savedNote} · 小样已按描述挂上，回音色库可直接试听` : '小样已按这段描述挂到音色库对应卡片，不用重新生成就能试听；填名字可另存一张新卡。'}</small>
    </div>}
    {error && <p className="qt-error" role="alert">{error}</p>}
  </div>;
}
