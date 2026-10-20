// Shared-service fixture JSON + real tiny CPU MP4/WAV; no live requests or generation.
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { build } from 'esbuild';
import { chromium } from 'playwright';

assert.ok(process.argv[2], 'Pass the isolated pytest fixture root');
const root = resolve(process.argv[2]);
function files(directory) { return readdirSync(directory, { withFileTypes: true }).flatMap(row => row.isDirectory() ? files(join(directory, row.name)) : [join(directory, row.name)]); }
const all = files(root);
const fixture = name => JSON.parse(readFileSync(all.find(path => path.endsWith(name)), 'utf8'));
const comparison = fixture('comparison-browser.json');
const timing = fixture('creative-browser.json');
const audioTask = fixture('audio-browser-result.json');
const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import CreativeInspectionPanel from './src/CreativeInspectionPanel';import AudioEditPanel from './src/AudioEditPanel';import AssemblyEditPanel from './src/AssemblyEditPanel';const root=createRoot(document.getElementById('root'));window.mount=(mode,key=1)=>root.render(mode==='audio'?<AudioEditPanel key={key} projectId="preset-test" audioAssets={[{id:'mp3',title:'MP3原音'}]} onComplete={()=>{}} onBrowse={()=>{}}/>:mode==='sound'?<AssemblyEditPanel key={key} projectId="preset-test" planRevision={1} onPlanChanged={()=>{}} onBrowse={()=>{}} onOpenShot={()=>{}}/>:<CreativeInspectionPanel key={key} projectId="preset-test" segmentId="S01" revision={key} onPlanChanged={()=>{}}/>);window.mount('inspection');`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /\.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  page.setDefaultTimeout(8000);
  const errors = [], writes = [];
  let data = comparison;
  page.on('pageerror', error => errors.push(error.message));
  let edit = { revision: 1, joins: [], blocking_joins: [], sound: { policy: 'segment_native', audio_asset_id: null, path: null },
    audio_assets: [{ id: 'mp3', title: 'MP3原音' }], target_duration_seconds: 5, timing_summary: { playback_seconds: 5, frame_count: 120, precision: '逐段按帧取整', segments: [] } };
  await page.route('http://localhost:4196/**', async route => {
    const request = route.request(), parsed = new URL(request.url()), path = parsed.pathname;
    if (path.endsWith('/versions')) return route.fulfill({ json: data });
    if (path.endsWith('/assembly/edit')) return route.fulfill({ json: edit });
    if (path.endsWith('/assembly/sound') && request.method() === 'PUT') {
      const body = request.postDataJSON(); writes.push({ path, body });
      edit = { ...edit, revision: 2, sound: { policy: body.policy, path: null } };
      return route.fulfill({ json: edit });
    }
    if (path.endsWith('/submission-receipt')) return writes.some(row => row.path.endsWith('/audio-edit-tasks')) ? route.fulfill({ json: audioTask }) : route.fulfill({ status: 404, json: { detail: '尚未提交' } });
    if (path.endsWith('/audio-edit-tasks')) { writes.push({ path, body: request.postDataJSON() }); return route.fulfill({ status: 202, json: audioTask }); }
    if (path.includes('/tasks/')) return route.fulfill({ json: audioTask });
    if (path.endsWith('/plan/segments/S01') && request.method() === 'PATCH') {
      writes.push({ path, body: request.postDataJSON() }); return route.fulfill({ json: { revision: data.revision + 1 } });
    }
    if (path === '/media-file') {
      const file = parsed.searchParams.get('path');
      if (file?.includes('unavailable')) return route.fulfill({ status: 404 });
      assert.ok(resolve(file).startsWith(root), 'Read only isolated fixtures');
      return route.fulfill({ contentType: file.endsWith('.wav') ? 'audio/wav' : 'video/mp4', body: readFileSync(file) });
    }
    assert.equal(request.method(), 'GET', 'No unapproved browser write');
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  await page.goto('http://localhost:4196/');
  await page.getByText('参考、时长与版本对照', { exact: true }).click();
  await page.waitForFunction(() => [...document.querySelectorAll('video')].length === 2 && [...document.querySelectorAll('video')].every(v => v.readyState >= 3));
  const align = page.getByRole('button', { name: '定位两侧片段起点' });
  // The comparison control is enabled by React state fed by the media elements' events, so it
  // follows their readyState by a render. Waiting for it keeps the coverage without sampling
  // it in the same tick as readyState; a gate that never recovers still fails here.
  const alignReady = async () => {
    const started = Date.now();
    for (;;) {
      if (await align.isEnabled()) return;
      assert.ok(Date.now() - started < 8000, 'The alignment control must become enabled once both sides are decodable');
      await page.waitForTimeout(10);
    }
  };
  await alignReady();
  await align.click();
  const media = await page.locator('video').evaluateAll(videos => videos.map(v => ({ duration: v.duration, ready: v.readyState, audio: v.mozHasAudio, currentTime: v.currentTime })));
  assert.equal(media[0].duration, .5);
  assert.equal(media[1].duration, 2);
  assert.ok(media.every(v => v.currentTime === 0));
  assert.ok((await page.locator('body').innerText()).includes('无音轨'));
  await page.getByLabel('右侧对照').selectOption('first');
  await page.waitForFunction(() => [...document.querySelectorAll('video')].every(v => v.readyState >= 3));
  await alignReady();
  await page.getByLabel('右侧对照').selectOption('reference');
  await page.getByText('重做理由与影响（仅预告，不提交）', { exact: true }).click();
  assert.ok((await page.locator('body').innerText()).includes('需主动核对输入'));
  assert.equal(writes.length, 0);
  await page.getByLabel('参考视频').selectOption('reference');
  assert.equal(writes[0].body.expected_revision, comparison.revision);
  assert.equal(writes[0].body.reference_asset_id, 'reference');
  data = timing;
  await page.evaluate(() => window.mount('inspection', 2));
  await page.getByText('参考、时长与版本对照', { exact: true }).click();
  assert.ok((await page.locator('body').innerText()).includes('模型对齐的预计候选'));
  assert.ok((await page.locator('body').innerText()).includes('当前候选实际测量'));
  assert.ok((await page.locator('body').innerText()).includes('装配采用的取片'));
  assert.ok(await align.isDisabled()); // No reference does not fake a playable comparison.
  data = structuredClone(comparison);
  data.comparison_sources[0].path = 'fixtures/unavailable.mp4';
  await page.evaluate(() => window.mount('inspection', 3));
  await page.getByText('参考、时长与版本对照', { exact: true }).click();
  await page.getByText('文件不能播放，请核对或重新上传。', { exact: true }).waitFor();
  assert.ok(await align.isDisabled());
  // Recover from bad source/cache entry without removing the readiness gate.
  data = comparison;
  await page.evaluate(() => window.mount('inspection', 4));
  await page.getByText('参考、时长与版本对照', { exact: true }).click();
  await page.waitForFunction(() => [...document.querySelectorAll('video')].every(v => v.readyState >= 3));
  await alignReady();
  await page.evaluate(() => window.mount('sound'));
  await page.locator('.assembly-edit-panel > summary').click();
  await page.getByLabel('音轨方式').selectOption('mute');
  await page.getByRole('button', { name: '保存音轨方案', exact: true }).click();
  assert.deepEqual(writes[1].body, { expected_revision: 1, policy: 'mute', audio_asset_id: null });
  await page.evaluate(() => window.mount('audio'));
  await page.getByLabel('原音频', { exact: true }).selectOption('mp3');
  await page.getByLabel('保留时长（秒）', { exact: true }).fill('1.5');
  await page.getByLabel('源不足所选时长时，明确在尾部补静音').check();
  await page.getByRole('button', { name: '保存编辑候选', exact: true }).click();
  assert.equal(writes[2].body.pad_silence, true);
  assert.equal(writes[2].body.duration_seconds, 1.5);
  await page.getByLabel('音频编辑候选试听', { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('audio')?.readyState >= 3);
  await page.locator('audio').evaluate(v => v.play());
  await page.waitForFunction(() => document.querySelector('audio').currentTime > .1);
  await page.locator('audio').evaluate(v => v.pause());
  assert.ok(await page.getByRole('button', { name: '继续编辑', exact: true }).isEnabled());
  await page.reload(); // Original idempotency receipt restores the audition; no new submit.
  await page.evaluate(() => window.mount('audio'));
  await page.getByLabel('音频编辑候选试听', { exact: true }).waitFor();
  assert.equal(writes.filter(row => row.path.endsWith('/audio-edit-tasks')).length, 1);
  assert.deepEqual(errors, []);
  console.log('Creative review browser passed: real unequal/silent MP4, cache/error gates, revision-bound reference, four timing labels, explicit mute/pad, real WAV audition and same receipt after refresh. No live request/GPU/artistic acceptance.');
} finally { await browser.close(); }
