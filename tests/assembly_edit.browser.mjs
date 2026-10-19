// Isolated browser check: all requests are mocked; no live service or GPU work.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import AssemblyEditPanel from './src/AssemblyEditPanel';createRoot(document.getElementById('root')).render(<AssemblyEditPanel projectId="browser-test" planRevision={3} assemblyPath="output/master.mp4" onPlanChanged={() => {}} onBrowse={() => {}} onOpenShot={() => {}} />);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /\.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  const writes = [];
  page.on('pageerror', error => errors.push(error.message));
  const edit = { revision: 3, joins: [{ key: 'S01>S02', left_id: 'S01', right_id: 'S02', cut_seconds: 5,
    left_preview_start: 3.5, right_preview_end: 1.5, status: 'pending', constraint: '', note: '',
    signature: { left_video: 'output/S01.mp4', right_video: 'output/S02.mp4' } }],
  blocking_joins: [], sound: { policy: 'segment_native', audio_asset_id: null, path: null, duration_seconds: null, selected: false, selection_source: null },
  audio_assets: [{ id: 'audio-master', title: '完整音轨' }], target_duration_seconds: 10,
  continuity_check: { schema_version: 1, revision: 3, read_only: true, automatic_actions: false, ready: false,
    counts: { blocker: 1, warning: 1, pass: 2, not_applicable: 0 },
    inputs: { segment_order: ['S01', 'S02'], join_review: 'legacy_untracked',
      sound: { selected: false, policy: 'segment_native', selection_source: null } },
    checks: [{ code: 'sound_policy_required', category: 'sound', severity: 'blocker',
      message: '装配前须显式选择音轨方式：保留各段原音、统一完整音轨或全片静音。',
      object: { kind: 'assembly', id: 'sound' } },
    { code: 'join_review_untracked', category: 'join', severity: 'warning',
      message: 'S01>S02 尚无接点审核记录（旧版装配项目）；人工审核仍是装配权威。', evidence: { join: 'S01>S02' } },
    { code: 'sound_policy', category: 'sound', severity: 'pass', message: '已显式选择音轨方式：保留各段原音。' }],
    blockers: [{ code: 'sound_policy_required', category: 'sound', severity: 'blocker',
      message: '装配前须显式选择音轨方式：保留各段原音、统一完整音轨或全片静音。',
      object: { kind: 'assembly', id: 'sound' } }],
    note: '只读检查：不自动修复、不自动选择候选、不自动采用或审核；阻断项需人工处理后重新检查。' },
  continuity: { advisory: true, available: true, note: '启发式、非艺术判断', joins: [{ key: 'S01>S02', left_id: 'S01', right_id: 'S02', advisory: true, available: true, status: 'ok', warnings: [], video: { differences: {} }, frames: { mean_absolute_rgb_difference: 0.12, brightness_delta: 2.1 }, audio: { differences: {} } }] } };
  await page.route('http://localhost:4198/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/projects/browser-test/assembly/edit') return route.fulfill({ json: edit });
    if (path === '/api/projects/browser-test/assembly/preflight') return route.fulfill({ json: { ...edit, ready: true, missing_videos: [] } });
    if (request.method() === 'PUT') {
      writes.push({ path, body: request.postDataJSON() });
      edit.revision += 1;
      if (path.endsWith('/sound')) edit.sound = { policy: 'complete_master', audio_asset_id: 'audio-master', path: 'output/master.wav', duration_seconds: 11, selected: true, selection_source: 'assembly_edit' };
      else { edit.joins[0].status = 'approved'; edit.joins[0].constraint = request.postDataJSON().constraint; edit.joins[0].note = request.postDataJSON().note; }
      return route.fulfill({ json: edit });
    }
    if (path === '/media-file') return route.fulfill({ status: 204 });
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  await page.goto('http://localhost:4198/');
  await page.locator('.assembly-edit-panel > summary').click();
  await page.getByText('自动检查（仅供参考）').waitFor();
  await page.getByText('已测量').waitFor();
  await page.getByText('跨分镜连续性检查（只读）').waitFor();
  await page.getByText('1 个阻断项').waitFor();
  await page.getByText('装配前须显式选择音轨方式：保留各段原音、统一完整音轨或全片静音。').waitFor();
  await page.getByText('尚未显式选择音轨方式；装配预检与提交会以 sound_policy_required 阻断，不会自动保留原音。').waitFor();
  await page.getByRole('button', { name: /S01 → S02/ }).click();
  await page.getByLabel('连续性约束').fill('方向与雨声连续');
  await page.getByLabel('审核记录').fill('两个切点已听看');
  await page.getByRole('button', { name: '已通过' }).click();
  assert.equal(writes[0].body.expected_revision, 3);
  assert.equal(writes[0].body.status, 'approved');
  assert.equal(writes[0].body.constraint, '方向与雨声连续');
  await page.getByLabel('音轨方式').selectOption('complete_master');
  await page.getByLabel('已登记音频').selectOption('audio-master');
  await page.getByRole('button', { name: '保存音轨方案' }).click();
  assert.equal(writes[1].body.expected_revision, 4);
  assert.equal(writes[1].body.audio_asset_id, 'audio-master');
  await page.getByRole('button', { name: '无 GPU 装配预检' }).click();
  await page.getByText('预检通过：接点和音轨可用于装配。').waitFor();
  assert.deepEqual(errors, []);
  console.log('Assembly edit browser: join review, registered master selection and no-GPU preflight passed');
} finally { await browser.close(); }
