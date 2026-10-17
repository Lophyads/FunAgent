import {el, button} from './ui.js';

/** File and task state belong to this dialog, never to the memory editor. */
export function mountMemoryImportDialog(root, {api, refresh}) {
  let sourceId, task = null, timer, generation = 0, disposed = false, pending = false, uncertain = false;
  const file = el('input', {type:'file', accept:'.json', 'aria-label':'导入 JSON 文件'});
  const info = el('p'), progress = el('p', {'aria-live':'polite'}), error = el('p', {role:'alert'});
  const submit = button('开始导入', start, 'primary');
  const download = button('下载结果 JSON', () => {
    if (!task) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(task,null,2)], {type:'application/json'}));
    const link = el('a', {href:url, download:`memory-import-${task.import_id}.json`});
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const reload = button('刷新目录', () => refresh(sourceId));
  const reconcile = button('核对最近任务', () => loadLatest(generation));
  const dialog = el('dialog', {'aria-label':'批量导入 JSON'}, el('h2', {text:'批量导入 JSON'}),
    file, info, progress, error, el('div', {class:'actions'}, submit, download, reload, reconcile,
      button('关闭', () => dialog.close())));
  root.append(dialog);
  function render() {
    const selected = file.files?.[0];
    info.textContent = `${sourceId || ''}${selected ? ` · ${selected.name} · ${selected.size} 字节` : ''}`;
    submit.disabled = pending || uncertain || task?.status === 'running' || !selected;
    file.disabled = pending || task?.status === 'running';
    download.disabled = !task || task.status === 'running';
    reload.disabled = !task || task.status === 'running';
    reconcile.hidden = !uncertain;
    reconcile.disabled = pending;
    const p = task?.progress;
    progress.textContent = task ? `${task.dataset_id} · ${task.status} · 主题 ${p.topics_done}/${p.topics_total} · 记忆 ${p.memories_done}/${p.memories_total}` : '';
    if (task?.status === 'completed') {
      const records = task.memories || [], pending = records.filter(r => r.index_status === 'pending').length,
        unknown = records.filter(r => r.index_status === 'unknown').length;
      progress.textContent += ` · 事实与主题已保存${pending || unknown ? ` · 索引待完成 ${pending} / 未知 ${unknown}` : ''}`;
    }
    if (task?.error) error.textContent = `${task.error.stage} ${task.error.key || ''} · ${task.error.code} · ${task.error.message}`;
  }
  function current(gen) {return !disposed && gen === generation && dialog.open;}
  function poll(gen) {
    clearTimeout(timer);
    if (task?.status !== 'running' || !current(gen)) return;
    timer = setTimeout(async () => {
      try {
        const value = await api.memoryImport(task.import_id);
        if (!current(gen)) return;
        task = value; error.textContent = ''; render();
      } catch (e) {
        if (!current(gen)) return;
        error.textContent = e.message;
        if (e.status >= 400 && e.status < 500 && ![408,429].includes(e.status)) {
          // A missing or inaccessible task cannot recover by polling the same ID.
          task = null; uncertain = true;
          error.textContent = '任务已不存在或无法读取，请核对最近任务后再操作。';
          render(); return;
        }
      }
      if (current(gen)) poll(gen);
    }, 1000);
  }
  async function start() {
    if (pending || uncertain || task?.status === 'running') return;
    const selected = file.files?.[0], own = sourceId, gen = generation, previousId = task?.import_id;
    if (!selected || !/\.json$/i.test(selected.name) || selected.size > 2*1024*1024) {
      error.textContent = '请选择不超过 2 MiB 的 .json 文件。'; return;
    }
    pending = true; error.textContent = ''; render();
    try {
      const value = await api.submitMemoryImport(own, selected);
      if (current(gen)) {task = value; render(); poll(gen);}
    } catch (e) {
      if (!current(gen)) return;
      error.textContent = `${e.path || ''} ${e.message}`;
      if (!e.status || e.status >= 500) {
        uncertain = true;
        task = null;
        try {
          // Hash only for uncertain submission recovery; protocol parsing stays server-side.
          const [latest, digest] = await Promise.all([
            api.latestMemoryImport(own),
            selected.arrayBuffer().then(bytes => crypto.subtle.digest('SHA-256', bytes))
          ]);
          if (!current(gen)) return;
          const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2,'0')).join('');
          if (latest?.source_id === own && latest.input_sha256 === sha256 && latest.import_id !== previousId) {
            task = latest; uncertain = false;
            error.textContent = '提交响应丢失，已核对文件并恢复任务。'; poll(gen);
          } else error.textContent = '提交结果未确认，最近任务无法关联此文件，请核对最近任务。';
        } catch {if (current(gen)) error.textContent = '提交结果未确认，请核对最近任务。';}
      }
    } finally {if (gen === generation && !disposed) {pending = false; render();}}
  }
  file.addEventListener('change', () => {error.textContent = ''; render();});
  dialog.addEventListener('close', () => {generation++; clearTimeout(timer); pending = false;});
  async function loadLatest(gen) {
    if (!current(gen) || pending) return;
    clearTimeout(timer); pending = true; render();
    try {
      const value = await api.latestMemoryImport(sourceId);
      if (current(gen)) {task = value; uncertain = false; error.textContent = ''; poll(gen);}
    } catch (e) {if (current(gen)) {uncertain = true; error.textContent = e.message;}}
    finally {if (current(gen)) {pending = false; render();}}
  }
  return {
    async open(source) {
      if (disposed || !source) return;
      clearTimeout(timer); const gen = ++generation;
      sourceId = source.source_id; task = null; file.value = ''; error.textContent = ''; pending = false;
      dialog.showModal(); render();
      await loadLatest(gen);
    },
    dispose() {disposed = true; generation++; clearTimeout(timer); dialog.close(); dialog.remove();}
  };
}
