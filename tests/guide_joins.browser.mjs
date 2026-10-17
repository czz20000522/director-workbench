// Actual isolated HTTP service fixtures, all browser network intercepted; no live instance.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const fixtureRoot = process.argv[2];
assert.ok(fixtureRoot, 'Pass the completed isolated test run directory');
// The guard keeps rglob inside the machine runtime root, so it has to ask paths.py for that
// root instead of repeating the literal: an override (DIRECTOR_DESKTOP_ROOT, or the runner's
// --runtime/--desktop) is exactly how a fixture run is pointed at a substitute host, and a
// literal silently rejects every one of those.
const cases = JSON.parse(execFileSync('./.venv/Scripts/python.exe', ['-c', `
import json,sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import paths
root = Path(sys.argv[1]).resolve()
allowed = paths.RUNTIME_ROOT.resolve()
assert root.is_relative_to(allowed), f'Fixture root must be under {allowed}, got {root}'
found = [json.loads(p.read_text(encoding='utf-8')) for p in root.rglob('guide-joins-browser.json')]
assert found, 'Run tests/test_guide_joins.py first: no guide-joins-browser.json below the given root'
print(json.dumps(found))
`, fixtureRoot], { encoding: 'utf8' }));
assert.equal(cases.length, 8);
const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import FirstUseGuide from './src/FirstUseGuide';import AssemblyEditPanel from './src/AssemblyEditPanel';createRoot(document.getElementById('root')).render(<><FirstUseGuide projectId="edit-test" refreshKey={1} onAction={()=>{}} onOpenProject={()=>{}}/><AssemblyEditPanel projectId="edit-test" planRevision={1} onPlanChanged={()=>{}} onBrowse={()=>{}} onOpenShot={()=>{}}/></>);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /\.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let fixture;
  await page.route('http://localhost:4195/**', route => {
    assert.equal(route.request().method(), 'GET', 'Reading must never create/review/adopt a candidate');
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/settings/guide') return route.fulfill({ json: fixture.guide });
    if (path === '/api/projects/edit-test/assembly/edit') return route.fulfill({ json: fixture.edit });
    if (path === '/media-file') return route.fulfill({ status: 204 });
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  for (const item of cases) {
    fixture = item;
    await page.goto('http://localhost:4195/');
    const step = item.guide.steps.find(row => row.id === 'joins');
    const button = page.getByRole('navigation', { name: '指引步骤' }).getByRole('button', { name: /检查相邻接点/ });
    if (item.case === 'single') {
      assert.equal(step.applicable, false);
      await page.getByRole('navigation', { name: '指引步骤' }).waitFor();
      assert.equal(await button.count(), 0);
      assert.equal(step.completed, false);
    } else {
      await button.click();
      assert.equal((await button.locator('span').textContent()) === '✓', step.completed);
      await page.getByRole('region', { name: '指引：检查相邻接点' }).getByRole('status').getByText(step.state_label, { exact: true }).waitFor();
      assert.equal(step.completed, item.case === 'approved');
      const panel = page.locator('.assembly-edit-panel');
      await panel.locator('summary').getByText(`${item.edit.joins.length} 个接点 · ${item.edit.join_review.remaining_joins.length} 个待处理`, { exact: true }).waitFor();
    }
    assert.deepEqual(errors, []);
  }
  console.log('Join guide browser: actual service no-videos/left/pending/approved/redo/stale/missing/single match UI; zero writes passed');
} finally { await browser.close(); }
