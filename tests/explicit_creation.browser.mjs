// Browser + actual isolated private HTTP handlers. All requests intercepted;
// Python uses TestClient only, a synthetic account/database and fake dispatch.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { build } from 'esbuild';
import { chromium } from 'playwright';

const env = { ...process.env, PYTHONPATH: process.cwd(), PYTHONIOENCODING: 'utf-8' };
const child = spawn('./.venv/Scripts/python.exe', ['tests/explicit_creation_bridge.py'], { env, windowsHide: true, stdio: ['pipe','pipe','pipe'] });
let stderr=''; child.stderr.on('data', data => { stderr += data; });
const pending=[]; const lines=createInterface({input:child.stdout});
let readyResolve; const ready=new Promise(resolve => {readyResolve=resolve;});
lines.on('line', line => { try { const value=JSON.parse(line); if(value.project_id) readyResolve(value); else pending.shift()?.(value); } catch { assert.fail('Unexpected bridge output: '+line); } });
child.on('exit', code => { if(code) { for(const deliver of pending.splice(0)) deliver({status:500,data:{detail:stderr}}); } });
async function call(method,path,body){ return await new Promise(resolve => { pending.push(resolve); child.stdin.write(JSON.stringify({method,path,...(body===undefined?{}:{body})})+'\n'); }); }
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
  const plan=async()=> (await call('GET',`/api/projects/${initial.project_id}/plan`)).data;
  const before=await plan();
  assert.equal(before.segments.length,0);

  // Missing explicit plan: the save is blocked and no plan revision or recipe is written.
  await page.getByRole('button',{name:'+ 添加分镜',exact:true}).click();
  const create=page.getByRole('region',{name:'添加分镜',exact:true});
  await create.getByPlaceholder('先写导演意图，再决定模型和工作流').fill('A brass clock ticks in an antique shop.');
  await create.getByRole('button',{name:'保存分镜计划',exact:true}).click();
  await page.locator('.director-toast').filter({hasText:/显式选择制作方案|制作方案或工作流/}).waitFor();
  assert.deepEqual(await plan(), before);
  assert.equal(posts.filter(row=>row.path.includes('/production-presets/')).length,0);
  assert.equal(posts.filter(row=>row.path.endsWith('/plan/segments')).length,1);

  // Installing a preset is an independent explicit write, then the shot names that plan.
  const picker=create.locator('.director-preset-compact').first();
  await picker.locator(':scope > summary').click();
  const textPreset=picker.locator('section').filter({hasText:'纯文本 + H3 原生声音横屏样片'}).first();
  await textPreset.getByRole('button',{name:'选用',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('select[aria-label="制作方案"] option').length>1);
  await create.getByLabel('制作方案').selectOption({index:1});
  await create.getByRole('button',{name:'保存分镜计划',exact:true}).click();
  await page.getByText(/已登记分段/).waitFor();
  const saved=await plan();
  assert.equal(saved.segments.length,1);
  assert.ok(saved.segments[0].workflow);
  assert.equal(posts.filter(row=>row.path.includes('/production-presets/')).length,1,'Only the explicit install writes a preset');
  assert.equal(posts.filter(row=>row.path.endsWith('/plan/segments')).length,2);

  // The final submission names the selected stage instead of the removed auto sentinel.
  await page.getByRole('button',{name:'保存并生成这一段',exact:true}).waitFor();
  await page.getByRole('button',{name:'保存并生成这一段',exact:true}).click();
  await page.getByText('已收到生成任务回执。',{exact:true}).waitFor();
  const submitted=posts.filter(row=>row.path.endsWith('/tasks'));
  assert.equal(submitted.length,1);
  assert.equal(submitted[0].body.pipeline_stage_id,'motion-text-native-landscape-generation');
  assert.notEqual(submitted[0].body.pipeline_stage_id,'auto');
  const tasks=(await call('GET',`/api/projects/${initial.project_id}/tasks`)).data.tasks;
  assert.equal(tasks.length,1);
  assert.equal(tasks[0].payload.execution_snapshot.pipeline_stage_id,'motion-text-native-landscape-generation');
  assert.deepEqual(tasks[0].payload.execution_snapshot.materials,{});
  assert.equal('resolved_generation' in tasks[0].payload.execution_snapshot,false);
  assert.deepEqual(errors,[]);
  console.log('Actual private HTTP browser: missing plan blocks without writes; explicit preset install and shot plan; task uses explicit stage, no auto sentinel; no GPU. Fixture: '+initial.root);
} finally {
  if(browser) await browser.close();
  child.stdin.end();
  await new Promise(resolve=>{if(child.exitCode!==null)resolve();else child.once('exit',resolve);});
}
