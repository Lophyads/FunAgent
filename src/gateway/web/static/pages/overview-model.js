// Presentation derived from the shared SSE snapshot; no synthetic progress.
export const isTerminal = status => ['completed', 'failed', 'interrupted'].includes(status);
export const phaseLabels = {
  acquire_execution:'接入与排队', prepare_conversation:'加载会话上下文', assemble_request:'组装模型请求',
  provider_attempts:'模型推理', tool_round:'执行工具', finish_text:'保存最终回复',
  repair_unresolved_tools:'整理工具结果', finalize_turn:'提交轮次结果', record_recovery_failure:'记录恢复状态',
  end_turn:'结束轮次', release_execution:'释放执行权', handle_turn_error:'处理异常',
};
export const nodeLabels = {gateway:'Gateway', context:'会话上下文', model:'模型推理', tools:'工具执行',
  reply:'最终回复', human:'人工交互', memory:'文件记忆', storage:'SQLite', hub:'WebEventHub', browser:'Web 前端'};
export const stateLabels = {idle:'等待执行', active:'执行中', done:'已完成', waiting:'等待答复',
  failed:'失败', interrupted:'已中断', queued:'排队中', unknown:'状态未确认', support:'按需使用',
  accepted:'已采纳', discarded:'已丢弃', started:'等待输出', delta:'流式输出',
  succeeded:'成功', running:'执行中', pending:'待执行', denied:'已拒绝', not_executed:'未执行'};
export const phaseNode = phase => ({acquire_execution:'gateway', prepare_conversation:'context',
  assemble_request:'context', provider_attempts:'model', tool_round:'tools'}[phase] || 'reply');
