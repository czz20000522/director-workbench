// Isolated real-browser check: no live service, account, model or GPU requests.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const result = await build({ stdin: { contents: `import React from 'react';
import {createRoot} from 'react-dom/client';
import SpeechPanel from './src/SpeechPanel';
createRoot(document.getElementById('root')).render(<SpeechPanel projectId="new-work" options={[]} onBrowse={()=>{}} onComplete={()=>{}} />);`,
  loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const submissions = [];
  const controls = [];
  let receipt = { id: 'candidate-1', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'gpu_busy', message: '等待共享执行资源' }, position: 2 } };
  await page.route('http://localhost:4199/**', async route => {
    const request = route.request();
    if (request.url().endsWith('/speech-capability')) {
      const vox = { available: true, missing: [], engine: 'VoxCPM2', id: 'voxcpm-2', modes: ['reference', 'design'], timbre: 'design-or-reference', seed: true, sample_rate: 48000, emotion_modes: { none: true, reference: false, text: true, vector: false }, emotion_unsupported: ['reference', 'vector'] };
      const auk = { available: true, missing: [], engine: 'AuK-Flash', id: 'auk-flash', modes: ['reference', 'design'] };
      const index = { available: false, missing: ['IndexTTS 主模型'], engine: 'IndexTTS 2.5', id: 'indextts-2.5', modes: ['reference'], timbre: 'reference-only', seed: false, emotion_modes: { none: true, reference: true, text: true, vector: true } };
      return route.fulfill({ json: { ...vox, engines: { 'voxcpm-2': vox, 'auk-flash': auk, 'indextts-2.5': index } } });
    }
    if (request.url().endsWith('/speech-tasks')) {
      submissions.push(request.postDataJSON());
      return route.fulfill({ json: receipt });
    }
    if (request.url().endsWith('/stop')) { controls.push(new URL(request.url()).pathname); receipt = { ...receipt, status: 'stopped', queue: { state: 'terminal' } }; }
    if (request.url().includes('/api/')) return route.fulfill({ json: receipt });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script>' + result.outputFiles[0].text + '</script>' });
  });
  await page.goto('http://localhost:4199/');
  
  await page.getByLabel('台词', { exact: true }).fill('天快黑了。');
  // VoxCPM2 is the default engine and is the one that designs a timbre from words.
  assert.equal(await page.getByLabel('声音引擎').inputValue(), 'voxcpm-2');
  assert.equal(await page.getByRole('option', { name: 'IndexTTS 2.5（未就绪）' }).count(), 1);
  // Modes VoxCPM2 structurally cannot take are labelled as such, not as missing installs.
  assert.equal(await page.getByRole('option', { name: '情绪参考音频（这个引擎不吃这种情绪）' }).count(), 1);
  assert.equal(await page.getByRole('option', { name: '八维情绪强度（这个引擎不吃这种情绪）' }).count(), 1);
  assert.equal(await page.getByRole('button', { name: '生成声音候选' }).isEnabled(), false);
  await page.getByLabel('声音来源').selectOption('design');
  assert.equal(await page.getByRole('button', { name: '生成声音候选' }).isEnabled(), false);
  await page.getByLabel('音色描述').fill('年长男性，低沉沙哑');
  await page.getByRole('button', { name: '生成声音候选' }).click();
  await page.waitForFunction(() => JSON.parse(localStorage.getItem('director-workbench:new-work:speech:v1')).pending);
  await page.waitForTimeout(100);
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].engine, 'voxcpm-2');
  assert.equal(submissions[0].mode, 'design');
  assert.equal(submissions[0].voice_description, '年长男性，低沉沙哑');
  assert.equal('voice_reference' in submissions[0], false);
  assert.equal('gen_seconds' in submissions[0], false);
  assert.equal(submissions[0].emotion, null);
  await page.reload();
  
  assert.equal(await page.getByLabel('声音来源').inputValue(), 'design');
  assert.equal(await page.getByLabel('音色描述').isDisabled(), true);
  assert.equal(submissions.length, 1, 'Reload must recover the receipt, not submit again');
  await page.getByRole('status', { name: '任务排队 声音' }).getByText(/等待共享执行资源 · 队列位置 2/).waitFor();
  await page.getByRole('button', { name: '停止声音任务' }).click();
  await page.getByRole('button', { name: '准备下一条台词' }).waitFor();
  assert.deepEqual(controls, ['/api/projects/new-work/tasks/candidate-1/stop']);
  assert.equal(submissions.length, 1);
  console.log('SpeechPanel: empty project design, payload exclusivity, frozen reload passed');
} finally { await browser.close(); }

