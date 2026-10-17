// Uses the real backend guide/readiness functions; all HTTP is intercepted.
// No listener, production database, media executor or GPU.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const cases = JSON.parse(execFileSync('./.venv/Scripts/python.exe', ['-c', `
import json
from backend.guide import steps
from backend.readiness import build_project_readiness
rows=[]
for has_candidate,review in [(False,None),(True,None),(True,'pending_review'),(True,'approved'),(True,'changes_requested'),(True,'stale')]:
 readiness=build_project_readiness(project_id='fixture',plan={'segments':[], 'assembly':{'output':'fixture.mp4'} if has_candidate else None},stages=[],project_state={'reviews':{'MASTER':{'final':{'status':review}}}},tasks=[],validate=lambda *a:{'valid':True},assembly_preflight=lambda:{'ready':False})
 rows.append(dict(status='active',project_id='fixture',completed_steps=['assembly'],revision=0,steps=steps(readiness,has_project=True),readiness=readiness))
print(json.dumps(rows))
`], { encoding: 'utf8' }));
const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import FirstUseGuide from './src/FirstUseGuide';createRoot(document.getElementById('root')).render(<FirstUseGuide projectId="fixture" refreshKey={1} onAction={()=>{}} onOpenProject={()=>{}}/>);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /\.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let guide = cases[0];
  await page.route('http://localhost:4196/**', route => {
    assert.equal(route.request().method(), 'GET', 'Reading a guide must never review, adopt or generate');
    if (new URL(route.request().url()).pathname === '/api/settings/guide') return route.fulfill({ json: guide });
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  for (const fixture of cases) {
    guide = fixture;
    await page.goto('http://localhost:4196/');
    const step = fixture.steps.find(item => item.id === 'assembly');
    const button = page.getByRole('navigation', { name: '指引步骤' }).getByRole('button', { name: /装配并通过成片审核/ });
    await button.click();
    assert.equal((await button.locator('span').textContent()) === '✓', step.completed);
    const section = page.getByRole('region', { name: '指引：装配并通过成片审核' });
    await section.getByRole('status').getByText(step.state_label, { exact: true }).waitFor();
    await section.getByRole('button', { name: '已了解', exact: true }).waitFor();
    assert.equal(step.completed, fixture.readiness.assembly.state === 'adopted');
    const exportStep = fixture.steps.find(item => item.id === 'export');
    const exportButton = page.getByRole('navigation', { name: '指引步骤' }).getByRole('button', { name: /导出交付/ });
    assert.notEqual(await exportButton.locator('span').textContent(), '✓', 'Review approval is not a delivery receipt');
    await exportButton.click();
    await page.getByRole('region', { name: '指引：导出交付' }).getByRole('status').getByText(exportStep.state_label, { exact: true }).waitFor();
    assert.equal(exportStep.available, fixture.readiness.assembly.state === 'adopted');
    assert.deepEqual(errors, []);
  }
  console.log('Guide final-review browser: absent/pending/approved/redo/stale match server steps; learning progress never grants ✓; export available is not delivered; zero writes passed');
} finally { await browser.close(); }
