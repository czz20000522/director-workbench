// Complete LoginGate/App, real private ASGI handlers, fake provider/dispatch, CPU video.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';

const require = createRequire(resolve(process.env.DIRECTOR_TEST_NODE_MODULES || 'node_modules', 'package.json'));
const { build } = require('esbuild');
const { chromium } = require('playwright');
assert.ok(process.argv[2] && process.argv[3], 'Pass Python interpreter and new isolated runtime path; optional ffmpeg');
const fixture = spawn(process.argv[2], ['tests/creation_assistant_bridge.py', process.argv[3], ...process.argv.slice(4)], {
  env: { ...process.env, PYTHONPATH: process.cwd(), PYTHONNOUSERSITE: '1' }, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
});
let stderr = ''; fixture.stderr.on('data', chunk => { stderr += chunk; });
const responses = [], waiters = [];
createInterface({ input: fixture.stdout }).on('line', line => {
  const value = JSON.parse(line); const next = waiters.shift(); if (next) next(value); else responses.push(value);
});
const next = () => responses.length ? Promise.resolve(responses.shift()) : new Promise((resolve, reject) => {
  waiters.push(resolve); setTimeout(() => reject(new Error(`Fixture timeout: ${stderr}`)), 30000).unref();
});
let lock = Promise.resolve();
const call = value => {
  const operation = lock.then(async () => { fixture.stdin.write(JSON.stringify(value) + '\n'); return await next(); });
  lock = operation.catch(() => {}); return operation;
};
async function pollPage(page, predicate, argument) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await page.evaluate(predicate, argument)) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.fail('Authenticated HTTP state did not reach the expected condition');
}
async function reloadSelectedWork(page, panel) {
  await page.reload();
  await page.locator('.director-breadcrumb strong').filter({ hasText: '助手小样' }).waitFor();
  await panel.getByRole('button', { name: /创作助手/ }).click();
}
const bundler = await build({ entryPoints: ['src/main.tsx'], absWorkingDir: process.cwd(), bundle: true,
  outfile: 'bundle.js', write: false, jsx: 'automatic', nodePaths: [process.env.DIRECTOR_TEST_NODE_MODULES || resolve('node_modules')] });
