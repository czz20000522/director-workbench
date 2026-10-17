// Full LoginGate/App with actual private ASGI HTTP through stdin; no socket, production DB or GPU.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const child = spawn('./.venv/Scripts/python.exe', ['tests/agent_access_bridge.py'], { env: { ...process.env, PYTHONPATH: process.cwd(), PYTHONIOENCODING: 'utf-8' }, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
let stderr = ''; child.stderr.on('data', data => { stderr += data; });
const pending = []; let readyResolve;
const ready = new Promise(resolve => { readyResolve = resolve; });
createInterface({ input: child.stdout }).on('line', line => { const value = JSON.parse(line); if (value.ready) readyResolve(value); else pending.shift()?.(value); });
child.on('exit', code => { if (code) for (const resolve of pending.splice(0)) resolve({ status: 500, data: { detail: stderr } }); });
function dispatch(request) { return new Promise(resolve => { pending.push(resolve); child.stdin.write(JSON.stringify(request) + '\n'); }); }
function agent(method, path, body) { return dispatch({ channel: 'agent', method, path, ...(body === undefined ? {} : { body }) }); }
let browser;
try {
  const initial = await Promise.race([ready, new Promise((_, reject) => setTimeout(() => reject(new Error(stderr || 'ASGI bridge startup timed out')), 20000))]);
  const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import LoginGate from './src/LoginGate';window.EventSource=class{close(){}};createRoot(document.getElementById('root')).render(<React.StrictMode><LoginGate/></React.StrictMode>);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /\.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });
  browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--autoplay-policy=no-user-gesture-required'] });
  const context = await browser.newContext();
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const errors = [], writes = [], pageReads = []; page.on('pageerror', error => errors.push(error.message));
  const routeToASGI = async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/agent/') || url.pathname.startsWith('/.well-known/') || url.pathname === '/media-file') {
      const headers = request.headers();
      if (request.method() === 'GET' && url.pathname.startsWith('/api/agent/pages/')) pageReads.push({ path: url.pathname, pageKey: headers['x-agent-page-key'] });
      if (request.method() !== 'GET') writes.push({ path: url.pathname, headers, body: request.postData() ? request.postDataJSON() : undefined });
      const response = await dispatch({ channel: 'browser', method: request.method(), path: url.pathname + url.search, headers, ...(request.postData() ? { body: request.postDataJSON() } : {}) });
      return response.binary === undefined ? route.fulfill({ status: response.status, headers: response.headers, json: response.data }) : route.fulfill({ status: response.status, headers: response.headers, body: Buffer.from(response.binary, 'base64') });
    }
    return route.fulfill({ contentType: 'text/html', body: `<title>Actual Agent browser</title><div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  };
  await page.route('http://localhost:4196/**', routeToASGI);
  await page.goto('http://localhost:4196/');
  await page.getByLabel('用户名', { exact: true }).fill('user001');
  await page.getByLabel('密码', { exact: true }).fill('one');
  await page.getByRole('button', { name: '进入工作台', exact: true }).click();
  await page.getByText('Agent 页面操作', { exact: true }).click();
  const permission = page.getByRole('checkbox', { name: '允许 Agent 引导此标签页' });
  await permission.waitFor();
  await page.waitForFunction(() => !document.querySelector('[aria-label="Agent 标签页引导"] input').disabled);
  assert.equal(await permission.isChecked(), false);
  assert.ok(writes.filter(write => write.path === '/api/agent/pages').every(write => !write.headers.authorization && write.headers.cookie && write.headers['x-workspace-user'] === 'user001' && write.headers.origin === 'http://localhost:4196'));
  await permission.click(); await page.waitForFunction(() => document.querySelector('[aria-label="Agent 标签页引导"] input').checked);
  const available = await agent('GET', '/api/agent/pages'); assert.equal(available.status, 200);
  const pageId = available.data.pages.find(item => item.enabled).page_id;
  assert.ok(!JSON.stringify(available.data).includes('page_key'));
  const created = await agent('POST', '/api/projects/create', { series: 'Isolated', title: 'External Agent visible project', creation_mode: 'auto', select: false });
  assert.equal(created.status, 200); const pid = created.data.project.id;
  assert.notEqual(pid, initial.project_id);
  let sequence = 0;
  async function present(kind, target = {}, values = {}, receivingPage = page, receivingPageId = pageId) {
    const id = `browser-action-${++sequence}`;
    const response = await agent('POST', '/api/agent/page-actions', { page_id: receivingPageId, action_id: id, kind, target: { project_id: pid, ...target }, values });
    assert.equal(response.status, 200, JSON.stringify(response.data));
    for (let attempts = 0; attempts < 90; attempts++) {
      const receipt = (await agent('GET', `/api/agent/pages/${receivingPageId}/actions?after=0`)).data.actions.find(item => item.id === id);
      if (receipt.status !== 'pending') return receipt;
      await receivingPage.waitForTimeout(100);
    }
    assert.fail(`Timed out waiting for actual App presentation: ${kind}; browser errors ${JSON.stringify(errors)}`);
  }
  const prompt = '夜色下的小猫坐在窗边，看着雨滴滑落。\n镜头缓慢靠近。';
  const draftReceipt = await present('show_draft', { asset_id: 'S01', revision: 0 }, { prompt, duration_seconds: 7.3, generation: { mode: 'auto', aspect_ratio: '9:16', seed: 42 } });
  assert.equal(draftReceipt.status, 'presented', JSON.stringify(draftReceipt));
  const editor = page.getByRole('region', { name: '添加分镜', exact: true });
  assert.equal(await editor.getByLabel('画面与动作描述').inputValue(), prompt);
  assert.equal(await editor.getByLabel('剧情时长（秒）').inputValue(), '7.3');
  assert.equal(await editor.getByLabel('目标画幅').inputValue(), '9:16');
  assert.equal((await agent('GET', `/api/projects/${pid}/plan`)).data.segments.length, 0, 'Presentation never saves a draft');
  assert.equal((await agent('GET', `/api/projects/${pid}/tasks`)).data.tasks.length, 0);
  // Agent saves exactly once through the existing business API, then App reads the saved shot.
  const saved = await agent('POST', `/api/projects/${pid}/plan/segments`, { segment_id: 'S01', prompt, duration_seconds: 7.3, generation: { mode: 'auto', aspect_ratio: '9:16', seed: 42 }, expected_revision: 0 });
  assert.equal(saved.status, 200, JSON.stringify(saved.data)); const revision = saved.data.plan.revision;
  assert.equal((await present('locate', { asset_id: 'S01', revision })).status, 'presented', 'Agent-filled draft must not be classified as manual dirty input');
  await page.getByLabel('当前分镜提示词', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('当前分镜提示词', { exact: true }).inputValue(), prompt);
  const checked = await agent('POST', `/api/projects/${pid}/pipeline/auto/validate`, { asset_id: 'S01', values: {} });
  assert.equal(checked.status, 200); assert.equal(checked.data.valid, true); assert.equal(checked.data.h3_duration.frame_count, 175);
  assert.equal((await present('show_preflight', { asset_id: 'S01', revision })).status, 'presented');
  await page.getByText('已定位原准备度和服务端阻塞原因；页面定位未额外提交生成。', { exact: true }).waitFor();
  const readiness = (await agent('GET', `/api/projects/${pid}/state`)).data.readiness.segments.find(item => item.asset_id === 'S01');
  const readinessPanel = page.locator('.director-readiness').filter({ has: page.getByRole('button', { name: '预检未保存输入', exact: true }) });
  assert.equal(await readinessPanel.isVisible(), true, 'The existing readiness panel is visible, beyond the Agent status caption');
  if (readiness.ready) assert.match(await readinessPanel.textContent(), /生成准备度已满足/);
  else for (const blocker of readiness.blockers.filter(item => item.message)) assert.ok((await readinessPanel.textContent()).includes(blocker.message));
  assert.equal(writes.filter(write => write.path.endsWith('/validate')).length, 0, 'Page presentation never repeats Agent preflight');
  const submitted = await agent('POST', `/api/projects/${pid}/tasks`, { asset_id: 'S01', pipeline_stage_id: 'auto', expected_revision: revision, idempotency_key: 'actual-agent-browser-one' });
  assert.equal(submitted.status, 200, JSON.stringify(submitted.data)); const taskId = submitted.data.id;
  assert.equal((await present('show_task', { asset_id: 'S01', task_id: taskId, revision })).status, 'presented');
  await page.getByText(new RegExp(`任务 ${taskId} ·`)).waitFor();
  assert.equal(writes.filter(write => write.path.endsWith('/tasks')).length, 0, 'Page presentation never repeats Agent generation');
  const complete = await dispatch({ operation: 'complete', project_id: pid, task_id: taskId }); assert.equal(complete.status, 200, JSON.stringify(complete.data));
  assert.equal((await present('open_candidate', { asset_id: 'S01', task_id: taskId })).status, 'presented');
  const media = page.locator('video[aria-label="S01 视频预览"]');
  await page.waitForFunction(() => document.querySelector('video[aria-label="S01 视频预览"]')?.readyState >= 3);
  const mediaSource = await media.getAttribute('src'); assert.ok(mediaSource.includes(taskId));
  await page.evaluate(() => { window.originalPlay = HTMLMediaElement.prototype.play; HTMLMediaElement.prototype.play = function () { return Promise.reject(new DOMException('Browser blocked autoplay', 'NotAllowedError')); }; });
  assert.equal((await present('play', { asset_id: 'S01', task_id: taskId })).status, 'not_presented');
  assert.equal(await media.evaluate(element => element.paused), true);
  await page.evaluate(() => { HTMLMediaElement.prototype.play = window.originalPlay; });
  assert.equal((await present('play', { asset_id: 'S01', task_id: taskId })).status, 'presented');
  await page.waitForFunction(() => document.querySelector('video[aria-label="S01 视频预览"]')?.currentTime > .1);
  assert.equal(await media.getAttribute('src'), mediaSource, 'Only the intended persisted candidate plays');
  assert.equal((await present('pause', { asset_id: 'S01', task_id: taskId })).status, 'presented');
  assert.equal(await media.evaluate(element => element.paused), true);
  const missing = await agent('POST', '/api/agent/page-actions', { page_id: pageId, action_id: 'missing-target-action', kind: 'play', target: { project_id: pid, asset_id: 'missing-shot' }, values: {} });
  assert.equal(missing.status, 404); assert.equal(await media.evaluate(element => element.paused), true);
  // Same account and shared Cookie jar, separate tabs/page IDs/nonces: only page two gets this draft.
  const firstPageSnapshot = {
    prompt: await page.getByLabel('当前分镜提示词', { exact: true }).inputValue(),
    currentPage: await page.locator('.director-topnav button.active').textContent(),
    source: await media.getAttribute('src'), time: await media.evaluate(element => element.currentTime),
    caption: await page.getByRole('status', { name: 'Agent 页面进度' }).textContent(),
  };
  const second = await page.context().newPage(); second.setDefaultTimeout(10000);
  second.on('pageerror', error => errors.push(error.message));
  await second.route('http://localhost:4196/**', routeToASGI);
  await second.goto('http://localhost:4196/');
  await second.getByText('Agent 页面操作', { exact: true }).click();
  const secondPermission = second.getByRole('checkbox', { name: '允许 Agent 引导此标签页' });
  await secondPermission.waitFor();
  await second.waitForFunction(() => !document.querySelector('[aria-label="Agent 标签页引导"] input').disabled && document.querySelector('video[aria-label="S01 视频预览"]')?.readyState >= 3);
  assert.equal(await secondPermission.isChecked(), false);
  const secondId = await second.getByLabel('页面编号', { exact: true }).textContent();
  assert.notEqual(secondId, pageId);
  assert.equal(writes.filter(write => write.path === '/api/auth/login').length, 1, 'Second tab uses the actual shared login Cookie, without another login');
  await secondPermission.click();
  await second.waitForFunction(() => document.querySelector('[aria-label="Agent 标签页引导"] input').checked);
  const twoPages = (await agent('GET', '/api/agent/pages')).data.pages;
  assert.equal(twoPages.filter(item => item.enabled).length, 2);
  const secondPrompt = '第二个标签页独立草稿；仅填入，不保存、不生成。';
  const secondReceipt = await present('show_draft', { asset_id: 'S02' }, { prompt: secondPrompt, duration_seconds: 5, generation: { mode: 'auto', aspect_ratio: '16:9', seed: 42 } }, second, secondId);
  assert.equal(secondReceipt.status, 'presented', JSON.stringify(secondReceipt));
  assert.equal(secondReceipt.confirmation, 'client_report');
  assert.equal(secondReceipt.verification, 'not_attested');
  const secondDraft = second.getByRole('region', { name: '添加分镜', exact: true });
  assert.equal(await secondDraft.getByLabel('画面与动作描述').inputValue(), secondPrompt);
  const firstAck = writes.find(write => write.path.startsWith(`/api/agent/pages/${pageId}/actions/`) && write.path.endsWith('/ack'));
  const secondAck = writes.find(write => write.path.startsWith(`/api/agent/pages/${secondId}/actions/`) && write.path.endsWith('/ack'));
  assert.ok(firstAck && secondAck);
  assert.notEqual(firstAck.headers['x-agent-page-key'], secondAck.headers['x-agent-page-key']);
  assert.equal(await page.getByLabel('当前分镜提示词', { exact: true }).inputValue(), firstPageSnapshot.prompt);
  assert.equal(await page.locator('.director-topnav button.active').textContent(), firstPageSnapshot.currentPage);
  assert.equal(await media.getAttribute('src'), firstPageSnapshot.source);
  assert.equal(await media.evaluate(element => element.paused), true);
  assert.equal(await media.evaluate(element => element.currentTime), firstPageSnapshot.time);
  assert.equal(await page.getByRole('status', { name: 'Agent 页面进度' }).textContent(), firstPageSnapshot.caption);
  const secondAckCount = writes.filter(write => write.path.startsWith(`/api/agent/pages/${secondId}/actions/`) && write.path.endsWith('/ack')).length;
  const firstNonce = secondAck.headers['x-agent-page-key'];
  await second.reload();
  await second.getByText('Agent 页面操作', { exact: true }).click();
  await secondPermission.waitFor();
  await second.waitForFunction(() => !document.querySelector('[aria-label="Agent 标签页引导"] input').disabled);
  assert.equal(await second.getByLabel('页面编号', { exact: true }).textContent(), secondId);
  assert.equal(await secondPermission.isChecked(), false);
  assert.equal(await secondDraft.count(), 0, 'Second-tab refresh reads the old receipt without replaying draft/navigation/playback');
  assert.equal(writes.filter(write => write.path.startsWith(`/api/agent/pages/${secondId}/actions/`) && write.path.endsWith('/ack')).length, secondAckCount);
  assert.notEqual(pageReads.filter(read => read.path === `/api/agent/pages/${secondId}/actions`).at(-1).pageKey, firstNonce, 'Refresh starts a new page nonce');
  assert.equal(await permission.isChecked(), true, 'Refreshing page two does not revoke page one');
  assert.equal(await page.getByLabel('当前分镜提示词', { exact: true }).inputValue(), firstPageSnapshot.prompt);
  assert.equal(await media.evaluate(element => element.paused), true);
  const allProjects = (await agent('GET', '/api/projects')).data.projects;
  const taskCounts = await Promise.all(allProjects.map(async item => (await agent('GET', `/api/projects/${item.id}/tasks`)).data.tasks.length));
  assert.equal(taskCounts.reduce((total, count) => total + count, 0), 1, 'Two-tab presentation creates no extra generation task');
  await second.close();
  // Test-only result identity changes the current candidate, without generating another task.
  const historyFixture = await dispatch({ operation: 'historical', project_id: pid, task_id: taskId });
  assert.equal(historyFixture.status, 200, JSON.stringify(historyFixture.data));
  const oldVersion = (await agent('GET', `/api/projects/${pid}/segments/S01/versions`)).data.items.find(item => item.task_id === taskId);
  assert.equal(oldVersion.is_current, false, 'The requested task is now an actual historical version');
  for (const kind of ['open_candidate', 'play', 'pause']) {
    const receipt = await present(kind, { asset_id: 'S01', task_id: taskId });
    assert.equal(receipt.status, 'not_presented', JSON.stringify(receipt));
    assert.match(receipt.message, /指定任务未确认为当前候选/);
    assert.equal(await media.getAttribute('src'), firstPageSnapshot.source);
    assert.equal(await media.evaluate(element => element.paused), true);
    assert.equal(await media.evaluate(element => element.currentTime), firstPageSnapshot.time);
    assert.equal(await page.locator('.director-topnav button.active').textContent(), firstPageSnapshot.currentPage);
    assert.equal(await page.getByLabel('当前分镜提示词', { exact: true }).inputValue(), firstPageSnapshot.prompt);
  }
  // A real original-navigation click revokes page permission before any later Agent action.
  const taskBeforeTakeover = (await agent('GET', `/api/projects/${pid}/tasks/${taskId}`)).data;
  await page.getByRole('navigation', { name: '工作台功能' }).getByRole('button', { name: '剧本与场次', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('[aria-label="Agent 标签页引导"] input').checked && !document.querySelector('[aria-label="Agent 标签页引导"] input').disabled);
  assert.equal((await agent('GET', '/api/agent/pages')).data.pages.find(item => item.page_id === pageId).enabled, false);
  const refusedNavigation = await agent('POST', '/api/agent/page-actions', { page_id: pageId, action_id: 'after-human-navigation', kind: 'navigate', target: { project_id: pid }, values: { page: 'shots' } });
  assert.equal(refusedNavigation.status, 409, JSON.stringify(refusedNavigation.data));
  assert.equal(await page.locator('.director-topnav button.active').textContent(), '剧本与场次');
  assert.equal((await agent('GET', `/api/projects/${pid}/tasks/${taskId}`)).data.status, taskBeforeTakeover.status);
  assert.equal(writes.filter(write => /\/tasks\/[^/]+\/(stop|cancel)$/.test(write.path)).length, 0, 'Page takeover never stops or cancels the generation task');
  // Explicitly allowing guidance again permits a fresh action; no old actions replay.
  await permission.click();
  await page.waitForFunction(() => document.querySelector('[aria-label="Agent 标签页引导"] input').checked && !document.querySelector('[aria-label="Agent 标签页引导"] input').disabled);
  assert.equal((await present('locate', { asset_id: 'S01' })).status, 'presented');
  assert.equal(await page.locator('.director-topnav button.active').textContent(), '分镜制作');
  // Editing the original field also revokes permission, preserving the human input.
  await page.getByLabel('当前分镜提示词', { exact: true }).fill('人工编辑的原文必须保留');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Agent 标签页引导"] input').checked && !document.querySelector('[aria-label="Agent 标签页引导"] input').disabled);
  const refusedEdit = await agent('POST', '/api/agent/page-actions', { page_id: pageId, action_id: 'after-human-edit', kind: 'navigate', target: { project_id: pid }, values: { page: 'script' } });
  assert.equal(refusedEdit.status, 409, JSON.stringify(refusedEdit.data));
  assert.equal(await page.getByLabel('当前分镜提示词', { exact: true }).inputValue(), '人工编辑的原文必须保留');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Agent 标签页引导"] input').checked);
  const beforeReload = writes.filter(write => write.path.endsWith('/ack')).length;
  await page.reload();
  await page.getByText('Agent 页面操作', { exact: true }).click();
  await permission.waitFor();
  await page.waitForFunction(() => !document.querySelector('[aria-label="Agent 标签页引导"] input').disabled);
  assert.equal(await permission.isChecked(), false);
  assert.equal(writes.filter(write => write.path.endsWith('/ack')).length, beforeReload, 'Reload never replays history');
  assert.equal((await agent('GET', `/api/projects/${pid}/tasks`)).data.tasks.length, 1);
  assert.equal(writes.filter(write => write.path.endsWith('/plan/segments')).length, 0);
  assert.ok(writes.filter(write => write.path.endsWith('/ack')).every(write => write.headers['x-agent-page-key'] && write.headers.cookie && !write.headers.authorization));
  assert.deepEqual(errors, []);
  console.log('Actual full App/private ASGI browser passed: Cookie login/Origin/user, new project async selection, original unsaved draft then one Agent save/preflight/task, original field readback, real current-task candidate decode/play/pause, autoplay rejection, two shared-Cookie tabs with independent IDs/nonces/no cross-page actions, refresh/no replay, historical task open/play/pause rejected without fallback/restore, trusted original navigation/edit disable server page and reject subsequent actions with 409, explicit re-enable works, exactly one task/no duplicate writes or task cancellation. Fixture: ' + initial.root);
} finally {
  if (browser) await browser.close();
  child.stdin.end();
  await new Promise(resolve => { if (child.exitCode !== null) resolve(); else child.once('exit', resolve); });
}
