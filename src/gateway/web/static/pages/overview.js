import {el, button, pageHeader, statusText, showDetail} from '../components/ui.js';
import {buildOverview, duration, eventInfo, nodeLabels, phaseNode, stateLabels} from './overview-model.js';

const ns='http://www.w3.org/2000/svg';
const svgEl=(tag,attrs={},text)=>{
  const node=document.createElementNS(ns,tag);
  for(const[key,value]of Object.entries(attrs))node.setAttribute(key,value);
  if(text!=null)node.textContent=text;
  return node;
};
const definitions=[
  ['gateway',28,104,130,76,'01','用户输入 / 会话队列'],
  ['context',194,104,146,76,'02','历史 + 提示词快照'],
  ['model',402,104,146,76,'03','路由 / 流式响应'],
  ['tools',402,238,146,76,'04','完整调用 → 执行'],
  ['reply',606,104,136,76,'05','提交结果 / 最终正文'],
  ['human',606,238,136,76,'↗','澄清 / 工具审批'],
  ['memory',194,270,146,64,'M','MEMORY / USER'],
  ['storage',28,270,130,64,'D','会话 / 消息 / 结果'],
  ['hub',794,104,146,76,'↗','执行事件 + 输出'],
  ['browser',794,238,146,76,'↗','Overview / ChatDock'],
];
const nodeDescriptions={
  gateway:'用户输入提交给 Gateway；同一会话按顺序执行，取得执行权后进入本轮。',
  context:'读取会话历史，复用该会话的提示词和记忆快照，再组装模型请求。',
  model:'按配置选择路由和重试。每次尝试独立累计输出，完整响应校验后才采纳。',
  tools:'完整工具调用经校验、审批后执行，结果写回历史，再进入下一次模型迭代。',
  reply:'最终正文和结构化结果提交后，Gateway 的 Future 确认本轮成功、失败或中断。',
  human:'工具需要澄清或审批时暂停等待；请在右侧对话面板提交答复。',
  memory:'数据库核心记忆参与新会话的提示词快照。已有会话复用原快照，工具可按需查询和更新记录。',
  storage:'SQLite 保存会话、消息、工具结果和轮次结果。可在 Data 页面查看已提交的数据。',
  hub:'WebEventHub 汇总诊断事件、模型输出和最终结果，保留快照与有界重放缓冲。',
  browser:'当前会话共享一条 SSE。断线后重放事件或用快照校准，ChatDock 和 Overview 同步更新。',
};
const filters=[['all','全部'],['model','模型'],['tool','工具'],['interaction','交互'],['system','系统']];

