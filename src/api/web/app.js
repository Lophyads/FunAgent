"use strict";
const $ = id => document.getElementById(id);
let sessionId = "", taskId = "", epoch = 0, pendingIds = "", polling = false, activeTask = false;
const tasksBySession = Object.create(null);
const terminal = status => !["queued", "running"].includes(status);
const states = {queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断"};
const notice = error => { $("notice").textContent = error ? error.message : ""; };
async function api(url, body) {
  const response = await fetch(url, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(`${value.error.code}: ${value.error.message}`);
  return value;
}
async function pages(url) {
  let values = [], cursor = null;
  do { const page = await api(url + (cursor ? "?cursor=" + encodeURIComponent(cursor) : "")); values.push(...page.items); cursor = page.next_cursor; } while (cursor);
  return values;
}
function remember() { localStorage.setItem("funagent.selection", JSON.stringify({sessionId, tasks: tasksBySession})); }
async function history(token) {
  if (!sessionId) return;
  const messages = await pages(`/sessions/${sessionId}/messages`);
  if (token !== epoch) return;
  $("history").replaceChildren();
  for (const message of messages) {
    const article = document.createElement("article"), label = document.createElement("strong"), text = document.createElement("pre");
    label.textContent = {user: "你", assistant: "助手", tool: "工具"}[message.role] || message.role;
    text.textContent = message.content;
    article.append(label, text);
    for (const call of message.calls) { const detail = document.createElement("pre"); detail.textContent = `${call.name}: ${JSON.stringify(call.args)}`; article.append(detail); }
    if (message.status) { const detail = document.createElement("small"); detail.textContent = `${message.status} · ${message.effect || "none"}`; article.append(detail); }
    $("history").append(article);
  }
  $("history").scrollTop = $("history").scrollHeight;
}
function showInteractions(items, token) {
  if (token !== epoch) return;
  const signature = items.map(r => r.interaction_id).join(",");
  if (signature === pendingIds) return; // Preserve an answer being typed across polls.
  pendingIds = signature; $("interactions").replaceChildren();
  for (const item of items) {
    const form = document.createElement("form"), prompt = document.createElement("p"); form.className = "interaction";
    prompt.textContent = item.prompt; form.append(prompt);
    const sendAnswer = async answer => {
      try {
        await api(`/interactions/${item.interaction_id}/answer`, {task_id: item.task_id, run_id: item.run_id, answer});
        if (token !== epoch) return;
        notice(null); pendingIds = ""; $("interactions").replaceChildren(); await poll(token);
      } catch (error) { notice(error); }
    };
    if (item.kind === "approval") {
      for (const [text, value] of [["批准", true], ["拒绝", false]]) { const button = document.createElement("button"); button.type = "button"; button.textContent = text; button.onclick = () => sendAnswer(value); form.append(button); }
    } else {
      const input = document.createElement("input"), button = document.createElement("button"); input.required = true; input.maxLength = 4000; input.setAttribute("aria-label", "人工答复"); button.textContent = "回答"; form.append(input, button);
      form.onsubmit = event => { event.preventDefault(); sendAnswer(input.value); };
    }
    $("interactions").append(form);
  }
}
async function poll(token = epoch) {
  if (!taskId || token !== epoch || polling) return;
  polling = true;
  try {
    const task = await api(`/tasks/${taskId}`);
    if (token !== epoch) return;
    if (task.session_id !== sessionId) throw new Error("任务不属于当前会话。");
    const active = !terminal(task.status); activeTask = active;
    $("task").textContent = `${states[task.status] || task.status} · ${task.task_id}` + (task.result && task.result.reason ? `\n${task.result.reason}` : "");
    $("cancel").hidden = !active; $("send").disabled = active;
    if (active) showInteractions((await api(`/tasks/${taskId}/interactions`)).items, token);
    else { showInteractions([], token); await history(token); }
  } catch (error) { if (token === epoch) notice(error); }
  finally { polling = false; }
}
async function select(id, resume = "") {
  epoch++; sessionId = id; taskId = resume || tasksBySession[id] || ""; activeTask = !!taskId; pendingIds = ""; $("interactions").replaceChildren(); $("history").replaceChildren(); $("task").textContent = ""; $("cancel").hidden = true;
  $("heading").textContent = $("sessions").selectedOptions[0]?.textContent || "开始一个会话"; $("send").disabled = !id || !!taskId; remember(); notice(null);
  try { await history(epoch); await poll(epoch); } catch (error) { notice(error); }
}
async function sessions(preferred = sessionId) {
  const items = await pages("/sessions"); $("sessions").replaceChildren();
  const empty = document.createElement("option"); empty.value = ""; empty.textContent = "请选择会话"; $("sessions").append(empty);
  for (const item of items) { const option = document.createElement("option"); option.value = item.session_id; option.textContent = item.title; $("sessions").append(option); }
  $("sessions").value = items.some(s => s.session_id === preferred) ? preferred : "";
}
$("new-session").onsubmit = async event => { event.preventDefault(); try { const session = await api("/sessions", {title: $("title").value}); await sessions(session.session_id); await select(session.session_id); } catch (error) { notice(error); } };
$("sessions").onchange = () => select($("sessions").value);
$("refresh").onclick = async () => { try { await sessions(); } catch (error) { notice(error); } };
$("submit").onsubmit = async event => {
  event.preventDefault(); if (!sessionId || $("send").disabled) return;
  const token = epoch, destination = sessionId; $("send").disabled = true;
  try {
    const task = await api(`/sessions/${destination}/tasks`, {text: $("message").value}); tasksBySession[destination] = task.task_id; remember();
    if (token !== epoch) return;
    taskId = task.task_id; activeTask = true; $("message").value = ""; notice(null); await history(token); await poll(token);
  } catch (error) { if (token === epoch) { $("send").disabled = false; notice(error); } }
};
$("cancel").onclick = async () => { try { await api(`/tasks/${taskId}/cancel`, {}); await poll(); } catch (error) { notice(error); } };
setInterval(() => { if (taskId && activeTask) poll(); }, 500);
(async () => {
  try {
    const saved = JSON.parse(localStorage.getItem("funagent.selection") || "{}");
    for (const [id, task] of Object.entries(saved.tasks || {})) { if (/^[A-Za-z0-9_-]{1,128}$/.test(id) && typeof task === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(task)) tasksBySession[id] = task; }
    await sessions(saved.sessionId); await select($("sessions").value);
  }
  catch (error) { notice(error); }
})();
