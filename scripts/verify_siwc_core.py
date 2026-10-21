"""Opt-in real SIWC smoke through the official CLI, factory, task and tool path.

Uses the selected ChatGPT subscription for two synthetic turns. All databases,
workspace files and diagnostics are isolated under --output-dir.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import re
import secrets
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.app.main import main
from src.frame.session_mgr import SessionMgr
from src.task.task_mgr import TaskMgr


def verify(state_dir, model, root, *, reasoning_effort=None):
    root.mkdir(parents=True, exist_ok=False)
    marker = 'SIWC-CORE-' + secrets.token_hex(12)
    (root / 'challenge.txt').write_text(marker + '\n', encoding='utf8')
    quoted = lambda value: json.dumps(str(value))
    docs = {
        'model': f'''adapters = ["openai_responses"]
[providers.gpt]
kind = "openai"
read_timeout = 120
connect_timeout = 15
[providers.gpt.auth]
kind = "siwc"
state_dir = {quoted(state_dir)}
[profiles.main]
provider_id = "gpt"
adapter_id = "openai_responses"
model = {quoted(model)}
timeout = 180
streaming = true
max_retries = 0
fallback_profiles = []
''',
        'tool': '[purposes]\nmain=["read_workspace"]\n[session_access]\ndefault=["workspace.read"]\n',
        'memory': '[policies.recall]\nprofile="main"\n[policies.review]\nenabled=false\n',
        'log': '[options]\nlevel="ERROR"\nconsole=false\n',
        'serv': f'''[options]
mode = "cli"
host = "127.0.0.1"
port = 18999
capacity = 4
workspace = {quoted(root)}
database = "application.sqlite3"
model_profile = "main"
max_iterations = 4
task_timeout = 240
'''}
    for name, content in docs.items():
        if name == 'model' and reasoning_effort is not None:
            content += f'reasoning_effort = {quoted(reasoning_effort)}\n'
        (root / f'{name}.conf').write_text(content, encoding='utf8')
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'model': model, 'passed': False}
    if reasoning_effort is not None:
        report['reasoning_effort'] = reasoning_effort
    state = {}
    try:
        for turn in (1, 2):
            report['stage'] = 'tool_turn' if turn == 1 else 'restart_turn'
            output, errors = StringIO(), StringIO()
            step = [0]
            def read(prompt):
                stage = step[0]
                step[0] += 1
                if turn == 1 and stage == 0:
                    return 'new SIWC core smoke'
                if (turn == 1 and stage == 1) or (turn == 2 and stage == 0):
                    if turn == 1:
                        state['session_id'] = output.getvalue().strip().splitlines()[-1]
                        if re.fullmatch(r'[a-f0-9]{32}', state['session_id']) is None:
                            raise RuntimeError('CLI did not create an isolated session')
                    else:
                        history = SessionMgr.Load(state['session_id']).messages
                        report['restart_replay_loaded'] = any(m.replay is not None for m in history)
                    question = ('Read challenge.txt using read_workspace. Return only the file contents.' if turn == 1 else
                        'Without calling any tools, reply with exactly the value you read from challenge.txt in our previous turn.')
                    return f'submit {state["session_id"]} {question}'
                if stage == (2 if turn == 1 else 1):
                    state['task_id'] = output.getvalue().strip().splitlines()[-1]
                    if re.fullmatch(r'[a-f0-9]{32}', state['task_id']) is None:
                        raise RuntimeError('CLI did not submit the smoke task')
                    return f'wait {state["task_id"]}'
                task = TaskMgr.GetInstance().Get(state['task_id'])
                if task.status != 'completed':
                    report['error_code'] = task.result.error_code or task.result.reason
                    raise RuntimeError('Formal core task failed')
                if task.result.final.strip() != marker:
                    raise RuntimeError('Formal core reply did not match the isolated file')
                if turn == 1:
                    if not task.result.tool_results or any(r.status != 'ok' for r in task.result.tool_results):
                        raise RuntimeError('Formal local tool did not succeed')
                    report['tool_passed'] = True
                    report['tool_calls'] = len(task.result.tool_results)
                else:
                    if task.result.tool_results:
                        raise RuntimeError('Restart check reread the file instead of using history')
                    report['restart_continuation_passed'] = True
                return 'quit'
            with redirect_stdout(output), redirect_stderr(errors), patch('builtins.input', read):
                result = main(['--conf-dir', str(root)])
            if result != 0:
                code = errors.getvalue().partition(':')[0].strip()
                if re.fullmatch(r'[A-Z_]{1,64}', code):
                    report['error_code'] = code
                raise RuntimeError('Formal CLI startup or shutdown failed')
        report['passed'] = all(report.get(k) for k in ('tool_passed', 'restart_replay_loaded', 'restart_continuation_passed'))
        report['stage'] = 'completed'
    except Exception as error:
        # Deliberate diagnostics only; never include credential or wire objects.
        report['failure_kind'] = type(error).__name__
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    return report


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--reasoning-effort', choices=('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'))
    parser.add_argument('--output-dir', default=str(Path('.funagent/siwc-core-smoke') / secrets.token_hex(6)))
    args = parser.parse_args()
    report = verify(Path(args.state_dir).resolve(), args.model, Path(args.output_dir).resolve(),
                    reasoning_effort=args.reasoning_effort)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(run())
