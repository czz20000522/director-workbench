// Full App + cookie login + real private API + isolated CPU fixtures; no GPU/cloud.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';
const require = createRequire(resolve(process.env.DIRECTOR_TEST_NODE_MODULES || 'node_modules', 'package.json'));
const { build } = require('esbuild');
const { chromium } = require('playwright');
assert.ok(process.argv[2] && process.argv[3], 'Pass project Python and a new isolated runtime path');
const fixture = spawn(process.argv[2], ['tests/quick_generation_bridge.py', process.argv[3]], {
  env: { ...process.env, PYTHONPATH: process.cwd(), PYTHONNOUSERSITE: '1' }, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
});
let stderr = ''; fixture.stderr.on('data', chunk => { stderr += chunk; });
const lines = [], waiters = [];
createInterface({ input: fixture.stdout }).on('line', line => {
  const value = JSON.parse(line); const waiter = waiters.shift(); if (waiter) waiter(value); else lines.push(value);
});
const next = () => lines.length ? Promise.resolve(lines.shift()) : new Promise((resolve, reject) => {
  waiters.push(resolve); setTimeout(() => reject(new Error(`Fixture timeout: ${stderr}`)), 30000).unref();
});
let lock = Promise.resolve();
const call = value => {
  const result = lock.then(async () => { fixture.stdin.write(JSON.stringify(value) + '\n'); return next(); });
  lock = result.catch(() => {}); return result;
};
const bundle = await build({ entryPoints: ['src/main.tsx'], absWorkingDir: process.cwd(), bundle: true,
  outfile: 'bundle.js', write: false, jsx: 'automatic', nodePaths: [process.env.DIRECTOR_TEST_NODE_MODULES || resolve('node_modules')] });
