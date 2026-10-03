// Uses actual UI/helpers and isolated HTTP-produced JPEGs; intercepts every request.
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { isAbsolute, join, relative, resolve } from 'node:path';
import { build } from 'esbuild';
import { chromium } from 'playwright';

assert.ok(process.argv[2], 'Pass the completed test_media_qc.py fixture root');
const fixtureRoot = resolve(process.argv[2]);
function files(root) {
  return readdirSync(root, { withFileTypes: true }).flatMap(row => row.isDirectory()
    ? files(join(root, row.name)) : [join(root, row.name)]);
}
const fixturePath = files(fixtureRoot).find(path => path.endsWith('media-qc-browser.json'));
assert.ok(fixturePath, 'Run the real CPU QC test first');
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));
const bundle = await build({
  stdin: {
    contents: `import React,{useState} from 'react';import {createRoot} from 'react-dom/client';
import MaterialSelect from './src/MaterialSelect';import MediaToolsPanel from './src/MediaToolsPanel';
import {diagnosticMaterialPaths,isCreativeMaterial,projectMaterialOptions} from './src/materialOptions';
const fixture=${JSON.stringify(fixture)};
function Harness(){const [first,setFirst]=useState(fixture.diagnostic_path.replaceAll('\\\\','/').toUpperCase());
const [last,setLast]=useState('');const options=projectMaterialOptions(fixture.project,fixture.segments);
const excluded=diagnosticMaterialPaths(fixture.project);
return <><section aria-label="创作关键帧">{fixture.project.assets.filter(isCreativeMaterial).map(a=><span key={a.id}>{a.title}</span>)}</section>
<MaterialSelect label="首帧" kind="image" value={first} options={options} excludedValues={excluded} onChange={setFirst} onBrowse={()=>{}}/>
<MaterialSelect label="尾帧" kind="image" value={last} options={options} excludedValues={excluded} onChange={setLast} onBrowse={()=>{}}/>
<MediaToolsPanel projectId="preset-test" assets={[{id:'source',title:'Test video',kind:'video'}]} onComplete={()=>{}} onBrowse={()=>{}}/></>};
createRoot(document.getElementById('root')).render(<Harness/>);`,
    loader: 'tsx', resolveDir: process.cwd(),
  },
  bundle: true, write: false, format: 'iife', jsx: 'automatic',
});
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  page.setDefaultTimeout(8000);
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem('director-workbench:preset-test:media-tools:v1',
    JSON.stringify({ draft: { operation: 'video_qc', source_asset_id: 'source', sample_count: 3 },
      pending: { operation: 'video_qc', source_asset_id: 'source', sample_count: 3, idempotency_key: 'qc-browser' } })));
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    requests.push({ method: request.method(), path: url.pathname });
    assert.equal(url.origin, 'http://localhost:4199', 'No external network');
    assert.equal(request.method(), 'GET', 'Viewing QC must not write or generate');
    if (url.pathname === '/api/media-capability') return route.fulfill({ json: {
      operations: { video_qc: { available: true } },
    } });
    if (url.pathname.endsWith('/submission-receipt') || url.pathname.includes('/tasks/')) {
      return route.fulfill({ json: fixture.task });
    }
    if (url.pathname === '/media-file') {
      const path = resolve(url.searchParams.get('path'));
      const inside = relative(fixtureRoot, path);
      assert.ok(!inside.startsWith('..') && !isAbsolute(inside), 'Read only isolated test artifacts');
      return route.fulfill({ contentType: path.endsWith('.png') ? 'image/png' : 'image/jpeg',
        body: readFileSync(path) });
    }
    assert.equal(url.pathname, '/', 'Unexpected UI route');
    return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
  });
  await page.goto('http://localhost:4199/');
  const first = page.getByLabel('首帧', { exact: true });
  const last = page.getByLabel('尾帧', { exact: true });
  const ordinary = fixture.ordinary_path;
  // Existing diagnostic bindings are explained, disabled and never offered as a new option.
  await first.locator('option', { hasText: '已保存诊断引用' }).waitFor({ state: 'attached' });
  assert.ok(await first.locator('option', { hasText: '已保存诊断引用' }).isDisabled());
  assert.equal(await page.getByAltText('首帧预览', { exact: true }).count(), 0);
  for (const select of [first, last]) {
    const enabled = await select.locator('option:not(:disabled)').evaluateAll(rows => rows.map(row => row.value));
    assert.deepEqual(enabled, ['', ordinary]);
  }
  assert.equal(await page.getByRole('region', { name: '创作关键帧' }).innerText(), 'Ordinary image');
  await last.selectOption(ordinary);
  await first.selectOption(ordinary);
  await page.waitForFunction(() => [...document.querySelectorAll('img.director-material-preview')]
    .length === 2 && [...document.querySelectorAll('img.director-material-preview')]
      .every(image => image.complete && image.naturalWidth === 32 && image.naturalHeight === 48));
  // The same real contact sheet and frames remain available in their QC task.
  await page.getByRole('group', { name: '媒体工具' }).locator(':scope > summary').click();
  await page.getByRole('region', { name: '媒体检查报告' }).waitFor();
  const outputs = page.locator('figure img');
  assert.equal(await outputs.count(), 4);
  await page.waitForFunction(() => [...document.querySelectorAll('figure img')]
    .length === 4 && [...document.querySelectorAll('figure img')]
      .every(image => image.complete && image.naturalWidth > 0));
  assert.equal(await page.locator('figure a', { hasText: '查看报告文件' }).count(), 1);
  await page.reload();
  await page.getByRole('group', { name: '媒体工具' }).locator(':scope > summary').click();
  await page.waitForFunction(() => [...document.querySelectorAll('figure img')]
    .length === 4 && [...document.querySelectorAll('figure img')]
      .every(image => image.complete && image.naturalWidth > 0));
  assert.deepEqual(errors, []);
  assert.ok(requests.every(request => request.method === 'GET'));
  console.log('Media QC browser passed: legacy operation/purpose and path aliases excluded; saved diagnostic option disabled without preview; ordinary image selectable and decoded; four real QC JPEGs and report retained after reload; zero writes or live requests.');
} finally {
  await browser.close();
}
