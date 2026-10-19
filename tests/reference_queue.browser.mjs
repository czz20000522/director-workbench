// Offline reference registration UI: fake upload and all HTTP intercepted.
// Upload must only register media facts; observations/ASR/conversion are explicit.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import ReverseWorkbench from './src/ReverseWorkbench';createRoot(document.getElementById('root')).render(<ReverseWorkbench projectId="reference-test" onConverted={()=>{}}/>);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const writes = []; const errors = []; let semanticJob = null;
  page.on('pageerror', error => errors.push(error.message));
  const registered = {
    id: 'analysis-test', title: '参考样例', status: 'registered',
    source: { original_name: 'my-reference.mp4', path: 'input/reference.mp4', duration_seconds: 5, width: 640, height: 640, fps: 24, has_audio: true, has_video: true, registered: true },
    stages: [{ id: 'import', label: '参考素材登记', status: 'done' }, { id: 'observation', label: '显式观察工具', status: 'ready' }],
    timeline: [], artifacts: {},
  };
  await page.route('http://localhost:4195/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    if (path === '/api/projects/reference-test/reverse-analyses') {
      if (request.method() === 'POST') { writes.push(path); return route.fulfill({ status: 201, json: registered }); }
      return route.fulfill({ json: { analyses: [] } });
    }
    if (path.endsWith('/semantic-jobs/current')) return route.fulfill({ json: { job: semanticJob } });
    if (path === '/api/projects/reference-test/tasks/semantic-task/stop') {
      assert.equal(request.method(), 'POST'); writes.push(path);
      semanticJob = { ...semanticJob, status: 'stopped', queue: { state: 'terminal' } };
      return route.fulfill({ json: { task: semanticJob } });
    }
    if (path === '/api/projects/reference-test/reverse-analyses/analysis-test') return route.fulfill({ json: registered });
    if (path === '/api/models/reverse-analysis') return route.fulfill({ json: {} });
    if (path === '/media-file') return route.fulfill({ status: 204 });
    assert.equal(request.method(), 'GET', 'must not start semantic/observation jobs while polling');
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  await page.goto('http://localhost:4195/');
  await page.getByRole('button', { name: '导入参考素材' }).waitFor();
  assert.equal(writes.length, 0);
  await page.locator('input[type=file]').setInputFiles({ name: 'my-reference.mp4', mimeType: 'video/mp4', buffer: Buffer.from('fake offline bytes') });
  await page.getByText('源素材my-reference.mp4包含音轨 · 已生成 0 个分析段').waitFor();
  assert.equal(await page.getByRole('button', { name: '停止参考拆分' }).count(), 0);
  assert.equal(await page.getByRole('button', { name: /转为制作草案/ }).isDisabled(), true);
  assert.deepEqual(writes, ['/api/projects/reference-test/reverse-analyses']);
  // An Agent-started semantic observation is visible and cancellable through the shared task API.
  semanticJob = { id: 'semantic-job', task_id: 'semantic-task', status: 'queued', queue: { state: 'waiting_resource', reason: { code: 'external_queue_busy', message: '等待已有 ComfyUI 工作结束' }, position: 2 } };
  await page.getByRole('status', { name: '任务排队 analysis-test' }).getByText(/等待已有 ComfyUI 工作结束 · 队列位置 2/).waitFor();
  await page.getByRole('button', { name: '停止内容分析' }).click();
  await page.getByText('已请求停止内容分析，等待服务器确认；原参考和人工校订保留。').waitFor();
  assert.deepEqual(writes, ['/api/projects/reference-test/reverse-analyses', '/api/projects/reference-test/tasks/semantic-task/stop']);
  assert.equal(await page.getByRole('button', { name: '停止内容分析' }).count(), 0);
  assert.deepEqual(errors, []);
  console.log('Reference registration browser: upload-only, no auto split/ASR, explicit observation/conversion gate, Agent job cancel passed');
} finally { await browser.close(); }
