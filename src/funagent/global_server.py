"""Startup owns connections; shutdown drains the main worker before closing them."""
import argparse
from pathlib import Path
import sys

from .conf import ConfMgr
from .contracts import DomainError, ToolDefinition
from .model import ModelRuntime
from .model.demo import DemoAdapter, DemoProvider, EchoHandler
from .model.openai_chat import OpenAIChatAdapter, OpenAIProvider
from .persistence import DbMgr, SQLiteStore
from .task import TaskMgr
from .tool import ToolMgr


PROVIDERS = {"demo": DemoProvider, "openai": OpenAIProvider}
ADAPTERS = {"demo": DemoAdapter, "openai_chat": OpenAIChatAdapter}
HANDLERS = {"echo": EchoHandler}


def Initialize(conf_dir):
    # The P0 configuration-only entry remains useful for validation.
    ConfMgr.Initialize(conf_dir)
    return ConfMgr.GetInstance()


def Start(conf_dir):
    conf = Initialize(conf_dir)
    providers, bound = {}, False
    store = None
    database_bound = False
    try:
        options = conf.Serv().Options()
        if options["mode"] != "cli":
            raise DomainError("UNSUPPORTED", "Only CLI mode is available in P2")
        profiles = {}
        pending = [options["model_profile"]]
        while pending:
            name = pending.pop()
            if name not in profiles:
                profiles[name] = conf.Model().Profile(name)
                pending.extend(profiles[name].fallback_profiles)
        for profile in profiles.values():
            if profile.adapter_id not in ADAPTERS:
                raise DomainError("DRIVER_UNAVAILABLE", "Selected model adapter is unavailable")
            if profile.provider_id not in providers:
                settings = conf.Model().Providers()[profile.provider_id]
                factory = PROVIDERS.get(settings["kind"])
                if factory is None:
                    raise DomainError("DRIVER_UNAVAILABLE", "Selected model provider is unavailable")
                providers[profile.provider_id] = factory(settings)
        definitions = {}
        handlers = {}
        for name, settings in conf.Tool().Definitions().items():
            if settings["handler"] not in HANDLERS or settings.get("backend_id") is not None:
                raise DomainError("DRIVER_UNAVAILABLE", "Selected tool handler is unavailable")
            definitions[name] = ToolDefinition(name, settings.get("description", ""), settings["parameters"])
            handlers[name] = HANDLERS[settings["handler"]]
        ToolMgr.Initialize(definitions, handlers)
        ModelRuntime.Initialize(profiles, providers, ADAPTERS)
        bound = True
        database = Path(options["database"])
        if not database.is_absolute():
            database = Path(conf_dir).resolve() / database
        store = SQLiteStore(database)
        DbMgr.Initialize(store)
        database_bound = True
        TaskMgr.Initialize(options["capacity"])
        return TaskMgr.GetInstance()
    except Exception as error:
        if store is not None and not database_bound:
            store.Close()
        if not bound:
            for provider in providers.values():
                try:
                    provider.Close()
                except Exception:
                    pass
        try:
            Stop()
        except DomainError:
            pass
        if isinstance(error, DomainError):
            raise error
        raise DomainError("STARTUP_FAILED", "Application startup failed") from None


def Stop():
    # TaskMgr.Close waits for actual synchronous IO to return.
    error = None
    try:
        TaskMgr.Close()
    except DomainError as failure:
        # Close clears TaskMgr only after its worker has actually exited.
        error = failure
    for close in (DbMgr.Close, ModelRuntime.Close, ToolMgr.Close, ConfMgr.Close):
        try:
            close()
        except Exception:
            error = DomainError("CLOSE_FAILED", "Application resource close failed")
    if error is not None:
        raise error


def main(argv=None):
    parser = argparse.ArgumentParser(prog="funagent")
    parser.add_argument("--conf-dir", required=True)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    result = 0
    try:
        if args.check_config:
            Initialize(args.conf_dir)
            print("Configuration valid.")
        else:
            Start(args.conf_dir)
            from .api.cli import CLIController
            result = CLIController.Run()
    except DomainError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        result = 1
    except KeyboardInterrupt:
        result = 130
    finally:
        try:
            Stop()
        except DomainError as error:
            print(f"{error.code}: {error}", file=sys.stderr)
            result = 1
    return result
