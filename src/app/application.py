"""Application assembly, resource ownership and ordered shutdown."""

import logging
from pathlib import Path

from ..api.diagnostics_mgr import DiagnosticsMgr
from ..api.diagnostic_service import DiagnosticService
from ..api.http_server import HttpServer
from ..api.log_reader import LogReader
from ..eval.eval_mgr import EvalMgr
from ..eval.judge import RetrievalJudge
from ..eval.repository import EvalRepository
from ..eval.service import EvalService
from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import DomainError
from ..frame.environment import Environment
from ..frame.event_mgr import EventMgr, TaskEvents
from ..frame.interaction_mgr import InteractionMgr
from ..frame.lifecycle import Lifecycle
from ..frame.log_session import LogSession
from ..frame.review_scheduler import ReviewScheduler
from ..memory.memory_mgr import MemoryMgr
from ..memory.memory_operations_mgr import MemoryOperationsMgr
from ..memory.factory import Create as CreateMemory, RequiredBackends, CreateInspection
from ..memory.memory_operations import MemoryOperations
from ..memory.memory_import_mgr import MemoryImportMgr
from ..memory.memory_import_service import MemoryImportService
from ..memory.review_process import ReviewProcess
from ..model.model_runtime import ModelRuntime
from ..model.factory import Create as CreateModel
from ..persistence.db_mgr import DbMgr
from ..persistence.factory import Create as CreatePersistence
from ..observability.observation_mgr import ObservationMgr
from ..observability.factory import Create as CreateObservation
from ..task.task_mgr import TaskMgr
from ..task.task_process import TaskProcess
from ..task.task_queue import TaskQueue
from ..tool.tool_mgr import ToolMgr
from ..tool.factory import Create as CreateTools


# The application owns one HTTP listener; controllers share the business managers.
_http_server = None
_lifecycle = None
logger = logging.getLogger("funagent.startup")


def Initialize(conf_dir):
    """Load and validate configuration without constructing drivers."""
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
        observation = CreateObservation(conf, logs, conf_dir)
        owner.Own(observation, observation.CloseResource)
        ObservationMgr.Initialize(observation)
        owner.TrackBinding(ObservationMgr, observation)
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
        from ..memory.factory import CreateTopics
        topics = CreateTopics(conf, conf_dir)
        memory = CreateMemory(conf, conf_dir, owner.Own, backends=backends, topics=topics)
        owner.Bind(MemoryMgr, memory)
        inspection = CreateInspection(conf, owner.Own)
        owner.Bind(MemoryOperationsMgr, MemoryOperations(memory, conf, inspection=inspection, topics=topics))
        owner.Bind(MemoryImportMgr, MemoryImportService(MemoryOperationsMgr.GetInstance()), worker=True)
        eval_profile = conf.Model().Profile(options.get("eval_model_profile", options["model_profile"]))
        evaluations = EvalService(memory, MemoryOperationsMgr.GetInstance(), conf.Tool().Access("management"),
            owner.Own(EvalRepository(conf_dir / ".funagent" / "eval.sqlite3")),
            RetrievalJudge(ModelRuntime.GetInstance(), eval_profile),
            source_timeout=options.get("eval_source_timeout", 30),
            judge_timeout=options.get("eval_judge_timeout", eval_profile.timeout), start_worker=False)
        owner.Bind(EvalMgr, evaluations, worker=True)
        evaluations.Start()
        if review.get("enabled", False) and review.get("sources"):
            owner.Own(ReviewScheduler, ReviewScheduler.Close)
            ReviewScheduler.Initialize(review.get("capacity", 16), executor=ReviewProcess, start_worker=False)
            worker = ReviewScheduler.GetInstance()
            owner.Forget(ReviewScheduler)
            owner.TrackBinding(ReviewScheduler, worker, worker=True)
            worker.Start()
        queue = TaskQueue(options["capacity"], executor=TaskProcess,
                          timeout=options.get("task_timeout", 60), start_worker=False)
        # The queue constructor has repaired main database interrupted tasks.
        # Recovery IO belongs to the observation writer before new task admission.
        observation.Recover(store.GetTask)
        owner.Bind(TaskMgr, queue, worker=True)
        queue.Start()
        owner.Bind(DiagnosticsMgr, DiagnosticService(conf, tools, ModelRuntime.GetInstance(), queue,
                                                     logs=LogReader(logs.Files(), logs.Redact), redact=logs.Redact,
                                                     traces=observation.Reader(store.GetTask), observation=observation))
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


def GetHttpServer() -> HttpServer | None:
    """Return the listener owned by the running application, if enabled."""
    return _http_server


def Stop():
    global _http_server, _lifecycle
    if _lifecycle is None:
        # Public test/embedding assembly uses the same drain path. Its bound
        # managers retain their existing ownership of stores and providers.
        _lifecycle = Lifecycle()
        for manager in (ConfMgr, ObservationMgr, ToolMgr, ModelRuntime, DbMgr, MemoryMgr, MemoryOperationsMgr, DiagnosticsMgr, InteractionMgr, EventMgr, ReviewScheduler, TaskMgr, EvalMgr, MemoryImportMgr):
            try:
                instance = manager.GetInstance()
            except DomainError as error:
                if error.code != "NOT_READY":
                    raise
            else:
                if manager is ObservationMgr and instance is ObservationMgr._null:
                    continue
                _lifecycle.TrackBinding(manager, instance, worker=manager in (TaskMgr, ReviewScheduler, EvalMgr, MemoryImportMgr))
        if _http_server is not None:
            _lifecycle.HTTP(_http_server)
    try:
        _lifecycle.Stop()
    finally:
        if _lifecycle.Empty():
            _lifecycle = _http_server = None
