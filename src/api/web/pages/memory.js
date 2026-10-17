import {mountMemoryImportDialog} from '../components/memory-import-dialog.js';
import {el, button, pageHeader, notify, errorBox, confirmAction} from '../components/ui.js';
import {dateLabel, memoryJSON} from '../api.js';
import {mountInspection} from '../components/memory-datasets.js';
import {mountTopicTree} from '../components/memory-topic-tree.js';
import {mountTopicDialog} from '../components/memory-topic-dialog.js';
import {createTopicState} from '../components/memory-topic-state.js';
import {UNCLASSIFIED, nodeKey, topicPath, memoryFailureDraft, memoryReconciledDraft} from '../components/memory-topic-model.js';

const labels = {all:'全部', user:'用户事实', note:'长期笔记', episode:'历史事件', knowledge:'知识文档'};
const recordTypes = {user:'profile', note:'note', episode:'episode', knowledge:'knowledge'};
const drivers = {sqlite:'SQLite', supabase:'Supabase', cognee:'Cognee'};

/** Page orchestrator. The injected API may be a fake with the same async methods. */
export function mount(root, {store, api}) {
  let sources = [], sourceId = '', kind = 'all', disposed = false, saving = false, importing = false;
  let selection = {kind:'root'}, state = {topics:[], pages:{}, revision:null}, scopeGeneration = 0;
  const source = () => sources.find(item => item.source_id === sourceId);
  const target = () => JSON.stringify([sourceId, kind]);
  const draft = () => store.get().drafts.memoryByTarget[target()] || null;
  const busy = () => saving || importing;
  const can = capability => Boolean(source()?.capabilities?.includes(capability) &&
    (!['write','update','delete'].includes(capability) || source().writable));
  const canManageTopics = () => Boolean(source()?.writable && can('list'));
  const setDraft = value => store.draft('memoryByTarget', target(), value);
  const emptyDraft = (type = recordTypes[kind], topicId = selection.kind === 'topic' ? selection.topicId : null) =>
    ({action:'add', type, topic_id:topicId, content:'', subject:'', origin:'', event_at:'', dirty:false});

  const sourceSelector = el('select', {'aria-label':'记忆库'}, el('option', {value:'', text:'加载记忆库…'}));
  const selector = el('select', {'aria-label':'记忆类型'});
  const file = el('input', {type:'file', accept:'.md', hidden:true, 'aria-label':'Markdown 文件'});
  const upload = button('导入 Markdown', () => file.click()), refresh = button('刷新', () => refreshPage());
  const batchUpload = button('批量导入 JSON', () => importDialog.open(source()));
  pageHeader(root, 'Memory', '按主题浏览记忆；标题、正文和所属主题分别管理。', sourceSelector, selector, upload, batchUpload, refresh);
  const importDialog = mountMemoryImportDialog(root, {api, refresh:id => !disposed && sourceId === id ? reads.refresh() : undefined});
  root.classList.add('memory-page');
  const scope = el('p', {class:'muted memory-scope'});
  const search = el('input', {type:'search', placeholder:'搜索当前记忆库', 'aria-label':'搜索记忆'});
  const searchButton = button('搜索', () => reads.search(search.value.trim()));
  const clearSearch = button('返回目录', () => {search.value = ''; reads.search('');});
  const newEntry = button('＋ 新条目', () => startNew()), newTopic = button('＋ 新主题', () => openTopic('create'));
  const count = el('p', {class:'muted', 'aria-live':'polite'}), catalogError = el('div');
  const topicNotice = el('p', {class:'memory-topic-notice', role:'status', hidden:true});
  const treeRoot = el('div'), resultsRoot = el('div', {class:'memory-entries'});
  const directoryTitle = el('h2'), directoryDescription = el('p', {class:'muted'});
  const editTopic = button('编辑主题', () => openTopic('edit')), deleteTopic = button('删除主题', () => removeTopic(), 'danger');
  const directoryPanel = el('div', {class:'memory-directory-info'}, directoryTitle, directoryDescription,
    el('div', {class:'actions'}, editTopic, deleteTopic));
  const title = el('h2'), version = el('small', {class:'muted memory-version'});
  const subject = el('input', {type:'text', maxlength:512, placeholder:'记忆标题（可选）', 'aria-label':'记忆标题'});
  const origin = el('input', {type:'text', maxlength:512, placeholder:'来源文件名（可选）', 'aria-label':'来源文件名'});
  const eventDate = el('input', {type:'text', maxlength:100, placeholder:'事件日期，如 2026-10-15（可选）', 'aria-label':'事件日期'});
  const entryType = el('select', {'aria-label':'新增记忆类型'}), topicSelect = el('select', {'aria-label':'新增记忆所属主题'});
  const assignedTopic = el('span'), assign = button('更换主题', () => openTopic('assign'));
  const topicRow = el('div', {class:'actions memory-assignment'}, assignedTopic, assign);
  const textarea = el('textarea', {class:'memory-editor', rows:16, maxlength:65536, 'aria-label':'记忆内容', placeholder:'记忆正文或 Markdown 原文…'});
  const status = el('p', {class:'muted', 'aria-live':'polite'});
  const conflict = el('div', {class:'conflict', hidden:true}, el('p', {text:'记录已变化。正文草稿已保留，请核对后采用当前版本。'}));
  const adopt = button('采用当前版本', () => {
    const current = draft(), latest = findRecord(current?.id);
    if (latest?.version) setDraft({...current, expected_version:latest.version, conflict:false, failure:null, uncertain:false});
  });
  const saveAsNew = button('草稿另存为新条目', () => {
    const current = draft();
    setDraft({...emptyDraft(current.type), content:current.content, subject:current.subject, origin:current.origin, event_at:current.event_at, dirty:true});
  });
  conflict.append(adopt, saveAsNew);
  const reconciled = button('已核对，继续编辑', async () => {
    const own = target(), current = draft(), gen = scopeGeneration;
    const approved = await confirmAction('核对操作结果', '请先刷新目录并核对正文是否已经写入、修改或删除。确认后允许继续编辑；新增原样重试仍使用原请求标识。');
    if (approved && !disposed && gen === scopeGeneration && own === target()) {
      const latest = findRecord(current.id || current.verified_ref?.record_id);
      const next = memoryReconciledDraft(current, latest, sourceId);
      if (!next) {notify('请先刷新并读取当前记录版本。', true); return;}
      setDraft(next);
    }
  });
  const save = button('保存', () => write(), 'primary'), remove = button('删除条目', () => write('remove'), 'danger');
  const editor = el('div', {class:'memory-panel memory-editor-form'}, title, version, conflict, subject, origin, eventDate,
    el('label', {}, '新增类型', entryType), el('label', {}, '所属主题（必选）', topicSelect), topicRow,
    textarea, status, reconciled, el('div', {class:'actions'}, remove, save));
  const layout = el('div', {class:'two-columns memory-layout', id:'memory-panel-records', role:'tabpanel', 'aria-labelledby':'memory-tab-records'},
    el('section', {class:'card memory-navigation'}, el('div', {class:'actions memory-search'}, search, searchButton, clearSearch),
      el('div', {class:'actions'}, newEntry, newTopic), count, topicNotice, catalogError, treeRoot, resultsRoot),
    el('section', {class:'card memory-panel'}, directoryPanel, editor));
  const inspectionRoot = el('div'); root.append(file, scope, inspectionRoot, layout);
  const inspection = mountInspection(inspectionRoot, {store, api, onTab:tab => {
    const inspecting = tab !== 'records'; layout.hidden = selector.hidden = upload.hidden = scope.hidden = inspecting;
  }});
  const tree = mountTopicTree(treeRoot, {
    onSelectTopic(value) {selection = value; saveSelection(); reads.expand(nodeKey(value)); render();},
    onSelectRecord:selectRecord, onExpand:key => reads.expand(key), onLoadMore:key => reads.loadNode(key, true),
    onDeleteTopic:topicId => removeTopic(topicId)
  });
  const reads = createTopicState({api, currentNode:() => nodeKey(selection), onChange:value => {
    state = value;
    if (value.revision !== null && ['topic','record'].includes(selection.kind) && selection.topicId &&
      !value.topics.some(topic => topic.topic_id === selection.topicId)) {
      selection = selection.kind === 'record' ? {...selection, topicId:null} : {kind:'root'};
    }
    store.server('memory', {...store.get().server.memory, [target()]:value}); render();
  }});
  const topicDialog = mountTopicDialog(root, {
    refresh:own => own.source.source_id === sourceId ? reads.refresh() : undefined, submit:submitTopic
  });

  function saveSelection() {store.ui('memorySelection', {sourceId, selection});}
  function findRecord(id) {return Object.values(state.pages).flatMap(page => page.entries).find(record => (record.id || record.record_id) === id);}
  function recordDraft(record, readonly = false) {
    return {action:readonly || !record.version ? 'view' : 'replace', id:record.id || record.record_id,
      expected_version:record.version, type:record.type, topic_id:record.topic_id, topic_path:record.topic_path, index_status:record.index_status,
      content:record.content, subject:record.subject || '', origin:record.origin || '',
      event_at:dateLabel(record.event_timestamp || record.event_at), event_timestamp:record.event_timestamp || record.event_at, dirty:false};
  }
  async function allowReplaceDraft(current = draft()) {
    const hasChanges = current?.dirty || current?.uncertain || current?.sync_failed || current?.saved_unconfirmed || (current?.dirty === undefined && current?.content);
    return !hasChanges ||
      await confirmAction('保留或放弃草稿', '当前正文有未保存内容或待核对的操作结果。可取消后先保存、核对；确认将放弃当前草稿并打开所选条目。');
  }
  async function selectRecord(record) {
    if (busy()) return;
    const id = record.id || record.record_id, gen = scopeGeneration;
    if (draft()?.id !== id || draft()?.sync_failed || draft()?.saved_unconfirmed || (draft()?.action === 'view') !== Boolean(state.results)) {
      if (!await allowReplaceDraft() || disposed || gen !== scopeGeneration) return;
      setDraft(recordDraft(record, Boolean(state.results)));
    }
    selection = {kind:'record', recordId:id, topicId:record.topic_id || null}; saveSelection(); render(true);
  }
  async function startNew() {
    if (busy() || !can('write')) return;
    const gen = scopeGeneration;
    if (await allowReplaceDraft() && !disposed && gen === scopeGeneration) {setDraft(emptyDraft()); render(true);}
  }
  function kindsFor(selected) {return selected?.store === 'cognee' ? ['all','note'] : selected?.kind === 'semantic' ? ['all','knowledge'] : Object.keys(labels);}
  async function changeTarget(nextSource, nextKind) {
    const changedSource = nextSource !== sourceId;
    if (changedSource) topicNotice.hidden = true;
    sourceId = nextSource;
    const kinds = kindsFor(source()); kind = kinds.includes(nextKind) ? nextKind : kinds[0];
    scopeGeneration++; const gen = scopeGeneration;
    if (changedSource) selection = {kind:'root'};
    else if (selection.kind === 'record') selection = selection.topicId ? {kind:'topic', topicId:selection.topicId} : {kind:'unclassified'};
    store.ui('memorySourceId', sourceId); store.ui('memoryKind', kind); saveSelection();
    selector.replaceChildren(...kinds.map(value => el('option', {value, text:labels[value]})));
    selector.value = kind; sourceSelector.value = sourceId; search.value = '';
    inspection.setSource(source());
    await reads.setScope(source(), kind);
    if (!disposed && gen === scopeGeneration) {
      for (const key of tree.expandedNodes()) reads.expand(key);
      render(true);
    }
  }
  async function refreshPage() {
    if (['data','graph'].includes(store.get().ui.memoryInspectionTab) && source()?.inspection) return inspection.refresh();
    await reads.refresh();
    if (!disposed && search.value.trim()) await reads.search(search.value.trim());
  }

  for (const [node, key] of [[textarea,'content'],[subject,'subject'],[origin,'origin'],[eventDate,'event_at']]) {
    node.addEventListener('input', () => setDraft({...draft(), [key]:node.value, dirty:true, request_key:undefined, failure:null}));
  }
  entryType.addEventListener('change', () => setDraft({...draft(), type:entryType.value, dirty:true, request_key:undefined}));
  topicSelect.addEventListener('change', () => setDraft({...draft(), topic_id:topicSelect.value || null, dirty:true, request_key:undefined}));
  sourceSelector.addEventListener('change', () => changeTarget(sourceSelector.value, kind));
  selector.addEventListener('change', () => changeTarget(sourceId, selector.value));
  search.addEventListener('keydown', event => {if (event.key === 'Enter' && !busy()) reads.search(search.value.trim());});
  search.addEventListener('input', () => {if (!search.value) reads.search('');});
  file.addEventListener('change', importMarkdown);

  async function importMarkdown() {
    const selected = file.files?.[0]; file.value = '';
    if (!selected || busy() || !can('write')) return;
    const gen = scopeGeneration, ownSource = source();
    if (!await allowReplaceDraft() || disposed || gen !== scopeGeneration) return;
    const importedKind = ownSource.store === 'cognee' ? 'note' : 'knowledge';
    if (importedKind !== kind && !await allowReplaceDraft(store.get().drafts.memoryByTarget[JSON.stringify([sourceId, importedKind])])) return;
    if (disposed || gen !== scopeGeneration) return;
    const selectedTopic = selection.kind === 'topic' ? selection.topicId : null;
    importing = true; render();
    try {
      if (!/\.md$/i.test(selected.name)) throw new Error('请选择 .md 文件。');
      if (selected.size > 65536) throw new Error('文档超过 64 KiB，请缩小文档或拆成多个文件。');
      let content;
      try {content = new TextDecoder('utf-8', {fatal:true, ignoreBOM:true}).decode(await selected.arrayBuffer());}
      catch {throw new Error('文档必须使用 UTF-8 编码。');}
      if (!content.trim()) throw new Error('文档内容为空。');
      if (disposed || gen !== scopeGeneration) return;
      const next = {...emptyDraft(recordTypes[importedKind], selectedTopic), content,
        subject:selected.name.replace(/\.md$/i, ''), origin:selected.name, imported:true, dirty:true};
      try {memoryJSON({source_id:ownSource.source_id, type:next.type, content, subject:next.subject, origin:next.origin,
        topic_id:selectedTopic, request_key:crypto.randomUUID(), event_at:null});}
      catch (error) {next.failure = error.message;}
      await changeTarget(ownSource.source_id, importedKind);
      if (!disposed) {setDraft(next); notify('文档已载入草稿，请选择主题后保存。');}
    } catch (error) {if (!disposed) notify(error.message, true);}
    finally {importing = false; if (!disposed) render(true);}
  }

  function openTopic(mode) {
    if (busy() || state.loading || state.error || state.revision === null) return;
    const topic = selection.kind === 'topic' ? state.topics.find(item => item.topic_id === selection.topicId) : null;
    if (mode === 'edit' && !topic) return;
    topicDialog.open({mode, source:source(), topics:state.topics, revision:state.revision,
      topic:mode === 'edit' ? topic : null, recordId:mode === 'assign' ? draft()?.id : null,
      topicId:mode === 'assign' ? findRecord(draft()?.id)?.topic_id || draft()?.topic_id : topic?.topic_id});
  }
  async function submitTopic(own, value) {
    // Targets and expected revisions belong to the form opening, never the current selection.
    if (own.mode === 'create') await api.createMemoryTopic(own.source, value);
    else if (own.mode === 'edit') await api.updateMemoryTopic(own.source, own.topic.topic_id, value, own.revision);
    else {
      const result = await api.assignMemoryTopic(own.source, own.recordId, value.topic_id, own.revision);
      if (!disposed && sourceId === own.source.source_id) {
        const current = draft();
        if (current?.id === own.recordId) setDraft({...current, topic_id:result.topic_id, topic_path:result.topic_path});
        if (selection.kind === 'record' && selection.recordId === own.recordId) selection = {...selection, topicId:result.topic_id};
      }
    }
    if (!disposed && sourceId === own.source.source_id) {notify('主题目录已保存'); await reads.refresh();}
  }
  function unclassifyTopicDrafts(ownSource, topicId) {
    for (const ownKind of Object.keys(labels)) {
      const key = JSON.stringify([ownSource.source_id, ownKind]);
      const current = store.get().drafts.memoryByTarget[key];
      if (!current || current.topic_id !== topicId || current.action === 'add' && current.uncertain) continue;
      store.draft('memoryByTarget', key, {...current, topic_id:null, topic_path:[],
        ...(current.action === 'add' ? {request_key:undefined} : {})});
    }
  }
  async function removeTopic(topicId = selection.kind === 'topic' ? selection.topicId : null) {
    if (busy() || !canManageTopics() || state.loading || state.error || state.revision === null) return;
    const topic = state.topics.find(item => item.topic_id === topicId);
    if (!topic) return;
    const ownSource = source(), revision = state.revision, gen = scopeGeneration;
    const path = topicPath(state.topics, topicId).join(' / ');
    if (!await confirmAction('删除主题', `主题：${path}\n\n删除此主题，其直接记忆将进入未归类，正文不会删除。`)) return;
    if (disposed || gen !== scopeGeneration) return;
    saving = true; render();
    let deleted = false;
    try {
      await api.deleteMemoryTopic(ownSource, topicId, revision); deleted = true;
      unclassifyTopicDrafts(ownSource, topicId);
      if (!disposed && gen === scopeGeneration) {
        if (selection.kind === 'topic' && selection.topicId === topicId) selection = {kind:'unclassified'};
        else if (selection.kind === 'record' && selection.topicId === topicId) selection = {...selection, topicId:null};
        saveSelection(); topicNotice.hidden = false; topicNotice.classList.remove('error');
        topicNotice.textContent = `已删除主题「${path}」，直接记忆已进入未归类，正文草稿已保留。`;
      }
    } catch (error) {
      if (!disposed && gen === scopeGeneration) {
        topicNotice.hidden = false; topicNotice.classList.add('error');
        const uncertain = !error.status || error.status >= 500;
        topicNotice.textContent = `删除主题「${path}」${uncertain ? '的结果未确认，请刷新核对后再操作。' : '失败：'} ${error.message}`;
      }
    } finally {
      if (!disposed && gen === scopeGeneration) {
        await reads.refresh();
        if (deleted) tree.expandNode(UNCLASSIFIED);
      }
      saving = false; if (!disposed) render();
    }
  }

  async function write(action) {
    const current = draft();
    if (busy() || !current || current.action === 'view' || current.uncertain || current.sync_failed || current.saved_unconfirmed) return;
    const ownSource = source(), ownKind = kind, own = target(), gen = scopeGeneration;
    action ||= current.action;
    const body = {...current, action};
    let submittedDraft = current;
    if (action === 'add') {
      if (!current.topic_id || !state.topics.some(topic => topic.topic_id === current.topic_id)) {notify('请选择有效的所属主题。正文草稿已保留。', true); return;}
      body.request_key = current.request_key || crypto.randomUUID();
      submittedDraft = {...current, request_key:body.request_key};
      store.draft('memoryByTarget', own, submittedDraft);
    }
    // A completed write still belongs to its captured target after unmount. Preserve
    // any newer draft created by another page instance while this request was pending.
    const commitDraft = value => {
      if (store.get().drafts.memoryByTarget[own] !== submittedDraft) return false;
      store.draft('memoryByTarget', own, value); return true;
    };
    saving = true; render();
    try {
      const result = await api.writeMemory(ownSource, ownKind, body);
      if (result.declined) {if (!disposed) notify('已放弃操作，草稿已保留'); return;}
      const next = action === 'remove' ? null : recordDraft({...result,
        topic_id:result.topic_id === undefined ? current.topic_id : result.topic_id,
        topic_path:result.topic_path || current.topic_path});
      const accepted = commitDraft(next);
      if (!disposed && gen === scopeGeneration && accepted) {
        if (next) selection = {kind:'record', recordId:next.id, topicId:next.topic_id || null};
        else selection = current.topic_id ? {kind:'topic', topicId:current.topic_id} : {kind:'unclassified'};
        saveSelection(); inspection.invalidate(); notify(action === 'remove' ? '记忆已删除' :
          result.index_status === 'pending' ? '事实与主题已保存，图谱索引待完成' :
          result.index_status === 'unknown' ? '事实与主题已保存，图谱索引状态未知' : '记忆已保存');
        await refreshPage();
      }
    } catch (error) {
      const partial = error.effect === 'known' || error.code === 'TOPIC_SYNC_FAILED';
      const accepted = commitDraft(memoryFailureDraft(submittedDraft, error, action, ownSource.source_id));
      if (!disposed && gen === scopeGeneration && accepted) {
        notify(error.message, true);
        const topicFailed = error.code === 'TOPIC_SYNC_FAILED' || error.issues?.some(issue => issue.code === 'TOPIC_SYNC_FAILED');
        if (partial && action === 'add') selection = topicFailed || !current.topic_id ? {kind:'unclassified'} : {kind:'topic', topicId:current.topic_id};
        await reads.refresh();
        if (partial && action === 'add') await reads.loadNode(nodeKey(selection));
      }
    } finally {saving = false; if (!disposed) render(true);}
  }

  function renderDirectory() {
    const key = nodeKey(selection), topic = state.topics.find(item => item.topic_id === key);
    directoryTitle.textContent = topic ? topicPath(state.topics, key).join(' / ') : key === UNCLASSIFIED ? '未归类' : '当前记忆库';
    directoryDescription.textContent = topic?.description || (key === UNCLASSIFIED ? '选择条目后可通过“更换主题”完成人工归类。' : '展开主题查看直接记忆；主题也可以包含子主题。');
    editTopic.hidden = deleteTopic.hidden = !topic;
    editTopic.disabled = deleteTopic.disabled = busy() || state.loading || Boolean(state.error) || !canManageTopics();
  }
  function renderEditor(force) {
    const current = draft(); editor.hidden = !current;
    if (!current) return;
    const adding = current.action === 'add', latest = findRecord(current.id);
    const changed = current.action === 'replace' && latest && latest.version !== current.expected_version;
    const readonly = current.action === 'view' || !can(adding ? 'write' : 'update');
    const locked = busy() || readonly || current.uncertain || Boolean(current.sync_failed || current.saved_unconfirmed);
    title.textContent = current.action === 'view' ? '查看检索结果' : adding ? current.imported ? '导入 Markdown' : '新增条目' : '编辑记忆';
    version.textContent = current.id ? `来源 ${sourceId} · 记录 ${current.id} · 版本 ${current.expected_version || '只读'}` : `目标：${sourceId}`;
    for (const [node, key] of [[textarea,'content'],[subject,'subject'],[origin,'origin'],[eventDate,'event_at']]) {
      if (force || document.activeElement !== node) node.value = current[key] || '';
      node.readOnly = locked;
    }
    eventDate.hidden = current.type !== 'episode';
    entryType.parentElement.hidden = topicSelect.parentElement.hidden = !adding;
    const concreteKinds = kindsFor(source()).filter(value => value !== 'all');
    entryType.replaceChildren(el('option', {value:'', text:'请选择具体类型'}), ...concreteKinds.map(value => el('option', {value:recordTypes[value], text:labels[value]})));
    entryType.value = current.type || ''; entryType.disabled = locked;
    topicSelect.replaceChildren(el('option', {value:'', text:'请选择所属主题'}), ...state.topics.map(topic => el('option', {value:topic.topic_id, text:topicPath(state.topics, topic.topic_id).join(' / ')})));
    topicSelect.value = current.topic_id || ''; topicSelect.disabled = locked || state.loading || Boolean(state.error);
    topicRow.hidden = adding;
    const topicId = latest ? latest.topic_id : current.topic_id;
    assignedTopic.textContent = '所属主题：' + (topicPath(state.topics, topicId).join(' / ') || '未归类');
    assign.hidden = current.action === 'view'; assign.disabled = busy() || !canManageTopics() || state.loading || Boolean(state.error) || state.revision === null;
    conflict.hidden = !(current.conflict || changed); adopt.disabled = busy() || !latest?.version; saveAsNew.disabled = busy() || !can('write');
    save.textContent = adding && current.imported ? '导入文档' : '保存';
    save.disabled = Boolean(locked || !current.type || !current.content?.trim() || changed || current.conflict ||
      adding && (!current.topic_id || !state.topics.some(topic => topic.topic_id === current.topic_id)));
    remove.hidden = current.action !== 'replace'; remove.disabled = busy() || Boolean(current.uncertain || current.sync_failed || current.saved_unconfirmed || changed || current.conflict) || !can('delete');
    reconciled.hidden = !(current.uncertain || current.saved_unconfirmed || current.sync_failed); reconciled.disabled = busy();
    status.textContent = saving ? '保存处理中…' : importing ? '读取文档…' : current.saved_unconfirmed ?
      (current.failure || '正文已保存，索引未确认完成。') + ' 草稿与请求标识已保留，请刷新核对。' : current.sync_failed ?
      '正文已经变化，但目录同步未完成。草稿已保留，请刷新核对。新增记录请从未归类找到后更换主题，避免重复新增。' + (current.failure || '') : current.uncertain ?
      '操作结果未确认，草稿与请求标识已保留。请先刷新核对，再继续编辑或原样重试。' : current.failure ? current.failure + ' 草稿已保留。' :
      current.action === 'view' ? '搜索为有限召回，结果只读；请返回目录选择条目进行编辑。' :
      adding && !current.topic_id ? '请选择所属主题；没有合适主题时请先新建主题。正文草稿将保留。' : '正文修改和删除需要确认；更换主题只保存目录，保留正文草稿。';
    if (!busy() && !current.dirty && current.index_status) status.textContent +=
      current.index_status === 'ready' ? ' 图谱索引已确认。' : current.index_status === 'pending' ?
      ' 事实已保存，图谱索引待完成；无需重复提交。' : ' 事实已保存，图谱索引状态未知；无需重复提交。';
  }
  function renderSearch() {
    resultsRoot.replaceChildren(); const results = state.results;
    if (!results) return;
    if (results.error) {resultsRoot.append(errorBox(results.error, () => reads.search(results.text))); return;}
    const incomplete = Object.values(results.source_status || {}).some(status => status !== 'ok');
    if (incomplete) resultsRoot.append(el('p', {class:'muted', text:`检索结果不完整：${(results.issues || []).map(issue => issue.code).join(' · ') || '部分路径未确认'}`}));
    for (const record of results.entries) {
      const item = button('', () => selectRecord(record), 'memory-entry'); item.disabled = busy();
      const path = (record.topic_path || []).map(item => typeof item === 'string' ? item : item.name).join(' / ');
      item.append(el('strong', {text:record.subject || record.origin || record.type}), el('small', {text:path || '未归类'}),
        el('p', {class:'memory-summary', text:(record.content || '').slice(0, 160)})); resultsRoot.append(item);
    }
    if (!results.loading && !results.entries.length) resultsRoot.append(el('p', {class:'empty', text:incomplete ? '本次检索尚未获得可用记录，请检查来源诊断。' : '本次搜索未命中记忆。'}));
  }
  function render(force = false) {
    if (disposed) return;
    inspection.busy(busy()); renderDirectory(); renderEditor(force);
    scope.textContent = [source()?.source_description, source()?.store === 'cognee' ? '主题操作保存到 FunAgent 目录。' : '',
      source() && !can('list') ? '此来源只支持搜索，无法枚举目录条目。' : ''].filter(Boolean).join(' · ');
    sourceSelector.disabled = busy() || !sources.length; selector.disabled = busy() || !source();
    batchUpload.disabled = !canManageTopics() || !can('write');
    batchUpload.title = batchUpload.disabled ? '需要目录读取和 list/write 能力及写权限' : '';
    upload.disabled = newEntry.disabled = busy() || !can('write');
    newTopic.disabled = busy() || !canManageTopics() || state.loading || Boolean(state.error) || state.revision === null;
    refresh.disabled = busy() || state.loading || !source(); search.disabled = searchButton.disabled = busy() || !can('search');
    clearSearch.hidden = !state.results;
    catalogError.replaceChildren(...(state.error ? [errorBox(state.error, () => reads.refresh())] : []));
    const loaded = new Set(Object.values(state.pages).flatMap(page => page.entries.map(record => record.id || record.record_id))).size;
    count.textContent = state.results ? state.results.loading ? '搜索中…' : `本次命中 ${state.results.entries.length} 条 · 有限召回 · 只读` :
      state.loading ? '读取目录中…' : state.error ? '目录读取失败，已保留草稿。' : `${state.topics.length} 个主题 · ${loaded} 条已加载`;
    treeRoot.hidden = Boolean(state.results); resultsRoot.hidden = !state.results;
    tree.render({sourceId, label:sourceId ? sourceId + ' · 当前记忆库' : '当前记忆库', topics:state.topics,
      pages:state.pages, selection, busy:busy(), canList:can('list'),
      canManageTopics:canManageTopics() && !state.loading && !state.error && state.revision !== null}); renderSearch();
  }

  const unsubscribeDraft = store.subscribe(value => value.drafts.memoryByTarget, () => render());
  const focus = () => {if (!busy() && !disposed) refreshPage();}; window.addEventListener('focus', focus);
  async function initialize() {
    try {
      const data = await api.memorySources(); if (disposed) return;
      sources = data.sources;
      sourceSelector.replaceChildren(...sources.map(item => el('option', {value:item.source_id, text:item.source_id + ' · ' + (drivers[item.store] || item.store)})));
      store.migrateMemoryDrafts(data.default_source_id && sources.some(item => item.source_id === data.default_source_id) ? data.default_source_id : null);
      const selected = sources.find(item => item.source_id === store.get().ui.memorySourceId) || sources.find(item => item.source_id === data.default_source_id) || sources[0];
      if (selected) await changeTarget(selected.source_id, store.get().ui.memoryKind || 'all');
      else {sourceSelector.replaceChildren(el('option', {value:'', text:'无可用记忆库'})); render();}
    } catch (error) {
      if (!disposed) {sourceSelector.replaceChildren(el('option', {value:'', text:'来源读取失败'})); catalogError.replaceChildren(errorBox(error.message, initialize));}
    }
  }
  render(); initialize();
  return () => {
    disposed = true; importDialog.dispose(); scopeGeneration++; reads.dispose(); tree.dispose(); topicDialog.dispose(); inspection.close();
    unsubscribeDraft(); window.removeEventListener('focus', focus); root.classList.remove('memory-page');
  };
}
