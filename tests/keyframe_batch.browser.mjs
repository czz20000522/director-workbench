// Isolated real-browser check: batch prompts each become their own queued task.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';
import {createRoot} from 'react-dom/client';
import KeyframePanel from './src/KeyframePanel';
createRoot(document.getElementById('root')).render(<KeyframePanel projectId="quick-tasks" simple fresh initialMode="text" options={[]} onBrowse={()=>{}} onComplete={()=>{}} />);`,
  loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const submissions = [];
  await page.route('http://localhost:4192/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/keyframe-capability')) return route.fulfill({ json: { available: true, mode: 'text' } });
    if (path.endsWith('/keyframe-tasks')) {
      submissions.push(route.request().postDataJSON());
      return route.fulfill({ json: { id: `task-${submissions.length}`, status: 'scheduler_waiting' } });
    }
    if (path.includes('/api/')) return route.fulfill({ json: { status: 'scheduler_waiting' } });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script>' + bundle.outputFiles[0].text + '</script>' });
  });
  await page.goto('http://localhost:4192/');
  await page.getByRole('button', { name: '批量排队' }).click();
  await page.getByLabel('提示词列表').fill('雨夜霓虹的街角\n\n清晨空荡的地铁站\n黄昏海边的旧灯塔');
  await page.getByRole('button', { name: '排队生成' }).click();
  await page.waitForFunction(() => document.querySelector('.sf-post-message')?.textContent?.includes('已排队'));
  assert.equal(submissions.length, 3);
  assert.deepEqual(submissions.map(item => item.prompt), ['雨夜霓虹的街角', '清晨空荡的地铁站', '黄昏海边的旧灯塔']);
  assert.equal(new Set(submissions.map(item => item.idempotency_key)).size, 3);
  assert.ok(submissions.every(item => item.mode === 'text' && item.seed === submissions[0].seed));
  const report = await page.locator('.sf-post-message').first().innerText();
  assert.match(report, /已排队 3 条/);
  console.log('KeyframePanel: batched prompts queue as separate tasks passed');
} finally { await browser.close(); }
