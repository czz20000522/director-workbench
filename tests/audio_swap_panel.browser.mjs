// Isolated real-browser check: no live service, account, model or GPU requests.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';
import {createRoot} from 'react-dom/client';
import AudioSwapPanel from './src/AudioSwapPanel';
createRoot(document.getElementById('root')).render(<AudioSwapPanel projectId="new-work" videos={[{id:'v1',title:'镜头 1'}]} audioAssets={[{id:'a1',title:'配乐候选 A'}]} onComplete={()=>{}} />);`,
  loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const submissions = [];
  const controls = [];
  let receipt = { id: 'swap-1', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'gpu_busy', message: '等待共享执行资源' }, position: 1 } };
  await page.route('http://localhost:4196/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/audio-swap-capability')) return route.fulfill({ json: { available: true, missing: [] } });
    if (path.endsWith('/audio-swap-tasks')) { submissions.push(route.request().postDataJSON()); return route.fulfill({ json: receipt }); }
    if (path.endsWith('/stop')) { controls.push(path); receipt = { ...receipt, status: 'stopped', queue: { state: 'terminal' } }; }
    if (path.includes('/api/')) return route.fulfill({ json: receipt });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script>' + bundle.outputFiles[0].text + '</script>' });
  });
  await page.goto('http://localhost:4196/');

  assert.equal(await page.getByRole('button', { name: '生成换音轨候选' }).isEnabled(), false);
  await page.getByLabel('视频', { exact: true }).selectOption('v1');
  // 默认静音：只用叠加音频，所以原声音量框不出现。
  assert.equal(await page.getByLabel('原声音量（倍）').count(), 0);
  await page.getByLabel('叠加音频', { exact: true }).selectOption('a1');
  assert.equal(await page.getByRole('button', { name: '生成换音轨候选' }).isEnabled(), true);
  await page.getByLabel('叠加音量（倍）').fill('0.6');
  await page.getByLabel('从第几秒开始放').fill('1.5');
  await page.getByLabel('音频比视频短时').selectOption('once');
  await page.getByRole('button', { name: '生成换音轨候选' }).click();
  await page.waitForFunction(() => JSON.parse(localStorage.getItem('director-workbench:new-work:audio-swap:v1')).pending);
  await page.waitForTimeout(100);
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].source_asset_id, 'v1');
  assert.equal(submissions[0].overlay_asset_id, 'a1');
  assert.equal(submissions[0].original_audio, 'mute');
  assert.equal(submissions[0].overlay_gain, 0.6);
  assert.equal(submissions[0].start_seconds, 1.5);
  assert.equal(submissions[0].overlay_loop, false);
  assert.equal('gen_seconds' in submissions[0], false);
  await page.reload();
  assert.equal(await page.getByLabel('视频', { exact: true }).isDisabled(), true);
  assert.equal(submissions.length, 1, 'Reload must recover the receipt, not submit again');
  await page.getByRole('button', { name: '停止任务' }).click();
  await page.getByRole('button', { name: '再换一版音轨' }).waitFor();
  assert.deepEqual(controls, ['/api/projects/new-work/tasks/swap-1/stop']);
  console.log('AudioSwapPanel: explicit audio-only payload, mute/keep switch, frozen reload and stop passed');
} finally { await browser.close(); }