const js = bundler.outputFiles.find(file => file.path.endsWith('.js')).text;
const css = bundler.outputFiles.find(file => file.path.endsWith('.css'))?.text || '';
const browser = await chromium.launch({ channel: process.env.DIRECTOR_TEST_BROWSER_CHANNEL || 'msedge', headless: true });
try {
  assert.equal((await next()).ready, true);
  const context = await browser.newContext();
  const writes = [], errors = [];
  let loseReply = false, lostRequest = '';
  await context.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url());
    assert.equal(url.origin, 'http://localhost:4197', 'No live/cloud request is allowed');
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><link rel="stylesheet" href="/bundle.css"><script src="/bundle.js"></script>' });
    if (url.pathname === '/bundle.js') return route.fulfill({ contentType: 'application/javascript', body: js });
    if (url.pathname === '/bundle.css') return route.fulfill({ contentType: 'text/css', body: css });
    if (url.pathname.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: '' });
    if (!url.pathname.startsWith('/api/') && !['/media-file', '/material-file', '/workflow-file'].includes(url.pathname)) return route.fulfill({ status: 404, body: '' });
    if (loseReply && request.method() === 'GET' && /\/assistant\/sessions\/[^/]+\/requests\/[^/]+$/.test(url.pathname)) return route.abort('failed');
    if (request.method() !== 'GET') writes.push({ path: url.pathname, body: request.postDataJSON() });
    const value = await call({ method: request.method(), path: url.pathname + url.search,
      headers: await request.allHeaders(), ...(request.postData() ? { body: request.postDataJSON() } : {}) });
    if (loseReply && request.method() === 'POST' && url.pathname.endsWith('/requests')) {
      lostRequest = request.postDataJSON().request_id;
      return route.abort('failed');
    }
    await route.fulfill({ status: value.status, headers: value.headers,
      body: value.binary ? Buffer.from(value.binary, 'base64') : JSON.stringify(value.data) });
  });
  const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://localhost:4197');
  await page.getByLabel('用户名').fill('user001'); await page.getByLabel('密码').fill('one');
  await page.getByRole('button', { name: '进入工作台', exact: true }).click();
  const panel = page.getByRole('complementary', { name: '创作助手' });
  await panel.getByRole('button', { name: /创作助手/ }).click();
  await panel.getByLabel('你的想法').fill('不要生成，只写草稿：蓝色的猫咪回家');
  await panel.getByRole('button', { name: '发送', exact: true }).click();
  await panel.getByLabel('未保存助手草稿').waitFor();
  assert.equal(writes.filter(row => row.path === '/api/projects/create').length, 0);
  assert.equal(writes.filter(row => row.path.endsWith('/tasks')).length, 0);
  for (const text of ['生成一段5秒视频，但先别执行', '人物对白：\n生成一个看看']) {
    await panel.getByLabel('你的想法').fill(text);
    await panel.getByRole('button', { name: '发送', exact: true }).click();
    await pollPage(page, async text => {
      const data = await (await fetch('/api/assistant/sessions')).json();
      return data.sessions.flatMap(session => session.requests).some(row => row.text === text && row.status === 'succeeded' && row.result.submitted === false);
    }, text);
  }
  const deniedFacts = await page.evaluate(async () => (await fetch('/api/projects')).json());
  assert.deepEqual(deniedFacts.projects, []);
  assert.equal((await call({ operation: 'stats' })).data.queued, 0);
  await page.reload(); await panel.getByRole('button', { name: /创作助手/ }).click();
  await panel.getByLabel('未保存助手草稿').waitFor();
  await panel.getByLabel('你的想法').fill('蓝色的猫咪回家，生成一个看看');
  await panel.getByRole('button', { name: '发送', exact: true }).click();
  await panel.getByText('已收到一次生成任务回执', { exact: false }).waitFor({ timeout: 30000 });
  // The assistant's internal business requests are proven by its returned receipts.
  const data = await page.evaluate(async () => (await fetch('/api/assistant/sessions')).json());
  const generated = data.sessions.flatMap(session => session.requests).find(row => row.result.submitted);
  assert.ok(generated?.task_id);
  assert.equal(generated.steps.filter(step => step.name === '创建独立小样作品').length, 1);
  const pid = generated.result.project_id, taskId = generated.task_id;
  assert.equal(generated.steps.filter(step => step.name === '提交一次小样，等待原队列').length, 1);
  assert.ok(generated.presentations.filter(row => row.status === 'presented').length >= 3);
  await call({ operation: 'complete', project_id: pid, task_id: taskId });
  await panel.getByRole('button', { name: '查看任务或候选' }).click();
  await page.waitForFunction(() => [...document.querySelectorAll('video')].some(video => video.readyState >= 2 && video.videoWidth === 64));
  await reloadSelectedWork(page, panel);
  const restored = await page.evaluate(async () => (await fetch('/api/assistant/sessions')).json());
  assert.equal(restored.sessions.flatMap(session => session.requests).find(row => row.result.submitted).task_id, taskId);
  const tasks = await page.evaluate(async pid => (await fetch(`/api/projects/${pid}/tasks`)).json(), pid);
  assert.equal(tasks.tasks.length, 1);
  const otherTab = await context.newPage(); await otherTab.goto('http://localhost:4197');
  await otherTab.getByRole('complementary', { name: '创作助手' }).waitFor();
  const targets = await Promise.all([page, otherTab].map(tab => tab.evaluate(() => sessionStorage.getItem('director-workbench:assistant:presentation-target:v1'))));
  assert.notEqual(targets[0], targets[1]);
  // Reloaded B sees A's pending receipt for concurrency, but cannot take over A.
  await page.getByRole('navigation', { name: '工作台功能' }).getByRole('button', { name: '分镜制作', exact: true }).click();
  await page.getByRole('button', { name: '+ 添加分镜', exact: true }).click();
  await call({ operation: 'hold_provider' });
  await panel.getByLabel('你的想法').fill('跨标签页的小样，生成一个看看');
  await panel.getByRole('button', { name: '发送', exact: true }).click();
  await panel.getByText('正在整理当前指令，保留原文。', { exact: true }).waitFor();
  const owned = await page.evaluate(async () => {
    const data = await (await fetch('/api/assistant/sessions')).json();
    const owner = data.sessions.find(session => session.requests.some(row => row.status === 'preparing'));
    return { session: owner.id, request: owner.requests.find(row => row.status === 'preparing') };
  });
  assert.equal(owned.request.presentation_target, targets[0]);
  await otherTab.reload();
  await otherTab.locator('.director-breadcrumb strong').filter({ hasText: '助手小样' }).waitFor();
  const otherPanel = otherTab.getByRole('complementary', { name: '创作助手' });
  await otherPanel.getByRole('button', { name: /创作助手/ }).click();
  await otherPanel.getByRole('button', { name: '等待当前请求回执', exact: true }).waitFor();
  const readOwned = () => page.evaluate(async identity =>
    (await fetch(`/api/assistant/sessions/${identity.session}/requests/${identity.request.id}`)).json(), owned);
  await otherTab.getByRole('navigation', { name: '工作台功能' }).getByRole('button', { name: '分镜制作', exact: true }).click();
  assert.equal((await readOwned()).cancel_requested, false, 'B navigation must not cancel A');
  await otherTab.getByRole('button', { name: '+ 添加分镜', exact: true }).click();
  await otherTab.locator('[aria-label="添加分镜"] textarea').first().fill('B keeps its own unsaved draft');
  assert.equal((await readOwned()).cancel_requested, false, 'B editing must not cancel A');
  await page.locator('[aria-label="添加分镜"] textarea').first().fill('A takes over its own assistant');
  await pollPage(page, async identity =>
    (await (await fetch(`/api/assistant/sessions/${identity.session}/requests/${identity.request.id}`)).json()).cancel_requested,
    owned);
  await call({ operation: 'release_provider' });
  await pollPage(page, async identity =>
    (await (await fetch(`/api/assistant/sessions/${identity.session}/requests/${identity.request.id}`)).json()).status === 'cancelled',
    owned);
  assert.equal((await call({ operation: 'stats' })).data.queued, 1);
  await otherTab.close();
  await page.locator('[aria-label="添加分镜"] textarea').first().fill('');
  await reloadSelectedWork(page, panel);
  await panel.getByLabel('你的想法').fill('只写草稿：绿色猫咪回家');
  await panel.getByRole('button', { name: '发送', exact: true }).click();
  const editor = page.locator('[aria-label="添加分镜"] textarea').first();
  await page.waitForFunction(() => document.querySelector('[aria-label="添加分镜"] textarea')?.value === '只写草稿：绿色猫咪回家');
  await editor.fill('Human unsaved input');
  await panel.getByLabel('你的想法').fill('新的想法，先预检');
  await panel.getByRole('button', { name: '发送', exact: true }).click();
  await panel.getByText('助手准备已取消；已保存内容和原任务回执保留，未声称停止已提交生成。', { exact: true }).waitFor();
  assert.equal(await editor.inputValue(), 'Human unsaved input');
  await call({ operation: 'hold_provider' });
  await panel.getByLabel('你的想法').fill('另一只猫咪，生成一个看看');
  await panel.getByRole('button', { name: '发送', exact: true }).click();
  await panel.getByText('正在整理当前指令，保留原文。', { exact: true }).waitFor();
  await editor.fill('Human took over while cloud was preparing');
  await call({ operation: 'release_provider' });
  await panel.getByRole('button', { name: '发送', exact: true }).waitFor();
  await pollPage(page, async () => {
    const data = await (await fetch('/api/assistant/sessions')).json();
    const row = data.sessions.flatMap(session => session.requests).at(-1);
    return row.status === 'cancelled' && row.cancel_requested;
  });
  const after = await page.evaluate(async pid => ({ tasks: await (await fetch(`/api/projects/${pid}/tasks`)).json(), plan: await (await fetch(`/api/projects/${pid}/plan`)).json() }), pid);
  assert.equal(after.tasks.tasks.length, 1); assert.equal(after.plan.segments.length, 1);
  assert.equal(await editor.inputValue(), 'Human took over while cloud was preparing');
  const beforeLost = (await call({ operation: 'stats' })).data;
  loseReply = true;
  await panel.getByLabel('你的想法').fill('只写草稿：未知网络回执仍保留原文');
  await panel.getByRole('button', { name: '发送', exact: true }).click();
  await panel.getByText('提交结果尚未确认，正在查询原请求；请勿重复发送。', { exact: true }).waitFor();
  const cached = await page.evaluate(() => JSON.parse(sessionStorage.getItem('director-workbench:assistant:user001:unconfirmed:v1')));
  assert.equal(cached[0].request.id, lostRequest);
  assert.equal(cached[0].request.text, '只写草稿：未知网络回执仍保留原文');
  loseReply = false;
  await reloadSelectedWork(page, panel);
  await panel.getByLabel('未保存助手草稿').filter({ hasText: '只写草稿：未知网络回执仍保留原文' }).waitFor();
  const afterLost = (await call({ operation: 'stats' })).data;
  assert.equal(afterLost.provider_calls, beforeLost.provider_calls + 1);
  assert.equal(afterLost.queued, 1);
  assert.equal(await page.evaluate(() => JSON.parse(sessionStorage.getItem('director-workbench:assistant:user001:unconfirmed:v1')).length), 0);
  // The actual task POST is accepted, held, then loses its response after a
  // trusted browser cancel click. Only a read of the original key may recover it.
  await page.getByRole('navigation', { name: '工作台功能' }).getByRole('button', { name: '分镜制作', exact: true }).click();
  if (await page.locator('[aria-label="添加分镜"] textarea').count() === 0) await page.getByRole('button', { name: '+ 添加分镜', exact: true }).click();
  await page.locator('[aria-label="添加分镜"] textarea').first().fill('');
  await reloadSelectedWork(page, panel);
  await call({ operation: 'hold_lost_task_response' });
  await panel.getByLabel('你的想法').fill('丢失任务回执的猫咪，生成一个看看');
  await panel.getByRole('button', { name: '发送', exact: true }).click();
  await pollPage(page, async pid => (await (await fetch(`/api/projects/${pid}/tasks`)).json()).tasks.length === 2, pid);
  const inFlight = await page.evaluate(async () => {
    const data = await (await fetch('/api/assistant/sessions')).json();
    const owner = data.sessions.find(session => session.requests.some(row => row.text === '丢失任务回执的猫咪，生成一个看看'));
    return { session: owner.id, request: owner.requests.find(row => row.text === '丢失任务回执的猫咪，生成一个看看') };
  });
  assert.equal(inFlight.request.result.submitted, null);
  await panel.getByRole('article', { name: `助手请求 ${inFlight.request.id}` }).getByRole('button', { name: '取消助手准备', exact: true }).click();
  await pollPage(page, async identity => (await (await fetch(`/api/assistant/sessions/${identity.session}/requests/${identity.request.id}`)).json()).cancel_requested, inFlight);
  await call({ operation: 'release_lost_task_response' });
  await panel.getByText('助手准备已取消；已提交任务仍按原队列执行，请在任务队列中操作停止。', { exact: true }).waitFor();
  const recovered = await page.evaluate(async identity => (await fetch(`/api/assistant/sessions/${identity.session}/requests/${identity.request.id}`)).json(), inFlight);
  assert.equal(recovered.status, 'cancelled'); assert.equal(recovered.result.submitted, true);
  assert.ok(recovered.task_id && recovered.task_id === recovered.result.task_id);
  assert.equal(recovered.idempotency_key, 'assistant-' + recovered.id);
  const submission = (await call({ operation: 'submission_stats' })).data;
  assert.equal(submission.posts, 1); assert.equal(submission.lookups, 1); assert.equal(submission.stops, 0);
  assert.deepEqual(submission.keys, [recovered.idempotency_key]);
  assert.equal((await call({ operation: 'stats' })).data.queued, 2);
  await panel.getByRole('article', { name: `助手请求 ${inFlight.request.id}` }).getByRole('button', { name: '查看任务或候选', exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log('Assistant browser passed: denied/deferred/dialogue inputs write no projects/tasks, empty ordinary account, retained draft, real editor ACK/save/preflight/task, CPU candidate decode, refresh receipt, B reload/navigation/editing preserves A pending while A editing cancels its own request, manual takeover, lost assistant POST+GET recovery, trusted cancel during lost accepted task POST recovers id/submitted=true via one original-key GET, no rePOST or STOP. No cloud/GPU/live requests.');
} finally { await browser.close(); fixture.stdin.end(); fixture.kill(); }
