import {createStore} from './store.js';
import {ApiClient,createCommands} from './api.js';
import {mount as chat} from './components/chat.js';
import {mount as overview} from './pages/overview.js';
import {mount as memory} from './pages/memory.js';
import {mount as evaluation} from './pages/eval.js';
import {mount as data} from './pages/data.js';
import {mount as observability} from './pages/observability.js';
import {mount as catalog} from './pages/catalog.js';
import {notify} from './components/ui.js';

const store=createStore(),api=new ApiClient(store),commands=createCommands(store,api);
const pages={overview,memory,eval:evaluation,data,observability,tools:catalog,connections:catalog,models:catalog,config:catalog};
const context={store,api,commands},pageRoot=document.querySelector('#page'),shell=document.querySelector('#shell');
let disposePage=()=>{};
function route(){const hash=location.hash.slice(1),separator=hash.indexOf('?'),name=(separator<0?hash:hash.slice(0,separator))||'overview',page=pages[name]?name:'overview',routeParams=new URLSearchParams(separator<0?'':hash.slice(separator+1));disposePage();store.ui('activePage',page);disposePage=pages[page](pageRoot,{...context,page,routeParams});
  for(const link of document.querySelectorAll('#sidebar a')){const active=link.hash===`#${page}`;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}}
window.addEventListener('hashchange',route);route();const disposeChat=chat(document.querySelector('#chat'),context);
store.subscribe(s=>s.ui.connectionStatus,value=>{const node=document.querySelector('#connection');node.textContent={connecting:'连接中',connected:'已连接',reconnecting:'重连中'}[value];node.className=`badge ${value}`;});
let theme=localStorage.getItem('funagent.theme')||'dark';document.documentElement.dataset.theme=theme;
document.querySelector('#theme-toggle').addEventListener('click',()=>{theme=theme==='dark'?'light':'dark';document.documentElement.dataset.theme=theme;localStorage.setItem('funagent.theme',theme);});
for(const [id,className,key]of [['nav-toggle','nav-collapsed','nav'],['chat-toggle','chat-collapsed','chat']]){
  shell.classList.toggle(className,localStorage.getItem(`funagent.${key}`)==='collapsed');
  document.querySelector(`#${id}`).addEventListener('click',()=>{shell.classList.toggle(className);localStorage.setItem(`funagent.${key}`,shell.classList.contains(className)?'collapsed':'open');});
}
const resize=document.querySelector('#resize');let width=Math.max(300,Math.min(650,Number(localStorage.getItem('funagent.chatWidth'))||390));
function setWidth(value){width=Math.max(300,Math.min(650,value));shell.style.setProperty('--chat-width',`${width}px`);localStorage.setItem('funagent.chatWidth',width);}setWidth(width);
resize.addEventListener('pointerdown',e=>{resize.setPointerCapture(e.pointerId);const start=e.clientX,original=width;const move=e=>setWidth(original+start-e.clientX);resize.addEventListener('pointermove',move);resize.addEventListener('pointerup',()=>resize.removeEventListener('pointermove',move),{once:true});});
resize.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();setWidth(width+(e.key==='ArrowLeft'?20:-20));}});
document.querySelector('#detail-close').addEventListener('click',()=>document.querySelector('#detail').close());
document.querySelector('#detail').addEventListener('click',e=>{if(e.target===e.currentTarget)e.currentTarget.close();});
api.request('/api/status').then(status=>{store.server('status',status);}).catch(e=>notify(e.message,true));
api.sessions().then(items=>{const saved=store.get().ui.activeSessionId;
  if(items.length)api.selectSession(items.some(s=>s.session_id===saved)?saved:items[0].session_id);else return api.createSession();
}).catch(e=>notify(e.message,true));
window.addEventListener('beforeunload',()=>{disposePage();disposeChat();api.close();});