export function duration(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}
// Only paired events produce a finished duration. Missing diagnostics stay unknown.
export function span(events, predicate, now, live) {
  const matches = events.filter(predicate), start = matches.find(e => /(?:started|pending)$/.test(e.kind));
  const end = [...matches].reverse().find(e => /(?:finished|failed|accepted|discarded|succeeded|denied|interrupted|resolved|cleared)$/.test(e.kind));
  if (!start || (!end && !live)) return null;
  const finish = end?.occurred_at ?? now;
  return Number.isFinite(start.occurred_at) && Number.isFinite(finish) && finish >= start.occurred_at
    ? finish - start.occurred_at : null;
}
export function eventInfo(event) {
  const p = event.payload || {}, kind = event.kind;
  let category = kind.startsWith('model.') || kind.startsWith('provider.') ? 'model'
    : kind.startsWith('tool.') ? 'tool' : kind.startsWith('interaction.') ? 'interaction' : 'system';
  let title = ({'turn.queued':'轮次进入队列', 'turn.submitted':'收到用户输入',
    'turn.cancel_requested':'请求停止执行', 'turn.finished':'轮次结束',
    'model.started':'开始模型尝试', 'model.accepted':'采纳模型响应', 'model.discarded':'丢弃模型尝试',
    'interaction.pending':'等待人工答复', 'interaction.resolved':'已收到人工答复',
    'interaction.cleared':'人工交互已结束', 'storage.failure':'持久化失败',
    'turn.request_assembled':'模型请求已就绪', 'turn.admitted':'开始本轮执行',
    'turn.finalized':'轮次结果已提交', 'turn.unhandled_error':'轮次发生异常',
    'session.created':'建立新会话', 'session.loaded':'读取已有会话',
    'lease.acquired':'取得执行权', 'execution.released':'释放执行资源',
    'lease.fenced_write_rejected':'拒绝过期执行者写入',
    'provider.attempt_started':'请求供应商', 'provider.attempt_failed':'供应商请求失败',
    'provider.response':'收到供应商响应', 'provider.retry_scheduled':'准备重试',
    'provider.fallback':'切换模型路由', 'provider.ambiguous_failure':'供应商结果未确认',
    'recovery.started':'开始恢复轮次', 'recovery.completed':'恢复完成', 'recovery.failed':'恢复失败',
    'recovery.incomplete_turn_found':'发现未完成轮次', 'recovery.tool_pair_repaired':'补齐工具结果',
    'recovery.history_repaired':'修复会话历史'}[kind] || kind);
  if (kind.startsWith('turn.phase_')) {
    title = `${phaseLabels[p.phase] || p.phase || '执行阶段'} · ${{started:'开始',finished:'完成',failed:'失败'}[kind.replace('turn.phase_','')] || ''}`;
    if (p.phase === 'provider_attempts') category = 'model';
    if (p.phase === 'tool_round') category = 'tool';
  } else if (kind.startsWith('tool.')) title = `${p.tool_name || '工具'} · ${stateLabels[kind.split('.')[1]] || kind.split('.')[1]}`;
  const detail = [p.iteration > 0 ? `迭代 ${p.iteration}` : '', p.route, p.error_type].filter(Boolean).join(' · ');
  const tone = /(?:failed|discarded|denied)$/.test(kind) ? 'failed'
    : kind === 'turn.finished' ? (p.status === 'completed' ? 'done' : p.status)
    : kind === 'interaction.pending' ? 'waiting' : /(?:finished|accepted|succeeded|resolved)$/.test(kind) ? 'done' : category;
  return {title, detail, category, tone};
}
export function buildOverview(turn, connection = 'connected', now = Date.now() / 1000) {
  const events = turn?.timeline || [], result = turn?.result, status = result?.status || turn?.status;
  const live = Boolean(turn) && !isTerminal(status), attempts = Object.values(turn?.attempts || {});
  const toolsById = {...turn?.tools};
  // Future outcomes remain authoritative when a tool diagnostic was lost.
  for (const outcome of result?.tool_outcomes || []) toolsById[outcome.tool_call_id] = {
    ...toolsById[outcome.tool_call_id], ...outcome, status:outcome.state,
  };
  const tools = Object.values(toolsById), phases = Object.values(turn?.phases || {});
  const nodeStates = Object.fromEntries(Object.keys(nodeLabels).map(id => [id, ['memory','storage'].includes(id) ? 'support' : 'idle']));
  for (const phase of phases) {
    const id = phaseNode(phase.phase);
    if (phase.status === 'finished' && nodeStates[id] === 'idle') nodeStates[id] = 'done';
    if (phase.status === 'failed') nodeStates[id] = 'failed';
  }
  if (turn?.started_at || result) nodeStates.gateway = 'done';
  if (attempts.some(a => a.status === 'accepted')) nodeStates.model = 'done';
  if (tools.length) nodeStates.tools = tools.some(t => t.status === 'failed') ? 'failed'
    : tools.every(t => ['succeeded','denied','not_executed'].includes(t.status)) ? 'done' : 'unknown';
  if (events.some(e => ['interaction.resolved','interaction.cleared'].includes(e.kind))) nodeStates.human = 'done';
  let activeNode = null;
  if (live) {
    const activeAttempt = [...attempts].reverse().find(a => ['started','delta'].includes(a.status));
    const activeTool = tools.find(t => ['started','running'].includes(t.status));
    activeNode = status === 'queued' ? 'gateway' : status === 'waiting' ? 'human'
      : activeAttempt ? 'model' : activeTool ? 'tools' : turn.phase ? phaseNode(turn.phase) : 'gateway';
    nodeStates[activeNode] = status === 'waiting' ? 'waiting' : status === 'queued' ? 'queued' : 'active';
    if (status === 'waiting') nodeStates.tools = 'waiting';
  }
  if (result) {
    nodeStates.reply = result.status === 'completed' ? 'done' : result.status;
    if (result.status === 'failed' && attempts.at(-1)?.status === 'discarded') nodeStates.model = 'failed';
    if (result.status === 'interrupted') {
      for (const id of ['model','tools']) if (nodeStates[id] === 'unknown' || (id === 'model' && attempts.some(a => ['started','delta'].includes(a.status)))) nodeStates[id] = 'interrupted';
    }
  }
  nodeStates.hub = nodeStates.browser = connection === 'connected' ? (live ? 'active' : 'idle') : 'waiting';
  const start = turn?.started_at ?? turn?.created_at, finish = turn?.finished_at ?? (live ? now : null);
  const elapsed = Number.isFinite(start) && Number.isFinite(finish) ? Math.max(0, finish - start) : null;
  const items = [
    ...attempts.map(a => ({...a, type:'model', id:a.attempt_id,
      status:!live && ['started','delta'].includes(a.status) ? 'unknown' : a.status,
      seconds:span(events, e => e.kind.startsWith('model.') && e.payload?.attempt_id === a.attempt_id, now, live && ['started','delta'].includes(a.status))})),
    ...tools.map(t => ({...t, type:'tool', id:t.tool_call_id,
      status:!live && ['started','running','pending'].includes(t.status) ? 'unknown' : t.status,
      seconds:Number.isFinite(t.duration_ms) ? t.duration_ms / 1000 : span(events,
        e => e.kind.startsWith('tool.') && e.payload?.tool_call_id === t.tool_call_id, now, live && ['started','running'].includes(t.status))})),
  ];
  const iterations = [...new Set(items.map(i => i.iteration))].sort((a,b) => (a ?? Infinity) - (b ?? Infinity));
  return {turn, status, live, result, events, attempts, tools, items, iterations, nodeStates, activeNode, elapsed,
    phaseTitle: status === 'waiting' ? '等待人工答复' : status === 'queued' ? '等待同会话前序轮次'
      : result ? ({completed:'执行完成', failed:'执行失败', interrupted:'执行中断'}[status])
      : phaseLabels[turn?.phase] || (turn ? '等待执行事件' : '等待第一条消息'),
    route:result?.route_used || attempts.at(-1)?.route || '尚未调用模型',
    iteration:result?.iterations ?? turn?.iteration ?? 0,
    attemptCount:result?.provider_attempts ?? attempts.length,
    tokens:Number.isFinite(result?.usage?.total_tokens) ? result.usage.total_tokens : null,
  };
}
