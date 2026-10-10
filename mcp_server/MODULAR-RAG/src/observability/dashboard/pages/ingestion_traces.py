"""Browse the current six-stage ingestion traces."""
from __future__ import annotations

import streamlit as st

from src.observability.dashboard.services.trace_service import TraceService

STAGES = ('integrity', 'load', 'split', 'transform', 'embed', 'upsert')


def render() -> None:
    st.header('🔬 Ingestion Traces')
    service = TraceService()
    traces = service.list_traces(trace_type='ingestion')
    if not traces:
        st.info('No ingestion traces recorded yet. Run an ingestion first!')
        return
    st.subheader(f'📋 Trace History ({len(traces)})')
    for index, trace in enumerate(traces):
        source = trace.get('metadata', {}).get('source_path', '—')
        elapsed = trace.get('total_elapsed_ms', 0)
        with st.expander(f'📄 {source} · {elapsed:.0f} ms', expanded=index == 0):
            timings = service.get_stage_timings(trace)
            stages = {t['stage_name']: t['data'] for t in timings}
            integrity = stages.get('integrity', {})
            if integrity.get('skipped'):
                st.info('Unchanged document; processing skipped.')
            elif missing := [s for s in STAGES if s not in stages]:
                st.warning('Incomplete processing: ' + ', '.join(missing))
            current = [t for t in timings if t['stage_name'] in STAGES]
            if current:
                st.bar_chart({t['stage_name']: t['elapsed_ms'] for t in current}, horizontal=True)
            columns = st.columns(4)
            columns[0].metric('Chunks', stages.get('split', {}).get('chunk_count', 0))
            columns[1].metric('Images', stages.get('load', {}).get('image_count', 0))
            columns[2].metric('Vectors', stages.get('upsert', {}).get('vector_count', 0))
            columns[3].metric('Total Time', f'{elapsed:.0f} ms')
            if current:
                tabs = st.tabs([t['stage_name'].title() for t in current])
                for tab, stage in zip(tabs, current):
                    with tab:
                        data = stage['data']
                        st.caption(f"{stage['elapsed_ms']:.1f} ms")
                        if data.get('error'):
                            st.error(data['error'])
                        for warning in data.get('warnings', []):
                            st.warning(warning)
                        name = stage['stage_name']
                        if name == 'load':
                            st.caption(f"Original characters: {data.get('raw_text_length', 0)} · Elements: {data.get('elements', 0)}")
                            if preview := data.get('text_preview'):
                                st.text_area('Preprocessed text', preview, height=240, disabled=True, key=f'load_{index}')
                        elif name == 'split':
                            st.metric('Evidence Groups', data.get('evidence_count', 0))
                        elif name == 'transform':
                            st.metric('Max Tokens', data.get('max_tokens', 0))
                            st.caption(f"LLM calls: {data.get('llm_calls', 0)} · Metadata calls: {data.get('metadata_calls', 0)} · Cache hits: {data.get('model_cache_hits', 0)} · Protected: {data.get('protected_chunks', 0)}")
                        elif name == 'embed':
                            st.metric('Search Units', data.get('units', 0))
                            st.caption(f"Embedding calls: {data.get('embedding_calls', 0)} · Cache hits: {data.get('embedding_cache_hits', 0)}")
                        elif name == 'upsert':
                            st.metric('Sparse Documents', data.get('bm25_docs', 0))
                            st.caption(f"{data.get('build_id', '')} · {data.get('publication_status', '')} · {data.get('technical_validation', '')}")
                        st.json(data, expanded=False)
