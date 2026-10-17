// Saved trace projections. No DOM or application ports are used here.
export const readable = v => typeof v === 'string' && v.trim() ? v.trim() : null;
export const seconds = v => Number.isFinite(v) && v >= 0 ? `${(v / 1000).toFixed(v < 10000 ? 2 : 1)} s` : '—';
export const dateTime = v => Number.isFinite(v) ? new Date(v * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '时间未知';
export const shortId = v => v ? `${v.slice(0,8)}…` : '未关联会话';
export const resultText = v => ({queued:'排队',running:'运行中',waiting:'等待答复',cancelling:'取消请求中',completed:'完成',succeeded:'成功',failed:'失败',cancelled:'已取消',interrupted:'中断',accepted:'已接受',ok:'成功',skipped:'已跳过',empty:'无结果',partial:'部分完成',ready:'就绪',dequeued:'已出队',allow:'允许',allowed:'已允许',denied:'已拒绝',blocked:'已阻止',timeout:'超时',exhausted:'已耗尽'}[v] || v || '—');
export const collectionText = v => ({pending:'采集中',complete:'采集完整',incomplete:'采集不完整',not_collected:'未采集'}[v] || v || '未知');
export const eventText = v => ({start_task:'任务开始',start:'开始',update:'更新',complete:'完成',end_task:'任务结束',seal:'封存',cancel:'取消'}[v] || v);
export const formatValue = v => v === undefined ? '未记录' : typeof v === 'string' ? v : JSON.stringify(v,null,2) ?? 'null';
export function parseJSON(v) {if(typeof v!=='string')return v;try{return JSON.parse(v);}catch{return v;}}
export const payload = (trace,ref) => ref ? trace?.payloads?.[ref]?.data : undefined;
const labels = {'conversation.prepare':'会话准备','profile.retrieve':'加载用户画像','memory.prepare':'记忆规划','memory.attach':'检索结果附加','memory.plan':'检索规划','memory.branch':'检索分支','memory.search':'记忆来源检索','memory.source.search':'来源查询','memory.query':'查询','memory.deduplicate':'候选去重','memory.filter':'分数筛选','memory.fit':'预算裁剪','memory.authorization':'访问校验','model.rank':'模型重排','model.encode':'请求编码','model.attempt':'模型交互','model.retry_wait':'重试等待','journal.tool_calls':'调用记录','tool.call':'工具调用','tool.policy_and_validation':'调用校验','tool.handler':'工具执行','journal.tool_result':'结果记录','agent.run':'执行器','agent.iteration':'模型循环','interaction.wait':'人工等待','turn.finalize':'最终保存','task.queue_wait':'排队等待'};
export function traceName(trace,node) {
  const name=labels[node.name]||node.name||'未命名步骤';
  if(node.name==='memory.rerank')return `记忆重排 · ${{facts:'事实',knowledge:'知识'}[node.branch]||node.branch||'记忆'}`;
  if(node.name==='memory.search'&&descendants(trace?.root).find(parent=>parent.span_id===node.parent_span_id)?.name==='profile.retrieve')return labels['profile.retrieve'];
  if(node.name==='tool.call'||node.kind==='tool')return `${name} · ${readable(payload(trace,node.input_ref)?.name)||'未命名工具'}`;
  if(node.name==='model.attempt')return `${node.kind==='rerank'?'重排评分':name} · ${node.model||payload(trace,node.input_ref)?.model||'未记录模型'}`;
  if(node.name==='memory.source.search'&&node.source_id)return `${name} · ${node.source_id}`;
  return name;
}
export function descendants(node) {
  const out=[],pending=node?[node]:[],seen=new Set();
  while(pending.length){const n=pending.pop();if(seen.has(n.span_id))continue;seen.add(n.span_id);out.push(n);pending.push(...[...(n.children||[])].reverse());}
  return out;
}
export function externalNodes(node) {
  return descendants(node).slice(1).filter(n => n.name==='model.attempt'||n.name==='memory.source.search'||n.name==='tool.call'||n.kind==='embedding'||(n.kind==='retriever'&&n.name==='memory.search'&&!descendants(n).some(c=>c.name==='memory.source.search')));
}
export function buildTrace(review) {
  const trace=review.trace||{},root=trace.root, nodes=new Map(),eventsBySpan=new Map(),startSeq=new Map();
  for(const n of [root,...(trace.orphan_nodes||[])].flatMap(descendants))nodes.set(n.span_id,n);
  for(const e of [...(trace.events||[])].sort((a,b)=>a.seq-b.seq)){if(!eventsBySpan.has(e.span_id))eventsBySpan.set(e.span_id,[]);eventsBySpan.get(e.span_id).push(e);if(e.operation==='start'||e.operation==='start_task')startSeq.set(e.span_id,e.seq);}
  const rows=[],stageIds=new Set();
  const add=(kind,node,note='')=>{if(stageIds.has(node.span_id))return;stageIds.add(node.span_id);rows.push({kind,node,note,name:traceName(trace,node)});};
  let loopNumber=0;
  for(const n of root?.children||[]){
    if(n.name==='agent.run'&&(n.purpose||'main')==='main'){
      for(const child of n.children||[]){
        if(child.name==='agent.iteration'){
          const number=child.iteration||++loopNumber;loopNumber=Math.max(loopNumber,number);
          add(`Loop ${number}`,child);
          for(const tool of descendants(child).filter(n=>n.name==='tool.call'||n.kind==='tool'))add('Tool Call',tool,`属于 Loop ${number}`);
        }else add('步骤',child);
      }
    }else add(n.name==='conversation.prepare'?'Prepare':n.name.startsWith('memory.')?'Retrieval':n.name==='turn.finalize'?'Finalize':'步骤',n);
  }
  for(const n of trace.orphan_nodes||[])add('未关联',n,`原父节点 ${n.parent_span_id||'未知'}`);
  for(const n of nodes.values()){
    if(n.name==='memory.search')add('Retrieval',n);
    if(n.name==='memory.rerank')add('重排',n);
  }
  if(!rows.length&&root)add('任务',root);
  rows.sort((a,b)=>(a.node.started_at??Infinity)-(b.node.started_at??Infinity)||(startSeq.get(a.node.span_id)??Infinity)-(startSeq.get(b.node.span_id)??Infinity));
  const userInput=payload(trace,root?.input_ref);
  return {review,trace,root,nodes,eventsBySpan,rows,stageIds,user:readable(userInput?.text)||readable(userInput?.content)||'未记录用户输入'};
}
export function eventsFor(model,node) {
  return descendants(node).flatMap(n=>model.eventsBySpan.get(n.span_id)||[]).sort((a,b)=>a.seq-b.seq);
}
export function stageFor(model,id) {
  const visited=new Set();let n=model.nodes.get(id);
  while(n&&!visited.has(n.span_id)){if(model.stageIds.has(n.span_id))return n.span_id;visited.add(n.span_id);n=model.nodes.get(n.parent_span_id);}
  return null;
}
export function modelReply(node,value) {
  const v=node.decoded_reply??value;
  if(!v||typeof v!=='object')return {content:v,calls:[],finish_reason:null,partial:false};
  const message=v.choices?.[0]?.message||v.choices?.[0]?.delta||v;
  return {content:message.content,calls:message.calls||message.tool_calls||[],finish_reason:v.finish_reason||v.choices?.[0]?.finish_reason,partial:!!v.partial};
}
export function toolContent(value) {
  const parsed=parseJSON(value);
  if(!parsed||typeof parsed!=='object'||!Array.isArray(parsed.content))return formatValue(parsed);
  const parts=parsed.content.map(part=>part?.type==='text'&&typeof part.text==='string'?formatValue(parseJSON(part.text)):`[${part?.type||'未知内容类型'}]\n${formatValue(part)}`);
  if(parsed.structuredContent!=null)parts.push(`结构化内容\n${formatValue(parsed.structuredContent)}`);
  return parts.length?parts.join('\n\n'):formatValue(parsed);
}
export function interactionText(node,side,value) {
  if(node.name==='memory.rerank'){
    const records=items=>Array.isArray(items)?(items.length?items.map((r,i)=>`${i+1}. 输入 #${r.index??'未记录'} · ${r.source_id||'来源未知'} / ${r.record_id||'记录未知'}\n版本  ${r.version??'未记录'}${Number.isFinite(r.score)?` · 重排分数 ${r.score}`:''}\n${formatValue(r.content)}`).join('\n\n'):'（无记录）'):'未完整采集';
    if(side==='input')return `评分问题\n${value?.question??'未记录'}\n\n去重后候选（输入索引从 0 开始）\n${records(value?.records)}`;
    if(value?.mode==='fallback')return `重排失败，沿用原候选顺序\n错误  ${value.error_code||node.error_code||'未记录'}\n\n${records(value.records)}`;
    if(value?.mode!=='reranked')return formatValue(value);
    return `模型  ${node.model||'未记录'}\n分数阈值  ${node.min_score??'未设置（仅排序）'}\n\n评分（按输入索引）\n${formatValue(value.scores)}\n\n选中记录（实际顺序，仍需后续预算裁剪）\n${records(value.selected)}\n\n排除记录\n${records(value.rejected)}`;
  }
  if(side==='input'&&['memory.search','memory.source.search','memory.query'].includes(node.name)&&value&&typeof value==='object')return `查询  ${readable(value.text)||'未提供文本查询'}\n\n${formatValue(value)}`;
  if(node.name==='memory.deduplicate'&&side==='output')return `代表记录\n${formatValue(value?.representatives)}\n\n副本分组\n${formatValue(value?.duplicate_groups)}\n\n身份冲突\n${formatValue(value?.identity_conflicts)}`;
  if(node.name==='memory.filter')return side==='input'?`分数阈值  ${node.min_score??'未设置（仅排序）'}\n\n候选评分\n${formatValue(value?.items)}`:`选中记录\n${formatValue(value?.selected)}\n\n排除记录\n${formatValue(value?.rejected)}`;
  if(node.name==='model.rank'||(node.name==='model.attempt'&&node.kind==='rerank')){
    if(side==='input')return `评分问题  ${value?.query??value?.question??'未记录'}\n\n候选正文（按输入索引排列）\n${formatValue(value?.texts)}`;
    if(node.error_code)return `评分失败  ${node.error_code}\n\n${formatValue(value)}`;
    return `候选评分（按输入索引排列）\n${formatValue(node.decoded_reply?.scores??value?.scores??value)}`;
  }
  if(node.name==='model.attempt'){
    if(side==='input'){
      const messages=Array.isArray(value?.messages)?value.messages:[];
      if(!messages.length)return formatValue(value);
      return `模型  ${value.model||node.model||'未记录'}\n消息  ${messages.length} 条\n\n${messages.map((m,i)=>`[${i+1} · ${m.role||'message'}]\n${formatValue(m.content)}${m.call_id||m.tool_call_id?`\n调用 ID  ${m.call_id||m.tool_call_id}`:''}${m.calls?.length||m.tool_calls?.length?`\n工具调用\n${formatValue(m.calls||m.tool_calls)}`:''}`).join('\n\n')}${value.tools?.length?`\n\n可用工具\n${formatValue(value.tools)}`:''}`;
    }
    const r=modelReply(node,value);
    return `${r.partial?'部分输出\n':''}模型输出\n${formatValue(r.content??'（无正文）')}${r.calls.length?`\n\n工具调用\n${r.calls.map(c=>`${c.name||c.function?.name||'工具'} · ${c.call_id||c.id||'—'}\n${formatValue(parseJSON(c.arguments??c.args??c.function?.arguments))}`).join('\n\n')}`:''}\n\n结束原因  ${r.finish_reason||'未记录'}`;
  }
  if(node.name==='tool.call'||node.kind==='tool'){
    if(side==='input')return `工具  ${value?.name||'未命名'}\n调用 ID  ${value?.call_id||node.call_id||'—'}\n\n参数\n${formatValue(value?.arguments)}${value?.argument_error?`\n\n参数错误\n${formatValue(value.argument_error)}`:''}`;
    return `状态  ${resultText(value?.status||node.status)}\n执行效果  ${value?.effect||node.effect||'未记录'}\n\n结果\n${toolContent(value?.content)}`;
  }
  return formatValue(value);
}
