// Isolated real-browser check: no live service, account, model or GPU requests.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const result = await build({ stdin: { contents: `import React from 'react';
import {createRoot} from 'react-dom/client';
import ObservationPanel from './src/ObservationPanel';
createRoot(document.getElementById('root')).render(<ObservationPanel projectId="new-work" options={[
  {value:'C:/frame.png',label:'关键帧.png',kind:'image'},
  {value:'C:/shot.mp4',label:'镜头.mp4',kind:'video'}]} onBrowse={()=>{}} onComplete={()=>{}} />);`,
  loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const submissions = [];
  const controls = [];
  let receipt = { id: 'observe-1', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'gpu_busy', message: '等待共享执行资源' }, position: 2 } };
  await page.route('http://localhost:4199/**', async route => {
    const request = route.request();
    if (request.url().endsWith('/vlm-capability')) return route.fulfill({ json: { available: true, model: 'qwen2.5vl:7b', service: 'http://127.0.0.1:11434', note: '', default_provider: 'local-ollama', providers: { 'local-ollama': { available: true, label: '本机 Ollama', model: 'qwen2.5vl:7b', service: 'http://127.0.0.1:11434', missing: [] }, 'deepseek-vision': { available: false, label: 'DeepSeek 视觉', model: 'deepseek-flash', service: 'https://api.deepseek.com', missing: ['DeepSeek API Key'], reason: '未配置 API Key' } } } });
    if (request.url().endsWith('/vlm-tasks')) {
      submissions.push(request.postDataJSON());
      return route.fulfill({ json: receipt });
    }
    if (request.url().endsWith('/stop')) { controls.push(new URL(request.url()).pathname); receipt = { ...receipt, status: 'stopped', queue: { state: 'terminal' } }; }
    if (request.url().includes('/api/')) return route.fulfill({ json: receipt });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script>' + result.outputFiles[0].text + '</script>' });
  });
  await page.goto('http://localhost:4199/');

  await page.getByLabel('观察图片', { exact: true }).selectOption('C:/frame.png');
  assert.equal(await page.getByRole('button', { name: '生成画面观察' }).isEnabled(), false);
  await page.getByLabel('观察问题').fill('画面里有几个人？机位是什么？');
  assert.equal(await page.getByRole('button', { name: '取帧时间点（秒）' }).count(), 0);
  await page.getByRole('button', { name: '生成画面观察' }).click();
  await page.waitForFunction(() => JSON.parse(localStorage.getItem('director-workbench:new-work:observation:v1')).pending);
  await page.waitForTimeout(100);
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].source, 'C:/frame.png');
  assert.equal(submissions[0].frame_seconds, null);
  assert.equal(submissions[0].prompt, '画面里有几个人？机位是什么？');
  assert.equal(submissions[0].temperature, 0.8);
  assert.equal(submissions[0].num_predict, 512);
  assert.equal(submissions[0].provider, 'local-ollama');
  // The cloud source stays visibly unavailable until a key exists.
  assert.equal(await page.getByRole('option', { name: /DeepSeek 视觉（未就绪）/ }).getAttribute('disabled'), '');
  await page.getByRole('status', { name: '任务排队 画面观察' }).getByText(/等待共享执行资源 · 队列位置 2/).waitFor();
  await page.getByRole('button', { name: '停止观察' }).click();
  await page.getByRole('button', { name: '准备下一个问题' }).waitFor();
  assert.deepEqual(controls, ['/api/projects/new-work/tasks/observe-1/stop']);

  await page.getByRole('button', { name: '准备下一个问题' }).click();
  await page.getByLabel('观察对象').selectOption('video');
  assert.equal(await page.getByLabel('准备下一个问题').count(), 0);
  await page.getByLabel('观察视频', { exact: true }).selectOption('C:/shot.mp4');
  await page.getByLabel('取帧时间点（秒）').fill('2.5');
  await page.getByRole('button', { name: '生成画面观察' }).click();
  await page.waitForTimeout(100);
  assert.equal(submissions.length, 2);
  assert.equal(submissions[1].source, 'C:/shot.mp4');
  assert.equal(submissions[1].frame_seconds, 2.5);
  await page.reload();
  assert.equal(submissions.length, 2, 'Reload must recover the receipt, not submit again');
  assert.equal(await page.getByLabel('观察问题').isDisabled(), true);
  assert.equal(await page.getByLabel('取帧时间点（秒）').inputValue(), '2.5');
  console.log('ObservationPanel: explicit frame, frozen reload, stop and read-only draft passed');
} finally { await browser.close(); }
