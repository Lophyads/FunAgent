"""Default-on memory composition; backend selection stays at the application edge."""
from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agentcore.background_review import BackgroundReviewScheduler
from agentcore.interfaces import NoopReviewScheduler
from agentcore.models import AgentConfig
from agentcore.prompt_builder import StaticSystemPromptBuilder
from tools.runtime import get_tool_services
from .prompt import MemoryPromptBuilder
from .embedding import EmbeddingConfig, http_url
from .review import ReviewProgress
from .store import MemoryError, MemoryStore
from .tool import memory_entry


@dataclass(frozen=True, slots=True)
class SqliteConfig:
    path: Path = Path("memories/memory.db")


@dataclass(frozen=True, slots=True)
class SupabaseConfig:
    url: str
    credential_ref: str
    embedding: EmbeddingConfig
    namespace: str = "personal-agent"
    timeout_seconds: float = 20
    min_similarity: float = 0.3

    def __post_init__(self):
        object.__setattr__(self, "url", http_url(self.url))
        if not isinstance(self.credential_ref, str) or not self.credential_ref.startswith("env:") or len(self.credential_ref) <= 4:
            raise MemoryError("supabase credential_ref must be an env reference")
        if not isinstance(self.namespace, str) or not self.namespace.strip() or len(self.namespace) > 200:
            raise MemoryError("supabase namespace must be nonblank text of at most 200 characters")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 600:
            raise MemoryError("supabase timeout_seconds must be from 0 to 600")
        if type(self.min_similarity) not in (int, float) or not -1 <= self.min_similarity <= 1:
            raise MemoryError("supabase min_similarity must be from -1 to 1")
        if not isinstance(self.embedding, EmbeddingConfig):
            raise MemoryError("supabase requires embedding configuration")


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    enabled: bool = True
    backend: str = "sqlite"
    review_every_turns: int = 10
    sqlite: SqliteConfig = field(default_factory=SqliteConfig)
    supabase: SupabaseConfig | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool or not isinstance(self.backend, str) or self.backend not in {"sqlite", "supabase"}:
            raise MemoryError("memory backend must be sqlite or supabase; unsupported backends never fall back")
        if self.backend == "supabase" and not isinstance(self.supabase, SupabaseConfig):
            raise MemoryError("supabase backend requires [memory.supabase]")
        if type(self.review_every_turns) is not int or self.review_every_turns < 0:
            raise MemoryError("review_every_turns must be a nonnegative integer")


def load_memory_config(path: Path | str, *, allow_legacy: bool = False) -> MemoryConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as file:
        raw = tomllib.load(file)
    if "file_memory" in raw and not allow_legacy:
        raise MemoryError("[file_memory] was removed: run 'agentcore memory import-legacy --directory <old-directory>' then replace it with [memory]")
    settings = raw.get("memory", {})
    if not isinstance(settings, dict) or set(settings) - {"enabled", "backend", "review_every_turns", "sqlite", "supabase"}:
        raise MemoryError("invalid [memory] configuration")
    sqlite = settings.get("sqlite", {}) if settings.get("backend", "sqlite") == "sqlite" or settings.get("review_every_turns", 10) else {}
    if not isinstance(sqlite, dict) or set(sqlite) - {"path"}:
        raise MemoryError("invalid [memory.sqlite] configuration")
    db_path = sqlite.get("path", "memories/memory.db")
    if not isinstance(db_path, str) or not db_path.strip():
        raise MemoryError("memory.sqlite.path must be nonblank text")
    db_path = Path(db_path).expanduser()
    if not db_path.is_absolute():
        db_path = config_path.parent / db_path
    supabase = None
    if settings.get("backend") == "supabase":
        try:
            values = dict(settings["supabase"])
            values["embedding"] = EmbeddingConfig(**values["embedding"])
            supabase = SupabaseConfig(**values)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, MemoryError):
                raise
            raise MemoryError("invalid [memory.supabase] or embedding configuration") from None
        from dotenv import load_dotenv
        load_dotenv(config_path.with_name(".env"), override=False)
    return MemoryConfig(**{k: v for k, v in settings.items() if k not in {"sqlite", "supabase"}},
                        sqlite=SqliteConfig(db_path.absolute()), supabase=supabase)


