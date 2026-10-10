"""Overview page – system configuration and data statistics.

Displays:
- Component configuration cards (LLM, Embedding, VectorStore …)
- Collection statistics (document count, chunk count, image count)
"""

from __future__ import annotations

from typing import Any, Dict

import streamlit as st

from src.observability.dashboard.services.config_service import ConfigService


def _safe_collection_stats() -> Dict[str, Any]:
    """Count logical published collections through the same browsing service."""
    from src.observability.dashboard.services.data_service import DataService
    service = DataService()
    try:
        return {name: service.get_collection_stats(name) for name in service.list_collections()}
    except Exception:
        return {}
    finally:
        service.close()


def render() -> None:
    """Render the Overview page."""
    st.header("📊 System Overview")

    # ── Component configuration cards ──────────────────────────────
    st.subheader("🔧 Component Configuration")

    try:
        config_service = ConfigService()
        cards = config_service.get_component_cards()
    except Exception as exc:
        st.error(f"Failed to load configuration: {exc}")
        return

    cols = st.columns(min(len(cards), 3))
    for idx, card in enumerate(cards):
        with cols[idx % len(cols)]:
            st.markdown(f"**{card.name}**")
            st.caption(f"Provider: `{card.provider}`  \nModel: `{card.model}`")
            with st.expander("Details"):
                for k, v in card.extra.items():
                    st.text(f"{k}: {v}")

    # ── Collection statistics ──────────────────────────────────────
    st.subheader("📁 Collection Statistics")

    stats = _safe_collection_stats()
    if stats:
        stat_cols = st.columns(min(len(stats), 4))
        for idx, (name, info) in enumerate(sorted(stats.items())):
            with stat_cols[idx % len(stat_cols)]:
                count = info.get("chunk_count", "?")
                st.metric(label=name, value=count)
                if count == 0 or count == "?":
                    st.caption("⚠️ Empty")
    else:
        st.warning(
            "**No collections found or ChromaDB unavailable.** "
            "Go to the Ingestion Manager page to upload and ingest documents."
        )

    # ── Trace file statistics ──────────────────────────────────────
    st.subheader("📈 Trace Statistics")

    from src.core.settings import resolve_path
    traces_path = resolve_path("logs/traces.jsonl")
    if traces_path.exists():
        line_count = sum(1 for _ in traces_path.open(encoding="utf-8"))
        if line_count > 0:
            st.metric("Total traces", line_count)
        else:
            st.info("No traces recorded yet. Run a query or ingestion first.")
    else:
        st.info("No traces recorded yet. Run a query or ingestion first.")
