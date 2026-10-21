"""Explicit SIWC CLI presentation; no task, tool or memory application startup."""
import json
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..frame.contracts import DomainError, RunContext


def Login(session, *, no_open=False, seconds=600, output=None):
    from ..model.siwc import callback_result
    output = output or sys.stdout
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if len(self.path) > 16384 or not self.path.startswith('/auth/callback?') or received:
                self.send_error(400)
                return
            try:
                callback_result(self.path, attempt.state, attempt.client_id)
            except DomainError:
                self.send_error(400, 'Invalid OAuth callback')
                return
            received.append(self.path)
            body = b'Callback received. Return to FunAgent to check authorization.'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'")
            self.end_headers()
            self.wfile.write(body)
    context = RunContext(purpose='auth', deadline=time.monotonic() + seconds)
    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        server.timeout = 0.25
        attempt = session.BeginLogin(f'http://127.0.0.1:{server.server_port}/auth/callback', context)
        # BeginLogin deliberately excludes ID-token hints from this printable URL.
        print('Continue with ChatGPT and authorize ChatGPT plan usage.', file=output, flush=True)
        print(attempt.url, file=output, flush=True)
        if not no_open:
            webbrowser.open(attempt.url, new=2)
        while not received:
            context.CheckCancelled()
            server.handle_request()
        session.CompleteLogin(attempt, received[0], context)
    return session.Status()


def Run(args):
    session = None
    result_code = 1
    try:
        from ..model.siwc import SiwcSession
        session = SiwcSession(args.state_dir, exclusive=args.command != 'status')
        context = RunContext(purpose='auth', deadline=time.monotonic() + 30)
        if args.command == 'login':
            result = Login(session, no_open=args.no_open, seconds=args.login_timeout)
        elif args.command == 'status':
            result = session.Status()
        elif args.command == 'models':
            result = session.ListModels(context)
        else:
            result = session.Logout(context)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        result_code = 0 if args.command != 'logout' or result['remote_revocation_confirmed'] else 1
    except DomainError as error:
        print(f'{error.code}: {error}', file=sys.stderr)
    except ImportError:
        print('DRIVER_UNAVAILABLE: Install funagent[siwc] to use SIWC.', file=sys.stderr)
    except OSError:
        print('AUTH_STORAGE: Cannot access SIWC local state or callback listener.', file=sys.stderr)
    finally:
        if session is not None:
            try:
                session.Close()
            except DomainError as error:
                print(f'{error.code}: {error}', file=sys.stderr)
                result_code = 1
    return result_code
