"""Plain local reports with explicit denominators and paired comparisons."""
import json
from pathlib import Path
from statistics import mean

from .dataset import Digest, EvalError, ReadJSON
from .grading import Key


def ReadRows(path):
    import json
    path = Path(path)
    if not path.exists():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines() if line.strip()]
    except (OSError, ValueError) as error:
        raise EvalError("Invalid saved results") from error


def _rate(values):
    values = [v for v in values if v is not None]
    return f"{mean(values):.1%} (n={len(values)})" if values else "— (n=0)"


def _latency(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return "—"
    def percentile(p):
        index = (len(values) - 1) * p
        low, high = int(index), min(int(index) + 1, len(values) - 1)
        return values[low] + (values[high] - values[low]) * (index - low)
    return f"{percentile(.5):.1f}/{percentile(.95):.1f} ms (n={len(values)})"


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def Report(directory):
    directory = Path(directory)
    manifest = ReadJSON(directory / "manifest.json")
    results, judgments = ReadRows(directory / "results.jsonl"), ReadRows(directory / "judgments.jsonl")
    grades = {Key(g): g for g in judgments}
    config = manifest["config"]
    expected = sum(len(c["questions"]) for c in manifest["dataset"]["cases"]) * config["repeats"]
    lines = ["# 外部记忆 Eval 报告", "", f"运行：{manifest['run_id']} · 模式：{config['mode']} · 状态：{manifest['status']}", "",
             "覆盖率按源材料证据组计算；空证据题不参与覆盖率。小样本延迟仅供描述。", "",
             "| Variant / 分类 | 完成 / 应执行 | 候选覆盖率 | 上下文覆盖率 | 完整上下文命中 | 答案通过率 | 检索 p50/p95 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    details = []
    for variant in config["variants"]:
        selected = [r for r in results if r["variant"] == variant["name"]]
        categories = [None] + sorted({tag for r in selected for tag in r["tags"]})
        for tag in categories:
            rows = [r for r in selected if tag is None or tag in r["tags"]]
            total = expected if tag is None else sum(len(c["questions"]) for c in manifest["dataset"]["cases"]
                                                     if tag in c.get("tags", [])) * config["repeats"]
            gs = [grades[Key(r)] for r in rows if Key(r) in grades]
            answers = [g["answer_status"] == "pass" for g in gs if g["answer_status"] in {"pass", "fail"}]
            values = [variant["name"] + (" / " + tag if tag else ""), f"{sum(r['status'] == 'completed' for r in rows)}/{total}",
                      _rate([g["candidate_coverage"] for g in gs]), _rate([g["context_coverage"] for g in gs]),
                      _rate([g["context_complete"] for g in gs]), _rate(answers),
                      _latency([r["timings_ms"].get("search") for r in rows])]
            lines.append("| " + " | ".join(_cell(v) for v in values) + " |")
        details.extend(["", f"{variant['name']} 设置：`{_cell(variant['settings'])}`", ""])
        spaces = [s for s in manifest["spaces"] if s["variant"] == variant["name"]]
        details.append("入库 p50/p95：" + _latency([s["timings_ms"].get("ingest") for s in spaces]) +
                     "；就绪等待：" + _latency([s["timings_ms"].get("index") for s in spaces]))
        details.append("回答 p50/p95：" + _latency([r["timings_ms"].get("answer") for r in selected]))
    lines.extend(details + ["", "## 逐题结果", ""])
    for row in results:
        grade = grades.get(Key(row), {})
        lines.extend([f"### {row['variant']} / {row['case_id']}/{row['question_id']} / r{row['repeat']}", "",
                      "问题：" + row["question"], "",
                      f"执行：{row['status']}；错误阶段：{row.get('failed_stage')}；错误码：{row.get('error_code')}",
                      f"答案评分：{grade.get('answer_status', 'ungraded')}；理由：{grade.get('reason')}",
                      "候选材料：" + repr([e["item_ids"] for e in row["candidates"]]),
                      "实际上下文材料：" + repr([e["item_ids"] for e in row["context"]]), "",
                      "```json", json.dumps(row["context"], ensure_ascii=False, indent=2), "```", "",
                      "回答：" + (row.get("answer") or "未生成"), ""])
    lines.extend(["## 完整性与资源", "",
                  f"答案已评分：{sum(g['answer_status'] in {'pass', 'fail'} for g in judgments)}/{sum(r['answer'] is not None for r in results)}；"
                  f"评分错误：{sum(g['answer_status'] == 'judge_error' for g in judgments)}。",
                  "外部测试空间、入库回执和关闭错误见 manifest.json；保留空间供审查，不自动清库。",
                  "token/费用未返回时为 null，不按零计算。失败及未产生结果的题目保留在总分母中。", ""])
    text = "\n".join(lines)
    (directory / "report.md").write_text(text, encoding="utf8")
    return text


def Compare(left, right=None, left_variant=None, right_variant=None):
    left, right = Path(left).resolve(), Path(right or left).resolve()
    lm, rm = ReadJSON(left / "manifest.json"), ReadJSON(right / "manifest.json")
    left_variant = left_variant or lm["config"]["variants"][0]["name"]
    choices = rm["config"]["variants"]
    right_variant = right_variant or choices[1 if left == right and len(choices) > 1 else 0]["name"]
    for manifest, name in ((lm, left_variant), (rm, right_variant)):
        if name not in {v["name"] for v in manifest["config"]["variants"]}:
            raise EvalError("Unknown comparison variant")
    def conditions(m):
        return {"dataset_hash": m["dataset_hash"], "grading_version": m["grading_version"],
                "prompts": m["prompts"], "judge_model": m.get("scoring_model"),
                **{k: m["config"].get(k) for k in ("mode", "top_k", "max_context_chars", "timeout", "index_timeout", "answer_model")}}
    compatible = Digest(conditions(lm)) == Digest(conditions(rm))
    lines = [f"# Eval 对照：{left_variant} → {right_variant}", "", "条件一致。" if compatible else
             "条件不一致：仅展示输出，不能据此生成公平胜负结论。", "",
             "| 题目 / 重复 | 左执行 | 右执行 | 上下文覆盖（左 → 右） | 答案（左 → 右） | 比较依据 | 判断 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    def indexed(path, filename, name):
        return {Key(r)[:3]: r for r in ReadRows(path / filename) if r["variant"] == name}
    lr, rr = indexed(left, "results.jsonl", left_variant), indexed(right, "results.jsonl", right_variant)
    lg, rg = indexed(left, "judgments.jsonl", left_variant), indexed(right, "judgments.jsonl", right_variant)
    counts = {"改善": 0, "退化": 0, "持平": 0, "无法比较": 0}
    for key in sorted(set(lr) | set(rr)):
        a, b, ga, gb = lr.get(key, {}), rr.get(key, {}), lg.get(key, {}), rg.get(key, {})
        va, vb = ga.get("context_coverage"), gb.get("context_coverage")
        sa, sb = ga.get("answer_status"), gb.get("answer_status")
        verdict, basis = "无法比较", "证据覆盖"
        if compatible and a.get("status") == b.get("status") == "completed":
            if sa in {"pass", "fail"} and sb in {"pass", "fail"}:
                va, vb = int(sa == "pass"), int(sb == "pass")
                basis = "答案评分"
            elif sa in {"pass", "fail", "judge_error"} or sb in {"pass", "fail", "judge_error"}:
                va = vb = None
            if va is not None and vb is not None:
                verdict = "改善" if vb > va else "退化" if vb < va else "持平"
        counts[verdict] += 1
        lines.append("| " + " | ".join(_cell(v) for v in ("/".join(map(str, key)), a.get("status", "缺失"),
                     b.get("status", "缺失"), f"{ga.get('context_coverage')} → {gb.get('context_coverage')}",
                     f"{sa} → {sb}", basis, verdict)) + " |")
    lines.extend(["", "逐题变化：" + str(counts), "", "条件（左）：`" + _cell(conditions(lm)) + "`",
                  "", "条件（右）：`" + _cell(conditions(rm)) + "`", "",
                  f"完整证据和回答：[左报告]({(left / 'report.md').resolve().as_posix()})、"
                  f"[右报告]({(right / 'report.md').resolve().as_posix()})。", ""])
    return "\n".join(lines)
