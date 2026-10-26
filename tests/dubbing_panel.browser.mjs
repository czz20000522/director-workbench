// Isolated real-browser check: no live service, account, model or GPU requests.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const result = await build({ stdin: { contents: `import React from 'react';
import {createRoot} from 'react-dom/client';
import DubbingPanel from './src/DubbingPanel';
createRoot(document.getElementById('root')).render(<DubbingPanel projectId="new-work" options={[{value:'C:/voice.wav',label:'参考.wav',kind:'audio'}]} videos={[{id:'v1',title:'镜头 1'}]} onBrowse={()=>{}} onComplete={()=>{}} />);`,
  loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const submissions = [];
  const controls = [];
  let receipt = { id: 'candidate-1', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'gpu_busy', message: '等待共享执行资源' }, position: 2 } };
  await page.route('http://localhost:4199/**', async route => {
    const request = route.request();
    if (request.url().endsWith('/dubbing-capability')) {
      const vox = { available: true, missing: [], engine: 'VoxCPM2', engine_id: 'voxcpm-2', modes: ['design', 'reference'], timbre: 'design-or-reference', emotion_modes: { none: true, reference: false, text: true, vector: false }, emotion_unsupported: ['reference', 'vector'] };
      const index = { available: true, missing: [], engine: 'IndexTTS 2.5', engine_id: 'indextts-2.5', modes: ['reference'], timbre: 'reference-only', emotion_modes: { none: true, reference: true, text: true, vector: true } };
      return route.fulfill({ json: { ...vox, default_engine: 'voxcpm-2', engines: { 'voxcpm-2': vox, 'indextts-2.5': index } } });
    }
    if (request.url().endsWith('/dubbing-tasks')) {
      submissions.push(request.postDataJSON());
      return route.fulfill({ json: receipt });
    }
    if (request.url().endsWith('/stop')) { controls.push(new URL(request.url()).pathname); receipt = { ...receipt, status: 'stopped', queue: { state: 'terminal' } }; }
    if (request.url().endsWith('/creative-settings')) return route.fulfill({ json: { settings: { music: [], dubbing: [] } } });
    if (request.url().includes('/api/')) return route.fulfill({ json: receipt });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script>' + result.outputFiles[0].text + '</script>' });
  });
  await page.goto('http://localhost:4199/');

  assert.equal(await page.getByRole('button', { name: '生成配音候选' }).isEnabled(), false);
  // VoxCPM2 is the default engine; IndexTTS keeps only the reference timbre source.
  assert.equal(await page.getByLabel('配音引擎').inputValue(), 'voxcpm-2');
  assert.equal(await page.getByRole('option', { name: '情绪参考音频（这个引擎不吃这种情绪）' }).count(), 1);
  await page.getByLabel('配音引擎').selectOption('indextts-2.5');
  assert.equal(await page.getByLabel('音色来源').isDisabled(), true);
  await page.getByLabel('配音引擎').selectOption('voxcpm-2');
  await page.getByLabel('音色来源').selectOption('design');
  assert.equal(await page.getByLabel('音色参考', { exact: true }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '生成配音候选' }).isEnabled(), false);
  await page.getByLabel('音色来源').selectOption('reference');
  await page.getByLabel('视频').selectOption('v1');
  await page.getByLabel('音色参考', { exact: true }).selectOption('C:/voice.wav');
  await page.getByLabel('第 1 句开始（秒）').fill('0.5');
  await page.getByLabel('台词', { exact: true }).fill('天快黑了，我们走吧。');
  await page.getByRole('button', { name: '添加一句' }).click();
  await page.getByLabel('第 2 句开始（秒）').fill('2.5');
  await page.getByLabel('台词', { exact: true }).nth(1).fill('别回头。');
  assert.equal(await page.getByRole('button', { name: '生成配音候选' }).isEnabled(), true);
  // Emotion is opt-in, and a chosen mode must be filled in before it can be sent.
  await page.getByLabel('情绪方式').selectOption('text');
  assert.equal(await page.getByRole('button', { name: '生成配音候选' }).isEnabled(), false);
  await page.getByLabel('情绪描述').fill('克制的悲伤，压低声音');
  assert.equal(await page.getByLabel('八维强度 happy').count(), 0);
  await page.getByRole('button', { name: '生成配音候选' }).click();
  await page.waitForFunction(() => JSON.parse(localStorage.getItem('director-workbench:new-work:dubbing:v1')).pending);
  await page.waitForTimeout(100);
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].source_asset_id, 'v1');
  assert.equal(submissions[0].engine, 'voxcpm-2');
  assert.equal(submissions[0].voice_reference, 'C:/voice.wav');
  assert.equal(submissions[0].voice_description, null);
  assert.equal(Number.isInteger(submissions[0].seed) && submissions[0].seed >= 0, true);
  assert.equal(submissions[0].original_audio, 'keep');
  assert.deepEqual(submissions[0].cues, [
    { at_seconds: 0.5, text: '天快黑了，我们走吧。', gain: 1 },
    { at_seconds: 2.5, text: '别回头。', gain: 1 },
  ]);
  assert.deepEqual(submissions[0].emotion, { mode: 'text', text: '克制的悲伤，压低声音' });
  assert.equal(await page.getByLabel('情绪描述').inputValue(), '克制的悲伤，压低声音');
  await page.reload();

  assert.equal(await page.getByLabel('视频').isDisabled(), true);
  assert.equal(await page.getByLabel('第 2 句开始（秒）').inputValue(), '2.5');
  assert.equal(submissions.length, 1, 'Reload must recover the receipt, not submit again');
  await page.getByRole('status', { name: '任务排队 锚点配音' }).getByText(/等待共享执行资源 · 队列位置 2/).waitFor();
  await page.getByRole('button', { name: '停止配音任务' }).click();
  await page.getByRole('button', { name: '继续配音' }).waitFor();
  assert.deepEqual(controls, ['/api/projects/new-work/tasks/candidate-1/stop']);
  assert.equal(submissions.length, 1);

  // 解锁后才出现导入入口；导入只搬时间轴与文字，行内标签清掉。
  await page.getByRole('button', { name: '继续配音' }).click();
  const srt = ['1', '00:00:00,500 --> 00:00:02,000', '第一句字幕', '', '2',
    '00:00:02,500 --> 00:00:04,000', '<i>第二句</i>字幕', ''].join('\n');
  await page.locator('input[type=file]').setInputFiles({ name: '台词.srt', mimeType: 'text/plain', buffer: Buffer.from(srt, 'utf8') });
  await page.waitForFunction(() => document.querySelectorAll('textarea').length >= 2);
  assert.equal(await page.getByLabel('第 1 句开始（秒）').inputValue(), '0.5');
  assert.equal(await page.getByLabel('台词', { exact: true }).first().inputValue(), '第一句字幕');
  assert.equal(await page.getByLabel('第 2 句开始（秒）').inputValue(), '2.5');
  assert.equal(await page.getByLabel('台词', { exact: true }).nth(1).inputValue(), '第二句字幕');
  assert.match(await page.locator('.sf-post-message').first().innerText(), /已从「台词\.srt」导入 2 句/);
  console.log('DubbingPanel: explicit cue timing, frozen reload, stop and subtitle import passed');
} finally { await browser.close(); }
