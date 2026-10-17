"""Executable entry for configuration validation and CLI/Web startup."""

import argparse
import sys

from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import DomainError
from . import application


def main(argv=None):
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
