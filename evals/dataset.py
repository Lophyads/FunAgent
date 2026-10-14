"""Small, explicit dataset/config validation, separate from backend inputs."""
import hashlib
import json
import math
import re
import tomllib
from pathlib import Path

from src.memory.common import timestamp


class EvalError(ValueError):
    pass


def Encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def Digest(value):
    return hashlib.sha256(Encode(value).encode("utf8")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvalError("Duplicate JSON field")
        result[key] = value
    return result


def ReadJSON(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf8"), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(EvalError("Non-finite JSON value")))
    except (OSError, ValueError) as error:
        raise EvalError("Cannot read valid JSON: " + str(path)) from error


def ReadTOML(path):
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf8"))
    except (OSError, ValueError) as error:
        raise EvalError("Cannot read valid TOML: " + str(path)) from error


def _require(value, message):
    if not value:
        raise EvalError(message)


def _text(value, maximum=65536):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is not None


def _date(value):
    if value is not None:
        try:
            timestamp(value)
        except Exception as error:
            raise EvalError("Dates require ISO timestamps with a timezone") from error


def Validate(data):
    _require(isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1,
             "Dataset schema_version must be 1")
    _require(isinstance(data.get("cases"), list) and data["cases"], "Dataset needs nonempty cases")
    cases = set()
    for case in data["cases"]:
        _require(isinstance(case, dict) and _id(case.get("id")) and case["id"] not in cases, "Invalid/duplicate case id")
        cases.add(case["id"])
        _require(isinstance(case.get("tags", []), list) and all(_id(t) for t in case.get("tags", [])), "Invalid tags")
        _require(isinstance(case.get("items"), list) and case["items"], "Case needs nonempty items")
        items = set()
        for item in case["items"]:
            _require(isinstance(item, dict) and _id(item.get("id")) and item["id"] not in items, "Invalid/duplicate item id")
            _require(_text(item.get("text")), "Invalid item text")
            _date(item.get("event_at"))
            items.add(item["id"])
        _require(isinstance(case.get("questions"), list) and case["questions"], "Case needs nonempty questions")
        questions = set()
        for question in case["questions"]:
            _require(isinstance(question, dict) and _id(question.get("id")) and question["id"] not in questions,
                     "Invalid/duplicate question id")
            questions.add(question["id"])
            _require(_text(question.get("text"), 8000), "Invalid question text")
            _date(question.get("question_date"))
            gold = question.get("gold")
            _require(isinstance(gold, dict) and type(gold.get("abstain")) is bool and _text(gold.get("answer")), "Invalid gold")
            groups = gold.get("evidence_groups")
            _require(isinstance(groups, list) and bool(groups) != gold["abstain"], "Evidence groups must match answerability")
            for group in groups:
                _require(isinstance(group, list) and group and all(isinstance(i, str) and i in items for i in group),
                         "Invalid evidence reference")
            _require(isinstance(gold.get("forbidden_claims", []), list) and
                     all(_text(t) for t in gold.get("forbidden_claims", [])), "Invalid forbidden claims")
    return data


def LoadDataset(path):
    return Validate(ReadJSON(path))


def _settings(path, model=False):
    settings = ReadTOML(path)
    allowed = {"model", "base_url", "credential_env", "timeout"} if model else {
        "endpoint", "credential_env", "api_key_env", "timeout", "similarity_threshold", "embedding"}
    _require(set(settings) <= allowed, "Unknown settings field (credentials must use credential_env)")
    if model:
        _require(_text(settings.get("model"), 256), "Model settings need a model")
        if "base_url" in settings:
            from src.memory.transport import ValidateConnection
            try:
                ValidateConnection(settings["base_url"], settings.get("timeout", 30))
            except Exception as error:
                raise EvalError("Invalid model base_url") from error
    else:
        _require(_text(settings.get("endpoint")) and isinstance(settings.get("embedding"), dict),
                 "Supabase settings need endpoint and embedding")
        from src.memory.transport import ValidateConnection
        from src.memory.embedding import ValidateProfile
        from src.memory.contracts import EmbeddingProfile
        embedding = settings["embedding"]
        _require(set(embedding) <= {"kind", "endpoint", "credential_env", "model", "dimensions", "timeout",
                                   "query_prefix", "document_prefix", "request_dimensions"}, "Unknown embedding field")
        _require(embedding.get("kind", "http") == "http", "Only http embedding is supported")
        if "credential_env" in embedding:
            _require(isinstance(embedding["credential_env"], str) and
                     re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", embedding["credential_env"]), "Invalid embedding credential env")
        if "request_dimensions" in embedding:
            _require(type(embedding["request_dimensions"]) is bool, "Invalid request_dimensions")
        try:
            ValidateConnection(settings["endpoint"], settings.get("timeout", 30))
            ValidateConnection(embedding["endpoint"], embedding.get("timeout", 30))
            ValidateProfile(EmbeddingProfile(embedding["model"], embedding["dimensions"],
                            embedding.get("query_prefix", ""), embedding.get("document_prefix", "")))
        except Exception as error:
            raise EvalError("Invalid Supabase/embedding settings") from error
        threshold = settings.get("similarity_threshold", 0)
        _require(type(threshold) in (int, float) and math.isfinite(threshold) and -1 <= threshold <= 1, "Invalid threshold")
    _require(type(settings.get("timeout", 30)) in (int, float) and math.isfinite(settings.get("timeout", 30))
             and settings.get("timeout", 30) > 0, "Invalid settings timeout")
    for key in ("credential_env", "api_key_env"):
        if key in settings:
            _require(isinstance(settings[key], str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", settings[key]), "Invalid credential env name")
    return settings


def LoadModelSettings(path):
    return _settings(Path(path), model=True)


def _cognee_settings(path):
    settings = ReadTOML(path)
    _require(set(settings) <= {"endpoint", "credential_env", "auth_header", "timeout", "search_type"},
             "Unknown Cognee setting; eval creates its own dataset scope")
    settings.setdefault("timeout", 30)
    from src.memory.cognee import ValidateSettings
    try:
        ValidateSettings(settings | {"datasets": ["eval_validation"]})
    except Exception as error:
        raise EvalError("Invalid Cognee settings") from error
    return settings


def LoadConfig(path):
    path = Path(path).resolve()
    config = ReadTOML(path)
    _require(set(config) <= {"dataset", "mode", "top_k", "max_context_chars", "timeout", "index_timeout",
                            "repeats", "variants", "answer_model", "judge_model"}, "Unknown experiment option")
    _require(_text(config.get("dataset")), "Config needs dataset")
    for key, default in (("mode", "retrieval"), ("top_k", 5), ("max_context_chars", 4000),
                         ("timeout", 30.0), ("index_timeout", 120.0), ("repeats", 1)):
        config.setdefault(key, default)
    _require(config["mode"] in {"retrieval", "qa"}, "Mode must be retrieval or qa")
    for key, minimum, maximum in (("top_k", 1, 100), ("max_context_chars", 2, 1048576), ("repeats", 1, 100)):
        _require(type(config[key]) is int and minimum <= config[key] <= maximum, "Invalid " + key)
    for key in ("timeout", "index_timeout"):
        _require(type(config[key]) in (int, float) and math.isfinite(config[key]) and config[key] > 0, "Invalid " + key)
    _require(isinstance(config.get("variants"), list) and config["variants"], "Config needs variants")
    names = set()
    for variant in config["variants"]:
        _require(isinstance(variant, dict) and set(variant) <= {"name", "backend", "settings_file"} and
                 _id(variant.get("name")) and variant["name"] not in names, "Invalid/duplicate variant")
        names.add(variant["name"])
        _require(variant.get("backend") in {"sqlite", "supabase", "cognee", "none", "oracle"}, "Unsupported backend")
        _require(config["mode"] == "qa" or variant["backend"] not in {"none", "oracle"}, "Baselines require qa mode")
        variant["settings"] = {}
        if variant["backend"] in {"supabase", "cognee"}:
            _require(_text(variant.get("settings_file")), "External backend needs settings_file")
            loader = _cognee_settings if variant["backend"] == "cognee" else _settings
            variant["settings"] = loader(path.parent / variant["settings_file"])
        else:
            _require("settings_file" not in variant, "This backend does not need settings_file")
    for key in ("answer_model", "judge_model"):
        if key in config:
            _require(_text(config[key]), "Invalid model settings reference")
            config[key] = LoadModelSettings(path.parent / config[key])
    _require(config["mode"] != "qa" or "answer_model" in config, "QA needs answer_model")
    _require(config["mode"] == "qa" or "judge_model" not in config, "Judge requires qa mode")
    dataset = LoadDataset(path.parent / config["dataset"])
    return config, dataset
