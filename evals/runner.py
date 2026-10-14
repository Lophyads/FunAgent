"""Sequential execution, independently saved results, and offline scoring."""
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from src.frame.contracts import DomainError, RunContext
from . import backends
from .dataset import Digest, Encode, EvalError, ReadJSON, Validate
from .grading import Fit, Grade, GRADING_VERSION, Key
from .model import ANSWER_PROMPT, JUDGE_PROMPT, SingleModel
from .report import ReadRows, Report


def Save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")
    temporary.replace(path)


def _rows(path, values):
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(Encode(v) + "\n" for v in values), encoding="utf8")
    temporary.replace(path)


def Context(timeout):
    return RunContext(purpose="eval", deadline=time.monotonic() + timeout)


def ErrorCode(error):
    # No exception body or repr: SDK errors may contain credentials or wire text.
    return error.code if isinstance(error, DomainError) else "EVAL_ERROR"


def _code():
    root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for folder in ("evals", "src/memory", "src/model", "src/frame"):
        for path in sorted((root / folder).glob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    result = {"content_hash": digest.hexdigest(), "commit": None, "dirty": None}
    try:
        options = {"cwd": root, "capture_output": True, "text": True, "timeout": 5,
                   "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        commit = subprocess.run(["git", "rev-parse", "HEAD"], **options)
        status = subprocess.run(["git", "status", "--porcelain"], **options)
        result.update(commit=commit.stdout.strip() if commit.returncode == 0 else None,
                      dirty=bool(status.stdout.strip()) if status.returncode == 0 else None)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def _evidence(payload, items, limit):
    if not isinstance(payload, dict) or not isinstance(payload.get("evidence"), list) or len(payload["evidence"]) > limit:
        raise DomainError("EVAL_PROTOCOL", "Invalid backend search results")
    seen, ids = set(), {i["id"] for i in items}
    for row in payload["evidence"]:
        if (not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip()
                for k in ("record_id", "text", "citation")) or row["record_id"] in seen
                or len(row["text"]) > 65536 or not isinstance(row.get("item_ids"), list)
                or any(not isinstance(i, str) or i not in ids for i in row["item_ids"])
                or type(row.get("truncated", False)) is not bool or type(row.get("unmapped", False)) is not bool):
            raise DomainError("EVAL_PROTOCOL", "Invalid evidence lineage")
        seen.add(row["record_id"])
    if len(Encode(payload)) > 2 * 1024 * 1024:
        raise DomainError("EVAL_PROTOCOL", "Search payload too large")
    return payload["evidence"]


def Run(config, dataset, output, *, backend_factory=backends.Create, model_factory=SingleModel, progress=None):
    from .model import Preflight as ModelPreflight
    backends.Preflight(config)
    for key in ("answer_model", "judge_model"):
        if key in config:
            ModelPreflight(config[key])
    Validate(dataset)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:12]
    directory = Path(output).resolve() / run_id
    directory.mkdir(parents=True, exist_ok=False)
    stores = directory / "stores"
    stores.mkdir()
    manifest = {"schema_version": 1, "run_id": run_id, "status": "running", "config": config,
                "dataset": dataset, "dataset_hash": Digest(dataset), "config_hash": Digest(config),
                "code": _code(), "grading_version": GRADING_VERSION,
                "prompts": {"answer": ANSWER_PROMPT, "judge": JUDGE_PROMPT}, "spaces": [], "resource_errors": []}
    manifest["dependencies"] = {}
    for name in ("httpx", "openai"):
        try:
            manifest["dependencies"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            manifest["dependencies"][name] = None
    Save(directory / "manifest.json", manifest)
    answer_model, interrupted = None, False
    try:
        if config["mode"] == "qa":
            answer_model = model_factory(config["answer_model"])
        with (directory / "results.jsonl").open("a", encoding="utf8") as stream:
            for case in dataset["cases"]:
                for repeat in range(1, config["repeats"] + 1):
                    for variant in config["variants"]:
                        space = "eval_" + run_id.replace("T", "").replace("Z", "") + "_" + Digest([case["id"], repeat, variant["name"]])[:12]
                        resource = {"case_id": case["id"], "repeat": repeat, "variant": variant["name"], "backend": variant["backend"],
                                    "namespace": space, "timings_ms": {}, "receipts": []}
                        manifest["spaces"].append(resource)
                        Save(directory / "manifest.json", manifest)
                        backend, preparation_error, stage = None, None, "prepare"
                        try:
                            if variant["backend"] not in {"none", "oracle"}:
                                backend = backend_factory(variant["backend"], variant["settings"], space, stores)
                                stage = "ingest"
                                started = time.monotonic()
                                try:
                                    context = Context(config["timeout"])
                                    resource["receipts"] = backend.Ingest([
                                        {k: item[k] for k in ("id", "text", "event_at") if k in item} for item in case["items"]],
                                        context)
                                    context.CheckCancelled()
                                finally:
                                    resource["timings_ms"]["ingest"] = (time.monotonic() - started) * 1000
                                    resource["receipts"] = getattr(backend, "receipts", resource["receipts"])
                                    Save(directory / "manifest.json", manifest)
                                stage = "index"
                                started = time.monotonic()
                                try:
                                    context = Context(config["index_timeout"])
                                    backend.WaitReady(context)
                                    context.CheckCancelled()
                                finally:
                                    resource["timings_ms"]["index"] = (time.monotonic() - started) * 1000
                        except Exception as error:
                            preparation_error = (stage, ErrorCode(error))
                            resource.update(failed_stage=stage, error_code=ErrorCode(error))
                            if stage == "ingest":
                                resource["write_effect"] = "unknown"
                        except KeyboardInterrupt:
                            if backend:
                                try:
                                    backend.Close()
                                except Exception as error:
                                    manifest["resource_errors"].append({"space": space, "error_code": ErrorCode(error)})
                            raise
                        try:
                            for question in case["questions"]:
                                if progress:
                                    progress(f"{variant['name']} {case['id']}/{question['id']} r{repeat}")
                                row = {"case_id": case["id"], "question_id": question["id"], "repeat": repeat,
                                       "variant": variant["name"], "backend": variant["backend"], "tags": case.get("tags", []),
                                       "question": question["text"], "status": "completed", "failed_stage": None, "error_code": None,
                                       "candidates": [], "context": [], "raw": None, "warnings": [], "answer": None,
                                       "model_input": None, "usage": None, "cost": None, "timings_ms": {}}
                                stage = "search"
                                try:
                                    if preparation_error:
                                        row.update(status="failed", failed_stage=preparation_error[0], error_code=preparation_error[1])
                                    else:
                                        started = time.monotonic()
                                        try:
                                            if variant["backend"] == "none":
                                                payload = {"evidence": []}
                                            elif variant["backend"] == "oracle":
                                                ids = dict.fromkeys(g[0] for g in question["gold"]["evidence_groups"])
                                                payload = {"evidence": [{"record_id": i["id"], "item_ids": [i["id"]],
                                                            "text": i["text"], "citation": "eval://" + case["id"] + "/" + i["id"]}
                                                           for i in case["items"] if i["id"] in ids]}
                                            else:
                                                context = Context(config["timeout"])
                                                payload = backend.Search(question["text"], config["top_k"], context)
                                                context.CheckCancelled()
                                            row["candidates"] = _evidence(payload, case["items"],
                                                                         len(case["items"]) if variant["backend"] == "oracle" else config["top_k"])
                                            row["raw"], row["warnings"] = payload.get("raw"), payload.get("warnings", [])
                                        finally:
                                            row["timings_ms"]["search"] = (time.monotonic() - started) * 1000
                                        row["context"] = Fit(row["candidates"], config["max_context_chars"])
                                        if answer_model:
                                            stage = "answer"
                                            started = time.monotonic()
                                            try:
                                                context = Context(config["timeout"])
                                                answer = answer_model.Answer({k: question[k] for k in ("text", "question_date") if k in question},
                                                                            row["context"], context)
                                                context.CheckCancelled()
                                                row.update(answer=answer["text"], model_input=answer["messages"], usage=answer["usage"])
                                            finally:
                                                row["timings_ms"]["answer"] = (time.monotonic() - started) * 1000
                                except Exception as error:
                                    row.update(status="failed", failed_stage=stage, error_code=ErrorCode(error))
                                stream.write(Encode(row) + "\n")
                                stream.flush()
                        finally:
                            if backend:
                                try:
                                    backend.Close()
                                except Exception as error:
                                    manifest["resource_errors"].append({"space": space, "error_code": ErrorCode(error)})
                            Save(directory / "manifest.json", manifest)
        manifest["status"] = "completed"
    except KeyboardInterrupt:
        interrupted = True
        manifest["status"] = "interrupted"
    except Exception as error:
        manifest.update(status="failed", error_code=ErrorCode(error))
    finally:
        if answer_model:
            try:
                answer_model.Close()
            except Exception as error:
                manifest["resource_errors"].append({"resource": "answer_model", "error_code": ErrorCode(error)})
        Save(directory / "manifest.json", manifest)
    try:
        Score(directory, judge_settings=None if interrupted else config.get("judge_model"), model_factory=model_factory)
    except KeyboardInterrupt:
        interrupted = True
        manifest.update(status="interrupted", failed_stage="score")
        Save(directory / "manifest.json", manifest)
        Score(directory)
    except Exception as error:
        manifest.update(status="failed", failed_stage="score", error_code=ErrorCode(error))
        Save(directory / "manifest.json", manifest)
        Score(directory)
    if interrupted:
        return directory, 130
    manifest = ReadJSON(directory / "manifest.json")
    failed = manifest["status"] != "completed" or manifest["resource_errors"] or any(r["status"] != "completed" for r in ReadRows(directory / "results.jsonl"))
    failed = failed or any(g["answer_status"] == "judge_error" for g in ReadRows(directory / "judgments.jsonl"))
    return directory, 1 if failed else 0


def Score(directory, *, judge_settings=None, labels=None, model_factory=SingleModel):
    directory = Path(directory)
    manifest = ReadJSON(directory / "manifest.json")
    old = {Key(r): r for r in ReadRows(directory / "judgments.jsonl")}
    results = ReadRows(directory / "results.jsonl")
    if labels and not Path(labels).is_file():
        raise EvalError("Manual labels file does not exist")
    supplied = ReadRows(labels) if labels else []
    overrides = {}
    keys = {Key(r) for r in results if r["answer"] is not None}
    for label in supplied:
        if not isinstance(label, dict) or not all(k in label for k in ("case_id", "question_id", "repeat", "variant")):
            raise EvalError("Invalid manual label identity")
        if Key(label) not in keys or Key(label) in overrides or label.get("answer_status") not in {"pass", "fail"} or not isinstance(label.get("reason"), str) or not label["reason"].strip():
            raise EvalError("Invalid manual label")
        overrides[Key(label)] = label
    questions = {(c["id"], q["id"]): q for c in manifest["dataset"]["cases"] for q in c["questions"]}
    grades, judge = [], None
    try:
        if judge_settings:
            from .model import Preflight
            Preflight(judge_settings)
            judge = model_factory(judge_settings)
            manifest["scoring_model"] = judge_settings
        for result in results:
            question = questions[(result["case_id"], result["question_id"])]
            grade = Grade(result, question["gold"])
            previous = old.get(Key(result))
            if previous:
                for key in ("answer_status", "reason", "history", "judge_usage", "judge_timings_ms", "source"):
                    if key in previous:
                        grade[key] = previous[key]
            replacement = None
            if Key(result) in overrides:
                replacement = {"answer_status": overrides[Key(result)]["answer_status"], "reason": overrides[Key(result)]["reason"], "source": "manual"}
            elif judge and result["status"] == "completed" and result["answer"] is not None:
                started = time.monotonic()
                try:
                    reply = judge.Call(JUDGE_PROMPT, Encode({"question": question["text"], "question_date": question.get("question_date"),
                                        "gold": question["gold"], "context": result["context"], "answer": result["answer"]}),
                                       Context(judge_settings.get("timeout", 30)))
                    verdict = json.loads(reply["text"])
                    if not isinstance(verdict, dict) or verdict.get("status") not in {"pass", "fail"} or not isinstance(verdict.get("reason"), str) or not verdict["reason"].strip():
                        raise EvalError("Invalid judge reply")
                    replacement = {"answer_status": verdict["status"], "reason": verdict["reason"], "source": "model", "judge_usage": reply["usage"]}
                except Exception as error:
                    replacement = {"answer_status": "judge_error", "reason": ErrorCode(error), "source": "model"}
                replacement["judge_timings_ms"] = (time.monotonic() - started) * 1000
            if replacement:
                history = list(grade.get("history", []))
                if grade["answer_status"] != "ungraded":
                    history.append({k: v for k, v in grade.items() if k not in {"history", "candidate_coverage", "context_coverage", "candidate_complete", "context_complete"}})
                grade.update(replacement, history=history)
            grades.append(grade)
            _rows(directory / "judgments.jsonl", grades)
    finally:
        if judge:
            judge.Close()
        Save(directory / "manifest.json", manifest)
    if not results:
        _rows(directory / "judgments.jsonl", [])
    Report(directory)
    return grades
