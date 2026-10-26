// Isolated real-browser check: no live service, account, model or GPU requests.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const result = await build({ stdin: { contents: `import React from 'react';
import {createRoot} from 'react-dom/client';
import TimbrePanel from './src/TimbrePanel';
createRoot(document.getElementById('root')).render(<TimbrePanel projectId="new-work" onComplete={()=>{}} />);`,
  loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const submissions = [];
  const controls = [];
  let receipt = { id: 'timbre-1', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'gpu_busy', message: '等待共享执行资源' }, position: 1 } };
  const vox = { available: true, missing: [], engine: 'VoxCPM2', id: 'voxcpm-2', modes: ['reference', 'design'], timbre: 'design-or-reference', seed: true, sample_rate: 48000, language: 'ZH + 30 语言 + 9 中文方言' };
  const index = { available: true, missing: [], engine: 'IndexTTS 2.5', id: 'indextts-2.5', modes: ['reference'], timbre: 'reference-only', seed: false, sample_rate: 22050, language: 'ZH' };
  await page.route('http://localhost:4198/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/speech-capability')) return route.fulfill({ json: { ...vox, default_engine: 'voxcpm-2', engines: { 'voxcpm-2': vox, 'indextts-2.5': index } } });
    if (path.endsWith('/speech-tasks')) {
      submissions.push(request.postDataJSON());
      return route.fulfill({ json: receipt });
    }
    if (path.endsWith('/stop')) { controls.push(path); receipt = { ...receipt, status: 'stopped', queue: { state: 'terminal' } }; }
    if (path === '/api/projects/new-work') {
      return route.fulfill({ json: { assets: [
        { id: 'speech-old', kind: 'audio', title: '音色 · 低沉沙哑的男声', subtitle: '音色样本 · 待试听', end: 4.2, sources: { A: 'workspaces/x/speech-tasks/old/result/speech.wav' } },
        { id: 'speech-line', kind: 'audio', title: '台词 · 别的句子', subtitle: '声音候选 · 待试听', end: 3, sources: { A: 'workspaces/x/other.wav' } },
        { id: 'voice-bare', kind: 'audio', title: '参考 · 旁白原声', sources: { A: 'workspaces/x/assets/audio/narration.wav' } },
      ] } });
    }
    if (path.includes('/api/')) return route.fulfill({ json: receipt });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script>' + result.outputFiles[0].text + '</script>' });
  });
  await page.goto('http://localhost:4198/');

  // Only a designed timbre is listed; ordinary dialogue candidates stay out.
  assert.equal(await page.getByText('音色 · 低沉沙哑的男声').count(), 1);
  assert.equal(await page.getByText('台词 · 别的句子').count(), 0);
  assert.equal(await page.getByLabel('声音引擎').inputValue(), 'voxcpm-2');
  assert.equal(await page.getByRole('button', { name: '生成音色样本' }).isEnabled(), false);
  await page.getByLabel('音色描述').fill('五十岁上下的男性，声音低沉沙哑');
  assert.equal(await page.getByRole('button', { name: '生成音色样本' }).isEnabled(), true);
  await page.getByLabel('试听台词').fill('这段声音只用来试音色。');
  // Switching engines swaps the fields instead of keeping a form the engine cannot use.
  await page.getByLabel('声音引擎').selectOption('indextts-2.5');
  assert.equal(await page.getByLabel('音色描述').count(), 0);
  assert.equal(await page.getByLabel('随机编号').count(), 0);
  assert.equal(await page.getByRole('button', { name: '生成音色样本' }).isEnabled(), false);
  await page.getByLabel(/参考音频/).selectOption('workspaces/x/assets/audio/narration.wav');
  await page.getByLabel('声音引擎').selectOption('voxcpm-2');
  assert.equal(await page.getByLabel('音色描述').inputValue(), '五十岁上下的男性，声音低沉沙哑');
  await page.getByRole('button', { name: '生成音色样本' }).click();
  await page.waitForFunction(() => JSON.parse(localStorage.getItem('director-workbench:new-work:timbre:v1')).pending);
  await page.waitForTimeout(100);
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].engine, 'voxcpm-2');
  assert.equal(submissions[0].mode, 'design');
  assert.equal(submissions[0].voice_description, '五十岁上下的男性，声音低沉沙哑');
  assert.equal(submissions[0].text, '这段声音只用来试音色。');
  assert.equal('voice_reference' in submissions[0], false);
  assert.equal('gen_seconds' in submissions[0], false);
  assert.equal(Number.isInteger(submissions[0].seed), true);
  // The trade-off block is part of the choice, not hidden behind a tooltip.
  assert.equal(await page.getByText('优势 · 能用一句话凭空造出新音色，不需要任何参考音频').count(), 1);
  assert.equal(await page.getByText('代价 · 情绪只能用文字描述，没有八维强度或情绪参考音频').count(), 1);
  await page.reload();
  assert.equal(await page.getByLabel('音色描述').isDisabled(), true);
  assert.equal(submissions.length, 1, 'Reload must recover the receipt, not submit again');
  await page.getByRole('button', { name: '停止任务' }).click();
  await page.getByRole('button', { name: '设计下一个音色' }).waitFor();
  assert.deepEqual(controls, ['/api/projects/new-work/tasks/timbre-1/stop']);
  console.log('TimbrePanel: engine-specific forms, design-only payload, sample list, frozen reload and stop passed');
} finally { await browser.close(); }
