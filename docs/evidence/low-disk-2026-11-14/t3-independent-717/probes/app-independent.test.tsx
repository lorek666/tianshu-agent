import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { HostClient } from '../work/ui/src/api/host';
import type { StateView, TaskView, RunView } from '../work/ui/src/api/types';
import { HostRpcError } from '../work/ui/src/api/errors';
const holder=vi.hoisted(()=>({host:null as unknown}));
vi.mock('../work/ui/src/api/host',()=>({createRealHostClient:()=>holder.host}));
import { App } from '../work/ui/src/App';
const EFFECTS={format_version:1,mode:'effects'};
const PURE={format_version:1,mode:'pure_only'};
let mounted:Root|null=null;
let target:HTMLDivElement;
let state:StateView;
let calls:Array<{method:string,params:Record<string,unknown>}>;
let rejectRun:string|null;
let serial:number;
function task(id:string,mode='writable'):TaskView{return {task_id:id,control:false,goal:`independent ${id}`,goal_revision:1,status:'idle',active_run_id:null,workspaces:[{root_path:`/independent/${id}`,mode}],created_at:1,updated_at:1};}
function host():HostClient{return {mode:'host',async getState(){return structuredClone(state);},async openEvents(_since,callbacks,signal){callbacks.onOpen?.();await new Promise<void>(resolve=>{if(signal.aborted)resolve();else signal.addEventListener('abort',()=>resolve(),{once:true});});},async rpc<T>(method,params={}){
 calls.push({method,params:structuredClone(params)});
 if(method==='task.create') {const t={...task(`created-${++serial}`),goal:params.goal as string,workspaces:structuredClone(params.workspaces) as TaskView['workspaces']};state.tasks.unshift(t);return {task:structuredClone(t)} as T;}
 if(method==='run.start'){
  if(rejectRun!==null)throw new HostRpcError('internal',rejectRun);
  const r:RunView={run_id:`run-${++serial}`,task_id:params.task_id as string,state:'succeeded',outcome:'succeeded',effect_state:'none_applied',control_requested:null,goal_revision:1,lease_epoch:1,started_at:1,ended_at:2,n_applied:0,n_unknown:0,undoable:false};state.runs.unshift(r);return {run:r} as T;
 }
 if(method==='run.status')return {run:state.runs.find(r=>r.run_id===params.run_id)} as T;
 if(method==='events.run')return {events:[],seq_head:0,has_more:false} as T;
 if(method==='run.list')return {runs:state.runs.filter(r=>r.task_id===params.task_id)} as T;
 throw new Error(`unexpected real App RPC: ${method}`);
}};}
async function flush(){for(let i=0;i<5;i++)await act(async()=>{await Promise.resolve();});}
async function mount(){await act(async()=>{mounted=createRoot(target);mounted.render(<App/>);});await flush();expect(target.textContent).toContain('independent task-A');}
function button(label:string,scope:ParentNode=target):HTMLButtonElement{const found=[...scope.querySelectorAll<HTMLButtonElement>('button')].find(b=>b.textContent?.trim()===label);if(!found)throw new Error(`button missing ${label}: ${scope.textContent?.slice(0,500)}`);return found;}
async function click(label:string,scope:ParentNode=target){await act(async()=>{button(label,scope).click();});await flush();}
async function input(element:HTMLInputElement|HTMLTextAreaElement,value:string){await act(async()=>{const proto=element.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value')!.set!.call(element,value);element.dispatchEvent(new Event('input',{bubbles:true}));});await flush();}
function starts(){return calls.filter(c=>c.method==='run.start');}
async function openNew(mode:'effects'|'pure_only'='effects',workspace:'writable'|'readonly'='writable'){
 await click('新建任务');
 const modal=document.querySelector<HTMLElement>('[role="dialog"]');expect(modal).not.toBeNull();
 await input(modal!.querySelector('textarea')!,'independent new task');
 await input(modal!.querySelector('input')!,'/independent/new-workspace');
 if(mode==='pure_only')await click('仅读取与对话',modal!);
 if(workspace==='readonly')await click('只读',modal!);
 return modal!;
}
async function selectTask(id:string){const b=[...target.querySelectorAll<HTMLButtonElement>('.task-item')].find(x=>x.textContent?.includes(`independent ${id}`));expect(b).toBeDefined();await act(async()=>b!.click());await flush();}
beforeEach(()=>{
 (globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
 document.head.innerHTML='<meta name="tianshu-token" content="independent-not-a-real-credential">';
 document.body.innerHTML='';target=document.createElement('div');document.body.append(target);
 state={tasks:[task('task-A'),task('task-B','readonly')],runs:[],approvals:[],event_seq_head:0,daemon:{version:'test-boundary',started_at:1,data_root:null}};
 calls=[];rejectRun=null;serial=0;holder.host=host();
});
afterEach(async()=>{if(mounted){await act(async()=>mounted!.unmount());mounted=null;}console.log(JSON.stringify({probe:'real-App-host-rpc',calls}));document.body.innerHTML='';});
describe('independent real App DOM policy boundaries',()=>{
 it('new task default effects stays independent of readonly workspace; only run.start carries policy',async()=>{
  await mount();const modal=await openNew('effects','readonly');await click('创建并开始运行',modal);
  const created=calls.find(c=>c.method==='task.create')!;
  expect(created.params).toEqual({goal:'independent new task',workspaces:[{root_path:'/independent/new-workspace',mode:'readonly'}]});
  expect(starts()).toHaveLength(1);expect(starts()[0].params).toEqual({task_id:'created-1',execution_policy:EFFECTS});
  expect(calls.indexOf(created)).toBeLessThan(calls.indexOf(starts()[0]));
  expect(document.querySelector('[role="dialog"]')).toBeNull();
 });
 it('new pure low failure survives dialog close, retry, task switch, and explicit mode change',async()=>{
  await mount();rejectRun='dispatch.disk_space_low: independent disk location has less than 2 GiB';
  const modal=await openNew('pure_only');await click('创建并开始运行',modal);
  expect(document.querySelector('[role="dialog"]')).toBeNull();expect(starts()).toHaveLength(1);
  expect(target.textContent).toContain('independent disk location');expect(target.textContent).toContain('dispatch.disk_space_low:');
  expect(starts()[0].params).toEqual({task_id:'created-1',execution_policy:PURE});
  expect((calls.find(c=>c.method==='task.create')!.params.workspaces as Array<{mode:string}>)[0].mode).toBe('writable');
  await click('开始运行');expect(starts()).toHaveLength(2);expect(starts()[1].params.execution_policy).toEqual(PURE);
  await selectTask('task-B');expect(button('允许工具操作').getAttribute('aria-pressed')).toBe('true');
  await click('开始运行');expect(starts()[2].params).toEqual({task_id:'task-B',execution_policy:EFFECTS});
  const createdButton=[...target.querySelectorAll<HTMLButtonElement>('.task-item')].find(b=>b.textContent?.includes('independent new task'))!;
  await act(async()=>createdButton.click());await flush();expect(button('仅读取与对话').getAttribute('aria-pressed')).toBe('true');
  await click('开始运行');expect(starts()[3].params).toEqual({task_id:'created-1',execution_policy:PURE});
  await click('允许工具操作');await click('开始运行');expect(starts()[4].params).toEqual({task_id:'created-1',execution_policy:EFFECTS});
 });
 it('existing task path sends both default and explicit pure policy using real TaskDetail buttons',async()=>{
  await mount();await click('开始运行');expect(starts()[0].params).toEqual({task_id:'task-A',execution_policy:EFFECTS});
  await click('仅读取与对话');await click('开始运行');expect(starts()[1].params).toEqual({task_id:'task-A',execution_policy:PURE});
  expect(calls.some(c=>c.method==='task.create')).toBe(false);
  expect(state.tasks[0].workspaces[0].mode).toBe('writable');
 });
 it('existing pure unknown failure retries same policy and never contaminates another Task',async()=>{
  await mount();rejectRun='dispatch.disk_space_unknown: independent free measurement failed';
  await click('仅读取与对话');await click('开始运行');expect(starts()).toHaveLength(1);
  expect(target.textContent).toContain('independent free measurement failed');
  await click('开始运行');expect(starts()).toHaveLength(2);expect(starts()[0].params.execution_policy).toEqual(PURE);expect(starts()[1].params.execution_policy).toEqual(PURE);
  await selectTask('task-B');await click('开始运行');expect(starts()[2].params).toEqual({task_id:'task-B',execution_policy:EFFECTS});
  await selectTask('task-A');expect(button('仅读取与对话').getAttribute('aria-pressed')).toBe('true');
  expect(state.tasks.map(t=>t.workspaces[0].mode)).toEqual(['writable','readonly']);
 });
});
