"""Executable entry for configuration validation and CLI/Web startup."""

import argparse
import sys

from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import DomainError


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'auth':
        parser = argparse.ArgumentParser(prog='funagent auth')
        parser.add_argument('command', choices=('login', 'status', 'models', 'logout'))
        parser.add_argument('--state-dir', default='.funagent/siwc')
        parser.add_argument('--no-open', action='store_true')
        parser.add_argument('--login-timeout', type=int, default=600)
        args = parser.parse_args(argv[1:])
        if not 1 <= args.login_timeout <= 1800:
            parser.error('--login-timeout must be between 1 and 1800 seconds')
        try:
            from ..api.siwc_controller import Run
            return Run(args)
        except KeyboardInterrupt:
            return 130
    from . import application
    parser = argparse.ArgumentParser(prog="funagent")
    parser.add_argument("--conf-dir", required=True)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    result = 0
    try:
        if args.check_config:
            application.Initialize(args.conf_dir)
            print("Configuration valid.")
        else:
            application.Start(args.conf_dir)
            mode = ConfMgr.GetInstance().Serv().Options()["mode"]
            server = application.GetHttpServer()
            if server is not None:
                print(f"FunAgent Web: {server.url}", flush=True)
            if mode in {"cli", "both"}:
                from ..api.cli_controller import CLIController
                result = CLIController.Run(memory_settings=application.GetMemorySettings())
            else:
                server.Wait()
    except DomainError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        result = 1
    except KeyboardInterrupt:
        result = 130
    finally:
        try:
            application.Stop()
        except DomainError as error:
            print(f"{error.code}: {error}", file=sys.stderr)
            result = 1
    return result
