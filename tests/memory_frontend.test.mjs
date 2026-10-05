// Exercise the actual memory page against a small DOM and HTTP adapter.
// Run: node --test tests/memory_frontend.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

globalThis.sessionStorage={getItem:()=>null};
globalThis.crypto||=(await import('node:crypto')).webcrypto;
const moduleURL=source=>'data:text/javascript;base64,'+Buffer.from(source).toString('base64');
const uiURL=moduleURL(await readFile('src/gateway/web/static/components/ui.js','utf8'));
const pageSource=(await readFile('src/gateway/web/static/pages/memory.js','utf8')).replace("'../components/ui.js'",JSON.stringify(uiURL));
const {mount}=await import(moduleURL(pageSource));
const {createStore}=await import(moduleURL(await readFile('src/gateway/web/static/store.js','utf8')));
const originalTimeout=globalThis.setTimeout;
globalThis.setTimeout=(...args)=>originalTimeout(...args).unref();

class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.listeners={};this.value='';this.textContent='';}
  setAttribute(name,value){this.attrs[name]=value;if(name==='value')this.value=value;}
  addEventListener(name,handler){this.listeners[name]=handler;}
  append(...children){this.children.push(...children);if(this.tag==='select'&&!this.value)this.value=this.children[0].value;}
  replaceChildren(...children){this.children=children;}
  remove(){}
  async emit(name){await this.listeners[name]?.({key:'Enter'});await new Promise(setImmediate);}
}
function walk(node){return [node,...node.children.flatMap(child=>child instanceof Element?walk(child):[])];}
function find(root,predicate){const node=walk(root).find(predicate);assert.ok(node,'DOM element must exist');return node;}
const input=(root,label)=>find(root,node=>node.attrs['aria-label']===label);
const button=(root,label)=>find(root,node=>node.tag==='button'&&node.textContent===label);
function environment(){
  const notifications=new Element('div');
  globalThis.document={activeElement:null,createElement:tag=>new Element(tag),querySelector:()=>notifications};
  globalThis.window={confirm:()=>true,addEventListener(){},removeEventListener(){}};
  return new Element('root');
}
const entry=(id,version=1,content='原始记忆')=>({id,version,content,kind:'user',subject:'偏好',event_at:null,source:{},created_at:1,updated_at:1});

test('editing with a stale version preserves the draft and rebases onto the latest record',async()=>{
  const root=environment(),store=createStore();let current=entry('remote:uuid'),requests=[];
  const api={async request(url,options){
    requests.push({url,options});
    if(!options)return {kind:'user',entries:[{...current}],total:1,next_cursor:null};
    const body=JSON.parse(options.body);
    if(body.expected_version!==current.version){const error=new Error('conflict');error.status=409;throw error;}
    current={...current,content:body.content,version:current.version+1};return {changed:true,entry:current};
  }};
  const dispose=mount(root,{store,api});await new Promise(setImmediate);
  await find(root,node=>node.className?.includes('memory-entry')).emit('click');
  const content=input(root,'记忆内容');content.value='我的草稿';await content.emit('input');
  current={...current,version:2,content:'其他入口的更新'};
  await button(root,'保存').emit('click');
  assert.equal(store.get().drafts.memoryByTarget.user.content,'我的草稿');
  assert.equal(content.value,'我的草稿');assert.equal(store.get().server.memory.user.entries[0].version,2);
  assert.equal(button(root,'保存').disabled,true);
  await button(root,'采用当前版本').emit('click');
  assert.equal(content.value,'我的草稿');assert.equal(button(root,'保存').disabled,false);
  await button(root,'保存').emit('click');
  const writes=requests.filter(request=>request.options).map(request=>JSON.parse(request.options.body));
  assert.deepEqual(writes.map(body=>body.expected_version),[1,2]);
  assert.ok(writes.every(body=>body.id==='remote:uuid'));assert.equal(current.content,'我的草稿');
  dispose();
});

test('paging, category drafts and keyword search use record APIs',async()=>{
  const root=environment(),store=createStore(),requests=[];
  const api={async request(url){
    requests.push(url);
    const parsed=new URL(url,'http://local');
    if(parsed.searchParams.get('q'))return {entries:[entry('match')],kind:'episode'};
    if(parsed.searchParams.get('cursor'))return {entries:[entry('second')],total:2,next_cursor:null};
    return {entries:[entry('first')],total:2,next_cursor:'opaque-cursor'};
  }};
  const dispose=mount(root,{store,api});await new Promise(setImmediate);
  await button(root,'加载更多').emit('click');
  assert.deepEqual(store.get().server.memory.user.entries.map(record=>record.id),['first','second']);
  const content=input(root,'记忆内容');content.value='保留用户草稿';await content.emit('input');
  const selector=input(root,'记忆类型');selector.value='episode';await selector.emit('change');
  assert.equal(content.value,'');assert.equal(input(root,'事件日期').hidden,false);
  const search=input(root,'搜索记忆');search.value='咖啡';await button(root,'搜索').emit('click');
  assert.equal(store.get().server.memory.episode.entries[0].id,'match');
  assert.ok(requests.some(url=>decodeURIComponent(url).includes('q=咖啡')));
  selector.value='user';await selector.emit('change');assert.equal(content.value,'保留用户草稿');
  dispose();
});