def create_store(settings: MemoryConfig) -> MemoryStore:
    if settings.backend == "sqlite":
        from .sqlite import SqliteMemoryStore
        return SqliteMemoryStore(settings.sqlite.path)
    if settings.backend == "supabase":
        from .supabase import SupabaseMemoryStore
        return SupabaseMemoryStore(settings.supabase)
    raise MemoryError(f"unsupported memory backend: {settings.backend}")


class MemoryFeature:
    def __init__(self, settings: MemoryConfig, agent_config: AgentConfig, *, store: MemoryStore | None = None, progress: ReviewProgress | None = None):
        self.settings = settings
        self.store = (store if store is not None else create_store(settings)) if settings.enabled else None
        self.prompt_builder = MemoryPromptBuilder(StaticSystemPromptBuilder(agent_config.system_prompt), self.store, agent_config.memory_max_chars) if self.store is not None else None
        from .retrieval import MemoryKnowledgeRetriever
        self.retriever = MemoryKnowledgeRetriever(self.store) if self.store is not None else None
        self.runner = self.queue = None
        self.review_scheduler = NoopReviewScheduler()
        self._protected = ()
        self._installed = False
        self._closed = False
        self._owned_progress = None
        self._local_paths = [self.store.path] if self.store is not None and hasattr(self.store, "path") else []
        if self.store is not None and settings.review_every_turns:
            from .review import MemoryReviewRunner, MemoryReviewScheduler
            # SQLite implements local checkpoints; alternative stores can inject a separate checkpoint object.
            try:
                if progress is None and settings.backend == "supabase":
                    from .sqlite import SqliteMemoryStore
                    target = settings.supabase
                    progress = self._owned_progress = SqliteMemoryStore(settings.sqlite.path,
                        review_scope=("supabase", target.url, target.namespace))
                progress = progress if progress is not None else self.store
                if hasattr(progress, "path"):
                    self._local_paths.append(progress.path)
                if not all(callable(getattr(progress, name, None)) for name in ("observe_turn", "review_state", "mark_reviewed")):
                    raise MemoryError("this memory backend requires a separate ReviewProgress adapter")
                self.runner = MemoryReviewRunner(agent_config, self.store, progress)
                self.queue = BackgroundReviewScheduler(self.runner, workers=1, capacity=32)
                self.review_scheduler = MemoryReviewScheduler(self.queue, settings.review_every_turns, progress)
            except BaseException:
                if self.runner is not None:
                    self.runner.close(5)
                self.store.close()
                if self._owned_progress is not None:
                    self._owned_progress.close()
                raise

    def install_tool(self, registry):
        if self._closed:
            raise MemoryError("memory feature is closed")
        if self.store is None or self._installed:
            return
        services = get_tool_services()
        paths = tuple(Path(str(path) + suffix) for path in dict.fromkeys(self._local_paths) for suffix in ("", "-wal", "-shm", "-journal"))
        for path in paths:
            services.protect_path(path)
        try:
            registry.replace_all((*registry.entries(), memory_entry(self.store)))
        except BaseException:
            for path in paths:
                services.unprotect_path(path)
            raise
        self._protected, self._installed = paths, True

    def close(self, timeout: float = 5) -> bool:
        if self._closed:
            return True
        deadline = time.monotonic() + max(0, timeout)
        if self.queue is not None and not self.queue.close(timeout=max(0, deadline - time.monotonic())):
            return False
        if self.runner is not None and not self.runner.close(max(0, deadline - time.monotonic())):
            return False
        if self.store is not None:
            self.store.close()
        if self._owned_progress is not None:
            self._owned_progress.close()
        for path in self._protected:
            get_tool_services().unprotect_path(path)
        self._protected = ()
        self._closed = True
        return True
