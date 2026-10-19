"""Deterministic, authorized fact and knowledge retrieval after one plan."""
from dataclasses import replace
from uuid import uuid4
import time
from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import DomainError, ControlError
from .contracts import MemoryContext, MemoryBranch, MemoryQuery, MemoryIssue
from .common import normalize, RecordIdentity, MergeSourceStatus
from .candidate_selection import DeduplicateRecords, SelectByScore
from .memory_mgr import MemoryMgr
from .memory_planner import MemoryPlanner
from .prompt import FitBranch
from .protocols import MemoryReader
from ..observability.observation_mgr import ObservationMgr
from ..observability.projections import Memory, Query, Candidates, Deduplication, ScoredCandidates
from .topic_projection import PlanningTopics
from ..model.contracts import RerankRequest
from ..model.model_runtime import ModelRuntime
from ..model.protocols import ModelError

class MemoryPrepare:
    @staticmethod
    def Run(request, context):
        mgr = ObservationMgr.GetInstance()
        with mgr.Scope('memory.prepare', input=lambda: dict(text=request.text)):
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
        mgr.Update(policies=policies, authorized_sources=names, rerank_policy=conf.Policy("rerank"))
        if not any(names.values()):
            mgr.Update(skip_reason='no_authorized_enabled_source')
            return MemoryContext(status="skipped")
        policy = conf.Policy("recall")
        child = replace(context, run_id=uuid4().hex, purpose="recall", record_scope="ephemeral",
                        access=frozenset(sources[name].get("read_access", f"memory.{name}.read")
                                         for group in names.values() for name in group),
                        deadline=min(context.deadline or float("inf"), time.monotonic() + policy.get("timeout", 2)))
        try:
            selected = tuple(dict.fromkeys(name for group in names.values() for name in group))
            snapshots = memory.ReadTopics(selected, child)
            plan = MemoryPlanner.Plan(request, child, PlanningTopics(snapshots, sources))
        except (DomainError, ControlError) as error:
            context.CheckCancelled()
            code = error.code if error.code in {"PLAN_INVALID", "PLAN_FAILED", "DEADLINE"} else "PLAN_FAILED"
            warning = f"recall:{code}"
            mgr.Event('memory.plan_skipped', error_code=code, warning=warning)
            return MemoryContext(warnings=(warning,), status="skipped")
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
                                           ("note", "episode") if branch == "facts" else (),
                                           policies[branch].get("candidate_limit", min(100, policies[branch].get("max_records", 10) + 1))) for text in texts)
                groups, statuses, warnings, issues = {name: [] for name in selected}, {}, list(plan.warnings), []
                branch_deadline = min(child.deadline, time.monotonic() + policies[branch].get("timeout", 2))
                for query in queries:
                    with mgr.Scope('memory.query', input=lambda: Query(query)):
                        for name in selected:
                            context.CheckCancelled()
                            source_context = replace(child, deadline=branch_deadline)
                            try:
                                result = memory.Search(replace(query, source_ids=(name,)), source_context)
                                groups[name].extend(result.records)
                                previous = statuses.get(name)
                                current = result.source_status.get(name, "failed")
                                statuses[name] = MergeSourceStatus(previous, current)
                                warnings.extend(result.warnings)
                                issues.extend(result.issues)
                            except ControlError:
                                context.CheckCancelled()
                                statuses[name] = MergeSourceStatus(statuses.get(name), "timeout")
                                warnings.append(f"{name}:DEADLINE")
                # Preserve local ordering and interleave sources without comparing scores.
                groups = [tuple(group) for group in groups.values()]
                ordered = tuple(group[index] for index in range(max((len(g) for g in groups), default=0))
                                for group in groups if index < len(group))
                candidates = ordered
                with mgr.Scope('memory.deduplicate', input=lambda: dict(records=Candidates(candidates))):
                    ordered, duplicates = DeduplicateRecords(candidates)
                    mgr.Complete(output=lambda: Deduplication(candidates, ordered, duplicates))
                rerank = conf.Policy("rerank")
                if ordered and rerank.get("enabled", False):
                    candidates = ordered
                    scoring_context = replace(child, deadline=branch_deadline)
                    threshold = policies[branch].get("min_score")
                    rank_question = request.text
                    if branch == "facts" and plan.memory_topics:
                        enriched = f"原问题：{request.text}\n相关记忆主题：{'；'.join(plan.memory_topics)}"
                        if len(enriched) <= 8000:
                            rank_question = enriched
                    with mgr.Scope('memory.rerank', branch=branch, profile_id=rerank["profile"],
                                   min_score=threshold, candidate_count=len(candidates),
                                   input=lambda: dict(question=rank_question, records=Candidates(candidates))):
                        try:
                            scoring_context.CheckCancelled()
                            ranked = ModelRuntime.GetInstance().Rank(
                                RerankRequest(rank_question, tuple(r.content for r in candidates)),
                                rerank["profile"], scoring_context)
                            scoring_context.CheckCancelled()
                        except (ControlError, ModelError) as error:
                            if isinstance(error, ControlError) and error.code != 'DEADLINE':
                                raise
                            context.CheckCancelled()
                            expired = isinstance(error, ControlError) or time.monotonic() >= scoring_context.deadline
                            warnings.append(f"{branch}:DEADLINE" if expired else f"{branch}:RERANK_FAILED")
                            code = 'MEMORY_TIMEOUT' if expired else 'RERANK_FAILED'
                            issues.append(MemoryIssue(code=code, stage='rerank'))
                            mgr.Event('memory.rerank_failed', error_code=error.code, warning=warnings[-1])
                            mgr.Complete(status='partial', error_code=code,
                                         output=lambda: dict(mode='fallback', scores=None, error_code=code,
                                                             records=Candidates(candidates)))
                        else:
                            scores = ranked.scores
                            with mgr.Scope('memory.filter', min_score=threshold,
                                           input=lambda: dict(items=ScoredCandidates(candidates, scores, candidates))):
                                ordered, rejected = SelectByScore(candidates, scores, threshold)
                                mgr.Complete(output=lambda: dict(selected=ScoredCandidates(candidates, scores, ordered),
                                                                 rejected=ScoredCandidates(candidates, scores, rejected)))
                            mgr.Complete(profile_id=ranked.profile_id, model=ranked.model,
                                         output=lambda: dict(mode='reranked', scores=scores,
                                             selected=ScoredCandidates(candidates, scores, ordered),
                                             rejected=ScoredCandidates(candidates, scores, rejected)))
                with mgr.Scope('memory.fit', input=lambda: dict(record_refs=[dict(source_id=r.ref.source_id, record_id=r.ref.record_id)
                                                                           for r in ordered], count=len(ordered),
                                                              chars=sum(len(r.content) for r in ordered)),
                               max_records=policies[branch].get('max_records', 10), max_chars=policies[branch].get('max_chars', 16000)):
                    branches[branch] = FitBranch(branch, ordered, statuses, warnings, queries,
                                                policies[branch].get("max_records", 10), policies[branch].get("max_chars", 16000), issues=issues)
                    mgr.Complete(status=branches[branch].status, output=lambda: Memory(branches[branch]))
                mgr.Complete(status=branches[branch].status, output=lambda: Memory(branches[branch]))
        facts, knowledge = branches["facts"], branches["knowledge"]
        unique = {}
        for record in (*facts.records, *knowledge.records):
            unique.setdefault(RecordIdentity(record), record)
        records = tuple(unique.values())
        warnings = tuple(dict.fromkeys((*facts.warnings, *knowledge.warnings)))
        active = [branch for branch in (facts, knowledge) if branch.status != "skipped"]
        status = ("skipped" if not active else "failed" if all(branch.status == "failed" for branch in active)
                  else "partial" if warnings or any(branch.status in {"partial", "failed"} for branch in active)
                  else "ready" if records else "empty")
        return MemoryContext(records, {**facts.source_status, **knowledge.source_status}, warnings, status,
                             (*facts.queries, *knowledge.queries), facts, knowledge,
                             issues=tuple(dict.fromkeys((*facts.issues, *knowledge.issues))))


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
