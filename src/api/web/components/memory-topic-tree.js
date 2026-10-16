import {el, button} from './ui.js';
import {ROOT, UNCLASSIFIED, topicChildren} from './memory-topic-model.js';

/** @typedef {{sourceId:string,label?:string,topics:import('./memory-topic-model.js').Topic[],
 * pages:Record<string,import('./memory-topic-model.js').NodePage>,selection:import('./memory-topic-model.js').Selection,
 * busy:boolean,canList:boolean,canManageTopics?:boolean}} TreeState */

/**
 * Pure tree view: selection and loaded data belong to the caller.
 * @param {HTMLElement} root
 * @param {{onSelectTopic:(selection:Object)=>void,onSelectRecord:(record:Object)=>void,
 * onExpand:(key:string)=>void,onLoadMore:(key:string)=>void,onDeleteTopic?:(topicId:string)=>void}} callbacks
 * @returns {{render:(state:TreeState)=>void,dispose:()=>void,expandedNodes:()=>string[],expandNode:(key:string)=>void}}
 */
export function mountTopicTree(root, callbacks) {
  let expanded = new Set([ROOT]), focusKey = ROOT, sourceId = null, rows = [], disposed = false;
  root.classList.add('memory-topic-tree');
  root.setAttribute('role', 'tree'); root.setAttribute('aria-label', '记忆主题目录');

  function setFocusKey(key) {
    focusKey = key;
    for (const row of rows) {
      row.node.tabIndex = row.key === key ? 0 : -1;
      if (row.deleteButton) row.deleteButton.tabIndex = row.key === key ? 0 : -1;
    }
  }
  function focus(key, preventScroll = false) {
    setFocusKey(key);
    rows.find(row => row.key === key)?.node.focus({preventScroll});
  }
  const keydown = event => {
    const index = rows.findIndex(row => row.node === event.target);
    if (index < 0) return;
    const row = rows[index];
    if (event.key === 'Delete' && row.remove) {event.preventDefault(); row.remove(); return;}
    if (!['ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Home','End','Enter',' '].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'ArrowDown') focus(rows[Math.min(index + 1, rows.length - 1)].key);
    else if (event.key === 'ArrowUp') focus(rows[Math.max(0, index - 1)].key);
    else if (event.key === 'Home') focus(rows[0].key);
    else if (event.key === 'End') focus(rows.at(-1).key);
    else if (event.key === 'ArrowRight') {
      if (row.toggle && !expanded.has(row.key)) row.toggle();
      else if (rows[index + 1]?.parent === row.key) focus(rows[index + 1].key);
    } else if (event.key === 'ArrowLeft') {
      if (row.toggle && expanded.has(row.key)) row.toggle();
      else if (row.parent) focus(row.parent);
    } else row.activate();
  };
  root.addEventListener('keydown', keydown);
  let lastValue;
  /** @param {TreeState} value */
  function render(value) {
    if (disposed) return;
    lastValue = value;
    const {topics = [], pages = {}, selection, busy = false} = value;
    if (sourceId !== value.sourceId) {sourceId = value.sourceId; expanded = new Set([ROOT]); focusKey = ROOT;}
    const children = topicChildren(topics), active = root.contains(document.activeElement);
    const oldRows = rows; rows = [];
    const fragment = document.createDocumentFragment();
    const selected = key => key === ROOT ? selection?.kind === 'root'
      : key === UNCLASSIFIED ? selection?.kind === 'unclassified'
      : selection?.kind === 'topic' && selection.topicId === key;

    function addRow(container, {key, parent, label, level, isSelected, activate, toggle, remove}) {
      const node = el('div', {role:'treeitem', 'aria-level':level, 'aria-selected':String(Boolean(isSelected)),
        tabindex:-1, class:'memory-tree-item' + (isSelected ? ' selected' : ''), 'data-tree-key':key});
      const line = el('div', {class:'memory-tree-row'});
      if (toggle) node.setAttribute('aria-expanded', String(expanded.has(key)));
      const labelButton = button(label, event => {event.stopPropagation(); focus(key); activate();}, 'memory-tree-label');
      labelButton.tabIndex = -1; labelButton.disabled = busy;
      if (toggle) {
        const expand = button(expanded.has(key) ? '▾' : '▸', event => {event.stopPropagation(); focus(key); toggle();}, 'memory-tree-toggle');
        expand.setAttribute('aria-label', (expanded.has(key) ? '折叠 ' : '展开 ') + label);
        expand.setAttribute('aria-expanded', String(expanded.has(key))); expand.tabIndex = -1; expand.disabled = busy;
        line.append(expand);
      } else line.append(el('span', {class:'memory-tree-spacer', 'aria-hidden':'true'}));
      line.append(labelButton);
      let deleteButton;
      if (remove) {
        deleteButton = button('删除', event => {event.stopPropagation(); focus(key); remove();}, 'memory-tree-delete');
        deleteButton.setAttribute('aria-label', '删除主题「' + label + '」');
        deleteButton.disabled = busy; deleteButton.tabIndex = key === focusKey ? 0 : -1;
        node.setAttribute('aria-keyshortcuts', 'Delete'); line.append(deleteButton);
      }
      node.append(line); container.append(node);
      node.addEventListener('focus', () => setFocusKey(key));
      rows.push({key, parent, node, deleteButton, remove:remove && (() => {if (!busy) remove();}),
        activate:() => {if (!busy) activate();}, toggle:toggle && (() => {if (!busy) toggle();})});
      return node;
    }
    function directory(container, key, label, level, parent, ancestry = new Set()) {
      if (ancestry.has(key)) return;
      const toggle = () => {
        if (expanded.has(key)) expanded.delete(key);
        else {expanded.add(key); callbacks.onExpand(key);}
        render(lastValue);
      };
      const choice = key === ROOT ? {kind:'root'} : key === UNCLASSIFIED ? {kind:'unclassified'} : {kind:'topic', topicId:key};
      const remove = choice.kind === 'topic' && value.canManageTopics && callbacks.onDeleteTopic
        ? () => callbacks.onDeleteTopic(key) : undefined;
      const node = addRow(container, {key, parent, label, level, isSelected:selected(key), toggle, remove, activate:() => callbacks.onSelectTopic(choice)});
      if (!expanded.has(key)) return;
      const group = el('div', {role:'group', class:'memory-tree-group'}); node.append(group);
      const next = new Set(ancestry).add(key);
      for (const topic of children.get(key) || []) directory(group, topic.topic_id, topic.name, level + 1, key, next);
      if (key === ROOT) {directory(group, UNCLASSIFIED, '未归类', level + 1, ROOT, next); return;}
      const page = pages[key];
      for (const record of page?.entries || []) {
        const id = record.id || record.record_id;
        addRow(group, {key:JSON.stringify(['record', sourceId, id]), parent:key,
          label:record.subject || record.origin || record.type, level:level + 1,
          isSelected:selection?.kind === 'record' && selection.recordId === id, activate:() => callbacks.onSelectRecord(record)});
      }
      if (page?.error || page?.next_cursor) addRow(group, {
        key:JSON.stringify(['more', key]), parent:key, label:page.error ? '读取失败：' + page.error + ' · 重试' : '加载更多', level:level + 1,
        activate:() => page.error ? callbacks.onExpand(key) : callbacks.onLoadMore(key)
      });
      if (!value.canList) group.append(el('p', {class:'muted memory-tree-status', text:'此来源只支持搜索，无法枚举目录条目。'}));
      else if (!page || page.loading) group.append(el('p', {class:'muted memory-tree-status', role:'status', text:'读取中…'}));
      else if (!page.error && !page.next_cursor && !page.entries.length) group.append(el('p', {class:'muted memory-tree-status', text:'当前类型下无条目。'}));
    }
    directory(fragment, ROOT, value.label || '当前记忆库', 1, null);
    if (!rows.some(row => row.key === focusKey)) {
      let previous = oldRows.find(row => row.key === focusKey);
      while (previous && !rows.some(row => row.key === previous.key)) previous = oldRows.find(row => row.key === previous.parent);
      focusKey = previous?.key || ROOT;
    }
    root.replaceChildren(fragment);
    setFocusKey(focusKey);
    if (active) focus(focusKey, true);
  }
  return {render, expandedNodes:() => [...expanded].filter(key => key !== ROOT),
    expandNode(key) {
      if (disposed || !lastValue) return;
      expanded.add(ROOT); expanded.add(key); render(lastValue); callbacks.onExpand(key);
    },
    dispose() {disposed = true; root.removeEventListener('keydown', keydown); root.replaceChildren();}};
}
