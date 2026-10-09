"""Configuration-only startup until application resources are introduced."""
import argparse
import sys

from .conf import ConfMgr
from .contracts import DomainError


def Initialize(conf_dir):
    ConfMgr.Initialize(conf_dir)
    return ConfMgr.GetInstance()


def Stop():
    ConfMgr.Close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="funagent")
    parser.add_argument("--conf-dir", required=True)
    parser.add_argument("--check-config", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        Initialize(args.conf_dir)
        print("Configuration valid.")
        return 0
    except DomainError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    finally:
        Stop()
