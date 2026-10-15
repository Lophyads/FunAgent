import {mountGraph} from './memory-graph.js';
import {el,button,errorBox} from './ui.js';

const unknown=value=>value==null?'未知':String(value);
export function mountInspection(root,{store,api,onTab}){
  let source='',enabled=false,disposed=false,tab=store.get().ui.memoryInspectionTab||'records',dataset='',cursor=null,history=[],next=null,failures=0,timer=null,checked=0;
  const generations={health:0,datasets:0,raw:0},controllers={};
  const health=el('section',{class:'card memory-health','aria-live':'polite'}),healthText=el('p'),healthInfo=el('p'),check=button('检查连接',()=>loadHealth(true));health.append(healthText,check,el('details',{},el('summary',{text:'连接读取信息'}),healthInfo));
  const tabs=el('div',{role:'tablist','aria-label':'Cognee 视图',class:'memory-tabs'});
  const buttons=['records','data','graph'].map((id,i)=>{const b=button(['记忆记录','数据集与原始数据','图谱'][i],()=>selectTab(id));b.setAttribute('role','tab');b.id='memory-tab-'+id;b.setAttribute('aria-controls','memory-panel-'+id);tabs.append(b);return b;});
  const graphPanel=el('section',{id:'memory-panel-graph',role:'tabpanel','aria-labelledby':'memory-tab-graph',class:'card memory-graph'});
  const graph=mountGraph(graphPanel,{api,store});
  tabs.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const index=e.key==='Home'?0:e.key==='End'?2:(['records','data','graph'].indexOf(tab)+(e.key==='ArrowRight'?1:2))%3;if(!buttons[index].disabled){buttons[index].focus();selectTab(['records','data','graph'][index]);}}});
  const panel=el('section',{id:'memory-panel-data',role:'tabpanel','aria-labelledby':'memory-tab-data',class:'card memory-data'});
  const datasets=el('select',{'aria-label':'当前数据集'}),summary=el('p',{'aria-live':'polite'}),count=el('p'),rows=el('tbody'),error=el('div');
  const filter=el('input',{type:'search','aria-label':'本页文件名筛选',placeholder:'本页文件名筛选'}),type=el('input',{'aria-label':'本页文件类型筛选',placeholder:'本页文件类型筛选'});
  let items=[];
  const prev=button('上一页',()=>{cursor=history.pop()??null;loadPage();}),more=button('下一页',()=>{history.push(cursor);cursor=next;loadPage();});
  const detail=el('section',{class:'memory-raw','aria-live':'polite'}),reload=button('刷新当前视图',()=>refresh());
  panel.append(el('div',{class:'actions'},datasets,reload),summary,el('div',{class:'actions'},filter,type),count,error,
    el('div',{class:'memory-table-wrap'},el('table',{},el('thead',{},el('tr',{},...['文件名 / ID','类型','大小','创建时间','标签'].map(t=>el('th',{text:t})))),rows)),el('div',{class:'actions'},prev,more),detail);
  root.append(health,tabs,panel,graphPanel);
  function cancel(kind){generations[kind]++;controllers[kind]?.abort();delete controllers[kind];}
  function request(kind){cancel(kind);const controller=new AbortController();controllers[kind]=controller;return {source,gen:generations[kind],signal:controller.signal};}
  function valid(kind,own){return !disposed&&enabled&&source===own.source&&generations[kind]===own.gen;}
  function save(key,value){store.server('memoryInspection',{...store.get().server.memoryInspection,[key]:value});}
  function selectTab(value){Object.keys(generations).filter(k=>k!=='health').forEach(cancel);graph.suspend();tab=value;store.ui('memoryInspectionTab',tab);renderTab();if(tab==='data'&&!document.hidden)loadDatasets();if(tab==='graph'&&!document.hidden)graph.open(source);}
  function renderTab(){health.hidden=tabs.hidden=!enabled;panel.hidden=!enabled||tab!=='data';graphPanel.hidden=!enabled||tab!=='graph';buttons.forEach((b,i)=>{const selected=['records','data','graph'][i]===tab;b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;});onTab(enabled?tab:'records');}
  function schedule(){clearTimeout(timer);if(!disposed&&enabled&&!document.hidden)timer=setTimeout(()=>loadHealth(),[30000,60000,120000][Math.min(failures,2)]);}
  async function loadHealth(refresh=false){
    if(!enabled||document.hidden)return;clearTimeout(timer);const own=request('health');check.disabled=true;
    try{const value=await api.inspectMemory('health',{source_id:source,...(refresh?{refresh:1}:{})},{signal:own.signal});if(!valid('health',own))return;
      checked=Date.now();failures=value.service.state==='healthy'&&value.access.state==='ok'?0:failures+1;
      if(['auth_failed','denied'].includes(value.access.state)){graph.clear();items=[];rows.replaceChildren();detail.replaceChildren();store.server('memoryInspection',{});}
      save('health:'+source,value);
      const service={healthy:'在线',degraded:'降级',unreachable:'无法连接',unknown:'未知'},access={ok:'认证可用',auth_failed:'认证失败',denied:'读取受限',unknown:'认证未知'},scope={available:'配置范围可读',missing:'未找到可读的配置数据集',unknown:'配置范围未知'};
      healthText.textContent=`Cognee 服务：${service[value.service.state]} · ${access[value.access.state]} · ${scope[value.scope.state]}`;
      healthInfo.textContent=`${value.service.version||'版本未知'} · ${value.latency_ms??'未知'} ms · 检查时间 ${value.checked_at||'未知'}`;
    }catch(e){if(valid('health',own)&&e.name!=='AbortError'){failures++;healthText.textContent=(healthText.textContent||'健康结果未知')+' · 结果已过期：'+e.message;}}
    finally{if(valid('health',own)){check.disabled=false;schedule();}}
  }
  async function loadDatasets(refresh=false){
    if(!enabled||tab!=='data'||document.hidden)return;const own=request('datasets');cancel('raw');error.replaceChildren();
    try{const value=await api.inspectMemory('datasets',{source_id:source,...(refresh?{refresh:1}:{})},{signal:own.signal});if(!valid('datasets',own))return;
      save('datasets:'+source,value);datasets.replaceChildren(...value.items.map(v=>el('option',{value:v.dataset_name,text:v.dataset_name})));
      if(!value.items.some(v=>v.dataset_name===dataset)){dataset=value.items[0]?.dataset_name||'';cursor=null;history=[];}
      datasets.value=dataset;if(!dataset){rows.replaceChildren();summary.textContent='未找到可读的配置数据集';count.textContent='总数未知';detail.replaceChildren();return;}
      await loadPage(refresh);
    }catch(e){if(valid('datasets',own)&&e.name!=='AbortError')error.replaceChildren(errorBox(e.message,()=>loadDatasets(true)));}
  }
  async function loadPage(refresh=false){
    if(!enabled||!dataset||document.hidden)return;const own=request('datasets');cancel('raw');detail.replaceChildren();error.replaceChildren();count.textContent='读取中…';prev.disabled=more.disabled=true;
    const params={source_id:source,dataset_name:dataset,...(refresh?{refresh:1}:{})};
    const summaryRequest=api.inspectMemory('dataset-summary',params,{signal:own.signal}).then(value=>{if(!valid('datasets',own))return;save('summary:'+source+':'+dataset,value);summary.textContent=`处理状态：${{pending:'待处理',running:'处理中',completed:'完成',failed:'失败',unknown:'未知'}[value.processing.state]} · 文件总数 ${unknown(value.total)} · 数据集统计节点 ${unknown(value.graph.nodes)} / 关系 ${unknown(value.graph.edges)} · 统计时间 ${value.graph.computed_at||'未知'} · 检查时间 ${value.checked_at}${value.warnings.length?' · 部分字段不可读取':''}`;}).catch(e=>{if(valid('datasets',own)&&e.name!=='AbortError')summary.textContent='数据集状态未知：'+e.message;});
    try{const value=await api.inspectMemory('data',{...params,limit:50,...(cursor?{cursor}:{})},{signal:own.signal});if(!valid('datasets',own))return;
      items=[...new Map(value.items.map(v=>[v.data_id,v])).values()];next=value.next_cursor;save('data:'+source+':'+dataset+':'+(cursor||''),value);count.textContent=`共 ${unknown(value.total)} 条 / 本页已加载 ${items.length} 条`;renderRows();prev.disabled=!history.length;more.disabled=!next;
    }catch(e){if(valid('datasets',own)&&e.name!=='AbortError'){count.textContent='读取失败';error.replaceChildren(errorBox(e.message,()=>loadPage(true)));}}
    await summaryRequest;
  }
  function renderRows(){const selected=items.filter(v=>v.name.toLowerCase().includes(filter.value.toLowerCase())&&String(v.mime_type||v.extension||'').toLowerCase().includes(type.value.toLowerCase()));rows.replaceChildren(...selected.map(v=>el('tr',{},el('td',{},button(v.name,()=>loadRaw(v)),el('small',{text:v.data_id})),el('td',{text:v.mime_type||v.extension||'未知'}),el('td',{text:unknown(v.size)}),el('td',{text:v.created_at||'未知'}),el('td',{text:unknown(v.tags)}))));}
  async function loadRaw(item){const own=request('raw');detail.replaceChildren(el('p',{text:'读取原文…'}));try{const value=await api.inspectMemory('raw',{source_id:source,dataset_name:dataset,data_id:item.data_id},{signal:own.signal});if(!valid('raw',own))return;detail.replaceChildren(el('h3',{text:item.name}),el('p',{text:value.funagent_note?'FunAgent 笔记 · 只读浏览':'原始文件 · 只读浏览'}),el('pre',{text:value.textual?value.preview:'此类型暂不提供文本预览'}),el('p',{text:value.truncated?'原文预览已截断（64 KiB）':''}),el('details',{},el('summary',{text:'文件元数据'}),el('pre',{text:JSON.stringify(value.item,null,2)})));}catch(e){if(valid('raw',own)&&e.name!=='AbortError')detail.replaceChildren(errorBox(e.status===404?'数据已不存在或不可读取':e.message,()=>loadPage(true)));}}
  datasets.addEventListener('change',()=>{dataset=datasets.value;cursor=null;history=[];loadPage();});filter.addEventListener('input',renderRows);type.addEventListener('input',renderRows);
  function visibility(){clearTimeout(timer);if(document.hidden){Object.keys(generations).forEach(cancel);graph.suspend();}else if(enabled){if(Date.now()-checked>30000)loadHealth();else schedule();if(tab==='data')loadDatasets();if(tab==='graph')graph.open(source);}}
  document.addEventListener('visibilitychange',visibility);
  function refresh(){if(tab==='graph')graph.refresh();else if(tab==='data')loadDatasets(true);else loadHealth(true);}
  return {setSource(value){graph.suspend();graph.clear();clearTimeout(timer);Object.keys(generations).forEach(cancel);source=value?.source_id||'';enabled=!!value?.inspection?.includes('datasets');dataset='';cursor=null;history=[];items=[];failures=0;checked=0;detail.replaceChildren();rows.replaceChildren();summary.textContent='';healthText.textContent='检查中…';healthInfo.textContent='';renderTab();if(enabled){loadHealth();if(tab==='data')loadDatasets();if(tab==='graph')graph.open(source);}},busy(value){buttons.forEach(b=>b.disabled=value);},invalidate(){store.server('memoryInspection',{});graph.invalidate();if(enabled&&tab==='data')loadDatasets(true);},refresh,close(){graph.close();disposed=true;clearTimeout(timer);Object.keys(generations).forEach(cancel);document.removeEventListener('visibilitychange',visibility);}};
}

