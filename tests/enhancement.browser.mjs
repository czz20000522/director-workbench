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
  let loseEnhancementResponse = false;
  let loseImageResponse = false, loseProjectResponse = false, loseAssemblyResponse = false;
  let holdPlan = null, onPlanHeld = null;
  let holdState = null, onStateHeld = null;
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    assert.equal(url.origin, 'http://localhost:4197', 'Never contact live services');
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><link rel="stylesheet" href="/bundle.css"><script src="/bundle.js"></script>' });
    if (url.pathname === '/bundle.js') return route.fulfill({ contentType: 'application/javascript', body: js });
    if (url.pathname === '/bundle.css') return route.fulfill({ contentType: 'text/css', body: css });
    if (url.pathname.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: '' });
    if (!url.pathname.startsWith('/api/') && !['/media-file', '/material-file', '/openapi.json'].includes(url.pathname)) return route.fulfill({ status: 404, body: '' });
    if (request.method() !== 'GET') writes.push({ path: url.pathname, method: request.method(), body: request.headers()['content-type']?.includes('application/json') ? request.postDataJSON() : null });
    if (request.method() === 'POST' && url.pathname.endsWith('/upload')) return route.continue();
    const value = await call({ method: request.method(), path: url.pathname + url.search, headers: await request.allHeaders(),
      ...(request.postDataBuffer() ? { body: request.postDataBuffer().toString('base64') } : {}) });
    if (holdPlan && request.method() === 'GET' && url.pathname.endsWith('/plan')) { onPlanHeld(); await holdPlan; }
    if (holdState && request.method() === 'GET' && url.pathname.endsWith('/state')) { onStateHeld(); await holdState; }
    if (loseProjectResponse && request.method() === 'POST' && url.pathname === '/api/projects/create') {
      assert.equal(value.status, 200); loseProjectResponse = false; return route.abort('failed');
    }
    if (loseEnhancementResponse && request.method() === 'POST' && url.pathname.endsWith('/enhancement-tasks')) {
      assert.equal(value.status, 202, JSON.stringify(value.data)); loseEnhancementResponse = false; return route.abort('failed');
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
  await page.goto('http://localhost:4197/?legacy=1');
  await page.getByLabel('用户名').fill('user001'); await page.getByLabel('密码').fill('one');
  await page.getByRole('button', { name: '进入工作台', exact: true }).click();
  const entry = page.getByRole('button', { name: '图像／视频清晰化 先生成，再用 AI 提高分辨率' });
  await entry.click();
  const panel = page.getByRole('region', { name: '图像／视频清晰化', exact: true });
  await panel.waitFor();
  assert.ok(await panel.getByRole('button', { name: '开始 AI 清晰化 · 2×', exact: true }).isDisabled());
  await panel.getByLabel('上传清晰化素材').setInputFiles(ready.picture);
  await page.waitForFunction(() => document.querySelector('[aria-label="清晰化原素材"]')?.value);
  const projectId = writes.find(row => row.path.endsWith('/upload')).path.split('/')[3];
  const tasks = () => page.evaluate(async pid => (await (await fetch(`/api/projects/${pid}/tasks`)).json()).tasks, projectId);
  const before = await page.evaluate(async pid => (await (await fetch(`/api/projects/${pid}/plan`)).json()), projectId);
  loseEnhancementResponse = true;
  await panel.getByRole('button', { name: '开始 AI 清晰化 · 2×', exact: true }).click();
  await page.waitForFunction(async pid => (await (await fetch(`/api/projects/${pid}/tasks`)).json()).tasks.length === 1, projectId);
  await page.reload();
  await entry.click();
  await panel.getByRole('button', { name: '停止处理', exact: true }).waitFor();
  assert.equal(writes.filter(row => row.path.endsWith('/enhancement-tasks')).length, 1);
  const first = (await tasks())[0];
  await call({ operation: 'complete', project_id: projectId, task_id: first.id });
  const download = panel.getByRole('link', { name: '下载清晰化候选', exact: true });
  await download.waitFor({ timeout: 15000 });
  await page.getByText('AI 清晰化候选 · 64 × 96', { exact: true }).waitFor();
  const [file] = await Promise.all([page.waitForEvent('download'), download.click()]);
  assert.equal(await file.failure(), null);
  const native = await file.path();
  const { readFile } = await import('node:fs/promises');
  const bytes = await readFile(native);
  assert.deepEqual([...bytes.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
  assert.equal(bytes.readUInt32BE(16), 64); assert.equal(bytes.readUInt32BE(20), 96);
  assert.deepEqual(await page.evaluate(async pid => (await (await fetch(`/api/projects/${pid}/plan`)).json()), projectId), before);
  const choose = panel.getByLabel('清晰化原素材');
  const originalSource = first.payload.enhancement.source;
  await panel.getByLabel('上传清晰化素材').setInputFiles({ name: 'broken.png', mimeType: 'image/png', buffer: Buffer.from('broken picture') });
  await page.waitForFunction(source => document.querySelector('[aria-label="清晰化原素材"]')?.value !== source, originalSource);
  await panel.getByRole('button', { name: '开始 AI 清晰化 · 2×', exact: true }).click();
  await panel.getByText(/无法处理素材/).waitFor();
  assert.ok(await choose.isEnabled());
  assert.equal((await tasks()).length, 1);

  await choose.selectOption(originalSource);
  await panel.getByRole('button', { name: '开始 AI 清晰化 · 2×', exact: true }).click();
  await page.waitForFunction(async pid => (await (await fetch(`/api/projects/${pid}/tasks`)).json()).tasks.length === 2, projectId);
  const second = (await tasks()).find(task => task.id !== first.id);
  await call({ operation: 'complete', project_id: projectId, task_id: second.id });
  await page.getByText('已完成 · 待比较 · ' + second.id.slice(0, 8), { exact: true }).waitFor({ timeout: 15000 });
  await panel.getByRole('button', { name: '图片 · ' + first.id.slice(0, 8) + ' · 已完成 · 待比较', exact: true }).click();
  await page.getByText('已完成 · 待比较 · ' + first.id.slice(0, 8), { exact: true }).waitFor();
  assert.equal((await tasks()).length, 2);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  await page.screenshot({ path: resolve(process.argv[3], 'enhancement-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: resolve(process.argv[3], 'enhancement-desktop.png'), fullPage: true });
  assert.deepEqual(errors, []);
  const { writeFile } = await import('node:fs/promises');
  await writeFile(resolve(process.argv[3], 'result.json'), JSON.stringify({ passed: true, tasks: (await tasks()).map(task => task.id), downloadBytes: bytes.length, downloadSize: [64, 96], execution: 'CPU synthetic fixture, no GPU' }, null, 2));
  console.log('PASS: general enhancement entry, upload, frozen submission, lost-response reload recovery with one POST, CPU fixture output publication, 2x preview, native PNG download, retained history, unchanged plan, responsive layout. GPU execution is stubbed.');
} catch (error) {
  if (page) { console.error((await page.locator('body').innerText()).slice(-9000)); await page.screenshot({ path: resolve(process.argv[3], 'failure.png'), fullPage: true }); }
  throw error;
} finally { await browser.close(); fixture.kill(); }
