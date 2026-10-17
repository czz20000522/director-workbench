// Isolated real-browser check: no live service, account, model or GPU requests.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const result = await build({ stdin: { contents: `import React from 'react';
import {createRoot} from 'react-dom/client';
import SpeechPanel from './src/SpeechPanel';
createRoot(document.getElementById('root')).render(<SpeechPanel projectId="new-work" options={[]} onBrowse={()=>{}} onComplete={()=>{}} />);`,
  loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const submissions = [];
  const controls = [];
  let receipt = { id: 'candidate-1', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'gpu_busy', message: '等待共享执行资源' }, position: 2 } };
  await page.route('http://localhost:4199/**', async route => {
    const request = route.request();
    if (request.url().endsWith('/speech-capability')) return route.fulfill({ json: { available: true } });
    if (request.url().endsWith('/speech-tasks')) {
      submissions.push(request.postDataJSON());
      return route.fulfill({ json: receipt });
    }
    if (request.url().endsWith('/stop')) { controls.push(new URL(request.url()).pathname); receipt = { ...receipt, status: 'stopped', queue: { state: 'terminal' } }; }
    if (request.url().includes('/api/')) return route.fulfill({ json: receipt });
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script>' + result.outputFiles[0].text + '</script>' });
  });
  await page.goto('http://localhost:4199/');
  await page.locator('summary').first().click();
  await page.getByLabel('台词', { exact: true }).fill('天快黑了。');
  assert.equal(await page.getByRole('button', { name: '生成声音候选' }).isEnabled(), false);
  await page.getByLabel('声音来源').selectOption('design');
  assert.equal(await page.getByRole('button', { name: '生成声音候选' }).isEnabled(), false);
  await page.getByLabel('音色描述').fill('年长男性，低沉沙哑');
  await page.getByRole('button', { name: '生成声音候选' }).click();
  await page.waitForFunction(() => JSON.parse(localStorage.getItem('director-workbench:new-work:speech:v1')).pending);
  await page.waitForTimeout(100);
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].mode, 'design');
  assert.equal(submissions[0].voice_description, '年长男性，低沉沙哑');
  assert.equal('voice_reference' in submissions[0], false);
  await page.reload();
  await page.locator('summary').first().click();
  assert.equal(await page.getByLabel('声音来源').inputValue(), 'design');
  assert.equal(await page.getByLabel('音色描述').isDisabled(), true);
  assert.equal(submissions.length, 1, 'Reload must recover the receipt, not submit again');
  await page.getByRole('status', { name: '任务排队 声音' }).getByText(/等待共享执行资源 · 队列位置 2/).waitFor();
  await page.getByRole('button', { name: '停止声音任务' }).click();
  await page.getByRole('button', { name: '准备下一条台词' }).waitFor();
  assert.deepEqual(controls, ['/api/projects/new-work/tasks/candidate-1/stop']);
  assert.equal(submissions.length, 1);
  console.log('SpeechPanel: empty project design, payload exclusivity, frozen reload passed');
} finally { await browser.close(); }

