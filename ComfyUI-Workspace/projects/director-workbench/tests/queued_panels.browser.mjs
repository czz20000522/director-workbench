// Previously admitted requests restored through existing HTTP receipts, no generation.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import KeyframePanel from './src/KeyframePanel';import AudioEditPanel from './src/AudioEditPanel';
const audio={source_asset_id:'voice',start_seconds:0,duration_seconds:5,gain:1,fade_in_seconds:0,fade_out_seconds:0,background_gain:.25,background_offset_seconds:0};
localStorage.setItem('director-workbench:panel-test:keyframe:v1',JSON.stringify({mode:'text',prompt:'人物参考',seed:42,pending:{mode:'text',prompt:'人物参考',seed:42,idempotency_key:'keyframe-key'}}));
localStorage.setItem('director-workbench:panel-test:audio-edit:v1',JSON.stringify({draft:audio,pending:{...audio,idempotency_key:'audio-key'}}));
createRoot(document.getElementById('root')).render(<><KeyframePanel projectId="panel-test" options={[]} onBrowse={()=>{}} onComplete={()=>{}}/><AudioEditPanel projectId="panel-test" audioAssets={[{id:'voice',title:'声音素材'}]} onBrowse={()=>{}} onComplete={()=>{}}/></>);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage(); const controls = []; const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const receipt = id => ({ id, status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'waiting_execution_slot', message: '等待共享执行资源' }, position: 2 } });
  const records = { 'keyframe-task': receipt('keyframe-task'), 'audio-task': receipt('audio-task') };
  await page.route('http://localhost:4194/**', route => {
    const request = route.request(); const url = new URL(request.url());
    if (url.pathname.endsWith('/keyframe-capability')) return route.fulfill({ json: { available: true } });
    if (url.pathname.endsWith('/audio-edit-capability')) return route.fulfill({ json: { available: true } });
    if (url.pathname.endsWith('/submission-receipt')) return route.fulfill({ json: records[url.searchParams.get('key') === 'keyframe-key' ? 'keyframe-task' : 'audio-task'] });
    if (url.pathname.includes('/tasks/')) {
      const id = url.pathname.split('/tasks/')[1].split('/')[0];
      if (request.method() === 'POST') { controls.push(url.pathname); assert.ok(url.pathname.endsWith('/stop')); records[id] = { ...records[id], status: 'stopped', queue: { state: 'terminal' } }; }
      return route.fulfill({ json: records[id] });
    }
    assert.equal(request.method(), 'GET', 'must recover queued receipt without new task admission');
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  await page.goto('http://localhost:4194/');
  for (const summary of await page.locator('summary').all()) await summary.click();
  await page.getByRole('status', { name: '任务排队 关键帧' }).getByText(/等待共享执行资源 · 队列位置 2/).waitFor();
  await page.getByRole('status', { name: '任务排队 音频编辑' }).getByText(/等待共享执行资源 · 队列位置 2/).waitFor();
  await page.getByRole('button', { name: '停止关键帧任务' }).click();
  await page.getByRole('button', { name: '停止音频编辑' }).click();
  await page.getByRole('button', { name: '准备下一张关键帧' }).waitFor();
  await page.getByRole('button', { name: '继续编辑' }).waitFor();
  assert.deepEqual(controls, ['/api/projects/panel-test/tasks/keyframe-task/stop', '/api/projects/panel-test/tasks/audio-task/stop']);
  assert.deepEqual(errors, []);
  console.log('Queued panels browser: keyframe/audio restore canonical queue receipt and cancel via existing task API, no repeated admission passed');
} finally { await browser.close(); }
