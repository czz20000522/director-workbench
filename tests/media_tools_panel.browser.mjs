// Runs entirely against mocked workbench API; no generation or production writes.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from 'playwright';
const bundle = await build({ stdin: { contents: `import React,{useState} from 'react';import {createRoot} from 'react-dom/client';import Panel from './src/MediaToolsPanel';
function Harness(){const [id,setId]=useState('work-a');return <><button onClick={()=>setId(id==='work-a'?'work-b':'work-a')}>切作品</button><Panel projectId={id} assets={[{id:'video-1',title:'已生成视频',kind:'video'},{id:'audio-1',title:'试音',kind:'audio'}]} onComplete={()=>{}} onBrowse={()=>{}}/></>};createRoot(document.getElementById('root')).render(<Harness/>);`, loader:'tsx',resolveDir:process.cwd()},bundle:true,write:false,format:'iife',jsx:'automatic'});
const browser=await chromium.launch({channel:'msedge',headless:true});
try {
 const page=await browser.newPage();const submissions=[];const receipts=new Map();let unknown=false;
 await page.route('http://localhost:4199/**',async route=>{
  const req=route.request(),url=new URL(req.url());
  if(url.pathname.endsWith('/media-capability'))return route.fulfill({json:{operations:Object.fromEntries(['silence','video_qc','transcribe','silent_video'].map(x=>[x,{available:true}]))}});
  if(url.pathname.endsWith('/media-tasks')) { const data=req.postDataJSON();submissions.push(data);const result={id:'task-'+submissions.length,status:unknown?'scheduler_waiting':'succeeded',queue:{state:unknown?'waiting_resource':'terminal',reason:{code:'waiting_execution_slot',message:'等待 CPU 执行资源'},position:1},result:{report:{text:'我哪儿还有家。'},outputs:[]}};receipts.set(data.idempotency_key,result);if(unknown)return route.abort();return route.fulfill({json:result}); }
  if(url.pathname.includes('/submission-receipt'))return route.fulfill({json:receipts.get(url.searchParams.get('key'))??{id:'missing',status:'running'}});
  if(url.pathname.includes('/tasks/'))return route.fulfill({json:[...receipts.values()].at(-1)});
  return route.fulfill({contentType:'text/html',body:'<div id="root"></div><script>'+bundle.outputFiles[0].text+'</script>'});
 });
 await page.goto('http://localhost:4199/');await page.locator('summary').first().click();
 await page.getByRole('button',{name:'开始处理',exact:true}).click();await page.getByRole('button',{name:'继续处理',exact:true}).waitFor();
 assert.deepEqual(Object.keys(submissions[0]).sort(),['operation','duration_seconds','sample_rate','channels','idempotency_key'].sort());
 await page.getByRole('button',{name:'继续处理',exact:true}).click();await page.locator('select').first().selectOption('transcribe');await page.getByLabel('来源素材').selectOption('audio-1');await page.getByLabel('预期台词').fill('我哪儿还有什么家呀。');
 await page.getByRole('button',{name:'开始处理',exact:true}).click();await page.getByRole('button',{name:'继续处理',exact:true}).waitFor();assert.equal(submissions[1].expected_text,'我哪儿还有什么家呀。');assert.equal('duration_seconds' in submissions[1],false);
 await page.getByRole('button',{name:'继续处理',exact:true}).click();await page.locator('select').first().selectOption('video_qc');await page.getByLabel('来源素材').selectOption('video-1');unknown=true;
 await page.getByRole('button',{name:'开始处理',exact:true}).click();await page.waitForTimeout(150);assert.equal(submissions.length,3);
 const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem('director-workbench:work-a:media-tools:v1')));assert.equal(saved.pending.idempotency_key,submissions[2].idempotency_key);
 await page.reload();await page.locator('summary').first().click();assert.equal(await page.locator('select').first().isDisabled(),true);assert.equal(submissions.length,3);
 await page.getByRole('status',{name:'任务排队 媒体处理'}).getByText(/等待 CPU 执行资源 · 队列位置 1/).waitFor();await page.getByRole('button',{name:'停止处理'}).waitFor();
 await page.getByRole('button',{name:'切作品'}).click();await page.locator('summary').first().click();assert.equal(await page.locator('select').first().inputValue(),'silence');assert.equal(await page.locator('select').first().isEnabled(),true);
 await page.getByRole('button',{name:'切作品'}).click();await page.locator('summary').first().click();assert.equal(await page.locator('select').first().inputValue(),'video_qc');assert.equal(submissions.length,3);
 console.log('MediaToolsPanel: exclusive payloads, ASR fields, ambiguous receipt recovery, project isolation passed');
} finally {await browser.close();}
