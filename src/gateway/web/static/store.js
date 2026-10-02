export const terminal = s => ['completed', 'failed', 'interrupted'].includes(s);
export function createStore() {
  let state = {server: {sessions: [], messagesBySession: {}, turns: {}, interactions: [], memory: {}, data: {}, catalog: {}},
    ui: {activeSessionId: sessionStorage.getItem('funagent.session') || crypto.randomUUID(), activePage: 'overview', connectionStatus: 'connecting'},
    drafts: {chatBySession: {}, memoryByTarget: {}}};
  const subscriptions = new Set();
  const update = fn => {state = fn(state); for (const sub of subscriptions) {
    const next = sub.selector(state); if (!Object.is(next, sub.value)) {sub.value = next; sub.listener(next);}
  }};
  return {get: () => state, update, subscribe(selector, listener) {
    const sub = {selector, listener, value: selector(state)}; subscriptions.add(sub); listener(sub.value);
    return () => subscriptions.delete(sub);
  }, server(key, value) {update(s => ({...s, server: {...s.server, [key]: value}}));},
  ui(key, value) {update(s => ({...s, ui: {...s.ui, [key]: value}}));},
  draft(group, key, value) {update(s => ({...s, drafts: {...s.drafts, [group]: {...s.drafts[group], [key]: value}}}));}};
}
export function applyEvent(store, e) {
  store.update(s => {
    let turns = {...s.server.turns};
    if (e.kind === 'snapshot' || e.kind === 'reset') {
      for (const [id, turn] of Object.entries(turns)) if (turn.session_id === e.session_id) delete turns[id];
      for (const t of e.payload.turns) turns[t.turn_id] = t;
    } else if (e.turn_id) {
      const previous = turns[e.turn_id] || {session_id: e.session_id, turn_id: e.turn_id, status: 'queued',
        created_at: e.occurred_at, attempts: {}, tools: {}, phases: {}, timeline: []};
      if (terminal(previous.status) && e.kind !== 'turn.finished') return s;
      const t = {...previous}, p = e.payload, kind = e.kind;
      if (kind === 'turn.finished') {t.status = p.status; t.result = p; t.finished_at = e.occurred_at;}
      else if (kind === 'turn.cancel_requested') t.status = 'cancelling';
      else if (kind === 'interaction.pending' && t.status !== 'cancelling') t.status = 'waiting';
      else if (['interaction.resolved','interaction.cleared'].includes(kind) && t.status === 'waiting') t.status = 'running';
      else if (!['turn.queued', 'turn.submitted'].includes(kind) && t.status === 'queued') {t.status = 'running'; t.started_at = e.occurred_at;}
      if (kind === 'turn.submitted') {t.input = p.text; t.client_request_id = p.client_request_id;}
      if (kind.startsWith('model.')) {
        const attempt = {...(t.attempts[p.attempt_id] || {text: ''}), attempt_id: p.attempt_id,
          iteration: p.iteration, route: p.route, status: kind.split('.')[1]};
        if (kind === 'model.delta') attempt.text += p.text;
        if (kind === 'model.accepted') attempt.text = p.text;
        t.attempts = {...t.attempts, [p.attempt_id]: attempt};
      }
      if (kind.startsWith('tool.') && p.tool_call_id) t.tools = {...t.tools, [p.tool_call_id]: {...p, status: kind.split('.')[1]}};
      if (kind.startsWith('turn.phase_')) {t.phase = p.phase; t.iteration = p.iteration;
        t.phases = {...t.phases, [`${p.iteration}:${p.phase}`]: {...p, status: kind.replace('turn.phase_', '')}};}
      if (kind !== 'model.delta') t.timeline = [...t.timeline, {...e, payload: Object.fromEntries(Object.entries(p).filter(([k]) => k !== 'text'))}].slice(-300);
      turns[e.turn_id] = t;
    }
    // Mirrors server retention without removing running turns.
    const done = Object.values(turns).filter(t => terminal(t.status)).sort((a,b) => (b.finished_at || 0) - (a.finished_at || 0));
    for (const t of done.slice(200)) delete turns[t.turn_id];
    return {...s, server: {...s.server, turns}};
  });
}
