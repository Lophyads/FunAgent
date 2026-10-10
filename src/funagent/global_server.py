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
from .tool.workspace import ReadWorkspaceHandler
from .tool.human import AskUserHandler
from .interaction import InteractionMgr
from .memory import MemoryMgr, SQLiteMemoryStore, CallbackMemoryStore
from .memory.http_query import HTTPMemoryQuery
from .memory.retrieval import MemorySearchHandler, SEARCH_DEFINITION


PROVIDERS = {"demo": DemoProvider, "openai": OpenAIProvider}
ADAPTERS = {"demo": DemoAdapter, "openai_chat": OpenAIChatAdapter}
HANDLERS = {"echo": EchoHandler, "read_workspace": ReadWorkspaceHandler, "ask_user": AskUserHandler,
            "memory_search": MemorySearchHandler}
MEMORY_QUERIES = {"http_json": HTTPMemoryQuery}


def _sqlite_memory(source_id, settings, conf_dir):
    path = Path(settings["path"])
    return SQLiteMemoryStore(path if path.is_absolute() else conf_dir / path, source_id, settings["kind"])


def _callback_memory(source_id, settings, conf_dir):
    factory = MEMORY_QUERIES.get(settings["callback_id"])
    if factory is None:
        raise DomainError("DRIVER_UNAVAILABLE", "Selected memory query callback is unavailable")
    return CallbackMemoryStore(source_id, factory(settings), settings["kind"])


MEMORY_STORES = {"sqlite": _sqlite_memory, "callback": _callback_memory}


def Initialize(conf_dir):
    # The P0 configuration-only entry remains useful for validation.
    ConfMgr.Initialize(conf_dir)
    return ConfMgr.GetInstance()


def Start(conf_dir):
    conf = Initialize(conf_dir)
    providers, bound = {}, False
    store = None
    database_bound = False
    memories, memory_bound = {}, False
    try:
        options = conf.Serv().Options()
        if options["mode"] != "cli":
            raise DomainError("UNSUPPORTED", "Only CLI mode is currently supported")
        profiles = {}
        pending = [options["model_profile"]]
        recall = conf.Memory().Policy("recall")
        if recall.get("enabled", True) and recall.get("sources"):
            pending.append(recall["profile"])
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
        # The internal recall tool is fixed and read-only, even if configuration
        # grants additional main/review tools in later phases.
        if "memory_search" in definitions and (definitions["memory_search"] != SEARCH_DEFINITION or
                                               handlers["memory_search"] is not MemorySearchHandler):
            raise DomainError("CONFIG_INVALID", "The retrieval tool definition is fixed")
        definitions["memory_search"] = SEARCH_DEFINITION
        handlers["memory_search"] = MemorySearchHandler
        approval_names = tuple(name for name, settings in conf.Tool().Definitions().items()
                               if settings.get("requires_approval", False))
        ToolMgr.Initialize(definitions, handlers, approval_names=approval_names)
        InteractionMgr.Initialize(options.get("interaction_timeout", 120))
        ModelRuntime.Initialize(profiles, providers, ADAPTERS)
        bound = True
        database = Path(options["database"])
        if not database.is_absolute():
            database = Path(conf_dir).resolve() / database
        store = SQLiteStore(database)
        DbMgr.Initialize(store)
        database_bound = True
        for name, settings in conf.Memory().Sources().items():
            factory = MEMORY_STORES.get(settings["store"])
            if factory is None:
                raise DomainError("DRIVER_UNAVAILABLE", "Selected memory store is unavailable")
            memories[name] = factory(name, settings, Path(conf_dir).resolve())
        MemoryMgr.Initialize(memories)
        memory_bound = True
        TaskMgr.Initialize(options["capacity"])
        return TaskMgr.GetInstance()
    except Exception as error:
        if not memory_bound:
            for memory in memories.values():
                try:
                    memory.Close()
                except Exception:
                    pass
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
    for close in (InteractionMgr.Close, MemoryMgr.Close, DbMgr.Close, ModelRuntime.Close, ToolMgr.Close, ConfMgr.Close):
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
