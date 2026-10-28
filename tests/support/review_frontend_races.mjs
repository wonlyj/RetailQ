// Run the actual controller module with controlled I/O; no browser internals or
// production test hooks. A late old response must not forget a newer intent.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';

const scenario=process.argv[2];
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const oldReply=deferred(), oldStarted=deferred(), frame=deferred();
const elements=new Map();
const element=id=>{
  if(!elements.has(id)) elements.set(id,{value:'',hidden:false,disabled:false,open:false,textContent:'',
    firstChild:{value:''},classList:{toggle(){}},addEventListener(){},append(){},replaceChildren(){},
    setAttribute(){},showModal(){this.open=true;},close(){this.open=false;}});
  return elements.get(id);
};
let keys=0,posts=0, trackFrame=false;
const issued=[];
const request=async(path,options)=>{
  if(options?.method==='POST'){
    posts++;issued.push({path,body:options.body});
    if(posts===1 && scenario!=='old-poll'){oldStarted.resolve();return oldReply.promise;}
    return {operation_id:options.body.request_key,run_id:'a'.repeat(32),phase:'queued'};
  }
  if(path==='/summary') return {chunks:0,current_run_id:null,counters:null};
  if(path.startsWith('/runs?')) return {items:[],total:0,offset:0,limit:20};
  if(path.startsWith('/categories?')) return {items:[],total:0};
  if(path==='/operations/key-1' && scenario==='old-poll'){oldStarted.resolve();return oldReply.promise;}
  if(path.startsWith('/operations/')) return {phase:'queued'};
  throw new Error('Unexpected actual controller request '+path);
};
const names=['byId','node','button','phaseLabel','message','empty','pager','stats','runPicker','queue','group',
  'destinations','legacy','history','sources','snapshot','chunk'];
const context=vm.createContext({URL,URLSearchParams,console,setTimeout:()=>1,clearTimeout(){},
  location:{href:'http://127.0.0.1:8000/knowledge'},history:{replaceState(){}},
  document:{hidden:false,querySelectorAll:()=>[],getElementById:element,addEventListener(){}},
  window:{addEventListener(){}},crypto:{randomUUID:()=>`key-${++keys}`}});
const api=new vm.SyntheticModule(['consoleRequest','newRequestKey'],function(){
  this.setExport('consoleRequest',request);this.setExport('newRequestKey',()=>`key-${++keys}`);
},{context});
const views=new vm.SyntheticModule(names,function(){for(const name of names){
  this.setExport(name,name==='byId'?element:name==='node'||name==='button'?()=>element('generated'):
    name==='stats'?()=>{if(trackFrame)frame.resolve();}:()=>{});
}},{context});
const source=await fs.readFile(new URL('../../retailq/web/knowledge.js',import.meta.url),'utf8');
const controller=new vm.SourceTextModule(source+'\nexport { state,write,refresh };',{context});
await controller.link(specifier=>specifier.includes('knowledge-api')?api:views);
await controller.evaluate();
const {state,write}=controller.namespace;
const oldWrite=write('/runs/'+ 'a'.repeat(32)+'/resume',{});
await oldStarted.promise;
if(scenario==='old-poll') await oldWrite;
await write('/runs/'+ 'a'.repeat(32)+'/seal',{confirm_run_id:'a'.repeat(32)});
assert.equal(issued[1].body.request_key,'key-2');
assert.equal(state.operationId,'key-2');
trackFrame=true;
if(scenario==='old-post-error') oldReply.reject(Object.assign(new Error('old HTTP failure'),{status:503}));
else if(scenario==='old-post-unknown') oldReply.reject(new Error('old transport failure'));
else oldReply.resolve({operation_id:'key-1',phase:'succeeded',run_id:'a'.repeat(32)});
if(scenario==='old-poll') await frame.promise;
else await oldWrite;
assert.equal(state.operationId,'key-2','stale response replaced the new seal operation');
assert.equal(state.pending?.key,'key-2','stale response forgot the seal retry key');
assert.equal(state.busy,true,'stale response made the pending seal look idle');
assert.equal(state.uncertain,false,'old transport error replaced the seal state');
console.log('PASS '+scenario);
