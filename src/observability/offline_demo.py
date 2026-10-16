"""Generate actual offline application traces in a new, isolated output directory."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time


def Generate(directory):
    from ..app import application
    from ..frame.contracts import SessionCreate, TurnRequest
    from ..frame.session_mgr import SessionMgr
    from ..frame.event_mgr import EventMgr
    from ..frame.interaction_mgr import InteractionMgr
    from ..memory.contracts import MemoryDraft
    from ..memory.memory_operations_mgr import MemoryOperationsMgr
    from ..persistence.db_mgr import DbMgr
    from .observation_mgr import ObservationMgr
    from .trace_reader import TraceReader

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {}
    with TemporaryDirectory(prefix='funagent-observation-demo-') as temporary:
        conf = Path(temporary)
        configurations = {
            'model': 'adapters = ["demo"]\n[providers.offline]\nkind = "demo"\n[profiles.main]\nprovider_id = "offline"\nadapter_id = "demo"\nmodel = "offline"\ntimeout = 3\n',
            'tool': '[purposes]\nmain = ["echo", "clarify"]\n[session_access]\ndefault = ["memory.facts.read", "memory.facts.write"]\n',
            'memory': '[sources.facts]\nkind = "episodic"\nstore = "sqlite"\npath = "facts.sqlite3"\ncapabilities = ["search", "list", "write"]\n[policies.facts]\nenabled = true\nsources = ["facts"]\n[policies.recall]\nprofile = "main"\n[policies.review]\nenabled = false\n',
            'log': '[options]\nlevel = "ERROR"\nconsole = false\nfile = ' + json.dumps(str(directory / 'logs' / 'app.log')) + '\n',
            'serv': '[options]\nmode = "cli"\ncapacity = 4\nhost = "127.0.0.1"\nport = 18999\nworkspace = "."\ndatabase = "application.sqlite3"\nmodel_profile = "main"\ntask_timeout = 5\n',
        }
        for name, content in configurations.items():
            (conf / (name + '.conf')).write_text(content, encoding='utf8')

        def run(queue, text):
            session = SessionMgr.Create(SessionCreate(workspace=str(conf)))
            task_id = queue.Submit(TurnRequest(session.session_id, text))
            events = EventMgr.GetInstance().Subscribe(task_id)
            deadline = time.monotonic() + 10
            try:
                for event in events:
                    if time.monotonic() > deadline:
                        raise TimeoutError('Offline demo did not finish')
                    if event.kind == 'interaction_pending':
                        request, = InteractionMgr.GetInstance().Pending(task_id)
                        InteractionMgr.GetInstance().Resolve(request.interaction_id,
                            {'answers': [{'question_id': 'answer', 'value': '蓝色'}]},
                            task_id=task_id, run_id=request.run_id)
                    if queue.Get(task_id).status not in {'queued', 'running'}:
                        break
            finally:
                events.close()
            return task_id

        try:
            queue = application.Start(conf)
            operations = MemoryOperationsMgr.GetInstance()
            topic = operations.CreateTopic('facts', None, '企鹅', '离线示例中的企鹅事实')['topic']
            operations.Write(MemoryDraft('facts', '企鹅喜欢吃鱼', 'offline-demo', ()), topic_id=topic['topic_id'])
            for name, text in [('normal', '你好'), ('tool', 'tool: 回声'),
                               ('recall', 'memory: 企鹅'), ('human_wait', 'ask: 喜欢什么颜色？')]:
                manifest[name] = run(queue, text)
            application.Stop()
            # An actual model iteration budget failure, with no injected provider IO.
            (conf / 'serv.conf').write_text(configurations['serv'] + 'max_iterations = 1\n', encoding='utf8')
            queue = application.Start(conf)
            manifest['failed'] = run(queue, 'tool: 无法进入下一轮模型')
            # Reproduce the durable admission boundary before queue publication.
            request = TurnRequest(SessionMgr.Create(SessionCreate(workspace=str(conf))).session_id, '已登记、尚未发布到工作队列')
            task = DbMgr.GetInstance().RegisterTurn(request)
            ObservationMgr.GetInstance().StartTask(task.task_id, session_id=request.session_id, input={'text': request.text})
            manifest['interrupted'] = task.task_id
            application.Stop()
            application.Start(conf)  # Main DB repairs the row; the writer recovers its trace.
        finally:
            application.Stop()
    reader = TraceReader(directory / 'trace_json')
    result = {}
    for name, task_id in manifest.items():
        review = reader.Read(task_id)
        result[name] = dict(task_id=task_id, path=review['path'], task_status=review['task_status'],
                            collection_state=review['collection_state'])
        (directory / (name + '.md')).write_text(reader.Markdown(review), encoding='utf8')
    (directory / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='A new directory; existing paths are never overwritten.')
    args = parser.parse_args(argv)
    print(json.dumps(Generate(args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