export function mount(root,{store}){
  root.classList.add('overview-page');
  const previousLive=root.getAttribute('aria-live');root.setAttribute('aria-live','off');
  const choose=el('select',{'aria-label':'选择轮次'});
  const follow=button('跟随最新',()=>{following=true;render();},'ov-follow');
  pageHeader(root,'Overview','实时观察一次对话如何流经上下文、模型、工具与最终回复。',choose,follow);
  const metrics=el('div',{class:'ov-metrics'});
  const runStatus=el('span',{class:'ov-status',role:'status','aria-live':'polite'}),currentPhase=el('span',{class:'ov-current'});
  const topology=el('section',{class:'ov-topology'},
    el('header',{class:'ov-panel-head'},el('div',{},el('span',{class:'ov-kicker',text:'LIVE EXECUTION'}),el('h2',{text:'执行拓扑'})),
      el('div',{class:'ov-topology-status'},runStatus,currentPhase)));
  const scroll=el('div',{class:'ov-graph-scroll',tabindex:0,role:'region','aria-label':'执行拓扑，窄屏可左右滚动'});
  const svg=svgEl('svg',{viewBox:'0 0 968 376',class:'ov-graph',role:'group','aria-label':'FunAgent 执行拓扑，点击节点查看详情'});
  const markerId=`ov-arrow-${crypto.randomUUID()}`;
  const marker=svgEl('marker',{id:markerId,viewBox:'0 0 8 8',refX:7,refY:4,markerWidth:5,markerHeight:5,orient:'auto-start-reverse'});
  marker.append(svgEl('path',{d:'M1 1 L7 4 L1 7',class:'ov-arrow'}));
  const defs=svgEl('defs');defs.append(marker);svg.append(defs);
  svg.append(svgEl('rect',{x:12,y:54,width:748,height:302,rx:16,class:'ov-boundary'}),
    svgEl('rect',{x:378,y:76,width:194,height:254,rx:14,class:'ov-loop-box'}),
    svgEl('rect',{x:778,y:54,width:178,height:302,rx:16,class:'ov-channel-box'}),
    svgEl('text',{x:28,y:79,class:'ov-group-label'},'AGENTCORE · 单轮执行'),
    svgEl('text',{x:402,y:96,class:'ov-loop-label'},'推理 / 行动循环'),
    svgEl('text',{x:794,y:79,class:'ov-group-label'},'实时通道'),
    svgEl('text',{x:215,y:235,class:'ov-edge-label'},'会话快照'),
    svgEl('text',{x:576,y:289,class:'ov-edge-label','text-anchor':'middle'},'按需'),
    svgEl('text',{x:618,y:28,class:'ov-edge-label','text-anchor':'middle'},'事件 / 输出推送'),
    svgEl('text',{x:882,y:211,class:'ov-edge-label'},'SSE'),
    svgEl('text',{x:498,y:213,class:'ov-edge-label'},'工具结果'),
    svgEl('text',{x:53,y:238,class:'ov-edge-label'},'读写历史'));
  const edgeSpecs=[
    ['entry','M158 142 H194','gateway','context'],
    ['request','M340 142 H402','context','model'],
    ['response','M548 142 H606','model','reply'],
    ['call','M450 180 V238','model','tools'],
    ['loop','M548 270 H561 V195 Q561 190 556 190 H528 V180','tools','model'],
    ['approval','M548 276 H606','tools','human',true],
    ['memory','M267 270 V180','memory','context',true],
    ['storage','M93 270 V180','storage','gateway',true],
    ['publish','M475 76 V43 Q475 37 481 37 H861 Q867 37 867 43 V104','model','hub',true],
    ['sse','M867 180 V238','hub','browser'],
  ];
  const edges=new Map();
  for(const[id,d,from,to,dashed]of edgeSpecs){const path=svgEl('path',{d,class:`ov-edge${dashed?' dashed':''}`,'marker-end':`url(#${markerId})`});svg.append(path);edges.set(id,{path,from,to});}
  const nodes=new Map();
  for(const[id,x,y,w,h,icon,subtitle]of definitions){
    const group=svgEl('g',{class:`ov-node node-${id}`,transform:`translate(${x} ${y})`,role:'button',tabindex:0,'aria-label':`查看${nodeLabels[id]}`,'data-node':id});
    group.append(svgEl('title',{},nodeDescriptions[id]),svgEl('rect',{width:w,height:h,rx:10,class:'ov-node-box'}),
      svgEl('rect',{x:12,y:13,width:21,height:21,rx:6,class:'ov-node-icon-box'}),
      svgEl('text',{x:22.5,y:27,'text-anchor':'middle',class:'ov-node-icon'},icon),
      svgEl('text',{x:40,y:29,class:'ov-node-title'},nodeLabels[id]),
      svgEl('circle',{cx:w-11,cy:15,r:3,class:'ov-node-dot'}),
      svgEl('text',{x:13,y:51,class:'ov-node-subtitle'},subtitle));
    group.addEventListener('click',()=>selectNode(id));
    group.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();selectNode(id);}});
    svg.append(group);nodes.set(id,group);
  }
  scroll.append(svg);topology.append(scroll);
  const focusName=el('strong'),focusState=el('span',{class:'ov-chip'}),focusText=el('p');
  const focusDetails=button('查看数据',()=>showNodeDetails(),'ov-text-button');
  const focusBar=el('div',{class:'ov-focus'},el('div',{class:'ov-focus-icon',text:'◈'}),
    el('div',{class:'ov-focus-copy'},el('div',{class:'ov-focus-title'},focusName,focusState),focusText),focusDetails);
  const legend=el('div',{class:'ov-legend'},...['active','done','waiting','idle'].map(s=>el('span',{},el('i',{class:s}),s==='idle'?'尚未执行':stateLabels[s])),el('span',{class:'ov-legend-hint',text:'点击节点查看链路职责'}));
  topology.append(focusBar,legend);
  const runTitle=el('p',{class:'ov-run-input'}),runMeta=el('span',{class:'ov-mono'}),lanes=el('div',{class:'ov-lanes'});
  const execution=el('section',{class:'ov-panel ov-execution'},
    el('header',{class:'ov-panel-head'},el('div',{},el('span',{class:'ov-kicker',text:'TURN INSPECTOR'}),el('h2',{text:'本轮执行'})),runMeta),runTitle,lanes);
  const eventCount=el('span',{class:'ov-mono'}),eventList=el('div',{class:'ov-event-list timeline'});
  const filterButtons=new Map(),filterBar=el('div',{class:'ov-filters',role:'group','aria-label':'时间线筛选'});
  for(const[id,label]of filters){const b=button(label,()=>{filter=id;renderTimeline();});filterButtons.set(id,b);filterBar.append(b);}
  const eventPanel=el('section',{class:'ov-panel ov-events'},
    el('header',{class:'ov-panel-head'},el('div',{},el('span',{class:'ov-kicker',text:'EVENT STREAM'}),el('h2',{text:'事件时间线'})),eventCount),filterBar,eventList);
  root.append(metrics,topology,el('div',{class:'ov-bottom-grid'},execution,eventPanel));

  let selected=null,following=true,focused=null,filter='all',view=buildOverview(null),frame=null,disposed=false;
  const eventNodes=new Map(),laneNodes=new Map(),metricNodes=[];
  for(const[label,note]of [['执行耗时','本轮实际经过时间'],['模型迭代','模型与工具循环'],['模型尝试','包含重试与路由切换'],['工具调用','本轮观察到的调用'],['Token usage','供应商最终返回']]){
    const value=el('strong',{text:'—'}),sub=el('span',{class:'ov-metric-note',text:note});metricNodes.push({value,sub});
    metrics.append(el('div',{class:'ov-metric'},el('small',{text:label}),value,sub));
  }
  choose.addEventListener('change',()=>{selected=choose.value;following=false;focused=null;render();});
  function selectNode(id){focused=id;renderGraph();}
  function showNodeDetails(){
    const id=focused||view.activeNode||(view.result?'reply':'gateway'),t=view.turn;
    const data=id==='model'?view.attempts:id==='tools'?view.tools:id==='human'?(t?.timeline||[]).filter(e=>e.kind.startsWith('interaction.'))
      : ['hub','browser'].includes(id)?{connection:store.get().ui.connectionStatus,retained_events:view.events.length,turn_id:t?.turn_id}
      : id==='memory'?{说明:nodeDescriptions.memory,查看:'Memory 页面'}:id==='storage'?{说明:nodeDescriptions.storage,查看:'Data 页面'}
      :{turn_id:t?.turn_id,status:t?.status,phases:Object.values(t?.phases||{}).filter(p=>phaseNode(p.phase)===id),result:id==='reply'?t?.result:undefined};
    showDetail(nodeLabels[id],el('div',{},el('p',{class:'muted',text:nodeDescriptions[id]}),el('pre',{text:JSON.stringify(data,null,2)})));
  }
  function renderGraph(){
    const id=focused||view.activeNode||(view.result?'reply':'gateway');
    for(const[key,node]of nodes){node.setAttribute('class',`ov-node node-${key} ${view.nodeStates[key]}${key===id?' selected':''}`);node.setAttribute('aria-pressed',String(key===id));
      node.setAttribute('aria-label',`${nodeLabels[key]}：${stateLabels[view.nodeStates[key]]}，查看详情`);}
    for(const[key,edge]of edges){
      const live=view.live&&(key==='publish'||key==='sse'?store.get().ui.connectionStatus==='connected'
        : key==='approval'?false:key==='loop'?view.activeNode==='model'&&view.tools.some(t=>t.iteration<view.iteration)
          :edge.to===view.activeNode&&!['memory','storage'].includes(edge.from));
      edge.path.classList.toggle('live',live);edge.path.classList.toggle('done',view.nodeStates[edge.from]==='done'&&view.nodeStates[edge.to]==='done');
      edge.path.classList.toggle('waiting',key==='approval'&&view.status==='waiting');
    }
    focusName.textContent=nodeLabels[id];focusState.textContent=['hub','browser'].includes(id)
      ? {connected:'SSE 已连接',connecting:'连接中',reconnecting:'重连中'}[store.get().ui.connectionStatus]
      : ['memory','storage'].includes(id)?'持久化资源':stateLabels[view.nodeStates[id]];
    focusState.className=`ov-chip ${view.nodeStates[id]}`;focusText.textContent=nodeDescriptions[id];
  }
  function renderLanes(){
    const keys=new Set();let previous=null;
    if(view.items.length)lanes.querySelector('.ov-empty')?.remove();
    const max=Math.max(1,...view.items.map(i=>i.seconds||0));
    for(const iteration of view.iterations){
      const key=`${selected}:${iteration}`,items=view.items.filter(i=>i.iteration===iteration);keys.add(key);
      let row=laneNodes.get(key);
      if(!row){row=el('div',{class:'ov-iteration'},el('div',{class:'ov-iteration-label'}),el('div',{class:'ov-iteration-items'}));row.items=new Map();laneNodes.set(key,row);}
      row.firstChild.textContent=iteration==null?'未标注迭代':`迭代 ${String(iteration).padStart(2,'0')}`;
      row.classList.toggle('current',view.live&&view.iteration===iteration);
      const keep=new Set();
      for(const item of items){
        const itemKey=`${item.type}:${item.id}`;keep.add(itemKey);let card=row.items.get(itemKey);
        if(!card){const name=el('strong'),badge=el('span'),seconds=el('span'),meta=el('span'),bar=el('i');
          card=el('button',{type:'button',class:'ov-call'},el('div',{class:'ov-call-title'},el('span',{class:'ov-call-icon',text:item.type==='model'?'◇':'⌘'}),name,badge),
            el('div',{class:'ov-call-meta'},meta,seconds),el('div',{class:'ov-duration-track'},bar));
          Object.assign(card,{nameNode:name,badge,seconds,meta,bar});card.addEventListener('click',()=>showDetail(card.item.type==='model'?'模型尝试':'工具调用',card.item));row.items.set(itemKey,card);row.lastChild.append(card);
        }
        card.item=item;card.nameNode.textContent=item.type==='model'?(item.route||'模型'):item.tool_name||'工具';
        card.badge.className=`ov-call-state ${item.status}`;card.badge.textContent=stateLabels[item.status]||item.status;
        const effect=item.effect_disposition||item.effect;
        card.seconds.textContent=duration(item.seconds);card.meta.textContent=item.type==='model'?`${(item.text||'').length.toLocaleString()} 字符`
          :(item.error_type||({none:'无副作用',not_applied:'未产生副作用',confirmed:'副作用已确认',unknown:'副作用未确认'}[effect])||'工具执行');
        card.title=`${item.type==='model'?'attempt_id':'tool_call_id'}: ${item.id}`;
        card.className=`ov-call ${item.type} ${item.status}`;card.bar.style.width=item.seconds==null?'0%':`${item.seconds/max*100}%`;
        card.bar.parentNode.title='耗时条相对本轮最慢调用，不代表完成进度';
      }
      for(const[id,card]of row.items)if(!keep.has(id)){card.remove();row.items.delete(id);}
      if(row.parentNode!==lanes||row.previousSibling!==previous)lanes.insertBefore(row,previous?previous.nextSibling:lanes.firstChild);previous=row;
    }
    for(const[key,node]of laneNodes)if(!keys.has(key)){node.remove();laneNodes.delete(key);}
    if(!view.items.length&&!lanes.querySelector('.ov-empty'))lanes.append(el('div',{class:'ov-empty'},el('span',{class:'ov-empty-mark',text:'◇'}),
      el('strong',{text:view.turn?'等待模型或工具事件':'从一条消息开始'}),el('p',{text:'在右侧发起对话，这里将逐次展开模型尝试与工具调用。'})));
  }
  function renderTimeline(){
    for(const[id,b]of filterButtons){b.classList.toggle('active',id===filter);b.setAttribute('aria-pressed',String(id===filter));}
    const events=view.events.filter(e=>filter==='all'||eventInfo(e).category===filter).slice(-100).reverse(),keys=new Set();let previous=null;
    eventCount.textContent=`${view.events.length} 条已保留${events.length<view.events.length?' · 筛选 / 最近 100 条':''}`;
    if(events.length)eventList.querySelector('.ov-empty')?.remove();
    for(const event of events){
      const key=`${selected}:${event.seq??`${event.occurred_at}:${event.kind}`}`;keys.add(key);let node=eventNodes.get(key);
      if(!node){const info=eventInfo(event),time=new Date(event.occurred_at*1000).toLocaleTimeString('zh-CN',{hour12:false});
        node=el('details',{class:`timeline-row ov-event ${info.tone}`},el('summary',{},el('span',{class:'ov-event-dot'}),
          el('div',{class:'ov-event-copy'},el('strong',{text:info.title}),el('small',{text:info.detail||event.kind})),
          el('time',{text:time}),el('span',{class:'ov-event-expand',text:'⌄'})),
          el('div',{class:'ov-event-payload'},el('small',{text:`${event.kind} · #${event.seq??'—'}`}),el('pre',{text:JSON.stringify(event.payload,null,2)})));
        eventNodes.set(key,node);
      }
      if(node.parentNode!==eventList||node.previousSibling!==previous)eventList.insertBefore(node,previous?previous.nextSibling:eventList.firstChild);previous=node;
    }
    // Preserve expanded rows through filters, pruning old turns and truncated events.
    const retained=new Set(view.events.map(e=>`${selected}:${e.seq??`${e.occurred_at}:${e.kind}`}`));
    for(const[key,node]of eventNodes)if(!keys.has(key)){node.remove();if(!retained.has(key))eventNodes.delete(key);}
    if(!events.length&&!eventList.querySelector('.ov-empty'))eventList.append(el('div',{class:'ov-empty'},el('p',{text:view.events.length?'当前筛选下暂无事件。':'执行事件到达后将在这里按时间展开。'})));
  }
  function render(){
    if(disposed)return;
    const state=store.get(),turns=Object.values(state.server.turns).filter(t=>t.session_id===state.ui.activeSessionId).sort((a,b)=>b.created_at-a.created_at);
    if(following||!turns.some(t=>t.turn_id===selected))selected=turns.find(t=>!['completed','failed','interrupted','queued'].includes(t.status))?.turn_id||turns[0]?.turn_id;
    const options=JSON.stringify(turns.map(t=>[t.turn_id,t.status,t.input]));
    if(choose.signature!==options){choose.signature=options;choose.replaceChildren(...(turns.length?turns.map(t=>el('option',{value:t.turn_id,text:`${statusText(t.status)} · ${(t.input||'恢复轮次').slice(0,20)} · ${t.turn_id.slice(0,6)}`})):[el('option',{value:'',text:'尚无执行轮次'})]));}
    choose.value=selected||'';choose.disabled=!turns.length;follow.classList.toggle('active',following);follow.setAttribute('aria-pressed',String(following));
    view=buildOverview(turns.find(t=>t.turn_id===selected),state.ui.connectionStatus);
    const values=[duration(view.elapsed),view.turn?String(view.iteration):'—',view.turn?String(view.attemptCount):'—',view.turn?String(view.tools.length):'—',view.tokens==null?'—':view.tokens.toLocaleString()];
    metricNodes.forEach((m,i)=>{m.value.textContent=values[i];});
    metricNodes[4].sub.textContent=view.tokens==null?(view.live?'完成后显示 usage':view.turn?'供应商未提供 usage':'供应商最终返回'):'供应商最终返回';
    runStatus.className=`ov-status ${view.status||'idle'}`;
    const statusLabel=view.turn?statusText(view.status):'等待对话';if(runStatus.textContent!==statusLabel)runStatus.textContent=statusLabel;
    currentPhase.textContent=view.phaseTitle;
    runTitle.textContent=view.turn?.input||(view.turn?'恢复中的轮次':'还没有执行记录');runTitle.title=view.turn?.input||'';
    runMeta.textContent=view.turn?`${view.turn.turn_id.slice(0,8)} · ${view.route}`:'本会话';
    renderGraph();renderLanes();renderTimeline();
  }
  const schedule=()=>{if(!frame)frame=requestAnimationFrame(()=>{frame=null;render();});};
  const unsubs=[store.subscribe(s=>s.server.turns,schedule),store.subscribe(s=>s.ui.connectionStatus,schedule),
    store.subscribe(s=>s.ui.activeSessionId,()=>{selected=null;focused=null;following=true;schedule();})];
  const clock=setInterval(()=>{if(!document.hidden&&view.live)render();},1000);
  render();
  return()=>{disposed=true;root.classList.remove('overview-page');if(previousLive==null)root.removeAttribute('aria-live');else root.setAttribute('aria-live',previousLive);
    clearInterval(clock);unsubs.forEach(f=>f());if(frame)cancelAnimationFrame(frame);};
}
