"""Configuration loading and validation for the Modular RAG MCP Server."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from src.core.path_resolver import PROJECT_ROOT, PathResolver

# ---------------------------------------------------------------------------
# Repo root & path resolution
# ---------------------------------------------------------------------------
# Anchored to this file's location: <repo>/src/core/settings.py → parents[2]
REPO_ROOT: Path = PROJECT_ROOT

# Default absolute path to settings.yaml
DEFAULT_SETTINGS_PATH: Path = REPO_ROOT / "config" / "settings.yaml"

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------
# Provider SDKs read their credentials straight from the environment (e.g.
# DeepSeekLLM looks up DEEPSEEK_API_KEY), so load <repo>/.env before any
# provider gets instantiated.  Variables already present in the environment
# win: load_dotenv does not override them by default.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is optional
    pass
else:
    load_dotenv(REPO_ROOT / ".env")


def resolve_path(relative: Union[str, Path]) -> Path:
    """Resolve a repo-relative path to an absolute path.

    If *relative* is already absolute it is returned as-is.  Otherwise
    it is resolved against :data:`REPO_ROOT`.

    >>> resolve_path("config/settings.yaml")  # doctest: +SKIP
    PosixPath('/home/user/Modular-RAG-MCP-Server/config/settings.yaml')
    """
    return PathResolver(REPO_ROOT).resolve(relative)


class SettingsError(ValueError):
    """Raised when settings validation fails."""


def _require_mapping(data: Dict[str, Any], key: str, path: str) -> Dict[str, Any]:
    value = data.get(key)
    if value is None:
        raise SettingsError(f"Missing required field: {path}.{key}")
    if not isinstance(value, dict):
        raise SettingsError(f"Expected mapping for field: {path}.{key}")
    return value


def _require_value(data: Dict[str, Any], key: str, path: str) -> Any:
    if key not in data or data.get(key) is None:
        raise SettingsError(f"Missing required field: {path}.{key}")
    return data[key]


def _require_str(data: Dict[str, Any], key: str, path: str) -> str:
    value = _require_value(data, key, path)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"Expected non-empty string for field: {path}.{key}")
    return value


def _require_int(data: Dict[str, Any], key: str, path: str) -> int:
    value = _require_value(data, key, path)
    if not isinstance(value, int):
        raise SettingsError(f"Expected integer for field: {path}.{key}")
    return value


def _require_number(data: Dict[str, Any], key: str, path: str) -> float:
    value = _require_value(data, key, path)
    if not isinstance(value, (int, float)):
        raise SettingsError(f"Expected number for field: {path}.{key}")
    return float(value)


def _require_bool(data: Dict[str, Any], key: str, path: str) -> bool:
    value = _require_value(data, key, path)
    if not isinstance(value, bool):
        raise SettingsError(f"Expected boolean for field: {path}.{key}")
    return value


def _require_list(data: Dict[str, Any], key: str, path: str) -> List[Any]:
    value = _require_value(data, key, path)
    if not isinstance(value, list):
        raise SettingsError(f"Expected list for field: {path}.{key}")
    return value


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    temperature: float
    max_tokens: int
    # Azure/OpenAI-specific optional fields
    api_key: Optional[str] = None
    api_version: Optional[str] = None
    azure_endpoint: Optional[str] = None
    deployment_name: Optional[str] = None
    # Ollama / OpenAI-compatible endpoint fields
    base_url: Optional[str] = None
    # Name of the environment variable holding the API key.  Lets a provider
    # read a credential that is not named after itself -- e.g. the OpenAI
    # provider talking to a third-party endpoint whose key lives in
    # DEEPSEEK_API_KEY.  Defaults to the provider's own variable when unset.
    api_key_env: Optional[str] = None


@dataclass(frozen=True)
class EmbeddingSettings:
    provider: str
    model: str
    dimensions: int
    # Azure-specific optional fields
    api_key: Optional[str] = None
    api_version: Optional[str] = None
    azure_endpoint: Optional[str] = None
    deployment_name: Optional[str] = None
    # Ollama-specific optional fields
    base_url: Optional[str] = None


@dataclass(frozen=True)
class VectorStoreSettings:
    provider: str
    persist_directory: str
    collection_name: str


@dataclass(frozen=True)
class RetrievalSettings:
    dense_top_k: int
    sparse_top_k: int
    fusion_top_k: int
    rrf_k: int

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in (
            self.dense_top_k, self.sparse_top_k, self.fusion_top_k, self.rrf_k
        )):
            raise SettingsError("Retrieval limits and rrf_k must be positive integers")


@dataclass(frozen=True)
class RerankLLMSettings:
    provider: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout: float = 60.0
    thinking: str = "disabled"
    response_format: str = "json_object"
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key: Optional[str] = None
    azure_endpoint: Optional[str] = None
    api_version: Optional[str] = None
    deployment_name: Optional[str] = None

    def __post_init__(self):
        if self.provider not in {"deepseek", "openai", "azure", "ollama"} or not self.model.strip():
            raise SettingsError("Invalid rerank.llm provider/model")
        if self.thinking not in {"enabled", "disabled"} or self.response_format != "json_object":
            raise SettingsError("Invalid rerank.llm thinking/response_format")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise SettingsError("Positive rerank.llm.max_tokens required")
        if isinstance(self.timeout,bool) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise SettingsError("Positive finite rerank.llm.timeout required")
        if isinstance(self.temperature,bool) or not math.isfinite(self.temperature) or not 0<=self.temperature<=2:
            raise SettingsError("rerank.llm.temperature must be in 0..2")


@dataclass(frozen=True)
class RerankSettings:
    enabled: bool
    provider: str
    model: str
    top_k: int
    llm: Optional[RerankLLMSettings] = None
    max_candidates: int = 20
    max_input_chars: int = 30000
    min_relevance_score: float = 2.0
    strict: bool = False
    cache_dir: str = "data/cache/rerank"

    def __post_init__(self):
        if self.strict and self.provider not in {"none", "llm", "cross_encoder"}:
            raise SettingsError("Unknown strict rerank backend")
        if type(self.max_candidates) is not int or not 1<=self.max_candidates<=20:
            raise SettingsError("rerank.max_candidates must be in 1..20")
        if type(self.max_input_chars) is not int or self.max_input_chars<1000:
            raise SettingsError("rerank.max_input_chars must be >=1000")
        if isinstance(self.min_relevance_score,bool) or not math.isfinite(self.min_relevance_score) or not 0<=self.min_relevance_score<=3:
            raise SettingsError("rerank.min_relevance_score must be in 0..3")


@dataclass(frozen=True)
class EvaluationSettings:
    enabled: bool
    provider: str
    metrics: List[str]


@dataclass(frozen=True)
class ObservabilitySettings:
    log_level: str
    trace_enabled: bool
    trace_file: str
    structured_logging: bool


@dataclass(frozen=True)
class VisionLLMSettings:
    enabled: bool
    provider: str
    model: str
    max_image_size: int
    api_key: Optional[str] = None
    api_version: Optional[str] = None
    azure_endpoint: Optional[str] = None
    deployment_name: Optional[str] = None
    base_url: Optional[str] = None
    thinking: Optional[bool] = None
    max_tokens: Optional[int] = None
    timeout: Optional[float] = None

    def __post_init__(self) -> None:
        if self.thinking is not None and not isinstance(self.thinking, bool):
            raise SettingsError("Expected boolean for field: vision_llm.thinking")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens <= 0
        ):
            raise SettingsError("Expected positive integer for field: vision_llm.max_tokens")
        if self.timeout is not None and (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise SettingsError("Expected positive finite number for field: vision_llm.timeout")


@dataclass(frozen=True)
class IngestionSettings:
    splitter: str
    batch_size: int
    chunk_refiner: Optional[Dict[str, Any]] = None  # 动态配置
    metadata_enricher: Optional[Dict[str, Any]] = None  # 动态配置
    pipeline_version: str = "structured_v2"
    figures: Optional[Dict[str, Any]] = None
    captions: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None
    budget: Optional[Dict[str, Any]] = None
    hierarchy: Optional[Dict[str, Any]] = None
    domain_tokenizer: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.pipeline_version != "structured_v2":
            raise SettingsError("Unknown ingestion.pipeline_version")
        defaults = {
            'budget': {'tokenizer_path':'data/tokenizers/qwen3-embedding',
                'tokenizer_revision':'1d8ad4ca9b3dd8059ad90a75d4983776a23d44af',
                'min_tokens':100,'target_tokens':384,'max_tokens':500,'overlap_tokens':0},
            'context': {'mode':'deterministic','max_tokens':64},
            'captions': {'max_tokens':180}, 'figures': {'crop_logical_regions':True},
            'hierarchy': {'enabled':False}, 'domain_tokenizer': {'terms':[],'min_keyword_length':1},
        }
        for key, value in defaults.items():
            if getattr(self,key) is None:
                object.__setattr__(self,key,value)
        if self.pipeline_version == "structured_v2":
            budget = self.budget or {}
            if self.splitter != "structural" or not budget.get("tokenizer_path") or not budget.get("tokenizer_revision"):
                raise SettingsError("Structured profile requires structural splitter and pinned tokenizer")
            if not 1<=budget.get("target_tokens",384)<=budget.get("max_tokens",500)<=500:
                raise SettingsError("Invalid strict structured token budget")
            if not 0 <= budget.get('min_tokens', 0) <= budget.get('target_tokens', 384):
                raise SettingsError('Invalid structured minimum token target')
            if not 0<=(self.context or {}).get("max_tokens",64)<budget.get("max_tokens",500):
                raise SettingsError("Context exceeds structured token budget")


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    embedding: EmbeddingSettings
    vector_store: VectorStoreSettings
    retrieval: RetrievalSettings
    rerank: RerankSettings
    evaluation: EvaluationSettings
    observability: ObservabilitySettings
    ingestion: Optional[IngestionSettings] = None
    vision_llm: Optional[VisionLLMSettings] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Settings":
        if not isinstance(data, dict):
            raise SettingsError("Settings root must be a mapping")

        llm = _require_mapping(data, "llm", "settings")
        embedding = _require_mapping(data, "embedding", "settings")
        vector_store = _require_mapping(data, "vector_store", "settings")
        retrieval = _require_mapping(data, "retrieval", "settings")
        rerank = _require_mapping(data, "rerank", "settings")
        evaluation = _require_mapping(data, "evaluation", "settings")
        observability = _require_mapping(data, "observability", "settings")

        ingestion_settings = None
        if "ingestion" in data:
            ingestion = _require_mapping(data, "ingestion", "settings")
            ingestion_settings = IngestionSettings(
                splitter=_require_str(ingestion, "splitter", "ingestion"),
                batch_size=_require_int(ingestion, "batch_size", "ingestion"),
                chunk_refiner=ingestion.get("chunk_refiner"),  # 可选配置
                metadata_enricher=ingestion.get("metadata_enricher"),  # 可选配置
                pipeline_version=ingestion.get("pipeline_version","structured_v2"),
                figures=ingestion.get("figures"), captions=ingestion.get("captions"),
                context=ingestion.get("context"), budget=ingestion.get("budget"), hierarchy=ingestion.get("hierarchy"),
                domain_tokenizer=ingestion.get("domain_tokenizer"),
            )

        vision_llm_settings = None
        if "vision_llm" in data:
            vision_llm = _require_mapping(data, "vision_llm", "settings")
            vision_llm_settings = VisionLLMSettings(
                enabled=_require_bool(vision_llm, "enabled", "vision_llm"),
                provider=_require_str(vision_llm, "provider", "vision_llm"),
                model=_require_str(vision_llm, "model", "vision_llm"),
                max_image_size=_require_int(vision_llm, "max_image_size", "vision_llm"),
                api_key=vision_llm.get("api_key"),
                api_version=vision_llm.get("api_version"),
                azure_endpoint=vision_llm.get("azure_endpoint"),
                deployment_name=vision_llm.get("deployment_name"),
                base_url=vision_llm.get("base_url"),
                thinking=vision_llm.get("thinking"),
                max_tokens=vision_llm.get("max_tokens"),
                timeout=vision_llm.get("timeout"),
            )

        settings = cls(
            llm=LLMSettings(
                provider=_require_str(llm, "provider", "llm"),
                model=_require_str(llm, "model", "llm"),
                temperature=_require_number(llm, "temperature", "llm"),
                max_tokens=_require_int(llm, "max_tokens", "llm"),
                api_key=llm.get("api_key"),
                api_version=llm.get("api_version"),
                azure_endpoint=llm.get("azure_endpoint"),
                deployment_name=llm.get("deployment_name"),
                base_url=llm.get("base_url"),
                api_key_env=llm.get("api_key_env"),
            ),
            embedding=EmbeddingSettings(
                provider=_require_str(embedding, "provider", "embedding"),
                model=_require_str(embedding, "model", "embedding"),
                dimensions=_require_int(embedding, "dimensions", "embedding"),
                api_key=embedding.get("api_key"),
                api_version=embedding.get("api_version"),
                azure_endpoint=embedding.get("azure_endpoint"),
                deployment_name=embedding.get("deployment_name"),
                base_url=embedding.get("base_url"),
            ),
            vector_store=VectorStoreSettings(
                provider=_require_str(vector_store, "provider", "vector_store"),
                persist_directory=_require_str(vector_store, "persist_directory", "vector_store"),
                collection_name=_require_str(vector_store, "collection_name", "vector_store"),
            ),
            retrieval=RetrievalSettings(
                dense_top_k=_require_int(retrieval, "dense_top_k", "retrieval"),
                sparse_top_k=_require_int(retrieval, "sparse_top_k", "retrieval"),
                fusion_top_k=_require_int(retrieval, "fusion_top_k", "retrieval"),
                rrf_k=_require_int(retrieval, "rrf_k", "retrieval"),
            ),
            rerank=RerankSettings(
                enabled=_require_bool(rerank, "enabled", "rerank"),
                provider=_require_str(rerank, "provider", "rerank"),
                model=_require_str(rerank, "model", "rerank"),
                top_k=_require_int(rerank, "top_k", "rerank"),
                llm=RerankLLMSettings(**rerank["llm"]) if rerank.get("llm") else None,
                max_candidates=rerank.get("max_candidates",20),max_input_chars=rerank.get("max_input_chars",30000),
                min_relevance_score=rerank.get("min_relevance_score",2),strict=rerank.get("strict",False),
                cache_dir=rerank.get("cache_dir","data/cache/rerank"),
            ),
            evaluation=EvaluationSettings(
                enabled=_require_bool(evaluation, "enabled", "evaluation"),
                provider=_require_str(evaluation, "provider", "evaluation"),
                metrics=[str(item) for item in _require_list(evaluation, "metrics", "evaluation")],
            ),
            observability=ObservabilitySettings(
                log_level=_require_str(observability, "log_level", "observability"),
                trace_enabled=_require_bool(observability, "trace_enabled", "observability"),
                trace_file=_require_str(observability, "trace_file", "observability"),
                structured_logging=_require_bool(observability, "structured_logging", "observability"),
            ),
            ingestion=ingestion_settings,
            vision_llm=vision_llm_settings,
        )

        return settings


def validate_settings(settings: Settings) -> None:
    """Validate settings and raise SettingsError if invalid."""

    if not settings.llm.provider:
        raise SettingsError("Missing required field: llm.provider")
    if not settings.embedding.provider:
        raise SettingsError("Missing required field: embedding.provider")
    if not settings.vector_store.provider:
        raise SettingsError("Missing required field: vector_store.provider")
    if not settings.retrieval.rrf_k:
        raise SettingsError("Missing required field: retrieval.rrf_k")
    if not settings.rerank.provider:
        raise SettingsError("Missing required field: rerank.provider")
    if not settings.evaluation.provider:
        raise SettingsError("Missing required field: evaluation.provider")
    if not settings.observability.log_level:
        raise SettingsError("Missing required field: observability.log_level")


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings from a YAML file and validate required fields.

    Args:
        path: Path to settings YAML.  Defaults to
            ``<repo>/config/settings.yaml`` (absolute, CWD-independent).
    """
    settings_path = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
    if not settings_path.is_absolute():
        settings_path = resolve_path(settings_path)
    if not settings_path.exists():
        raise SettingsError(f"Settings file not found: {settings_path}")

    with settings_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    settings = Settings.from_dict(data or {})
    validate_settings(settings)
    return settings
