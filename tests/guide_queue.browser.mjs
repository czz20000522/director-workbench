// All browser requests are intercepted. No live service, production data or GPU.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import FirstUseGuide from './src/FirstUseGuide';import TaskQueueStatus from './src/TaskQueueStatus';window.render=(id)=>createRoot(document.getElementById('root')).render(<><FirstUseGuide projectId={id} refreshKey={1} onAction={action=>window.actions.push(action)} onOpenProject={id=>window.actions.push(id)}/><TaskQueueStatus task={{id:'task-b',asset_id:'S01',status:'scheduler_waiting',queue:{state:'waiting_resource',reason:{code:'gpu_busy',message:'等待共享 GPU 执行槽'},position:2}}}/></>);window.actions=[];window.render('test-b');`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /\.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const writes = []; const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let guide = { status: 'active', project_id: 'test-a', route: 'idea', completed_steps: ['goal'], revision: 4, steps: [
    { id: 'goal', title: '目标与参考', purpose: '决定这部作品表达什么。', prepare: '自己的故事或参考。', result: '创作目标已保存。', action: 'goal', completed: true },
    { id: 'project', title: '教学作品', purpose: '使用当前账号的独立小样。', prepare: '小样名称。', result: '保留已有作品。', action: 'project' },
    { id: 'preflight', title: '预检输入', purpose: '检查缺失输入。', prepare: '当前方案需要的素材和提示词。', result: '通过不等于画质通过。', action: 'preflight' },
    { id: 'joins', title: '检查接点', purpose: '检查前后画面和声音。', prepare: '两段当前视频。', result: '逐个审核接点。', action: 'joins', applicable: false },
  ] };
  await page.route('http://localhost:4196/**', route => {
    const request = route.request();
    if (new URL(request.url()).pathname === '/api/settings/guide') {
      if (request.method() === 'PATCH') { const changes = request.postDataJSON(); writes.push(changes); assert.equal(changes.expected_revision, guide.revision); guide = { ...guide, ...changes, revision: guide.revision + 1 }; }
      return route.fulfill({ json: guide });
    }
    assert.equal(request.method(), 'GET', 'navigation must not create production tasks');
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  await page.goto('http://localhost:4196/');
  await page.getByRole('region', { name: '指引：教学作品' }).waitFor();
  await page.getByRole('button', { name: '打开这一步' }).click();
  assert.deepEqual(await page.evaluate(() => window.actions), ['project']);
  assert.equal(writes.length, 0);
  await page.getByRole('button', { name: '从参考开始' }).click();
  assert.equal(writes.at(-1).route, 'reference');
  await page.getByRole('button', { name: '在当前作品继续指引' }).click();
  assert.equal(writes.at(-1).project_id, 'test-b');
  await page.getByRole('button', { name: '我已了解这一步' }).click();
  await page.getByRole('region', { name: '指引：预检输入' }).waitFor();
  assert.deepEqual(writes.at(-1).completed_steps, ['goal', 'project']);
  await page.getByRole('button', { name: '跳过指引' }).click();
  await page.getByRole('button', { name: '重新打开使用指引' }).waitFor();
  await page.reload();
  await page.getByRole('button', { name: '重新打开使用指引' }).click();
  await page.getByRole('region', { name: '指引：预检输入' }).waitFor();
  assert.equal(await page.getByRole('button', { name: /检查接点/ }).count(), 0);
  await page.getByRole('status', { name: '任务排队 S01' }).getByText(/等待共享 GPU 执行槽 · 队列位置 2/).waitFor();
  await page.setViewportSize({ width: 800, height: 700 });
  assert.deepEqual(errors, []);
  console.log('Guide / queue browser: persisted continue, skip/reopen, route/project, server applicable steps and queue metadata; zero generation passed');
} finally { await browser.close(); }
