"""Deterministic, authorized fact and knowledge retrieval after one plan."""
from dataclasses import replace
from uuid import uuid4
import time
from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import DomainError, ControlError
from .contracts import MemoryContext, MemoryBranch, MemoryQuery, RetrievalPlan
from .common import normalize
from .memory_mgr import MemoryMgr
from .memory_planner import MemoryPlanner
from .prompt import FitBranch
from .protocols import MemoryReader

class MemoryPrepare:
    @staticmethod
    def Run(request, context):
        memory: MemoryReader = MemoryMgr.GetInstance()
        context.CheckCancelled()
        if not isinstance(request.text, str) or not request.text.strip() or len(request.text) > 8000:
            raise DomainError("INVALID_REQUEST", "Invalid memory prepare request")
        conf = ConfMgr.GetInstance().Memory()
        sources = conf.Sources()
        policies = {branch: conf.Policy(branch) for branch in ("facts", "knowledge")}
        names = {branch: tuple(name for name in policy.get("sources", ())
                               if policy.get("enabled", False) and
                               sources[name].get("read_access", f"memory.{name}.read") in context.access)
                 for branch, policy in policies.items()}
        if not any(names.values()):
            return MemoryContext(status="skipped")
        policy = conf.Policy("recall")
        child = replace(context, run_id=uuid4().hex, purpose="recall", record_scope="ephemeral",
                        access=frozenset(sources[name].get("read_access", f"memory.{name}.read")
                                         for group in names.values() for name in group),
                        deadline=min(context.deadline or float("inf"), time.monotonic() + policy.get("timeout", 2)))
        descriptions = tuple({"source_id": name, "kind": sources[name]["kind"],
                              "source_description": sources[name].get("source_description", "")}
                             for name in dict.fromkeys(name for group in names.values() for name in group))
        try:
            plan = MemoryPlanner.Plan(request, child, descriptions)
        except (DomainError, ControlError) as error:
            context.CheckCancelled()
            code = error.code if error.code in {"PLAN_INVALID", "PLAN_FAILED", "DEADLINE"} else "PLAN_FAILED"
            plan = RetrievalPlan(bool(names["facts"]), bool(names["knowledge"]), request.text, request.text,
                                 degraded=True, warnings=(f"recall:{code}",))
        context.CheckCancelled()
        branches = {}
        for branch, needed in (("facts", plan.need_memory), ("knowledge", plan.need_knowledge)):
            selected = names[branch]
            if not selected or not needed:
                branches[branch] = MemoryBranch(source_status={name: "skipped" for name in selected}, status="skipped")
                continue
            texts = ((plan.memory_query or request.text,) if branch == "facts" else
                     (plan.knowledge_query, plan.variant, request.text))
            texts = Queries(texts, 1 if branch == "facts" else policies[branch].get("max_queries", 3))
            queries = tuple(MemoryQuery(text, selected, "episodic" if branch == "facts" else "semantic",
                                       ("note", "episode") if branch == "facts" else (), min(100, policies[branch].get("max_records", 10) + 1)) for text in texts)
            groups, statuses, warnings = {name: {} for name in selected}, {}, list(plan.warnings)
            branch_deadline = min(context.deadline or float("inf"), time.monotonic() + policies[branch].get("timeout", 2))
            for query in queries:
                for name in selected:
                    context.CheckCancelled()
                    source_context = replace(child, deadline=branch_deadline)
                    try:
                        result = memory.Search(replace(query, source_ids=(name,)), source_context)
                        for record in result.records:
                            groups[name][record.ref] = record
                        previous = statuses.get(name)
                        current = result.source_status.get(name, "failed")
                        statuses[name] = current if previous in {None, "ok"} else previous
                        warnings.extend(result.warnings)
                    except ControlError:
                        context.CheckCancelled()
                        statuses[name] = "failed"
                        warnings.append(f"{name}:DEADLINE")
            # Preserve local ordering and interleave sources without comparing scores.
            groups = [tuple(group.values()) for group in groups.values()]
            ordered = tuple(group[index] for index in range(max((len(g) for g in groups), default=0))
                            for group in groups if index < len(group))
            statuses = {name: ("partial" if groups[index] else "failed") if statuses.get(name) != "ok" else
                        "ready" if groups[index] else "empty" for index, name in enumerate(selected)}
            branches[branch] = FitBranch(branch, ordered, statuses, warnings, queries,
                                        policies[branch].get("max_records", 10), policies[branch].get("max_chars", 16000))
        facts, knowledge = branches["facts"], branches["knowledge"]
        records = tuple(dict((r.ref, r) for r in (*facts.records, *knowledge.records)).values())
        warnings = tuple(dict.fromkeys((*facts.warnings, *knowledge.warnings)))
        active = [branch for branch in (facts, knowledge) if branch.status != "skipped"]
        status = ("skipped" if not active else "failed" if all(branch.status == "failed" for branch in active)
                  else "partial" if warnings or any(branch.status in {"partial", "failed"} for branch in active)
                  else "ready" if records else "empty")
        return MemoryContext(records, {**facts.source_status, **knowledge.source_status}, warnings, status,
                             (*facts.queries, *knowledge.queries), facts, knowledge)


def Queries(texts, maximum):
    values, seen = [], set()
    for text in texts:
        if not text or not text.strip():
            continue
        text = " ".join(text.split())
        key = normalize(text)
        if key not in seen:
            values.append(text)
            seen.add(key)
    return tuple(values[:maximum])
