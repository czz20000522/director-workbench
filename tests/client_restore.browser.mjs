// Fully intercepted app/HTTP/SSE; actual decodable MP4 served read-only from a test argument.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { build } from 'esbuild';
import { chromium } from 'playwright';

assert.ok(process.argv[2], 'Pass a local decodable MP4 test fixture');
const videoBytes = readFileSync(process.argv[2]);
const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import App from './src/App';window.streams=[];window.EventSource=class{constructor(url){this.url=url;window.streams.push(this)}close(){window.streams=window.streams.filter(x=>x!==this)}};createRoot(document.getElementById('root')).render(<App privateMode/>);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /\.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const project = { id: 'restore-test', title: '客户端恢复验收', series: '隔离测试', assembly_asset_id: 'FULL', assets: [], pipeline: [] };
  let tasks = [{ id: 'a1', project_id: project.id, asset_id: 'S01', status: 'running', batch_id: 'persisted-batch', sequence: 1, created_at: 100 }, { id: 'a2', project_id: project.id, asset_id: 'S02', status: 'batch_waiting', batch_id: 'persisted-batch', sequence: 2, created_at: 100 }];
  const plan = { revision: 1, segments: ['S01','S02'].map((id,index) => ({ id, start_seconds: index * 7.3, end_seconds: (index+1)*7.3, duration_seconds: 7.3, status: 'video_generated', dependencies: [], prompt: 'test', keyframes: {}, audio: {}, video: { path: `fixtures/${id}.mp4` } })), assembly: null };
  let heldPlan;
  let delayPlan = true;
  let requestCount = 0;
  await page.route('http://localhost:4193/**', async route => {
    const path = new URL(route.request().url()).pathname;
    assert.equal(route.request().method(), 'GET', 'Restoration must not submit or alter tasks');
    requestCount++;
    if (path === '/api/project') return route.fulfill({ json: project });
    if (path === '/api/projects') return route.fulfill({ json: { projects: [project] } });
    if (path === '/api/status') return route.fulfill({ json: { comfy: { online: true } } });
    if (path === '/api/projects/restore-test/plan') {
      if (delayPlan) { heldPlan = route; return; }
      return route.fulfill({ json: plan });
    }
    if (path === '/api/projects/restore-test/state') return route.fulfill({ json: { project_id: project.id, reviews: {}, checkpoints: {}, artifacts: [], history: [], readiness: { schema_version: 1, revision: 1, source: 'saved', segments: [], assembly: { ready: false, state: 'blocked', blockers: [], next_actions: [] }, next_actions: [] } } });
    if (path === '/api/projects/restore-test/pipeline') return route.fulfill({ json: { stages: [] } });
    if (path === '/api/projects/restore-test/tasks') return route.fulfill({ json: { project_id: project.id, tasks, next_offset: null } });
    if (path.endsWith('/versions')) return route.fulfill({ json: { items: [], total: 0 } });
    if (path.endsWith('/assembly/edit')) return route.fulfill({ status: 404, json: { detail: 'fixture has no edit' } });
    if (path === '/api/settings/guide') return route.fulfill({ json: { status: 'active', completed_steps: [], revision: 0, steps: [] } });
    if (path === '/api/production-presets') return route.fulfill({ json: { presets: [] } });
    if (path === '/media-file') {
      const name = new URL(route.request().url()).searchParams.get('path');
      if (name?.includes('missing')) return route.fulfill({ status: 404 });
      return route.fulfill({ contentType: 'video/mp4', body: videoBytes });
    }
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  await page.goto('http://localhost:4193/');
  // Quick mode is the default and hides the whole operations section on the delivery
  // page, batch panel included, so leave it before looking for anything below.
  await page.getByRole('button', { name: '作品、片段与更多功能', exact: true }).click();
  await page.getByRole('button', { name: '任务与成片', exact: true }).click();
  await page.waitForFunction(() => document.body.textContent.includes('客户端恢复验收'));
  while (!heldPlan) await page.waitForTimeout(10);
  delayPlan = false;
  await heldPlan.fulfill({ json: plan });
  await page.getByText('2 个分镜已登记', { exact: true }).waitFor();
  const batchPanel = page.locator('.director-delivery-panel');
  const batchRestored = !(await batchPanel.textContent()).includes('未运行批次') && await batchPanel.getByRole('button', { name: '启动可执行片段批次' }).isDisabled();
  await page.getByRole('button', { name: '分镜制作', exact: true }).click();
  const preview = page.getByRole('region', { name: '当前片段预览', exact: true });
  await preview.locator('video').waitFor();
  await page.waitForFunction(() => { const video=document.querySelector('video[aria-label="S01 视频预览"]');return video?.readyState>=3 && !video.error; });
  const outerReady = await preview.getByRole('button', { name: '播放当前片段', exact: true }).isEnabled();
  console.log(JSON.stringify({ reproduced_batch_restore_gap: !batchRestored, reproduced_player_ready_gap: !outerReady, native_media: await preview.locator('video').evaluate(v => ({ readyState: v.readyState, duration: v.duration, width: v.videoWidth, error: v.error?.code })) }));
  assert.ok(batchRestored, 'Persisted Agent batch must restore its status and stop action after project load');
  assert.ok(outerReady, 'Mounted decodable media must enable its external player after deferred plan load');
  await preview.getByRole('button', { name: '播放当前片段', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('video[aria-label="S01 视频预览"]')?.currentTime > 0.1);
  await page.evaluate(records => window.streams.forEach(stream => stream.onmessage?.({ data: JSON.stringify({ tasks: records }) })), tasks);
  assert.ok(await preview.getByRole('button', { name: '暂停当前片段', exact: true }).isEnabled());
  await preview.getByRole('button', { name: '暂停当前片段', exact: true }).click();
  await preview.getByRole('button', { name: '下一段', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('video[aria-label="S02 视频预览"]')?.readyState >= 3);
  assert.ok(await preview.getByRole('button', { name: '播放当前片段', exact: true }).isEnabled());
  await preview.getByRole('button', { name: '上一段', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('video[aria-label="S01 视频预览"]')?.readyState >= 3);
  assert.ok(await preview.getByRole('button', { name: '播放当前片段', exact: true }).isEnabled());
  // Remount same cached source after leaving the page; retain a real ready gate.
  await page.getByRole('button', { name: '任务与成片', exact: true }).click();
  await batchPanel.getByRole('button', { name: '停止批次', exact: true }).waitFor();
  await page.getByRole('button', { name: '分镜制作', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('video[aria-label="S01 视频预览"]')?.readyState >= 3);
  assert.ok(await preview.getByRole('button', { name: '播放当前片段', exact: true }).isEnabled());
  // Polling restores terminal results, rather than retaining an obsolete running batch.
  tasks = tasks.map((row,index) => ({ ...row, status: index ? 'stopped' : 'succeeded' }));
  await page.evaluate(records => window.streams.forEach(stream => stream.onmessage?.({ data: JSON.stringify({ tasks: records }) })), tasks);
  await page.getByRole('button', { name: '任务与成片', exact: true }).click();
  await batchPanel.getByRole('button', { name: '恢复批次', exact: true }).waitFor();
  assert.ok(await batchPanel.getByRole('button', { name: '启动可执行片段批次' }).isEnabled());
  await page.reload();
  // A reload drops the app back into its default quick mode, so leave it again.
  await page.getByRole('button', { name: '作品、片段与更多功能', exact: true }).click();
  await page.getByRole('button', { name: '任务与成片', exact: true }).click();
  await batchPanel.getByRole('button', { name: '恢复批次', exact: true }).waitFor();
  // Replace the source through a new persisted task/plan snapshot: errors must stay gated.
  plan.segments[0].video.path = 'fixtures/missing.mp4';
  tasks[0] = { ...tasks[0], id: 'replacement-error' };
  await page.evaluate(records => window.streams.forEach(stream => stream.onmessage?.({ data: JSON.stringify({ tasks: records }) })), tasks);
  await page.getByRole('button', { name: '分镜制作', exact: true }).click();
  await page.waitForFunction(() => Boolean(document.querySelector('video[aria-label="S01 视频预览"]')?.error));
  await page.getByText('素材加载失败，请检查本机作品文件。', { exact: true }).waitFor();
  assert.ok(await preview.getByRole('button', { name: /当前片段$/, exact: false }).filter({ has: page.locator('svg') }).nth(0).isDisabled());
  plan.segments[0].video.path = 'fixtures/S01.mp4';
  tasks[0] = { ...tasks[0], id: 'replacement-ok' };
  await page.evaluate(records => window.streams.forEach(stream => stream.onmessage?.({ data: JSON.stringify({ tasks: records }) })), tasks);
  await page.waitForFunction(() => { const v=document.querySelector('video[aria-label="S01 视频预览"]');return v?.readyState>=3 && !v.error; });
  assert.ok(await preview.getByRole('button', { name: '播放当前片段', exact: true }).isEnabled());
  await preview.getByLabel('连续播放', { exact: true }).check();
  await preview.getByRole('button', { name: '播放当前片段', exact: true }).click();
  await preview.locator('video').evaluate(v => { v.currentTime = v.duration - 0.2; });
  await page.waitForFunction(() => { const v=document.querySelector('video[aria-label="S02 视频预览"]');return v?.readyState>=3 && !v.paused; });
  assert.ok(await preview.getByRole('button', { name: '暂停当前片段', exact: true }).isEnabled());
  assert.deepEqual(errors, []);
  console.log(`Client restoration browser passed: persisted batch/terminal results, deferred mount, real MP4 play/poll/switch/cache/page-reentry/reload/error/change-source/continuous; ${requestCount} reads, zero creative writes`);
} finally { await browser.close(); }
