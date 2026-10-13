"""Human-readable Markdown from one already selected saved snapshot."""
import json
import re
from .trace_projection import TraceNodes


def _inline(value):
    return str(value).replace('\n', ' ').replace('|', '\\|').replace('`', "'")


def _block(value):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    fence = '`' * max(3, max((len(run) for run in re.findall(r'`+', text)), default=0) + 1)
    return f'{fence}json\n{text}\n{fence}'


def Markdown(review):
    lines = [f"# 任务轨迹：{_inline(review['task_id'])}", '',
             f"业务状态：{_inline(review['task_status'])}；采集状态：{review['collection_state']}；IO 完整：{review['io_complete']}",
             f"文件位置：{_inline(review['path'])}", '']
    if review['diagnostics']:
        lines.extend(['诊断：' + ', '.join(review['diagnostics']), ''])
    trace = review['trace']
    if trace is None:
        lines.append('未采集：没有已保存的轨迹。' if review['collection_state'] == 'not_collected' else '轨迹不可解析；请根据诊断检查原文件。')
        return '\n'.join(lines) + '\n'
    summary = review['summary']
    lines.extend([f"已保存序号：{review['saved_seq']}；已记录业务状态：{_inline(review['recorded_task_status'])}",
                  f"已知 token：{summary['known_total_tokens']}；主模型 token：{summary['main_model_tokens']}；未知用量调用：{summary['unknown_usage_calls']}",
                  f"任务耗时（ms）：{summary['duration_ms']}", '', '## 操作树', '', '```text'])
    pending = [(trace['root'], 0)] if trace['root'] else []
    while pending:
        node, depth = pending.pop()
        lines.append('  ' * depth + f"{_inline(node['name'])} [{_inline(node['status'])}] {node.get('duration_ms')} ms · {node['span_id']}")
        pending.extend((child, depth + 1) for child in reversed(node['children']))
    if trace.get('orphan_nodes'):
        lines.append('父节点缺失的已保存操作：')
        for node in trace['orphan_nodes']:
            lines.append(f"  {_inline(node['name'])} [{_inline(node['status'])}] · 原父节点 {node['parent_span_id']}")
    lines.extend(['```', '', '## 等待汇总', '', _block(summary['waits']), '', '## IO 与执行事实', ''])
    for node in TraceNodes(trace):
        lines.extend([f"### {_inline(node['name'])} · {node['span_id']}", '',
                      _block({key: value for key, value in node.items() if key != 'children'}), ''])
        for key, label in (('input_ref', '输入'), ('output_ref', '输出')):
            if key not in node:
                continue
            payload = trace['payloads'].get(node[key])
            lines.extend([f"{label}（引用 {_inline(node[key])}）：", '',
                          _block(payload) if payload is not None else '正文引用缺失。', ''])
    lines.extend(['## 时间线（按保存序号）', '', _block(trace['events']), '', '## 保存与完整性', '', _block(trace['checkpoint'])])
    return '\n'.join(lines) + '\n'
