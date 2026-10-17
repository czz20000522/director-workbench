// 受控 `<details open={...}>` 面板的取证驱动：真实后端与登录，只观察展开状态，不生成作品。
// 覆盖 `<details className="quick-agent-settings" open={!quickShell}>` 的两个面板。
// 用法：node tests/quick_mode_panels.browser.mjs <项目 python> <一个尚未存在的运行目录>
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';
const require = createRequire(resolve(process.env.DIRECTOR_TEST_NODE_MODULES || 'node_modules', 'package.json'));
const { build } = require('esbuild');
const { chromium } = require('playwright');
assert.ok(process.argv[2] && process.argv[3], 'Pass project Python and a new isolated runtime path');
const CONNECT = '连接与 Agent 设置';
const PAGE = 'Agent 页面操作';
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
const observed = [];
try {
  const ready = await next(); assert.equal(ready.ready, true);
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    assert.equal(url.origin, 'http://localhost:4197', 'Never contact live services');
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><link rel="stylesheet" href="/bundle.css"><script src="/bundle.js"></script>' });
    if (url.pathname === '/bundle.js') return route.fulfill({ contentType: 'application/javascript', body: js });
    if (url.pathname === '/bundle.css') return route.fulfill({ contentType: 'text/css', body: css });
    if (url.pathname.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: '' });
    if (!url.pathname.startsWith('/api/') && !['/media-file', '/material-file', '/openapi.json'].includes(url.pathname)) return route.fulfill({ status: 404, body: '' });
    const value = await call({ method: request.method(), path: url.pathname + url.search, headers: await request.allHeaders(),
      ...(request.postDataBuffer() ? { body: request.postDataBuffer().toString('base64') } : {}) });
    return route.fulfill({ status: value.status, headers: value.headers, body: value.binary ? Buffer.from(value.binary, 'base64') : JSON.stringify(value.data) });
  });
  page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://localhost:4197');
  await page.getByLabel('用户名').fill('user001'); await page.getByLabel('密码').fill('one');
  await page.getByRole('button', { name: '进入工作台', exact: true }).click();
  // 按 summary 文字定位，避免依赖 DOM 顺序。
  const summary = label => page.locator(`details.quick-agent-settings > summary:text-is("${label}")`);
  const read = label => summary(label).evaluate(node => node.parentElement.open);
  const panels = () => page.locator('details.quick-agent-settings').evaluateAll(nodes => nodes.map(node => [node.querySelector(':scope > summary').textContent.trim(), node.open]));
  const quick = async () => (await page.locator('.director-workbench.director-quick').count()) > 0;
  // summary 的中心点必须真的能被点到（被吸顶侧栏盖住时这里会暴露出来）。
  const reachable = () => page.locator('details.quick-agent-settings').evaluateAll(nodes => nodes.map(node => {
    const summary = node.querySelector(':scope > summary');
    summary.scrollIntoView({ block: 'center' });
    const box = summary.getBoundingClientRect();
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return [summary.textContent.trim(), getComputedStyle(node).gridColumnStart, summary.contains(hit), hit ? `${hit.tagName}.${hit.className}` : null];
  }));
  const note = async label => { const value = await panels(); observed.push({ label, quick: await quick(), panels: value, reachable: await reachable() }); return value; };
  const toggle = async label => { await summary(label).click(); return [await read(CONNECT), await read(PAGE)]; };
  const quickMode = () => page.getByRole('button', { name: '快速生成', exact: true }).click();
  const fullMode = () => page.getByRole('button', { name: '作品、片段与更多功能', exact: true }).click();

  await summary(CONNECT).waitFor();
  assert.equal(await page.locator('details.quick-agent-settings').count(), 2, '两个设置面板');
  assert.deepEqual(await note('登录后'), [[CONNECT, false], [PAGE, false]], '快速模式默认收起两个设置面板');
  assert.deepEqual(await toggle(CONNECT), [true, false], '点击 summary 就地展开');
  await page.waitForTimeout(1500);
  await note('等待1.5秒（含任何轮询重渲染）');
  await page.getByRole('button', { name: '文生图 描述画面，生成一张图片' }).click();
  assert.deepEqual([await read(CONNECT), await read(PAGE)], [true, false], '无关重渲染不得重新合上用户展开的面板');
  await note('切换分类（强制重渲染）');
  // 用户报告的症状：展开的面板在模式往返后又被合上。
  await fullMode();
  await note('切到完整模式（完整模式的默认是都展开）');
  await quickMode();
  assert.deepEqual([await read(CONNECT), await read(PAGE)], [true, false], '快速↔完整往返不得重新合上用户展开的面板');
  await note('快速↔完整往返一次');
  await fullMode();
  assert.deepEqual(await toggle(CONNECT), [false, true], '收起只作用于被点的那一个面板');
  await note('完整模式收起 #0');
  await quickMode(); await fullMode();
  assert.deepEqual([await read(CONNECT), await read(PAGE)], [false, true], '模式往返不得重新打开用户已收起的面板');
  await note('再往返一次');
  await quickMode();
  assert.deepEqual(await toggle(PAGE), [false, true], '两个面板互相独立');
  await note('快速模式单独展开 #1');
  await page.reload();
  await summary(CONNECT).waitFor({ timeout: 15000 });
  assert.deepEqual(await note('刷新后'), [[CONNECT, false], [PAGE, false]], '刷新回到当前模式的默认状态');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ observations: observed, errors }, null, 1));
  console.log('PASS: 用户手动的展开/收起优先于派生默认值，模式往返不覆盖');
} catch (error) {
  console.error(JSON.stringify({ observations: observed }, null, 1));
  if (page) { console.error((await page.locator('body').innerText()).slice(-4000)); await page.screenshot({ path: resolve(process.argv[3], 'failure.png'), fullPage: true }); }
  throw error;
} finally { await browser.close(); fixture.kill(); }
