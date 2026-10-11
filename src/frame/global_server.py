"""Startup owns connections; shutdown drains both workers before closing them."""
import argparse
from pathlib import Path
import sys
import logging

from .conf import ConfMgr
from .environment import Environment
from .logging import LogSession
from .lifecycle import Lifecycle
from .events import EventMgr, TaskEvents
from ..api.http import HttpServer
from ..api import DiagnosticsMgr
from ..api.diagnostics import DiagnosticService
from ..api.logs import LogReader
from .contracts import DomainError
from ..model import ModelRuntime
from ..model.factory import Create as CreateModel
from ..persistence import DbMgr
from ..persistence.factory import Create as CreatePersistence
from ..task import TaskMgr
from ..task.queue import TaskQueue
from ..task.process import TaskProcess
from ..tool import ToolMgr
from ..tool.factory import Create as CreateTools
from .interaction import InteractionMgr
from ..memory import MemoryMgr, MemoryOperationsMgr
from ..memory.operations import MemoryOperations
from ..memory.factory import Create as CreateMemory, RequiredBackends
from ..memory.review import ReviewProcess
from .review import ReviewScheduler


# The entry owns one HTTP listener; controllers share the business managers.
_http_server = None
_lifecycle = None
logger = logging.getLogger("funagent.startup")


def Initialize(conf_dir):
    # The P0 configuration-only entry remains useful for validation.
    ConfMgr.Initialize(conf_dir)
    return ConfMgr.GetInstance()


def Start(conf_dir):
    global _http_server, _lifecycle
    try:
        ConfMgr.GetInstance()
    except DomainError as error:
        if error.code != "NOT_READY":
            raise
    else:
        raise DomainError("ALREADY_INITIALIZED", "Application already initialized")
    if _lifecycle is not None:
        raise DomainError("ALREADY_INITIALIZED", "Application still owns resources")
    conf_dir = Path(conf_dir).resolve()
    owner = _lifecycle = Lifecycle()
    try:
        owner.Own(Environment(conf_dir))
        owner.Own(ConfMgr, ConfMgr.Close)
        conf = Initialize(conf_dir)
        logs = owner.Own(LogSession(conf, conf_dir))
        options = conf.Serv().Options()
        review = conf.Memory().Policy("review")
        store = owner.Own(CreatePersistence(options, conf_dir))
        owner.Bind(DbMgr, store)
        owner.Bind(EventMgr, TaskEvents())
        backends = {}
        required = RequiredBackends(conf)
        tools = (CreateTools(conf, owner.Own, required_backends=required,
                 bind_backend=lambda key, value: backends.__setitem__(key, value)) if required else CreateTools(conf, owner.Own))
        owner.Bind(ToolMgr, tools)
        owner.Bind(ModelRuntime, CreateModel(conf, owner.Own, tool_dispatch=ToolMgr.GetInstance(), journal=store,
                                            events=EventMgr.GetInstance()))
        owner.Own(InteractionMgr, InteractionMgr.Close)
        InteractionMgr.Initialize(options.get("interaction_timeout", 120))
        memory = (CreateMemory(conf, conf_dir, owner.Own, backends=backends) if required else CreateMemory(conf, conf_dir, owner.Own))
        owner.Bind(MemoryMgr, memory)
        owner.Bind(MemoryOperationsMgr, MemoryOperations(memory, conf))
        if review.get("enabled", False) and review.get("sources"):
            owner.Own(ReviewScheduler, ReviewScheduler.Close)
            ReviewScheduler.Initialize(review.get("capacity", 16), executor=ReviewProcess, start_worker=False)
            worker = ReviewScheduler.GetInstance()
            owner.Forget(ReviewScheduler)
            owner.TrackBinding(ReviewScheduler, worker, worker=True)
            worker.Start()
        queue = TaskQueue(options["capacity"], executor=TaskProcess,
                          timeout=options.get("task_timeout", 60), start_worker=False)
        owner.Bind(TaskMgr, queue, worker=True)
        queue.Start()
        owner.Bind(DiagnosticsMgr, DiagnosticService(conf, tools, ModelRuntime.GetInstance(), queue,
                                                     logs=LogReader(logs.Files(), logs.Redact), redact=logs.Redact))
        if options["mode"] in {"web", "both"}:
            _http_server = owner.HTTP(HttpServer(options["host"], options["port"]))
            _http_server.Start()
        logger.info("Application started")
        return TaskMgr.GetInstance()
    except Exception as error:
        logger.error("Application startup failed: %s", error.code if isinstance(error, DomainError) else "STARTUP_FAILED")
        try:
            Stop()
        except DomainError:
            pass
        if isinstance(error, DomainError):
            raise error
        raise DomainError("STARTUP_FAILED", "Application startup failed") from None


def Stop():
    global _http_server, _lifecycle
    if _lifecycle is None:
        # Public test/embedding assembly uses the same drain path. Its bound
        # managers retain their existing ownership of stores and providers.
        _lifecycle = Lifecycle()
        for manager in (ConfMgr, ToolMgr, ModelRuntime, DbMgr, MemoryMgr, MemoryOperationsMgr, DiagnosticsMgr, InteractionMgr, EventMgr, ReviewScheduler, TaskMgr):
            try:
                instance = manager.GetInstance()
            except DomainError as error:
                if error.code != "NOT_READY":
                    raise
            else:
                _lifecycle.TrackBinding(manager, instance, worker=manager in (TaskMgr, ReviewScheduler))
        if _http_server is not None:
            _lifecycle.HTTP(_http_server)
    try:
        _lifecycle.Stop()
    finally:
        if _lifecycle.Empty():
            _lifecycle = _http_server = None


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
            mode = ConfMgr.GetInstance().Serv().Options()["mode"]
            if _http_server is not None:
                print(f"FunAgent Web: {_http_server.url}", flush=True)
            if mode in {"cli", "both"}:
                from ..api.cli import CLIController
                result = CLIController.Run()
            else:
                _http_server.Wait()
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
