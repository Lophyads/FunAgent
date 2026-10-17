/** @typedef {{topic_id:string,parent_id:string|null,name:string,description:string}} Topic */
/** @typedef {{kind:'root'}|{kind:'topic',topicId:string}|{kind:'unclassified'}|{kind:'record',recordId:string,topicId:string|null}} Selection */
/** @typedef {{id?:string,record_id:string,content:string,type:string,subject?:string,origin?:string,version?:string,topic_id:string|null,topic_path:string[]}} MemoryRecord */
/** @typedef {{entries:MemoryRecord[],next_cursor:string|null,loading:boolean,error:string|null}} NodePage */
export const ROOT = '$root';
export const UNCLASSIFIED = '$unclassified';
export const recordKey = (sourceId, recordId) => JSON.stringify([sourceId, recordId]);
export const nodeKey = selection => selection?.kind === 'topic' ? selection.topicId
  : selection?.kind === 'record' ? selection.topicId || UNCLASSIFIED
  : selection?.kind === 'unclassified' ? UNCLASSIFIED : ROOT;

/** Build child indexes without changing server order. @param {Topic[]} topics */
export function topicChildren(topics) {
  const children = new Map([[ROOT, []]]);
  for (const topic of topics) {
    const parent = topic.parent_id || ROOT;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(topic);
  }
  return children;
}
/** @param {Topic[]} topics @param {string|null} topicId */
export function topicPath(topics, topicId) {
  const index = new Map(topics.map(topic => [topic.topic_id, topic]));
  const path = [], seen = new Set();
  while (topicId && index.has(topicId) && !seen.has(topicId)) {
    seen.add(topicId);
    const topic = index.get(topicId);
    path.unshift(topic.name);
    topicId = topic.parent_id;
  }
  return path;
}
export function mergeRecords(sourceId, previous, next) {
  return [...new Map([...previous, ...next].map(record =>
    [recordKey(sourceId, record.id || record.record_id), record])).values()];
}

/** Preserve the submitted identity and draft; only a refreshed record can supply a new edit version. */
export function memoryFailureDraft(draft, error, action, sourceId) {
  const known = error.effect === 'known' || !error.effect && error.code === 'TOPIC_SYNC_FAILED';
  const uncertain = error.effect === 'unknown' || !error.effect && !known && (!error.status || error.status >= 500);
  const ref = error.ref?.source_id === sourceId ? error.ref : undefined;
  return {...draft, conflict:error.status === 409 || error.status === 404, failure:error.message,
    effect:error.effect, issues:error.issues, verified_ref:ref, verified_version:ref ? error.version : undefined,
    uncertain, uncertain_action:uncertain ? action : undefined,
    sync_failed:known && error.code === 'TOPIC_SYNC_FAILED' ? action : undefined,
    saved_unconfirmed:known && error.code !== 'TOPIC_SYNC_FAILED' ? action : undefined};
}

/** Adopt a freshly read version after the user has inspected the saved body. */
export function memoryReconciledDraft(draft, record, sourceId) {
  const id = draft.id || draft.verified_ref?.record_id;
  if (record && (record.source_id !== sourceId || (id && (record.id || record.record_id) !== id))) return null;
  if ((draft.action === 'replace' || draft.saved_unconfirmed || draft.sync_failed || id) && !record?.version) return null;
  return {...draft, uncertain:false, saved_unconfirmed:undefined, sync_failed:undefined,
    failure:null, conflict:false, ...(record?.version ? {action:'replace', id:record.id || record.record_id,
      expected_version:record.version, topic_id:record.topic_id, topic_path:record.topic_path} : {})};
}
