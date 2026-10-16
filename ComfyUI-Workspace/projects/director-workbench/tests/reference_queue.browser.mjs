// Offline reference queue UI: fake upload/task and all HTTP intercepted.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import ReverseWorkbench from './src/ReverseWorkbench';createRoot(document.getElementById('root')).render(<ReverseWorkbench projectId="reference-test" onConverted={()=>{}}/>);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const writes = []; const errors = []; let complete = false; let semanticJob = null;
  page.on('pageerror', error => errors.push(error.message));
  const queued = { id: 'analysis-test', title: '参考样例', status: 'queued', source: { original_name: 'my-reference.mp4', path: 'input/reference.mp4', duration_seconds: 0, width: null, height: null }, stages: [{ id: 'materials', label: '拆分素材', status: 'waiting' }], timeline: [], artifacts: {}, import_task_id: 'reference-task', import_status: 'queued', import_task: { id: 'reference-task', asset_id: 'analysis-test', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'gpu_busy', message: '等待共享 GPU 执行槽' }, position: 3 } } };
  await page.route('http://localhost:4195/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    if (path === '/api/projects/reference-test/reverse-analyses') {
      if (request.method() === 'POST') { writes.push(path); return route.fulfill({ status: 202, json: queued }); }
      return route.fulfill({ json: { analyses: [] } });
    }
    if (path.endsWith('/semantic-jobs/current')) return route.fulfill({ json: { job: semanticJob } });
    if (path === '/api/projects/reference-test/tasks/semantic-task/stop') {
      assert.equal(request.method(), 'POST'); writes.push(path);
      semanticJob = { ...semanticJob, status: 'stopped', queue: { state: 'terminal' } };
      return route.fulfill({ json: { task: semanticJob } });
    }
    if (path === '/api/projects/reference-test/reverse-analyses/analysis-test') return route.fulfill({ json: complete ? { ...queued, status: 'ready', import_status: 'succeeded', import_task: { ...queued.import_task, status: 'succeeded', queue: { state: 'terminal' } }, source: { ...queued.source, duration_seconds: 5, width: 640, height: 640 }, timeline: [{ id: 'R01', start_seconds: 0, end_seconds: 5, status: 'draft', facts: [], inference: '可人工校订', user_note: '' }] } : queued });
    if (path === '/api/models/reverse-analysis') return route.fulfill({ json: {} });
    if (path === '/media-file') return route.fulfill({ status: 204 });
    assert.equal(request.method(), 'GET', 'must not start semantic jobs while polling');
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  await page.goto('http://localhost:4195/');
  await page.getByRole('button', { name: '导入参考素材' }).waitFor();
  assert.equal(writes.length, 0);
  await page.locator('input[type=file]').setInputFiles({ name: 'my-reference.mp4', mimeType: 'video/mp4', buffer: Buffer.from('fake offline bytes') });
  await page.getByRole('status', { name: '任务排队 analysis-test' }).getByText(/等待共享 GPU 执行槽 · 队列位置 3/).waitFor();
  await page.getByRole('button', { name: '停止参考拆分' }).waitFor();
  assert.ok(await page.getByRole('button', { name: '转为制作草案' }).isDisabled());
  complete = true;
  await page.getByText('参考素材拆分已完成，等待人工校订；不会自动提交内容分析。').waitFor();
  await page.getByText('可人工校订').waitFor();
  assert.equal(await page.getByRole('button', { name: '停止参考拆分' }).count(), 0);
  assert.deepEqual(writes, ['/api/projects/reference-test/reverse-analyses']);
  // A job admitted by an Agent is visible and cancellable through the same public task API.
  semanticJob = { id: 'semantic-job', task_id: 'semantic-task', status: 'queued', queue: { state: 'waiting_resource', reason: { code: 'external_queue_busy', message: '等待已有 ComfyUI 工作结束' }, position: 2 } };
  await page.getByRole('status', { name: '任务排队 analysis-test' }).getByText(/等待已有 ComfyUI 工作结束 · 队列位置 2/).waitFor();
  await page.getByRole('button', { name: '停止内容分析' }).click();
  await page.getByText('已请求停止内容分析，等待服务器确认；原参考和人工校订保留。').waitFor();
  assert.deepEqual(writes, ['/api/projects/reference-test/reverse-analyses', '/api/projects/reference-test/tasks/semantic-task/stop']);
  assert.equal(await page.getByRole('button', { name: '停止内容分析' }).count(), 0);
  assert.deepEqual(errors, []);
  console.log('Reference queue browser: explicit upload, waiting resource receipt, durable poll completion, Agent job cancel through shared task API, no automatic semantic/GPU submission passed');
} finally { await browser.close(); }
