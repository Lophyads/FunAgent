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
from ..observability.observation_mgr import ObservationMgr
from ..observability.projections import Memory, Messages, Query, Plan

class MemoryPrepare:
    @staticmethod
    def Run(request, context):
        mgr = ObservationMgr.GetInstance()
        with mgr.Scope('memory.prepare', input=lambda: dict(text=request.text, history=Messages(request.history))):
            result = MemoryPrepare._run(request, context)
            mgr.Complete(status=result.status, output=lambda: Memory(result))
            return result

    @staticmethod
    def _run(request, context):
        mgr = ObservationMgr.GetInstance()
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
        mgr.Update(policies=policies, authorized_sources=names)
        if not any(names.values()):
            mgr.Update(skip_reason='no_authorized_enabled_source')
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
            mgr.Event('memory.plan_degraded', error_code=code, plan=lambda: Plan(plan))
        context.CheckCancelled()
        branches = {}
        for branch, needed in (("facts", plan.need_memory), ("knowledge", plan.need_knowledge)):
            with mgr.Scope('memory.branch', run_id=child.run_id, purpose='recall', branch=branch,
                           input=lambda: dict(needed=needed, sources=names[branch], policy=policies[branch])):
                selected = names[branch]
                if not selected or not needed:
                    branches[branch] = MemoryBranch(source_status={name: "skipped" for name in selected}, status="skipped")
                    mgr.Complete(status='skipped', output=lambda: Memory(branches[branch]), reason='not_needed' if not needed else 'no_source')
                    continue
                texts = ((plan.memory_query or request.text,) if branch == "facts" else
                         (plan.knowledge_query, plan.variant, request.text))
                texts = Queries(texts, 1 if branch == "facts" else policies[branch].get("max_queries", 3))
                queries = tuple(MemoryQuery(text, selected, "episodic" if branch == "facts" else "semantic",
                                           ("note", "episode") if branch == "facts" else (), min(100, policies[branch].get("max_records", 10) + 1)) for text in texts)
                groups, statuses, warnings = {name: {} for name in selected}, {}, list(plan.warnings)
                branch_deadline = min(context.deadline or float("inf"), time.monotonic() + policies[branch].get("timeout", 2))
                for query in queries:
                    with mgr.Scope('memory.query', input=lambda: Query(query)):
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
                with mgr.Scope('memory.fit', input=lambda: dict(record_refs=[dict(source_id=r.ref.source_id, record_id=r.ref.record_id)
                                                                           for r in ordered], count=len(ordered),
                                                              chars=sum(len(r.content) for r in ordered)),
                               max_records=policies[branch].get('max_records', 10), max_chars=policies[branch].get('max_chars', 16000)):
                    branches[branch] = FitBranch(branch, ordered, statuses, warnings, queries,
                                                policies[branch].get("max_records", 10), policies[branch].get("max_chars", 16000))
                    mgr.Complete(status=branches[branch].status, output=lambda: Memory(branches[branch]))
                mgr.Complete(status=branches[branch].status, output=lambda: Memory(branches[branch]))
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
