// Isolated browser regression: every HTTP request is mocked; no live service or GPU work.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const bundle = await build({ stdin: { contents: `import React from 'react';import {createRoot} from 'react-dom/client';import App from './src/App';createRoot(document.getElementById('root')).render(<App privateMode />);`, loader: 'tsx', resolveDir: process.cwd() }, bundle: true, write: false, format: 'iife', jsx: 'automatic', plugins: [{ name: 'ignore-css', setup(build) { build.onLoad({ filter: /\.css$/ }, () => ({ contents: '', loader: 'js' })); } }] });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const segment = { id: 'S01', start_seconds: 0, end_seconds: 5, duration_seconds: 5, status: 'draft', location: '测试场景', shot_size: '中景', camera: '固定', wardrobe: '', performance: '', dependencies: [], prompt: '鼠标转身', keyframes: { first: 'assets/first.png' }, audio: {}, video: { path: 'assets/sample.mp4' } };
const stage = (id, workflow) => ({ id, order: 1, title: id === 'motion-generation' ? '首尾帧音频驱动' : id.includes('square') ? 'H3 原生声音方形样片' : 'H3 原生声音横屏样片', purpose: '', backend: 'ComfyUI 工作流', status: '已接入', recipe: 'H3 音画样片', note: '', inputs: ['首帧', '提示词'], outputs: ['视频候选'], input_specs: [{ id: 'prompt', kind: '文本', label: '提示词', required: true, control: 'text' }, { id: 'first-frame', kind: '图片', label: '首帧', required: true, control: 'asset' }, { id: 'duration', kind: '参数', label: '时长', required: true, control: 'number', min: 4, max: 15 }], execution: { mode: 'comfyui', label: '测试方案', references: [workflow], state: '已验证' } });

