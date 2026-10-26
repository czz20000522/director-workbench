// Quick image entry starts blank; a stale receipt from a deleted task can be abandoned.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import KeyframePanel from './src/KeyframePanel';
localStorage.setItem('director-workbench:quick-tasks:keyframe:v1',JSON.stringify({mode:'text',prompt:'上一次的旧提示词',seed:42,pending:{mode:'text',prompt:'上一次的旧提示词',seed:42,idempotency_key:'stale-key'}}));
localStorage.setItem('director-workbench:stale-test:keyframe:v1',JSON.stringify({mode:'text',prompt:'已删除任务的旧提示词',seed:42,pending:{mode:'text',prompt:'已删除任务的旧提示词',seed:42,idempotency_key:'missing-key'}}));
createRoot(document.getElementById('root')).render(<><KeyframePanel projectId="quick-tasks" simple fresh initialMode="text" options={[]} onBrowse={()=>{}} onComplete={()=>{}}/><KeyframePanel projectId="stale-test" options={[]} onBrowse={()=>{}} onComplete={()=>{}}/></>);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });

const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage(); const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://localhost:4194/**', route => {
    const request = route.request(); const url = new URL(request.url());
    if (url.pathname.endsWith('/keyframe-capability')) return route.fulfill({ json: { available: true } });
    if (url.pathname.endsWith('/submission-receipt')) {
      return route.fulfill({ status: 404, json: { detail: { code: 'submission_not_found', message: '尚未找到已落库任务' } } });
    }
    assert.equal(request.method(), 'GET', 'recovery must not admit a new task');
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  await page.goto('http://localhost:4194/');

  const fresh = page.getByRole('region', { name: '生成图片设置' });
  const freshPrompt = fresh.getByRole('textbox', { name: '图片提示词' });
  await freshPrompt.waitFor();
  assert.equal(await freshPrompt.inputValue(), '', 'quick entry must not restore the previous draft');
  assert.ok(await freshPrompt.isEditable(), 'quick entry must start editable');
  assert.equal(await fresh.getByRole('button', { name: '放弃核对，重新填写' }).count(), 0, 'fresh entry must not inherit a stale receipt');

  await fresh.getByText('更多设置 · 幅面、输出规格、随机编号').click();
  await fresh.getByRole('heading', { name: '随机编号' }).waitFor({ timeout: 5000 });
  const seed = fresh.locator('input[type=number]');
  const before = await seed.inputValue();
  await fresh.getByRole('button', { name: '换一个' }).click();
  assert.notEqual(await seed.inputValue(), before, 'random seed button must change the number');

  const stale = page.getByRole('region', { name: '生成关键帧设置' });
  const stalePrompt = stale.getByRole('textbox', { name: '画面描述' });
  await stalePrompt.waitFor();
  assert.ok(!(await stalePrompt.isEditable()), 'an unresolved receipt keeps the form locked');
  await stale.getByRole('button', { name: '放弃核对，重新填写' }).click();
  assert.ok(await stalePrompt.isEditable(), 'abandoning a stale receipt unlocks the form');
  assert.equal(JSON.parse(await page.evaluate(() => localStorage.getItem('director-workbench:stale-test:keyframe:v1'))).pending, undefined);
  assert.deepEqual(errors, []);
  console.log('Keyframe recovery browser: quick entry starts blank; stale receipt can be abandoned passed');
} finally { await browser.close(); }
