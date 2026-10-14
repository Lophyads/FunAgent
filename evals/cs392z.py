"""Build reproducible text-only cases from the supplied CS329Z lecture notes."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from .dataset import EvalError, ReadJSON, Validate


def Generate(source, questions):
    items, files = [], []
    for number in range(1, 5):
        path = Path(source) / f"lecture{number:02}.md"
        raw = path.read_bytes()
        lines = raw.decode("utf-8-sig").splitlines()
        starts = [(i, re.match(r"^## (\d+)(?:[.–-])", line)) for i, line in enumerate(lines)]
        starts = [(i, match) for i, match in starts if match]
        files.append({"path": path.name, "sha256": hashlib.sha256(raw).hexdigest()})
        for index, (start, match) in enumerate(starts):
            end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
            body = [line for line in lines[start + 1:end] if not
                    (re.fullmatch(r"\s*!\[.*\]\(.*\)\s*", line) or line.strip() == "---")]
            content = "\n".join(body).strip()
            if not content:
                continue  # Image-only slides have no textual evidence to score.
            items.append({"id": f"L{number:02}-S{int(match[1]):03}",
                          "text": f"文件：{path.name}\n{lines[start]}\n\n{content}",
                          "source": {"file": path.name, "line_start": start + 1, "line_end": end,
                                     "heading": lines[start]}})
    data = {"schema_version": 1,
            "source": {"name": "CS329Z lecture Markdown (local folder cs392z)", "files": files,
                       "preprocessing": "numbered sections; remove image links and slide separators; no OCR",
                       "questions": "manually grounded, frozen before retrieval"},
            "cases": [{"id": "cs392z-corpus", "tags": ["cs392z", "course-notes", "full-corpus"],
                       "items": items, "questions": questions}]}
    return Validate(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("docs/cs392z"))
    parser.add_argument("--questions", type=Path, default=Path(__file__).parent / "data/cs392z_questions.json")
    parser.add_argument("--output", type=Path, default=Path("evals/data/cs392z.json"))
    args = parser.parse_args()
    try:
        data = Generate(args.source, ReadJSON(args.questions))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
        print(json.dumps({"items": len(data["cases"][0]["items"]),
                          "questions": len(data["cases"][0]["questions"]), "output": str(args.output)}))
    except (EvalError, OSError, UnicodeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
