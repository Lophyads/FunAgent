"""Real Chromium + HTTP SSE + SDK stream, with gates for retry and commit."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Event, enumerate as threads


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', required=True)
    parser.add_argument('--playwright', required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'tests_refactor'))
    import httpx
    from pytest import MonkeyPatch
    from src.api.http_server import _Handler
    from src.app import application
    from test_http_sse import streaming_app
    from test_model_streaming import ByteStream, chunk, frame
    retry, finish = Event(), Event()
    sent = []

    class ControlledStream(ByteStream):
        def __init__(self, packets, number):
            super().__init__(packets)
            self.number = number

        def close(self):
            super().close()
            (retry if self.number == 1 else finish).set()

    def handle(request):
        sent.append(json.loads(request.content))
        number = len(sent)
        def packets():
            yield frame(chunk('discarded' if number == 1 else 'accepted <b>literal</b>'))
            if number == 1:
                assert retry.wait(30)
                raise httpx.ReadError('controlled interruption')
            assert finish.wait(30)
            yield frame(chunk(finish='stop'))
            yield b'data: [DONE]\n\n'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=ControlledStream(packets(), number))

    original = _Handler.do_GET
    def controls(handler):
        gates = {'/test/retry': retry, '/test/finish': finish}
        if handler.path in gates:
            gates[handler.path].set()
            handler._send(200, {'released': True})
        else:
            original(handler)

    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='funagent-stream-browser-') as temporary, MonkeyPatch.context() as patch:
        conf = Path(temporary) / 'conf'
        shutil.copytree(root / 'examples/refactor-cli', conf)
        patch.setattr(_Handler, 'do_GET', controls)
        patch.setattr('src.model.runtime.Wait', lambda *args: True)
        try:
            with streaming_app(conf, patch, handle, timeout=60) as (_, server, _):
                result = subprocess.run([args.node, str(root / 'scripts/refactor_streaming_smoke.cjs'),
                    args.playwright, args.browser, server.url, str(args.artifact_dir.resolve())], timeout=100)
                assert result.returncode == 0
                assert len(sent) == 2 and all(r['stream'] for r in sent)
        finally:
            retry.set()
            finish.set()
            application.Stop()
    assert not any(t.name.startswith('funagent-') for t in threads())
    print('Real SDK streaming, Chromium, SQLite and all worker/subscriber resources verified.')


if __name__ == '__main__':
    main()
