import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
const scenario=process.argv[2];
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.handlers={};this.value='';this.textContent='';this.disabled=false;this.open=false;}
  append(...items){for(const item of items){this.children.push(item);if(item&&typeof item==='object')item.parent=this;}}
  replaceChildren(...items){this.children=[];this.append(...items);}
  addEventListener(name,fn){this.handlers[name]=fn;}
  setAttribute(){} showModal(){this.open=true;} close(){this.open=false;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
  focus(){} setCustomValidity(value){this.customError=value;} reportValidity(){return !this.customError;}
  async click(){if(this.disabled)return;return this.handlers.click?.({preventDefault(){}});}
  async submit(){if(find(this,n=>n.tagName==='textarea'&&(n.customError||!n.value)))return;return this.handlers.submit?.({preventDefault(){}});}
}
const body=new Element('body');
const find=(root,predicate)=>predicate(root)?root:root.children.filter(x=>x instanceof Element).map(x=>find(x,predicate)).find(Boolean);
const context=vm.createContext({console,URLSearchParams,document:{body,createElement:tag=>new Element(tag)},setTimeout,clearTimeout});
const module=new vm.SourceTextModule(await fs.readFile(new URL('../../retailq/web/actions.js',import.meta.url),'utf8'),{context});
await module.link(()=>{throw new Error('Unexpected dependency');});await module.evaluate();
const {renderActions,validateActions}=module.namespace;
const mid='42',cid=100;
const actions=[{kind:'handoff',action_id:'a'.repeat(32),status:'offered',origin_assistant_message_id:mid},
 {kind:'ticket',action_id:'b'.repeat(32),status:'offered',origin_assistant_message_id:mid}];
const envelope={message_id:mid,conversation_id:cid,turn_id:'c'.repeat(32),actions};
if(scenario==='validate'){
 assert.equal(validateActions(envelope).length,2);
 assert.throws(()=>validateActions({...envelope,actions:[{...actions[0],origin_assistant_message_id:'99'}]}));
 assert.throws(()=>validateActions({...envelope,actions:[actions[0],actions[0]]}));
}else{
 const message={status:'complete',role:'assistant',text:'可以选择',serverMessageId:mid,conversationId:cid,
  actions:validateActions(envelope).map(a=>({...a,verified:true}))};
 const container=new Element('div'),issued=[],greetings=[];
 let resolvePost;
 const request=async(path,options)=>{
  issued.push({path,method:options?.method||'GET',body:options?.body});
  if(scenario==='dedupe'&&options?.method==='POST')return new Promise(resolve=>{resolvePost=resolve;});
  if(scenario==='unknown'&&options?.method==='POST')throw new Error('network outcome unknown');
  return {action_id:'b'.repeat(32),status:'completed',ticket:{ticket_no:'T1',status:'待处理'}};
 };
 const ctx={userId:'user',isBusy:()=>false,request,changed(){renderActions(container,message,ctx);},
  simulateHandoff(lines){greetings.push(...lines);},notify(){}};
 renderActions(container,message,ctx);
 const button=kind=>find(container,n=>n.dataset.kind===kind);
 const dialog=()=>find(body,n=>n.tagName==='dialog');
 const cancel=()=>find(dialog(),n=>n.dataset.cancel==='yes').click();
 const confirm=async()=>{const d=dialog();const desc=find(d,n=>n.tagName==='textarea');if(desc)desc.value='订单1001物流延迟';return find(d,n=>n.tagName==='form').submit();};
 if(scenario==='incomplete'){
  message.status='stopped';renderActions(container,message,ctx);assert.equal(container.children.length,0);
 }else if(scenario==='handoff-cancel'){
  await button('handoff').click();await cancel();assert.equal(greetings.length,0);assert.equal(issued.length,0);
 }else if(scenario==='handoff'){
  await button('handoff').click();await confirm();
  assert.deepEqual(greetings,['已转接人工客服','您好,我是客服小猫,请问有什么可以帮您的']);
  assert.equal(issued.length,0);assert.equal(button('ticket').disabled,false);
 }else if(scenario==='ticket-cancel'){
  await button('ticket').click();await cancel();assert.equal(issued.length,0);
 }else if(scenario==='dedupe'){
  await button('ticket').click();const pending=confirm();await Promise.resolve();
  assert.equal(button('ticket').disabled,true);await button('ticket').click();
  assert.equal(issued.filter(x=>x.method==='POST').length,1);
  resolvePost({action_id:'b'.repeat(32),status:'completed',ticket:{ticket_no:'T1',status:'待处理'}});await pending;
  assert.equal(button('handoff').disabled,false);assert.equal(greetings.length,0);
 }else if(scenario==='unknown'){
  await button('ticket').click();await confirm();await button('ticket').click();
  assert.deepEqual(issued.map(x=>x.method),['POST','GET']);
  assert.equal(message.actions.find(x=>x.kind==='ticket').status,'completed');
 }else if(scenario==='whitespace'){
  await button('ticket').click();const d=dialog(),desc=find(d,n=>n.tagName==='textarea'),form=find(d,n=>n.tagName==='form');
  desc.value='   ';await form.submit();assert.equal(issued.length,0);
  desc.value='订单1001延迟';desc.handlers.input?.();await form.submit();
  assert.equal(issued.filter(x=>x.method==='POST').length,1);
 }else if(scenario==='restore'||scenario==='restore-first'){
  if(scenario==='restore-first')message.actions.reverse();
  const ticket=message.actions.find(x=>x.kind==='ticket');ticket.status='completed';ticket.verified=false;ticket.needsCheck=true;
  renderActions(container,message,ctx);assert.equal(container.children.length,2);await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepEqual(issued.map(x=>x.method),['GET']);assert.equal(ticket.verified,true);
  assert.equal(container.children.length,2);
 }
}
console.log('PASS '+scenario);
