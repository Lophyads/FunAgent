import {el, button} from './ui.js';
import {topicPath} from './memory-topic-model.js';

/** Shared create/edit/assign form. Captured scope and revision never change on conflict.
 * @param {HTMLElement} root
 * @param {{submit:Function,refresh:Function}} callbacks
 */
export function mountTopicDialog(root, callbacks) {
  let context = null, pending = false, disposed = false;
  const title = el('h2'), name = el('input', {required:true, maxlength:64, 'aria-label':'主题名称'});
  const description = el('textarea', {required:true, maxlength:240, rows:3, 'aria-label':'主题说明'});
  const parent = el('select', {'aria-label':'父主题'}), message = el('p', {role:'alert', class:'error'});
  const nameField = el('label', {}, '名称', name), descriptionField = el('label', {}, '说明', description);
  const parentLabel = el('span'), parentField = el('label', {}, parentLabel, parent);
  const save = button('保存', () => form.requestSubmit(), 'primary');
  const cancel = button('取消', () => dialog.close());
  const form = el('form', {class:'memory-topic-form'}, title, nameField, descriptionField, parentField, message,
    el('div', {class:'actions'}, cancel, save));
  const dialog = el('dialog', {'aria-label':'主题管理'}, form); root.append(dialog);
  const submitting = value => {pending = value; for (const node of [name,description,parent,save,cancel]) node.disabled = value;};
  dialog.addEventListener('cancel', event => {if (pending) event.preventDefault();});
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (pending || !context) return;
    const own = context, assigning = own.mode === 'assign';
    const value = assigning ? {topic_id:parent.value} : {name:name.value.trim(), description:description.value.trim(), parent_id:parent.value || null};
    if (assigning ? !value.topic_id : !value.name || !value.description) {message.textContent = '请填写必填字段。'; return;}
    submitting(true); message.textContent = '';
    try {await callbacks.submit(own, value); if (!disposed) dialog.close();}
    catch (error) {
      if (!disposed) {
        message.textContent = error.message + (error.code === 'CONFLICT' ? ' 输入已保留；目录已刷新，请核对后重新打开表单。' : !error.status ? ' 结果未确认，请刷新核对后再操作。' : '');
        if (['CONFLICT','NOT_FOUND'].includes(error.code) || !error.status || error.status >= 500) await callbacks.refresh(own);
      }
    } finally {if (!disposed) submitting(false);}
  });
  return {
    open({mode, source, topics, revision, topic = null, recordId = null, topicId = null}) {
      if (disposed || pending) return;
      context = {mode, source, revision, topic, recordId};
      const assigning = mode === 'assign';
      title.textContent = assigning ? '更换所属主题' : mode === 'create' ? '新建主题' : '编辑主题';
      nameField.hidden = descriptionField.hidden = assigning;
      name.required = description.required = !assigning;
      parentLabel.textContent = assigning ? '所属主题（必选）' : '父主题';
      parent.setAttribute('aria-label', assigning ? '所属主题' : '父主题');
      parent.required = assigning;
      name.value = topic?.name || ''; description.value = topic?.description || '';
      // Exclude this topic and its descendants when moving a directory.
      const excluded = new Set(topic ? [topic.topic_id] : []);
      let changed = true;
      while (changed) {changed = false; for (const item of topics) if (excluded.has(item.parent_id) && !excluded.has(item.topic_id)) {excluded.add(item.topic_id); changed = true;}}
      parent.replaceChildren(el('option', {value:'', text:assigning ? '请选择主题' : '当前记忆库（顶层）'}),
        ...topics.filter(item => !excluded.has(item.topic_id)).map(item => el('option', {value:item.topic_id, text:topicPath(topics, item.topic_id).join(' / ')})));
      parent.value = assigning ? topicId || '' : topic?.parent_id || topicId || '';
      message.textContent = ''; submitting(false); dialog.showModal();
      (assigning ? parent : name).focus();
    },
    dispose() {disposed = true; dialog.close(); dialog.remove();}
  };
}
