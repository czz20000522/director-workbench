// All HTTP intercepted; no real Agent, service, media operation, GPU or cloud request.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import AgentPageBridge from './src/components/AgentPageBridge';import ConnectAgent from './src/components/ConnectAgent';
window.presented=[];window.mode='presented';createRoot(document.getElementById('root')).render(<React.StrictMode><ConnectAgent/><AgentPageBridge onAction={async action=>{window.presented.push(action);if(action.kind==='show_draft'&&window.mode!=='takeover')document.getElementById('draft').value=action.values.prompt||'';return {status:window.mode,message:window.mode==='not_presented'?'浏览器拒绝自动播放':undefined}}}/><textarea id="draft" aria-label="页面草稿" defaultValue="人工原文"/><p id="blank">普通页面文字</p></React.StrictMode>);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const errors = []; const writes = []; const pageReads = []; const pages = new Map(); let failAck = false; let failRead = false;
function action(id, seq, kind = 'show_draft', values = {}) { return { id, seq, kind, target: { project_id: 'test-project' }, values, status: 'pending', expires_at: Date.now() / 1000 + 300 }; }
async function waitForReceipt(page, pageId, actionId) {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (pages.get(pageId).actions.find(item => item.id === actionId)?.status !== 'pending') return;
    await page.waitForTimeout(100);
  }
  assert.fail(`Receipt did not complete: ${pageId}/${actionId}`);
}
async function openPage(pageId, initial = []) {
  pages.set(pageId, { actions: initial, enabled: false, registration: 0, pageKey: '' });
  const page = await browser.newPage(); page.setDefaultTimeout(8000);
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(id => sessionStorage.setItem('director-workbench:agent-page:v1', id), pageId);
  await page.route('http://localhost:4199/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    if (!path.startsWith('/api/')) return route.fulfill({ contentType: 'text/html', body: `<title>页面桥接验收</title><div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
    assert.ok(path.startsWith('/api/agent/pages'), `Only page bridge API permitted: ${path}`);
    const body = request.postDataJSON();
    if (request.method() === 'POST') writes.push({ pageId, path, body, pageKey: request.headers()['x-agent-page-key'] });
    if (path === '/api/agent/pages') {
      assert.equal(request.method(), 'POST'); assert.equal(body.page_id, pageId);
      assert.equal(typeof body.enabled, 'boolean');
      const state = pages.get(pageId); state.enabled = body.enabled;
      const suppliedKey = request.headers()['x-agent-page-key'];
      if (suppliedKey) assert.equal(suppliedKey, state.pageKey, 'Permission changes keep the current page lifetime nonce');
      else state.pageKey = `${pageId}-private-nonce-${++state.registration}`;
      return route.fulfill({ json: { page_id: pageId, page_key: state.pageKey } });
    }
    const match = path.match(/^\/api\/agent\/pages\/([^/]+)\/actions(?:\/([^/]+)\/ack)?$/);
    assert.ok(match, `Unexpected page bridge request: ${path}`);
    assert.equal(match[1], pageId, 'Each tab only requests its own page');
    const state = pages.get(pageId);
    if (match[2]) {
      assert.equal(request.method(), 'POST');
      assert.equal(request.headers()['x-agent-page-key'], state.pageKey, 'Only the registered page nonce can acknowledge its action');
      assert.ok(['presented', 'not_presented', 'takeover'].includes(body.status));
      if (failAck && match[2] === 'lost-ack') { failAck = false; return route.abort('failed'); }
      const record = state.actions.find(item => item.id === match[2]); assert.ok(record);
      if (record.id === 'server-takeover') {
        record.status = 'takeover'; record.message = '服务端已记录用户接管';
        return route.fulfill({ json: record });
      }
      Object.assign(record, body); return route.fulfill({ json: record });
    }
    assert.equal(request.method(), 'GET');
    assert.equal(request.headers()['x-agent-page-key'], state.pageKey, 'Browser action reads renew the page lease with its current nonce');
    pageReads.push({ pageId, pageKey: request.headers()['x-agent-page-key'] });
    if (failRead) return route.fulfill({ status: 401, json: { detail: 'login expired' } });
    const after = Number(url.searchParams.get('after'));
    return route.fulfill({ json: { actions: state.actions.filter(item => item.seq > after).toReversed(), cursor: Math.max(0, ...state.actions.map(item => item.seq)) } });
  });
  await page.goto('http://localhost:4199/');
  await page.getByRole('checkbox', { name: '允许 Agent 引导此标签页' }).waitFor();
  await page.waitForFunction(() => !document.querySelector('input[type=checkbox]').disabled);
  return page;
}
try {
  const first = await openPage('tab-one', [action('old-navigation', 1, 'navigate', { page: 'shots' })]);
  assert.equal(await first.getByRole('checkbox', { name: '允许 Agent 引导此标签页' }).isChecked(), false);
  assert.deepEqual(await first.evaluate(() => window.presented), []);
  assert.ok(writes.filter(write => write.pageId === 'tab-one').every(write => write.body.enabled === false));
  assert.equal(await first.getByLabel('页面草稿').inputValue(), '人工原文');

  // Discovery is honest and uses only same-origin URLs; no machine paths or secret fields.
  await first.locator('details > summary').click();
  const links = await first.getByRole('navigation', { name: 'Agent 接入说明' }).locator('a').evaluateAll(items => items.map(item => item.getAttribute('href')));
  assert.deepEqual(links, ['/.well-known/director-workbench.json', '/agent/start', '/agent/skill/SKILL.md', '/agent/capabilities?group=creation', '/mcp/']);
  assert.equal(await first.locator('details > p a').getAttribute('href'), '/agent/start');
  assert.equal(await first.locator('details > p a').textContent(), 'http://localhost:4199/agent/start');
  assert.doesNotMatch(await first.locator('details').textContent(), /[A-Z]:\\|已连接成功|已上线/);

  // New actions remain inert before permission. Poll advances history without presentation.
  pages.get('tab-one').actions.push(action('disabled-draft', 2, 'show_draft', { prompt: '未经允许不能改写' }));
  await first.waitForTimeout(1200);
  assert.deepEqual(await first.evaluate(() => window.presented), []);
  assert.equal(await first.getByLabel('页面草稿').inputValue(), '人工原文');
  await first.getByRole('checkbox', { name: '允许 Agent 引导此标签页' }).click();
  await first.waitForFunction(() => document.querySelector('input[type=checkbox]').checked);
  assert.equal(pages.get('tab-one').enabled, true);
  await first.locator('details > summary').click();
  await first.locator('#blank').click();
  await first.evaluate(() => document.getElementById('draft').dispatchEvent(new Event('input', { bubbles: true })));
  assert.equal(await first.getByRole('checkbox', { name: '允许 Agent 引导此标签页' }).isChecked(), true, 'Connect controls, plain text and programmatic input do not revoke permission');
  assert.ok(writes.filter(write => write.pageId === 'tab-one' && write.path === '/api/agent/pages').some(write => write.body.enabled && write.pageKey === pages.get('tab-one').pageKey));
  failAck = true;
  pages.get('tab-one').actions.push(action('lost-ack', 3, 'show_draft', { prompt: 'Agent 可见草稿' }), action('ordered-next', 4, 'navigate', { page: 'shots' }));
  await first.waitForFunction(() => window.presented.length === 2);
  assert.deepEqual(await first.evaluate(() => window.presented.map(item => item.seq)), [3, 4]);
  assert.equal(await first.getByLabel('页面草稿').inputValue(), 'Agent 可见草稿');
  assert.deepEqual(writes.filter(write => write.path.endsWith('/ack')).map(write => write.path.split('/').at(-2)), ['lost-ack', 'lost-ack', 'ordered-next']);
  const lostAckWrites = writes.filter(write => write.path.endsWith('/lost-ack/ack'));
  assert.equal(lostAckWrites[0].pageKey, lostAckWrites[1].pageKey, 'An uncertain ACK retries with the identical nonce');
  assert.doesNotMatch(await first.locator('body').textContent(), /private-nonce/);
  assert.doesNotMatch(await first.evaluate(() => JSON.stringify(window.presented)), /private-nonce|page_key/);
  assert.doesNotMatch(await first.evaluate(() => JSON.stringify({ ...sessionStorage })), /private-nonce/);

  // A second tab has its own ID and an independent opt-in; actions cannot steal another page.
  const second = await openPage('tab-two');
  assert.equal(await second.getByLabel('页面编号').textContent(), 'tab-two');
  assert.ok(pageReads.some(read => read.pageId === 'tab-one') && pageReads.some(read => read.pageId === 'tab-two'));
  assert.equal(await first.getByLabel('页面编号').textContent(), 'tab-one');
  await second.getByRole('checkbox', { name: '允许 Agent 引导此标签页' }).click();
  await second.waitForFunction(() => document.querySelector('input[type=checkbox]').checked);
  pages.get('tab-two').actions.push(action('second-tab-action', 1, 'show_draft', { prompt: '只属于第二页' }));
  await second.waitForFunction(() => window.presented.length === 1);
  assert.equal(await second.getByLabel('页面草稿').inputValue(), '只属于第二页');
  assert.equal(await first.getByLabel('页面草稿').inputValue(), 'Agent 可见草稿');

  // Manual takeover disables binding and returns takeover for subsequent pending actions.
  await first.getByRole('button', { name: '手动接管此标签页' }).click();
  await first.waitForFunction(() => !document.querySelector('input[type=checkbox]').checked && !document.querySelector('input[type=checkbox]').disabled);
  assert.equal(pages.get('tab-one').enabled, false);
  pages.get('tab-one').actions.push(action('after-takeover', 5, 'show_draft', { prompt: '不能覆盖人工输入' }));
  await first.getByLabel('页面草稿').fill('人工接管后的新原文');
  await first.waitForTimeout(1200);
  assert.equal(await first.getByLabel('页面草稿').inputValue(), '人工接管后的新原文');
  assert.equal(pages.get('tab-one').actions.find(item => item.id === 'after-takeover').status, 'takeover');
  assert.equal(await first.evaluate(() => window.presented.length), 2);

  // Editor conflict returned by the handler also revokes the binding and stops queued actions.
  await first.getByRole('checkbox', { name: '允许 Agent 引导此标签页' }).click();
  await first.waitForFunction(() => document.querySelector('input[type=checkbox]').checked);
  await first.evaluate(() => { window.mode = 'takeover'; });
  pages.get('tab-one').actions.push(action('editor-conflict', 6, 'show_draft', { prompt: '不能覆盖已编辑草稿' }), action('after-editor-conflict', 7, 'navigate'));
  await first.waitForFunction(() => window.presented.length === 3 && !document.querySelector('input[type=checkbox]').checked && !document.querySelector('input[type=checkbox]').disabled);
  await waitForReceipt(first, 'tab-one', 'after-editor-conflict');
  assert.equal(pages.get('tab-one').enabled, false);
  assert.equal(pages.get('tab-one').actions.find(item => item.id === 'editor-conflict').status, 'takeover');
  assert.equal(pages.get('tab-one').actions.find(item => item.id === 'after-editor-conflict').status, 'takeover');
  assert.equal(await first.getByLabel('页面草稿').inputValue(), '人工接管后的新原文');

  // Refresh retains page ID, disables authorization again and never replays pending/old actions.
  pages.get('tab-two').actions.push(action('before-refresh', 2, 'play', { asset_id: 'candidate' }));
  const secondWrites = writes.filter(write => write.pageId === 'tab-two' && write.path.endsWith('/ack')).length;
  await second.reload();
  await second.waitForFunction(() => !document.querySelector('input[type=checkbox]').disabled);
  assert.equal(await second.getByLabel('页面编号').textContent(), 'tab-two');
  assert.equal(await second.getByRole('checkbox', { name: '允许 Agent 引导此标签页' }).isChecked(), false);
  assert.deepEqual(await second.evaluate(() => window.presented), []);
  assert.equal(writes.filter(write => write.pageId === 'tab-two' && write.path.endsWith('/ack')).length, secondWrites);

  // Playback rejection and expired actions are truthful, never claimed as presented.
  await second.getByRole('checkbox', { name: '允许 Agent 引导此标签页' }).click();
  await second.waitForFunction(() => document.querySelector('input[type=checkbox]').checked);
  await second.evaluate(() => { window.mode = 'not_presented'; });
  pages.get('tab-two').actions.push(action('blocked-play', 3, 'play'));
  await second.getByRole('status').getByText(/浏览器拒绝自动播放/).waitFor();
  assert.equal(pages.get('tab-two').actions.find(item => item.id === 'blocked-play').status, 'not_presented');
  const expiredAction = action('expired', 4, 'navigate'); expiredAction.expires_at = Date.now() / 1000 - 5;
  pages.get('tab-two').actions.push(expiredAction);
  await second.getByRole('status').getByText(/页面动作已过期，未执行/).waitFor();
  assert.equal(await second.evaluate(() => window.presented.length), 1);
  assert.equal(expiredAction.status, 'not_presented');
  assert.equal(writes.find(write => write.path.endsWith('/expired/ack')).body.status, 'not_presented');

  // The authoritative server takeover overrides a late presented ACK and stops queued work.
  await second.evaluate(() => { window.mode = 'presented'; });
  pages.get('tab-two').actions.push(action('server-takeover', 5, 'navigate'), action('after-server-takeover', 6, 'navigate'));
  await second.waitForFunction(() => !document.querySelector('input[type=checkbox]').checked && !document.querySelector('input[type=checkbox]').disabled);
  await waitForReceipt(second, 'tab-two', 'after-server-takeover');
  assert.equal(pages.get('tab-two').actions.find(item => item.id === 'after-server-takeover').status, 'takeover');
  assert.equal(await second.evaluate(() => window.presented.length), 2, 'A terminal server takeover does not repeat the handler or present subsequent work');
  assert.equal(writes.filter(write => write.path.endsWith('/server-takeover/ack')).length, 1, 'A terminal server ACK is not retried');
  assert.match(await second.locator('[aria-label="Agent 标签页引导"] p[role="status"]').textContent(), /已手动接管/);

  // Lost authentication is a single persistent error, with no false presentation receipt.
  failRead = true;
  await second.getByRole('alert').getByText('登录已过期，页面引导暂不可用。', { exact: true }).waitFor();
  await second.waitForTimeout(1200);
  assert.equal(await second.getByRole('alert').count(), 1);
  assert.equal(await second.evaluate(() => window.presented.length), 2);
  assert.deepEqual(errors, []);
  console.log('Agent page bridge browser passed: discovery links, default opt-out, original editor text, ordered actions/ACK retry, page nonce/lease reads, two tabs, manual/editor takeover, trusted-control exclusions, authoritative server takeover ends ACK job/no repeated handler, refresh ID/no replay, blocked playback, expiry, persistent 401; StrictMode and all HTTP intercepted.');
} finally { await browser.close(); }

