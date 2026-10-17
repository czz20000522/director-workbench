// Browser + actual isolated private HTTP handlers. All requests intercepted;
// Python uses TestClient only, a synthetic account/database and fake dispatch.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const env = { ...process.env, PYTHONPATH: process.cwd(), PYTHONIOENCODING: 'utf-8' };
const child = spawn('./.venv/Scripts/python.exe', ['tests/automatic_creation_bridge.py'], { env, windowsHide: true, stdio: ['pipe','pipe','pipe'] });
let stderr=''; child.stderr.on('data', data => { stderr += data; });
const pending=[]; const lines=createInterface({input:child.stdout});
let readyResolve; const ready=new Promise(resolve => {readyResolve=resolve;});
lines.on('line', line => { try { const value=JSON.parse(line); if(value.project_id) readyResolve(value); else pending.shift()?.(value); } catch { assert.fail('Unexpected bridge output: '+line); } });
child.on('exit', code => { if(code) { for(const deliver of pending.splice(0)) deliver({status:500,data:{detail:stderr}}); } });
async function call(method,path,body){ return await new Promise(resolve => { pending.push(resolve); child.stdin.write(JSON.stringify({method,path,...(body===undefined?{}:{body})})+'\n'); }); }
async function complete(){ return await new Promise(resolve => { pending.push(resolve); child.stdin.write(JSON.stringify({operation:'complete'})+'\n'); }); }
let browser;
try {
  const initial=await Promise.race([ready,new Promise((_,reject)=>setTimeout(()=>reject(new Error(stderr || 'bridge start timeout')),15000))]);
  const bundle=await build({stdin:{contents:`import React from 'react';import {createRoot} from 'react-dom/client';import App from './src/App';createRoot(document.getElementById('root')).render(<App privateMode/>);`,loader:'tsx',resolveDir:process.cwd()},bundle:true,write:false,format:'iife',jsx:'automatic',plugins:[{name:'ignore-css',setup(build){build.onLoad({filter:/\.css$/},()=>({contents:'',loader:'js'}));}}]});
  browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage(); const errors=[],posts=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('http://localhost:4194/**',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.pathname.endsWith('/events')) return route.fulfill({status:204});
    if(url.pathname.startsWith('/api/') || url.pathname==='/openapi.json') {
      const body=request.postData() ? request.postDataJSON() : undefined;
      if(request.method()!=='GET') posts.push({path:url.pathname,body});
      const response=await call(request.method(),url.pathname+url.search,body);
      return route.fulfill({status:response.status,json:response.data});
    }
    return route.fulfill({contentType:'text/html',body:`<div id="root"></div><script>${bundle.outputFiles[0].text}</script>`});
  });
  await page.goto('http://localhost:4194/');
  await page.getByRole('button',{name:'作品、片段与更多功能',exact:true}).click();
  await page.getByRole('button',{name:'分镜制作',exact:true}).click();
  assert.equal(await page.locator('.director-preset-compact').count(),0);
  await page.getByRole('button',{name:'+ 添加分镜',exact:true}).click();
  const create=page.getByRole('region',{name:'添加分镜',exact:true});
  await create.getByLabel('画面与动作描述').fill('A brass clock ticks in an antique shop.');
  await create.getByLabel('目标画幅').selectOption('1:1');
  await create.getByRole('button',{name:'保存分镜',exact:true}).click();
  await page.getByRole('button',{name:'保存并生成这一段',exact:true}).waitFor();
  assert.equal(await page.locator('.director-preset-compact').count(),0);
  assert.equal(await page.getByLabel('当前生成方案').count(),0);
  const duration=page.getByRole('region',{name:'H3 镜头时长'});
  assert.equal(await duration.getByLabel('随机种子').count(),0,'technical seed stays outside ordinary inputs');
  await page.getByRole('button',{name:'更多操作',exact:true}).click();
  await duration.getByLabel('随机种子').fill('123');
  await page.getByRole('button',{name:'收起更多操作',exact:true}).click();
  await duration.getByLabel('请求时长（秒）').fill('7.3');
  await duration.getByRole('button',{name:'保存并预检时长'}).click();
  await duration.getByText('7.3 秒 → 175 帧／7.292 秒').waitFor();
  await duration.getByText('1024×1024 · 1:1 · 24 fps').waitFor();
  await duration.getByText('纯文本 · 首尾帧不适用').waitFor();
  assert.equal(posts.filter(row=>row.path.endsWith('/tasks')).length,0);
  await page.getByRole('button',{name:'保存并生成这一段',exact:true}).click();
  await page.getByText('已收到生成任务回执。',{exact:true}).waitFor();
  const submitted=posts.filter(row=>row.path.endsWith('/tasks'));
  assert.equal(submitted.length,1); assert.equal(submitted[0].body.pipeline_stage_id,'auto');
  assert.ok(submitted[0].body.expected_revision);
  assert.equal(posts.filter(row=>row.path.includes('/production-presets/')).length,0);
  const tasks=(await call('GET',`/api/projects/${initial.project_id}/tasks`)).data.tasks;
  assert.equal(tasks.length,1); assert.equal(tasks[0].payload.execution_snapshot.h3_duration.generation_mode,'text_to_video');
  assert.deepEqual(tasks[0].payload.execution_snapshot.materials,{});
  assert.equal(tasks[0].payload.execution_snapshot.parameters.seed,123);
  const originalPlan=(await call('GET',`/api/projects/${initial.project_id}/plan`)).data;
  assert.equal((await complete()).status,200);
  await page.reload();
  await page.getByRole('button',{name:'作品、片段与更多功能',exact:true}).click();
  await page.getByRole('button',{name:'分镜制作',exact:true}).click();
  assert.equal(await page.locator('.director-preset-compact').count(),0);
  assert.equal((await call('GET',`/api/projects/${initial.project_id}/tasks`)).data.tasks.length,1);
  const after=(await call('GET',`/api/projects/${initial.project_id}/plan`)).data;
  assert.equal(after.segments[0].workflow,originalPlan.segments[0].workflow);
  await page.getByRole('button',{name:'预检未保存输入',exact:true}).click();
  await page.getByText(/未保存参数预检.*可提交/).waitFor();
  assert.equal((await call('GET',`/api/projects/${initial.project_id}/plan`)).data.revision,after.revision);
  await page.getByRole('region',{name:'H3 镜头时长'}).getByLabel('请求时长（秒）').fill('3.9');
  await page.getByRole('button',{name:'预检未保存输入',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'预检未通过'}).waitFor();
  assert.match(await page.getByRole('alert').filter({hasText:'预检未通过'}).innerText(),/生成时长.*允许范围/);
  await page.waitForTimeout(5300);
  assert.ok(await page.getByRole('alert').filter({hasText:'预检未通过'}).isVisible(),'validation error persists beyond temporary toast/polling');
  assert.equal((await call('GET',`/api/projects/${initial.project_id}/plan`)).data.revision,after.revision);
  await page.getByRole('region',{name:'H3 镜头时长'}).getByLabel('请求时长（秒）').fill('7.3');
  await page.getByRole('button',{name:'预检未保存输入',exact:true}).click();
  await page.getByText(/未保存参数预检.*可提交/).waitFor();
  assert.equal(await page.getByRole('alert').filter({hasText:'预检未通过'}).count(),0);
  const advanced=await call('PATCH',`/api/projects/${initial.project_id}/plan/segments/S01`,{generation:{mode:'advanced'}});
  assert.equal(advanced.status,200);
  await page.reload();
  await page.getByRole('button',{name:'作品、片段与更多功能',exact:true}).click();
  await page.getByRole('button',{name:'分镜制作',exact:true}).click();
  await page.getByRole('button',{name:'保存并重新生成',exact:true}).waitFor();
  assert.equal(await page.getByLabel('目标画幅',{exact:true}).count(),0,'explicit advanced shot must override a new-project auto default');
  assert.deepEqual((await call('GET',`/api/projects/${initial.project_id}/tasks`)).data.tasks[0].payload.execution_snapshot.materials,{});
  assert.deepEqual(errors,[]);
  console.log('Actual private HTTP browser: save/preflight -> fake task completed -> refresh -> repeat preflight without saving; stable recipe, persistent 422 alert, valid retry, revision unchanged; no GPU. Fixture: '+initial.root);
} finally {
  if(browser) await browser.close();
  child.stdin.end();
  await new Promise(resolve=>{if(child.exitCode!==null)resolve();else child.once('exit',resolve);});
}
