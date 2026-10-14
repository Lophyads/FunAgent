"""python -m evals: validate, run, score, compare."""
import argparse
import json
from pathlib import Path
import sys

from .dataset import EvalError, LoadConfig, LoadDataset, LoadModelSettings
from .report import Compare
from .runner import Run, Score


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local external-memory evaluation")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a dataset without external calls")
    validate.add_argument("dataset", type=Path)
    run = commands.add_parser("run", help="Execute a new isolated experiment")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output", default=Path("eval_runs"), type=Path)
    run.add_argument("--env-file", type=Path)
    score = commands.add_parser("score", help="Rescore saved results; no retrieval or answer calls")
    score.add_argument("directory", type=Path)
    score.add_argument("--judge-model", type=Path, help="Explicitly call a grading model")
    score.add_argument("--labels", type=Path, help="Import human pass/fail labels")
    score.add_argument("--env-file", type=Path)
    compare = commands.add_parser("compare", help="Compare two variants or runs")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", nargs="?", type=Path)
    compare.add_argument("--left-variant")
    compare.add_argument("--right-variant")
    compare.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if getattr(args, "env_file", None):
            if not args.env_file.is_file():
                raise EvalError("Environment file does not exist")
            try:
                from dotenv import load_dotenv
            except ImportError as error:
                raise EvalError("Environment files need python-dotenv") from error
            load_dotenv(args.env_file, override=False)
        if args.command == "validate":
            data = LoadDataset(args.dataset)
            print(json.dumps({"cases": len(data["cases"]), "questions": sum(len(c["questions"]) for c in data["cases"])}, ensure_ascii=False))
        elif args.command == "run":
            config, dataset = LoadConfig(args.config)
            directory, status = Run(config, dataset, args.output, progress=lambda message: print(message, file=sys.stderr))
            print(json.dumps({"directory": str(directory), "exit_code": status}, ensure_ascii=False))
            return status
        elif args.command == "score":
            settings = LoadModelSettings(args.judge_model) if args.judge_model else None
            grades = Score(args.directory, judge_settings=settings, labels=args.labels)
            print(json.dumps({"scored": len(grades)}, ensure_ascii=False))
            return int(any(g["answer_status"] == "judge_error" for g in grades))
        else:
            text = Compare(args.left, args.right, args.left_variant, args.right_variant)
            if args.output:
                with args.output.open("x", encoding="utf8") as stream:
                    stream.write(text)
            else:
                print(text)
        return 0
    except EvalError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Evaluation failed; inspect saved results if a run was created.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