try {
  for (const { ids, shotWorkflow, oldContract } of [
    { ids: ['motion-generation'] },
    { ids: ['motion-first-native-landscape-generation'] },
    { ids: ['motion-first-native-square-generation'] },
    { ids: ['motion-first-native-square-generation'], oldContract: true },
    { ids: ['motion-generation', 'motion-first-native-landscape-generation'] },
    { ids: ['motion-generation', 'motion-first-native-landscape-generation'], shotWorkflow: 'workflows/motion-generation.json' },
    { ids: ['motion-generation', 'motion-first-native-landscape-generation'], shotWorkflow: 'workflows/custom.json' },
  ]) {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const installed = ids.at(-1);
    const selected = shotWorkflow === 'workflows/motion-generation.json' ? 'motion-generation' : installed;
    const stages = ids.map(id => stage(id, `workflows/${id}.json`));
    if (oldContract) stages[0].input_specs.find(input => input.id === 'duration').max = 5;
    const project = { id: 'browser-test', title: '测试作品', series: '测试系列', assembly_asset_id: 'FULL', assets: [], pipeline: stages, production_preset: { id: installed, title: installed, workflow: `workflows/${installed}.json` } };
    const shotSegment = { ...segment, ...(shotWorkflow ? { workflow: shotWorkflow } : {}) };
    const posts = [];
    const validations = [];
    let assemblyAllowed = false; const deliveryPosts = [];
    await page.route('http://localhost:4199/**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      if (path === '/api/project') return route.fulfill({ json: project });
      if (path === '/api/projects') return route.fulfill({ json: { projects: [{ id: project.id, title: project.title, series: project.series }] } });
      if (path === '/api/status') return route.fulfill({ json: { comfy: { online: false } } });
      if (path === '/api/projects/browser-test/plan') return route.fulfill({ json: { revision: 1, segments: [shotSegment], assembly: null } });
      if (path === '/api/projects/browser-test/assembly/edit') return route.fulfill({ status: 404, json: { detail: 'No legacy edit' } });
      if (path === '/api/projects/browser-test/state') return route.fulfill({ json: { project_id: project.id, creative: null, reviews: {}, checkpoints: {}, artifacts: [], history: [], readiness: { schema_version: 1, revision: 1, source: 'saved', segments: [{ asset_id: 'S01', stage_id: selected, revision: 1, source: 'saved', state: 'ready', ready: true, blockers: [], allowed_actions: [{ id: 'submit', label: '提交' }], next_actions: [{ id: 'validate', label: '预检当前输入' }] }], assembly: { revision: 1, state: assemblyAllowed ? 'ready' : 'blocked', ready: assemblyAllowed, blockers: assemblyAllowed ? [] : [{ code: 'future_unknown', message: '', action: { id: 'review_join', label: '检查接点' } }], allowed_actions: [], next_actions: assemblyAllowed ? [] : [{ id: 'review_join', label: '检查接点' }] }, next_actions: [] } } });
      if (path === '/api/projects/browser-test/pipeline') return route.fulfill({ json: { stages } });
      if (path === '/api/projects/browser-test/tasks' && request.method() === 'GET') return route.fulfill({ json: { tasks: [] } });
      if (path === '/api/projects/browser-test/segments/S01/versions') return route.fulfill({ json: { items: [], total: 2 } });
      if (path.endsWith('/validate')) {
        validations.push(path);
        const requested = Number(request.postDataJSON().values.duration ?? shotSegment.duration_seconds);
        const requestedFrames = Math.max(5, Math.round(requested * 24));
        const frames = requestedFrames + (5 - (requestedFrames % 17) + 17) % 17;
        return route.fulfill({ json: { stage_id: selected, valid: true, values: { prompt: segment.prompt, 'first-frame': segment.keyframes.first, duration: requested }, args: [], binding_count: 3,
          readiness: { asset_id: 'S01', stage_id: selected, revision: 1, source: 'draft', state: 'ready', ready: true, blockers: [], allowed_actions: [], next_actions: [] },
          h3_duration: { requested_seconds: requested, frame_count: frames, playback_seconds: Number((frames / 24).toFixed(3)), fps: 24, width: selected.includes('square') ? 640 : 864, height: selected.includes('square') ? 640 : 480, aspect_ratio: selected.includes('square') ? '1:1' : '16:9', first_frame: segment.keyframes.first, first_frame_node_id: '114', prompt: segment.prompt, prompt_node_id: '105:104' } } });
      }
      if (path === '/api/projects/browser-test/plan/segments/S01' && request.method() === 'PATCH') { shotSegment.duration_seconds = Number(request.postDataJSON().duration_seconds); return route.fulfill({ json: { plan: { revision: 2, segments: [shotSegment], assembly: null } } }); }
      if (path === '/openapi.json') return route.fulfill({ json: { paths: { '/api/projects/{project_id}/assembly': { post: { requestBody: { content: { 'application/json': { schema: { $ref: '#/components/schemas/AssemblyRequest' } } } } } } }, components: { schemas: { StartRequest: { properties: { idempotency_key: {} } }, AssemblyRequest: { properties: { idempotency_key: {}, expected_revision: {} } } } } } });
      if (path === '/api/projects/browser-test/assembly' && request.method() === 'POST') { deliveryPosts.push({ path, body: request.postDataJSON() }); return route.fulfill({ json: { id: 'assembly-task', project_id: project.id, asset_id: 'FULL', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'resource_status_unknown', message: '等待引擎恢复' }, position: 3 } } }); }
      if (path === '/api/projects/browser-test/batches' && request.method() === 'POST') { deliveryPosts.push({ path, body: request.postDataJSON() }); return route.fulfill({ json: { batch_id: 'offline-batch', task_ids: ['offline-batch-task'] } }); }
      if (path === '/api/projects/browser-test/tasks' && request.method() === 'POST') { posts.push(request.postDataJSON()); return route.fulfill({ json: { id: 'mock-task', project_id: project.id, asset_id: 'S01', status: 'scheduler_waiting', queue: { state: 'waiting_resource', reason: { code: 'gpu_busy', message: '等待共享 GPU 执行槽' }, position: 2 } } }); }
      if (path === '/api/production-presets') return route.fulfill({ json: { presets: [{ id: 'h3-first-native-square', title: '首帧 + H3 原生声音方形样片', available: true, missing_models: [] }] } });
      if (path.endsWith('/events')) return route.fulfill({ status: 204 });
      if (path.startsWith('/api/')) return route.fulfill({ json: {} });
      return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script>${bundle.outputFiles[0].text}</script>` });
    });
    await page.goto('http://localhost:4199/');
    await page.getByRole('button', { name: '作品、片段与更多功能' }).click();
    await page.getByRole('button', { name: '分镜制作' }).click();
    const generate = page.getByRole('button', { name: '保存并重新生成' });
    await generate.waitFor();
    await page.getByText('生成准备度已满足').waitFor();
    await page.getByRole('button', { name: '预检当前输入' }).waitFor();
    await page.getByRole('button', { name: '更多操作', exact: true }).click();
    assert.equal(await page.getByLabel('当前生成方案').textContent(), stages.find(item => item.id === selected).title);
    await page.locator('.director-preset-compact summary').first().click();
    await page.getByRole('heading', { name: '首帧 + H3 原生声音方形样片' }).waitFor();
    if (oldContract) assert.equal(await page.getByRole('region', { name: 'H3 镜头时长' }).count(), 0);
    if (selected === 'motion-first-native-square-generation' && !oldContract) {
      const duration = page.getByRole('region', { name: 'H3 镜头时长' });
      await duration.getByLabel('请求时长（秒）').fill('8');
      await page.getByRole('button', { name: '预检未保存输入' }).click();
      await page.getByText(/未保存参数预检 · revision 1：可提交/).waitFor();
      assert.equal(shotSegment.duration_seconds, 5, 'draft preflight must not write saved shot');
      assert.equal(posts.length, 0);
      await duration.getByRole('button', { name: '保存并预检时长' }).click();
      await duration.getByText('8 秒 → 192 帧／8 秒').waitFor();
      assert.equal(posts.length, 0);
    }
    await generate.click();
    await page.waitForFunction(() => document.body.textContent.includes('已收到生成任务回执'));
    await page.getByRole('status', { name: '任务排队 S01' }).getByText(/等待共享 GPU 执行槽 · 队列位置 2/).waitFor();
    await page.getByRole('button', { name: '任务与成片' }).click();
    await page.getByLabel('装配准备度').getByText(/future_unknown/).waitFor();
    assert.ok(await page.getByRole('button', { name: '启动装配', exact: true }).isDisabled(), 'server assembly blocker must disable assembly despite all current videos present');
    assert.deepEqual(validations, Array(selected === 'motion-first-native-square-generation' && !oldContract ? 3 : 1).fill(`/api/projects/browser-test/pipeline/${selected}/validate`));
    assert.equal(posts.length, 1);
    assert.equal(posts[0].pipeline_stage_id, selected);
    assert.equal(posts[0].asset_id, 'S01');
    if (selected === 'motion-first-native-square-generation' && !oldContract) assert.equal(posts[0].pipeline_values.duration, 8);
    assert.ok(posts[0].idempotency_key);
    if (installed === 'motion-generation') {
      assemblyAllowed = true;
      await page.reload();
      await page.getByRole('button', { name: '作品、片段与更多功能' }).click();
      await page.getByRole('button', { name: '任务与成片' }).click();
      await page.getByLabel('装配准备度').getByText('准备完成，可明确提交装配。').waitFor();
      await page.getByRole('button', { name: '启动装配', exact: true }).click();
      await page.getByText('已收到装配任务回执。').waitFor();
      await page.getByRole('button', { name: '启动可执行片段批次' }).click();
      await page.getByText(/已提交 1 个片段/).waitFor();
      assert.deepEqual(deliveryPosts.map(item => item.path), ['/api/projects/browser-test/assembly', '/api/projects/browser-test/batches']);
      assert.ok(deliveryPosts[0].body.idempotency_key);
      assert.equal(deliveryPosts[1].body.pipeline_stage_id, selected);
    }
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log('Generation stage: 7 stage cases; offline engine accepts validated shot, server-ready assembly and batch queue admission; draft duration, picker, service blockers and idempotent payload passed');
} finally { await browser.close(); }
