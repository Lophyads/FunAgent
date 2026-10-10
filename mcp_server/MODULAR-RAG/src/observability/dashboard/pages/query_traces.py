"""Browse current structured query diagnostics and original evidence."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import streamlit as st

from src.observability.dashboard.services.trace_service import TraceService

logger = logging.getLogger(__name__)


def render() -> None:
    st.header('🔎 Query Traces')
    service = TraceService()
    traces = service.list_traces(trace_type='query')
    if not traces:
        st.info('No query traces recorded yet. Run a query first!')
        return
    with st.form('qt_search_form'):
        keyword = st.text_input('Search by query keyword', key='qt_keyword')
        st.form_submit_button('Search')
    if keyword.strip():
        traces = [t for t in traces if keyword.strip().casefold() in t.get('metadata', {}).get('query', '').casefold()]
    st.subheader(f'📋 Query History ({len(traces)})')
    for index, trace in enumerate(traces):
        metadata = trace.get('metadata', {})
        query = metadata.get('query', '')
        elapsed = trace.get('total_elapsed_ms', 0)
        with st.expander(f'🔍 {query[:60]} · {elapsed:.0f} ms', expanded=index == 0):
            st.markdown(query)
            st.caption(f"Collection: {metadata.get('collection', '')} · Build: {metadata.get('index_build_id', '')} · Status: {metadata.get('result_status', '')}")
            diagnostics = metadata.get('structured_diagnostics', {})
            columns = st.columns(4)
            columns[0].metric('Dense Hits', len(diagnostics.get('dense_results', [])))
            columns[1].metric('Sparse Hits', len(diagnostics.get('sparse_results', [])))
            columns[2].metric('Fused', diagnostics.get('candidate_count', 0))
            columns[3].metric('Total Time', f'{elapsed:.0f} ms')
            for channel, error in diagnostics.get('retrieval_errors', {}).items():
                st.warning(f'{channel}: {error}')
            if diagnostics.get('used_fallback'):
                st.warning('This query used a fallback. See diagnostics for the affected step.')
            timings = service.get_stage_timings(trace)
            if timings:
                st.bar_chart({t['stage_name']: t['elapsed_ms'] for t in timings}, horizontal=True)
            st.json(diagnostics, expanded=False)
            for rank, result in enumerate(metadata.get('final_results', []), 1):
                st.markdown(f"**{rank}. {result.get('source', '')}**")
                st.caption(f"{result.get('chunk_id', '')} · Score: {result.get('score', 0):.4f}")
                st.text_area('Original evidence', result.get('text', ''), height=160,
                             disabled=True, key=f'evidence_{index}_{rank}')
            _render_evaluate_button(trace, index)


def _render_evaluate_button(trace: Dict[str, Any], idx: int) -> None:
    """Render a Ragas evaluate button for a single query trace.

    Re-runs retrieval for the stored query and evaluates with
    RagasEvaluator (LLM-as-Judge).  Only works when query text
    is available in trace metadata.
    """
    meta = trace.get("metadata", {})
    query = meta.get("query", "")
    if not query:
        return

    st.divider()
    st.markdown("#### 📏 Ragas Evaluation")
    st.caption(
        "RAGAS 需要 **Query + Retrieved Context + Answer** 三要素来评估。"
        "日志中仅包含 Query 和检索到的上下文，请在下方输入实际回答后再运行评估。"
    )

    # Answer input box — user provides the actual generated answer
    answer_key = f"eval_answer_{idx}"
    user_answer = st.text_area(
        "✏️ Generated Answer (回答)",
        value=st.session_state.get(answer_key, ""),
        height=120,
        key=answer_key,
        placeholder="请输入系统生成的回答，或粘贴 LLM 的实际输出…",
        help=(
            "Ragas 使用 LLM-as-Judge 评估回答质量。"
            "faithfulness 衡量回答是否忠于检索到的上下文，"
            "answer_relevancy 衡量回答与问题的相关性。"
            "如果不填写回答，将无法获得有意义的评估结果。"
        ),
    )

    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        clicked = st.button(
            "📏 Ragas Evaluate",
            key=f"eval_trace_{idx}",
            help="Re-run this query and score with Ragas (LLM-as-Judge)",
            disabled=not user_answer.strip(),
        )
    with col_info:
        if not user_answer.strip():
            st.warning("⚠️ 请先在上方输入回答内容，再运行 Ragas 评估。")
        else:
            st.caption(
                "Uses Ragas to score faithfulness, answer relevancy, "
                "and context precision. Calls LLM — may take a few seconds."
            )

    # Show previous result from session state
    result_key = f"eval_result_{idx}"
    if result_key in st.session_state and not clicked:
        _display_eval_metrics(st.session_state[result_key])

    if clicked:
        with st.spinner("Running Ragas evaluation…"):
            result = _evaluate_single_trace(query, meta, user_answer=user_answer.strip())
        st.session_state[result_key] = result
        _display_eval_metrics(result)


def _evaluate_single_trace(
    query: str,
    meta: Dict[str, Any],
    user_answer: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-run retrieval and evaluate a single query with Ragas.

    Returns dict with 'metrics' (score dict) or 'error' (str).
    """
    try:
        from dataclasses import replace as dc_replace

        from src.core.settings import load_settings, EvaluationSettings
        from src.libs.evaluator.evaluator_factory import EvaluatorFactory

        settings = load_settings()

        # Override evaluation settings to force Ragas (frozen dataclass, use replace)
        ragas_eval = EvaluationSettings(
            enabled=True,
            provider="ragas",
            metrics=["faithfulness", "answer_relevancy", "context_precision"],
        )
        settings = dc_replace(settings, evaluation=ragas_eval)
        evaluator = EvaluatorFactory.create(settings)

        # Re-run retrieval
        collection = meta.get("collection", "default")
        top_k = meta.get("top_k", 10)
        chunks = _retrieve_chunks(settings, query, top_k, collection)

        if not chunks:
            return {"error": "No chunks retrieved — is data indexed?"}

        # Use user-provided answer; fall back to chunk concatenation only
        # as a last resort (produces less meaningful RAGAS scores).
        if user_answer:
            answer = user_answer
        else:
            _MAX_ANSWER_CHARS = 1500
            texts = []
            for c in chunks:
                if hasattr(c, "text"):
                    texts.append(c.text)
                elif isinstance(c, dict):
                    texts.append(c.get("text", str(c)))
                else:
                    texts.append(str(c))
            answer = " ".join(texts[:3])
            if len(answer) > _MAX_ANSWER_CHARS:
                answer = answer[:_MAX_ANSWER_CHARS]

        # Evaluate
        metrics = evaluator.evaluate(
            query=query,
            retrieved_chunks=chunks,
            generated_answer=answer,
        )
        return {"metrics": metrics, "answer_used": answer}

    except ImportError as exc:
        return {"error": f"Ragas not installed: {exc}"}
    except Exception as exc:
        logger.exception("Ragas evaluation failed")
        return {"error": str(exc)}


def _retrieve_chunks(settings, query, top_k, collection):
    from src.core.query_engine.query_entrypoint import query_collection, retrieval_results
    return retrieval_results(query_collection(settings,query,collection,top_k))


def _display_eval_metrics(result: Dict[str, Any]) -> None:
    """Display evaluation result (metrics or error)."""
    if "error" in result:
        st.error(f"❌ Evaluation failed: {result['error']}")
        return

    metrics = result.get("metrics", {})
    if not metrics:
        st.warning("No metrics returned.")
        return

    st.markdown("**📏 Ragas Scores**")
    cols = st.columns(min(len(metrics), 4))
    for i, (name, value) in enumerate(sorted(metrics.items())):
        with cols[i % len(cols)]:
            st.metric(
                label=name.replace("_", " ").title(),
                value=f"{value:.4f}",
            )


