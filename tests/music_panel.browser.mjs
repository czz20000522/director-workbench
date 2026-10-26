// Isolated real-browser check: no live service, model or GPU requests.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const result = await build({ stdin: { contents: `import React from 'react';
import {createRoot} from 'react-dom/client';
import MusicPanel from './src/MusicPanel';
createRoot(document.getElementById('root')).render(<MusicPanel projectId="new-work" onComplete={()=>{}} />);`,
  loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const submissions = [];
  const controls = [];
  let receipt = { id: 'music-1', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'gpu_busy', message: '等待共享执行资源' }, position: 2 } };
  await page.route('http://localhost:4199/**', async route => {
    const request = route.request();
    if (request.url().endsWith('/music-capability')) {
      return route.fulfill({ json: { available: true, missing_nodes: [], missing_models: [], keyscales: ['C major', 'A minor'], timesignatures: ['2', '3', '4', '6'], languages: ['unknown', 'en', 'zh'], mp3_qualities: ['V0', '128k', '320k'], duration_seconds: [1, 300] } });
    }
    if (request.url().endsWith('/music-tasks')) {
      submissions.push(request.postDataJSON());
      return route.fulfill({ json: receipt });
    }
    if (request.url().endsWith('/stop')) { controls.push(new URL(request.url()).pathname); receipt = { ...receipt, status: 'stopped', queue: { state: 'terminal' } }; }
    if (request.url().endsWith('/creative-settings')) return route.fulfill({ json: { settings: { music: [], dubbing: [] } } });
    if (request.url().includes('/api/')) return route.fulfill({ json: receipt });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script>' + result.outputFiles[0].text + '</script>' });
  });
  await page.goto('http://localhost:4199/');

  await page.waitForSelector('#root form, #root section');
  assert.equal(await page.getByRole('button', { name: '生成配乐候选' }).isEnabled(), false);
  await page.getByLabel('风格标签').fill('夜景追逐铺垫，悬疑弦乐与低频脉冲');
  await page.getByLabel('时长（秒）').fill('18');
  await page.getByLabel('BPM').fill('88');
  await page.getByLabel('调性').selectOption('A minor');
  await page.getByLabel('随机编号').fill('20261025');
  assert.equal(await page.getByLabel('歌词（留空＝器乐）').inputValue(), '');
  await page.getByRole('button', { name: '生成配乐候选' }).click();
  await page.waitForFunction(() => JSON.parse(localStorage.getItem('director-workbench:new-work:music:v1')).pending);
  await page.waitForTimeout(150);
  assert.equal(submissions.length, 1);
  const sent = submissions[0];
  assert.equal(sent.tags, '夜景追逐铺垫，悬疑弦乐与低频脉冲');
  assert.equal(sent.lyrics, '');
  assert.equal(sent.duration_seconds, 18);
  assert.equal(sent.bpm, 88);
  assert.equal(sent.keyscale, 'A minor');
  assert.equal(sent.timesignature, '4');
  assert.equal(sent.language, 'unknown');
  assert.equal(sent.seed, 20261025);
  assert.equal(sent.steps, 8);
  assert.equal(sent.output_format, 'mp3');
  assert.equal(sent.quality, 'V0');
  assert.equal(sent.generate_audio_codes, true);
  await page.reload();
  assert.equal(submissions.length, 1, 'Reload must recover the receipt, not submit again');
  assert.equal(await page.getByLabel('风格标签').isDisabled(), true);
  assert.equal(await page.getByLabel('随机编号').inputValue(), '20261025');
  await page.getByRole('status', { name: '任务排队 配乐' }).getByText(/等待共享执行资源 · 队列位置 2/).waitFor();
  await page.getByRole('button', { name: '停止配乐任务' }).click();
  await page.getByRole('button', { name: '准备下一段配乐' }).waitFor();
  assert.deepEqual(controls, ['/api/projects/new-work/tasks/music-1/stop']);
  await page.getByRole('button', { name: '准备下一段配乐' }).click();
  await page.getByLabel('输出格式').selectOption('flac');
  assert.equal(await page.getByLabel('MP3 质量').count(), 0);
  console.log('MusicPanel: explicit music parameters, frozen reload and stop passed');
} finally { await browser.close(); }
