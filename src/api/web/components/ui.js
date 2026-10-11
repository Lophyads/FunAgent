export const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  node.append(...children.filter(c => c != null)); return node;
};
export const button = (text, action, cls = '') => el('button', {type:'button', text, onclick: action, class: cls});
export function notify(text, error = false) {
  const node = el('div', {class: `toast ${error ? 'error' : ''}`, text}); document.querySelector('#notifications').append(node);
  setTimeout(() => node.remove(), 6000);
}
export function showDetail(title, value) {
  const dialog = document.querySelector('#detail'), body = document.querySelector('#detail-body');
  document.querySelector('#detail-title').textContent = title; body.replaceChildren();
  if (value instanceof Node) body.append(value); else body.append(el('pre', {text: typeof value === 'string' ? value : JSON.stringify(value, null, 2)}));
  if (!dialog.open) dialog.showModal();
}
export function jsonView(value) {
  if (value == null || typeof value !== 'object') return el('span', {class: 'json-value', text: JSON.stringify(value)});
  const details = el('details', {class: 'json-tree'}, el('summary', {text: Array.isArray(value) ? `Array (${value.length})` : `Object (${Object.keys(value).length})`}));
  for (const [key, item] of Object.entries(value)) details.append(el('div', {}, el('span', {class: 'json-key', text: `${key}: `}), jsonView(item)));
  return details;
}
export const escapeHTML = text => String(text).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
export function markdown(text) {
  // Escape first; links accept only HTTP(S) or mailto. No model-authored HTML.
  const escaped = escapeHTML(text);
  return escaped.split(/(```[\s\S]*?```)/g).map(block => {
    if (block.startsWith('```')) return `<pre><code>${block.slice(3,-3).replace(/^\w*\n/, '')}</code></pre>`;
    return block.replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/^### (.+)$/gm, '<h4>$1</h4>').replace(/^## (.+)$/gm, '<h3>$1</h3>').replace(/^# (.+)$/gm, '<h2>$1</h2>')
      .replace(/\n/g, '<br>');
  }).join('');
}
export function pageHeader(root, title, description, ...actions) {
  root.replaceChildren(el('header', {class: 'page-header'}, el('div', {}, el('div', {class: 'eyebrow', text: 'FUNAGENT WORKSPACE'}),
    el('h1', {text: title}), el('p', {text: description})), el('div', {class: 'actions'}, ...actions)));
}
export function errorBox(text, retry) {return el('div', {class: 'empty error'}, el('p', {text}), retry && button('重试', retry));}
export const statusText = s => ({queued:'排队', running:'运行中', waiting:'等待答复', cancelling:'取消请求中', completed:'完成', failed:'失败', cancelled:'已取消', interrupted:'中断'}[s] || s);

export function confirmAction(title,text){return new Promise(resolve=>{const dialog=document.querySelector('#detail');let approved=false;
  showDetail(title,el('div',{},el('pre',{text}),el('div',{class:'actions'},button('放弃操作',()=>dialog.close()),button('确认操作',()=>{approved=true;dialog.close();},'primary'))));
  dialog.addEventListener('close',()=>resolve(approved),{once:true});
});}