const js = bundle.outputFiles.find(file => file.path.endsWith('.js')).text;
const css = bundle.outputFiles.find(file => file.path.endsWith('.css')).text;
const browser = await chromium.launch({ channel: process.env.DIRECTOR_TEST_BROWSER_CHANNEL || 'msedge', headless: true });
let page;
try {
  const ready = await next(); assert.equal(ready.ready, true);
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const writes = [], errors = [];
  let loseImageResponse = false, loseProjectResponse = true, loseAssemblyResponse = false;
  // A hold pairs "the driver is waiting to see this request" with "the request is still unanswered",
  // so it is armed as one object: arming is a single write, and the handler reads it once on entry.
  const arm = () => {
    let arrived, release;
    return { arrival: new Promise(resolve => { arrived = resolve; }),
      gate: new Promise(resolve => { release = resolve; }), arrived, release };
  };
  let holdPlan = null, holdState = null;
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    assert.equal(url.origin, 'http://localhost:4197', 'Never contact live services');
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><link rel="stylesheet" href="/bundle.css"><script src="/bundle.js"></script>' });
    if (url.pathname === '/bundle.js') return route.fulfill({ contentType: 'application/javascript', body: js });
    if (url.pathname === '/bundle.css') return route.fulfill({ contentType: 'text/css', body: css });
    if (url.pathname.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: '' });
    if (!url.pathname.startsWith('/api/') && !['/media-file', '/material-file', '/openapi.json'].includes(url.pathname)) return route.fulfill({ status: 404, body: '' });
    if (request.method() !== 'GET') writes.push({ path: url.pathname, method: request.method(), body: request.headers()['content-type']?.includes('application/json') ? request.postDataJSON() : null });
    // Snapshot the holds before the fixture round trip: a request that arrived while nothing was
    // armed must not be held retroactively (the app's own refresh lands in this window), and one
    // that arrived while armed must not slip through.
    const planHold = holdPlan, stateHold = holdState;
    const value = await call({ method: request.method(), path: url.pathname + url.search, headers: await request.allHeaders(),
      ...(request.postDataBuffer() ? { body: request.postDataBuffer().toString('base64') } : {}) });
    if (planHold && request.method() === 'GET' && url.pathname.endsWith('/plan')) { planHold.arrived(); await planHold.gate; }
    if (stateHold && request.method() === 'GET' && url.pathname.endsWith('/state')) { stateHold.arrived(); await stateHold.gate; }
    if (loseProjectResponse && request.method() === 'POST' && url.pathname === '/api/projects/create') {
      assert.equal(value.status, 200); loseProjectResponse = false; return route.abort('failed');
    }
    if (loseImageResponse && request.method() === 'POST' && url.pathname.endsWith('/keyframe-tasks')) {
      assert.equal(value.status, 202, JSON.stringify(value.data));
      loseImageResponse = false; return route.abort('failed');
    }
    if (loseAssemblyResponse && request.method() === 'POST' && url.pathname.endsWith('/assembly')) {
      assert.equal(value.status, 200, JSON.stringify(value.data));
      loseAssemblyResponse = false; return route.abort('failed');
    }
    return route.fulfill({ status: value.status, headers: value.headers, body: value.binary ? Buffer.from(value.binary, 'base64') : JSON.stringify(value.data) });
  });
  page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://localhost:4197');
  await page.getByLabel('用户名').fill('user001'); await page.getByLabel('密码').fill('one');
  await page.getByRole('button', { name: '进入工作台', exact: true }).click();
  await page.getByRole('button', { name: '文生图 描述画面，生成一张图片' }).click();
  await page.getByRole('alert').waitFor();
  await page.getByRole('button', { name: '文生图 描述画面，生成一张图片' }).click();
  const imagePanel = page.getByLabel('生成关键帧', { exact: true });
  await imagePanel.getByLabel('图片提示词').fill('A blue lighthouse on a quiet island.');
  await imagePanel.locator('.resolution-picker summary').click();
  await imagePanel.getByLabel('图片画幅', { exact: true }).selectOption('1:1');
  await imagePanel.getByLabel('图片输出分辨率').selectOption('2k');
  loseImageResponse = true;
  await imagePanel.getByRole('button', { name: '生成图片', exact: true }).click();
  const imageWrite = writes.find(row => row.path.endsWith('/keyframe-tasks'));
  assert.equal(imageWrite.body.mode, 'text'); assert.equal(imageWrite.body.width, 1024);
  assert.equal(imageWrite.body.reference_image, undefined);
  assert.equal(imageWrite.body.output_resolution, '2k');
  const projectId = imageWrite.path.split('/')[3];
  const tasks = () => page.evaluate(async pid => (await (await fetch(`/api/projects/${pid}/tasks`)).json()).tasks, projectId);
  await page.waitForFunction(async pid => (await (await fetch(`/api/projects/${pid}/tasks`)).json()).tasks.some(task => task.payload.kind === 'keyframe'), projectId);
  let imageTask = (await tasks()).find(task => task.payload.kind === 'keyframe');
  assert.ok(imageTask);
  await page.reload();
  await page.getByRole('button', { name: '文生图 描述画面，生成一张图片' }).click();
  await imagePanel.getByRole('button', { name: '停止关键帧任务', exact: true }).waitFor();
  assert.equal(writes.filter(row => row.path === '/api/projects/create').length, 1);
  assert.equal(writes.filter(row => row.path.endsWith('/keyframe-tasks')).length, 1);
  await call({ operation: 'complete', project_id: projectId, task_id: imageTask.id });
  await page.getByRole('button', { name: '让这张图动起来', exact: true }).waitFor({ timeout: 15000 });
  await imagePanel.getByRole('button', { name: '修改提示词 / 再生成', exact: true }).click();
  await imagePanel.getByLabel('图片提示词').fill('A red lighthouse at sunset.');
  await imagePanel.getByRole('button', { name: '生成图片', exact: true }).click();
  await imagePanel.getByRole('button', { name: '停止关键帧任务', exact: true }).waitFor();
  const images = (await tasks()).filter(task => task.payload.kind === 'keyframe');
  assert.equal(images.length, 2);
  const nextImage = images.find(task => task.id !== imageTask.id);
  assert.equal(nextImage.payload.request.prompt, 'A red lighthouse at sunset.');
  assert.equal(imageTask.payload.request.prompt, 'A blue lighthouse on a quiet island.');
  await call({ operation: 'complete', project_id: projectId, task_id: nextImage.id });
  await imagePanel.getByRole('button', { name: '修改提示词 / 再生成', exact: true }).waitFor({ timeout: 15000 });
  await imagePanel.getByRole('button', { name: '载入当前图片的提示词', exact: true }).click();
  await imagePanel.getByLabel('图片提示词').waitFor({ state: 'visible' });
  assert.equal(await imagePanel.getByLabel('图片提示词').inputValue(), 'A red lighthouse at sunset.');
  await page.getByLabel('使用生成结果').getByRole('button', { name: '采用当前候选', exact: true }).click();
  await page.getByLabel('使用生成结果').getByText('已采用', { exact: true }).waitFor();
  const downloaded = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载图片', exact: true }).click();
  assert.ok((await downloaded).suggestedFilename().endsWith('.png'));
  await page.screenshot({ path: resolve(process.argv[3], 'image-result.png'), fullPage: true });
  await page.getByRole('button', { name: '让这张图动起来', exact: true }).click();
  const videoForm = page.getByRole('group', { name: '让图片动起来', exact: true });
  assert.ok(await videoForm.getByLabel('开始画面', { exact: true }).inputValue());
  await videoForm.getByLabel('提示词', { exact: true }).fill('Slow camera push in; clouds drift over the lighthouse.');
  await videoForm.getByLabel('上传图片', { exact: true }).setInputFiles(ready.picture);
  await page.waitForFunction(() => !document.querySelector('.quick-video-form')?.disabled);
  await videoForm.getByRole('button', { name: '生成视频', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.director-edit-heading')?.textContent.includes('等待'));
  let videoTasks = (await tasks()).filter(task => task.asset_id === 'S01');
  assert.equal(videoTasks.length, 1);
  assert.equal(videoTasks[0].payload.execution_snapshot.resolved_generation.generation_mode, 'image_to_video');
  assert.equal(videoTasks[0].payload.execution_snapshot.prompt, 'Slow camera push in; clouds drift over the lighthouse.');
  assert.equal(writes.filter(row => row.path.endsWith('/plan/segments')).length, 1);
  assert.equal(writes.filter(row => row.path.endsWith('/tasks')).length, 1);
  await call({ operation: 'complete', project_id: projectId, task_id: videoTasks[0].id });
  // Refresh reloads the same project/segment and candidate, with no new submission.
  await page.reload();
  await page.locator('.director-preview-area video').evaluate(async video => { await video.play(); });
  assert.ok(await page.locator('.director-preview-area video').evaluate(video => !video.paused));
  const firstVideoId = videoTasks[0].id;
  await page.getByLabel('使用生成结果').getByRole('button', { name: '采用当前候选', exact: true }).click();
  await page.getByLabel('使用生成结果').getByText('已采用', { exact: true }).waitFor();
  await page.getByRole('tab', { name: /提示词/ }).click();
  await page.getByLabel('当前分镜提示词', { exact: true }).fill('The camera slowly circles the lighthouse.');
  await page.getByRole('button', { name: '保存并重新生成', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.director-edit-heading')?.textContent.includes('等待'));
  videoTasks = (await tasks()).filter(task => task.asset_id === 'S01');
  assert.equal(videoTasks.length, 2);
  assert.ok(videoTasks.some(task => task.payload.execution_snapshot.prompt === 'The camera slowly circles the lighthouse.'));
  assert.equal(writes.filter(row => row.path.endsWith('/plan/segments')).length, 1, 'Regeneration must reuse the shot');
  await page.getByRole('tab', { name: /历史记录/ }).click();
  await page.getByText('Slow camera push in; clouds drift over the lighthouse.', { exact: true }).waitFor({ state: 'attached' });
  await page.screenshot({ path: resolve(process.argv[3], 'video-history.png'), fullPage: true });
  const secondVideoId = videoTasks.find(task => task.id !== firstVideoId).id;
  await call({ operation: 'complete', project_id: projectId, task_id: secondVideoId });
  // The fixture deliberately supplies no live SSE stream; reload completed receipts.
  await page.reload();
  await page.getByRole('button', { name: '并排比较候选', exact: true }).waitFor({ timeout: 15000 });
  await page.getByRole('tab', { name: /提示词/ }).click();
  await page.getByLabel('当前分镜提示词', { exact: true }).fill('A ship passes beyond the lighthouse.');
  await page.getByRole('button', { name: '保存并重新生成', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.director-edit-heading')?.textContent.includes('等待'));
  videoTasks = (await tasks()).filter(task => task.asset_id === 'S01');
  assert.equal(videoTasks.length, 3);
  const thirdVideoId = videoTasks.find(task => ![firstVideoId, secondVideoId].includes(task.id)).id;
  await call({ operation: 'complete', project_id: projectId, task_id: thirdVideoId });
  await page.reload();
  await page.getByRole('tab', { name: /历史记录\s*3/ }).waitFor({ timeout: 15000 });
  const comparison = page.getByRole('region', { name: '候选并排比较', exact: true });
  const leftVersion = comparison.getByLabel('左侧比较版本', { exact: true });
  const rightVersion = comparison.getByLabel('右侧比较版本', { exact: true });
  const selection = () => page.evaluate(async pid => {
    const plan = await (await fetch(`/api/projects/${pid}/plan`)).json();
    const state = await (await fetch(`/api/projects/${pid}/state`)).json();
    return { segment: plan.segments.find(segment => segment.id === 'S01'), reviews: state.reviews.S01 };
  }, projectId);
  const beforeCompare = await selection();
  const writesBeforeCompare = writes.length;
  await page.getByRole('button', { name: '并排比较候选', exact: true }).click();
  assert.equal(await leftVersion.inputValue(), secondVideoId, 'Default to previous/latest, independently of the old adopted version');
  assert.equal(await rightVersion.inputValue(), thirdVideoId);
  await leftVersion.selectOption(firstVideoId);
  await rightVersion.selectOption(secondVideoId);
  await page.waitForFunction(({ left, right }) => {
    const a = document.querySelector('select[aria-label="左侧比较版本"]');
    const b = document.querySelector('select[aria-label="右侧比较版本"]');
    return a?.value === left && b?.value === right && Array.from(a.options).some(option => option.value === right && option.disabled);
  }, { left: firstVideoId, right: secondVideoId });
  assert.equal(await leftVersion.locator(`option[value="${secondVideoId}"]`).evaluate(option => option.disabled), true);
  await comparison.getByRole('article', { name: '左侧候选', exact: true }).locator('summary').press('Enter');
  await comparison.getByRole('article', { name: '右侧候选', exact: true }).locator('summary').press('Enter');
  assert.equal(await comparison.getByLabel('左侧候选提示词').textContent(), 'Slow camera push in; clouds drift over the lighthouse.');
  assert.equal(await comparison.getByLabel('右侧候选提示词').textContent(), 'The camera slowly circles the lighthouse.');
  await rightVersion.press('Home'); await rightVersion.press('Enter');
  await page.waitForFunction(id => document.querySelector('select[aria-label="右侧比较版本"]')?.value === id, thirdVideoId);
  assert.equal(await rightVersion.inputValue(), thirdVideoId, 'Version selection supports the keyboard');
  await rightVersion.selectOption(secondVideoId);
  await page.waitForFunction(() => Array.from(document.querySelectorAll('.candidate-comparison video')).every(video => video.readyState >= 1));
  assert.ok(await comparison.locator('video').evaluateAll(videos => videos.length === 2 && videos.every(video => video.muted && video.paused)));
  await comparison.locator('video').evaluateAll(async videos => {
    for (const video of videos) { await video.play(); video.pause(); }
  });
  await page.waitForFunction(() => Array.from(document.querySelectorAll('.candidate-comparison video')).every(video => video.readyState >= 2 && video.paused));
  assert.equal(writes.length, writesBeforeCompare, 'Preview/select must not mutate the plan or reviews');
  assert.deepEqual(await selection(), beforeCompare);
  await comparison.screenshot({ path: resolve(process.argv[3], 'candidate-comparison-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await comparison.screenshot({ path: resolve(process.argv[3], 'candidate-comparison-mobile.png') });
  await page.screenshot({ path: resolve(process.argv[3], 'mobile.png'), fullPage: true });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'No horizontal page overflow');
  await comparison.getByRole('article', { name: '左侧候选', exact: true }).getByRole('button', { name: '恢复为当前候选', exact: true }).click();
  await page.waitForFunction(async ({ pid, tid }) => {
    const plan = await (await fetch(`/api/projects/${pid}/plan`)).json();
    return plan.segments.find(segment => segment.id === 'S01')?.current_version_task_id === tid;
  }, { pid: projectId, tid: firstVideoId });
  assert.equal(writes.filter(row => row.path.endsWith('/restore')).length, 1);
  assert.notEqual((await selection()).reviews?.sample?.status, 'approved', 'Restoring is not adopting');
  await page.waitForFunction(() => document.querySelector('.quick-candidate-meta-heading')?.textContent.includes('v1'));
  await page.getByLabel('使用生成结果').getByRole('button', { name: '采用当前候选', exact: true }).click();
  await page.getByLabel('使用生成结果').getByText('已采用', { exact: true }).waitFor();
  await page.getByRole('button', { name: '并排比较候选', exact: true }).click();
  assert.equal(await leftVersion.inputValue(), secondVideoId);
  assert.equal(await rightVersion.inputValue(), thirdVideoId, 'Restoring/adopting V1 must not pin the default comparison');
  assert.equal((await tasks()).filter(task => task.asset_id === 'S01').length, 3, 'Comparing/restoring must not enqueue a generation');
  // Switching from a populated image form to text video must not silently bind the image.
  await page.getByRole('button', { name: '图生视频 放入图片，让画面动起来' }).click();
  await videoForm.getByLabel('开始画面', { exact: true }).selectOption({ index: 1 });
  await page.getByRole('button', { name: '文生视频 描述画面与动作，直接生成视频' }).click();
  const textForm = page.getByRole('group', { name: '用文字生成视频', exact: true });
  assert.equal(await textForm.getByLabel('开始画面', { exact: true }).count(), 0);
  await textForm.getByLabel('提示词', { exact: true }).fill('A quiet river at dawn, mist rises above the water.');
  await textForm.getByLabel('视频生成方案', { exact: true }).selectOption('fast');
  assert.equal(await textForm.getByLabel('视频生成方案', { exact: true }).inputValue(), 'fast');
  await textForm.locator('.resolution-picker summary').click();
  await textForm.getByLabel('视频画幅').selectOption('9:16');
  assert.equal(await textForm.getByLabel('视频生成方案', { exact: true }).inputValue(), 'standard', 'Non-16:9 switches back to the standard recipe');
  await textForm.getByLabel('视频输出分辨率').selectOption('4k');
  assert.ok((await textForm.getByLabel('视频输出分辨率').innerText()).includes('2160 × 3840'));
  await textForm.getByRole('button', { name: '生成视频', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.director-edit-heading')?.textContent.includes('分镜2'));
  await page.getByRole('button', { name: '保存并生成这一段', exact: true }).waitFor();
  let textTask;
  for (let i = 0; i < 100 && !textTask; i++) {
    textTask = (await tasks()).find(task => task.asset_id === 'S02');
    if (!textTask) await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(textTask, 'Text video submission must return a persisted task');
  assert.equal(textTask.payload.execution_snapshot.resolved_generation.generation_mode, 'text_to_video');
  assert.deepEqual(textTask.payload.execution_snapshot.materials, {});
  assert.equal(textTask.payload.execution_snapshot.resolved_generation.aspect_ratio, '9:16');
  assert.deepEqual(textTask.payload.output_size, { resolution: '4k', aspect_ratio: '9:16' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '查看上一段', exact: true }).click();
  await page.getByRole('heading', { name: '分镜1', exact: true }).waitFor();
  await page.getByRole('button', { name: '查看下一段', exact: true }).click();
  await page.getByRole('heading', { name: '分镜2', exact: true }).waitFor();
  assert.equal(await page.getByRole('region', { name: '候选并排比较', exact: true }).count(), 0, 'Never show another shot\'s candidate comparison');
  await page.screenshot({ path: resolve(process.argv[3], 'stacked-cards.png'), fullPage: true });
  await page.getByRole('button', { name: '检查并装配成片', exact: true }).click();
  const delivery = page.getByRole('region', { name: '装配与取得文件', exact: true });
  await delivery.getByRole('heading', { name: '已采用 1 / 2 段', exact: true }).waitFor();
  assert.ok(await delivery.getByRole('button', { name: '装配成片', exact: true }).isDisabled());
  await delivery.getByRole('button', { name: '打开分镜2', exact: true }).click();
  await call({ operation: 'complete', project_id: projectId, task_id: textTask.id });
  await page.reload(); // The isolated fixture has no event stream.
  await page.getByRole('button', { name: '查看下一段', exact: true }).click();
  await page.getByRole('heading', { name: '分镜2', exact: true }).waitFor();
  await page.getByLabel('使用生成结果').getByRole('button', { name: '采用当前候选', exact: true }).click();
  await page.getByLabel('使用生成结果').getByText('已采用', { exact: true }).waitFor();
  await page.getByRole('button', { name: '查看上一段', exact: true }).click();
  await page.getByRole('button', { name: '继续下一段', exact: true }).click();
  await page.getByRole('heading', { name: '分镜2', exact: true }).waitFor();
  const countBeforeNextDraft = (await tasks()).length;
  await page.getByRole('button', { name: '添加下一段（文字）', exact: true }).click();
  await textForm.getByRole('button', { name: '返回已有片段', exact: true }).click();
  assert.equal((await tasks()).length, countBeforeNextDraft, 'Opening the next-shot form never generates or creates another shot');
  await page.getByRole('button', { name: '检查并装配成片', exact: true }).click();
  await delivery.getByRole('heading', { name: '已采用 2 / 2 段', exact: true }).waitFor();
  await delivery.locator('.assembly-edit-panel > summary').click();
  await delivery.getByText('S01 → S02', { exact: true }).click();
  await delivery.getByLabel('审核记录', { exact: true }).fill('Isolated fixture: both sides play; cut and sound policy checked.');
  await delivery.getByRole('button', { name: '已通过', exact: true }).click();
  await delivery.getByText('S01 → S02', { exact: true }).locator('..').getByText(/已通过/).waitFor();
  await delivery.getByLabel('音轨方式').selectOption('mute');
  holdPlan = arm();
  const soundReload = page.waitForResponse(response => response.url().endsWith('/assembly/edit') && response.request().method() === 'GET');
  await delivery.getByRole('button', { name: '保存音轨方案', exact: true }).click();
  await holdPlan.arrival;
  assert.ok(await delivery.getByRole('button', { name: '无 GPU 装配预检', exact: true }).isDisabled(), 'Preflight waits for the saved-plan refresh');
  holdPlan.release(); holdPlan = null;
  await (await soundReload).finished();
  await page.waitForFunction(async pid => (await (await fetch(`/api/projects/${pid}/assembly/edit`)).json()).sound.policy === 'mute', projectId);
  await delivery.getByRole('button', { name: '无 GPU 装配预检', exact: true }).click();
  await delivery.getByRole('status').filter({ hasText: '预检通过' }).waitFor();
  await delivery.screenshot({ path: resolve(process.argv[3], 'delivery-ready.png') });
  loseAssemblyResponse = true;
  await delivery.getByRole('button', { name: '装配成片', exact: true }).click();
  await delivery.getByRole('button', { name: '核对装配提交', exact: true }).waitFor();
  const assemblyRequests = writes.filter(row => row.path.endsWith('/assembly') && row.method === 'POST');
  assert.equal(assemblyRequests.length, 1);
  await delivery.getByRole('button', { name: '核对装配提交', exact: true }).click();
  await delivery.getByRole('button', { name: '核对装配提交', exact: true }).waitFor({ state: 'hidden' });
  assert.equal(writes.filter(row => row.path.endsWith('/assembly') && row.method === 'POST').length, 1, 'Recover the original receipt without resubmitting');
  const assemblyTask = (await tasks()).find(task => task.payload.assembly_asset_id === task.asset_id);
  assert.ok(assemblyTask);
  await call({ operation: 'complete', project_id: projectId, task_id: assemblyTask.id });
  // Reload then reopen delivery, as the fixture deliberately omits production SSE.
  await page.reload();
  await page.getByRole('button', { name: '检查并装配成片', exact: true }).click();
  await delivery.getByRole('heading', { name: '成片候选 · 等待审核', exact: true }).waitFor();
  await delivery.getByLabel('装配成片预览').evaluate(async video => { await video.play(); video.pause(); });
  const finalDownload = page.waitForEvent('download');
  await delivery.getByRole('link', { name: '下载成片候选', exact: true }).click();
  const finalFile = await finalDownload;
  await finalFile.saveAs(resolve(process.argv[3], 'downloaded-assembly.mp4'));
  assert.equal(await finalFile.failure(), null);
  const proof = await call({ operation: 'delivery-proof', project_id: projectId, task_id: assemblyTask.id });
  assert.equal(proof.foreign_task_status, 404); assert.equal(proof.foreign_file_status, 403);
  assert.equal(proof.plan.assembly.duration_seconds, 10);
  for (const segment of proof.plan.segments) {
    const frozen = proof.staged.find(item => item.node_id === `load-${segment.id}`);
    assert.equal(frozen.source, segment.video.path);
    assert.equal(proof.graph[`load-${segment.id}`].inputs.file, frozen.reference);
    assert.ok(frozen.copy_matches_source, 'Assembly consumes a frozen copy of the chosen candidate');
  }
  assert.equal(proof.plan.segments[0].current_version_task_id, firstVideoId, 'Assembly uses the explicitly restored/adopted V1');
  const finalReviews = () => page.evaluate(async pid => (await (await fetch(`/api/projects/${pid}/state`)).json()).reviews.MASTER?.final, projectId);
  assert.notEqual((await finalReviews())?.status, 'approved', 'Download is not adoption');
  await delivery.getByRole('button', { name: '查看成片并审核', exact: true }).click();
  await page.getByLabel('使用生成结果').getByRole('button', { name: '通过全片审核', exact: true }).click();
  await page.getByLabel('使用生成结果').getByText('已采用', { exact: true }).waitFor();
  await page.getByRole('button', { name: '返回装配与下载', exact: true }).click();
  await delivery.getByRole('link', { name: '下载已采用成片', exact: true }).waitFor();
  await delivery.locator('.assembly-edit-panel > summary').waitFor();
  await delivery.getByLabel('装配成片预览').evaluate(async video => { await video.play(); video.pause(); });
  await page.waitForFunction(() => document.querySelector('video[aria-label="装配成片预览"]')?.readyState >= 2);
  await delivery.screenshot({ path: resolve(process.argv[3], 'delivery-complete.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await delivery.screenshot({ path: resolve(process.argv[3], 'delivery-mobile.png') });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await delivery.getByRole('button', { name: '打开分镜1', exact: true }).click();
  await page.getByRole('button', { name: '并排比较候选', exact: true }).click();
  holdState = arm();
  await comparison.getByRole('article', { name: '左侧候选', exact: true }).getByRole('button', { name: '恢复为当前候选', exact: true }).click();
  await page.waitForFunction(async pid => (await (await fetch(`/api/projects/${pid}/plan`)).json()).assembly.status === 'stale', projectId);
  await page.getByRole('button', { name: '检查并装配成片', exact: true }).click();
  await delivery.getByRole('heading', { name: '历史成片 · 需重新装配', exact: true }).waitFor();
  await delivery.getByRole('link', { name: '下载历史成片', exact: true }).waitFor();
  await holdState.arrival;
  assert.ok(await delivery.getByRole('button', { name: '重新装配成片', exact: true }).isDisabled());
  holdState.release(); holdState = null;
  await delivery.getByRole('heading', { name: '已采用 1 / 2 段', exact: true }).waitFor();
  await delivery.getByRole('button', { name: '查看成片并审核', exact: true }).click();
  assert.ok(await page.getByLabel('使用生成结果').getByRole('button', { name: '通过全片审核', exact: true }).isDisabled());
  assert.equal((await tasks()).length, 7, 'Restoring and viewing stale assembly retain all original task candidates without new generation');
  // The adult zone needs the same clarify entry as the general zone, but it must not dress it up
  // as an adult capability: 清晰化 upscales with RealESRGAN and loads no adult configuration.
  await page.getByRole('button', { name: '快速生成', exact: true }).click();
  const destination = await page.locator('.quick-navigation > span').textContent();
  assert.ok(destination.startsWith('保存到：'), `Unexpected quick destination: ${destination}`);
  await page.getByRole('group', { name: '快速生成分区', exact: true }).getByRole('button', { name: '成人向创作', exact: true }).click();
  assert.equal(await page.locator('.quick-navigation > span').textContent(), destination,
    'Opening the adult zone keeps the current work instead of moving it into the adult series');
  const adultTemplates = page.locator('.quick-templates-adult button');
  assert.equal(await adultTemplates.count(), 4, 'The adult zone offers the three adult entries plus clarify');
  const adultEnhance = adultTemplates.filter({ hasText: '提高当前作品分辨率' });
  assert.equal(await adultEnhance.locator('strong').textContent(), '图像／视频清晰化');
  assert.equal(await adultEnhance.locator('span').textContent(), '提高当前作品分辨率，不经过成人向配方');
  assert.equal(await adultTemplates.locator('strong').filter({ hasText: '成人向' }).count(), 3, 'Only the entrants that load an adult profile are named 成人向');
  assert.ok(!(await adultEnhance.locator('strong').textContent()).startsWith('成人向'));
  await adultEnhance.click();
  await page.getByLabel('图像／视频清晰化', { exact: true }).getByRole('heading', { name: '选择原素材', exact: true }).waitFor();
  assert.equal(await page.locator('.quick-navigation > span').textContent(), destination,
    'Clarify acts on the current work instead of moving it into the adult series');
  assert.equal(writes.filter(row => row.path === '/api/projects/create').length, 1, 'Clarify creates no work');
  assert.deepEqual(errors, []);
  const stats = await call({ operation: 'stats' });
  assert.equal(stats.queued.length, 7); assert.deepEqual(stats.other_projects, []);
  console.log('PASS: three entry types, adult-zone clarify entry, candidate comparison/restore/adopt, next shot, two-shot joins and sound, lost assembly receipt recovery, final preview/download/adopt, shot/account/file isolation, responsive layout. GPU and assembly execution are stubbed.');
} catch (error) {
  if (page) { console.error((await page.locator('body').innerText()).slice(-9000)); await page.screenshot({ path: resolve(process.argv[3], 'failure.png'), fullPage: true }); }
  throw error;
} finally { await browser.close(); fixture.kill(); }
