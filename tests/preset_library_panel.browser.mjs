// Isolated real-browser check: no live service, account, model or GPU requests.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const result = await build({ stdin: { contents: `import React from 'react';
import {createRoot} from 'react-dom/client';
import PresetLibraryPanel from './src/PresetLibraryPanel';
(window as unknown as { used?: unknown }).used = null;
createRoot(document.getElementById('root')).render(
  <PresetLibraryPanel kind="timbre" onUse={(params, name) => { (window as unknown as { used: unknown }).used = { params, name }; }} />);`,
  loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const deleted = [];
  const library = { library: {
    timbre: [
      { name: '沉稳中年旁白', params: { engine: 'voxcpm-2', voice_description: '四十岁上下的男性，音色低沉干净' }, builtin: true, modified: false },
      { name: '我的克隆音色', params: { engine: 'indextts-2.5', voice_reference: 'workspaces/x/voice.wav', generated_by: 'indextts-2.5' }, builtin: false, modified: false },
      { name: '沙哑老翁', params: { engine: 'voxcpm-2', voice_description: '七十岁男性，声音沙哑' }, builtin: true, modified: true },
      { name: '我的解说腔', params: { engine: 'voxcpm-2', voice_description: '三十岁女性，干脆' }, builtin: false, modified: false },
    ],
    music: [],
  } };
  await page.route('http://localhost:4197/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/preset-library') return route.fulfill({ json: library });
    if (route.request().method() === 'DELETE') {
      deleted.push(decodeURIComponent(path.split('/').pop()));
      library.library.timbre = library.library.timbre.filter(card => card.name !== '我的解说腔');
      return route.fulfill({ json: { removed: true } });
    }
    return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div><script>' + result.outputFiles[0].text + '</script>' });
  });
  await page.goto('http://localhost:4197/');

  assert.equal(await page.getByRole('heading', { name: '音色库' }).count(), 1);
  assert.equal(await page.getByText('四十岁上下的男性，音色低沉干净').count(), 1);
  assert.equal(await page.getByText('（已改）').count(), 1);
  // 每张音色卡都标出是哪台引擎做的。
  assert.equal(await page.getByRole('listitem').filter({ hasText: '沉稳中年旁白' }).getByText('内置起点 · 由 VoxCPM2 生成').count(), 1);
  assert.equal(await page.getByText('我的卡片 · 由 IndexTTS 2.5 生成').count(), 1);
  // Built-in cards are bring-in only; a saved card can also be deleted.
  assert.equal(await page.getByRole('button', { name: /删除 沉稳中年旁白/ }).count(), 0);
  assert.equal(await page.getByRole('button', { name: /删除 我的解说腔/ }).count(), 1);
  await page.getByRole('button', { name: '删除 我的解说腔' }).click();
  await page.waitForFunction(() => !Array.from(document.querySelectorAll('.pl-card strong')).some(node => node.textContent === '我的解说腔'));
  assert.deepEqual(deleted, ['我的解说腔']);

  await page.getByRole('listitem').filter({ hasText: '沉稳中年旁白' }).getByRole('button', { name: '带入' }).click();
  const used = await page.evaluate(() => window.used);
  assert.equal(used?.name, '沉稳中年旁白');
  assert.equal(used?.params.voice_description, '四十岁上下的男性，音色低沉干净');
  console.log('PresetLibraryPanel: built-in vs saved cards, delete scoping and explicit bring-in passed');
} finally { await browser.close(); }
