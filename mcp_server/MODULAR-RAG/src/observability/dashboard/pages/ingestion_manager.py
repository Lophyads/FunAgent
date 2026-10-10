"""Ingestion Manager page – upload files, trigger ingestion, delete documents.

Layout:
1. File uploader + collection selector
2. Ingest button → progress bar (using on_progress callback)
3. Document list with delete buttons
"""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

import streamlit as st

from src.observability.dashboard.services.data_service import DataService


def _run_ingestion(
    uploaded_file: "st.runtime.uploaded_file_manager.UploadedFile",
    collection: str,
    progress_bar: "st.delta_generator.DeltaGenerator",
    status_text: "st.delta_generator.DeltaGenerator",
) -> None:
    """Save the uploaded file to a temp location and run the pipeline."""
    from src.core.settings import load_settings
    from src.core.trace import TraceContext, TraceCollector
    from src.ingestion.pipeline import IngestionPipeline

    settings = load_settings()

    # Write uploaded file to a temp location
    suffix = Path(uploaded_file.name).suffix
    with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getbuffer())
        tmp_path = tmp.name

    _STAGE_LABELS = {
        "integrity": "🔍 Checking file integrity…",
        "load": "📄 Loading document…",
        "split": "✂️ Chunking document…",
        "transform": "🔄 Transforming chunks…",
        "embed": "🔢 Encoding vectors…",
        "upsert": "💾 Storing to database…",
    }

    def on_progress(stage: str, current: int, total: int) -> None:
        frac = (current - 1) / total  # stage just started, show partial progress
        label = _STAGE_LABELS.get(stage, stage)
        progress_bar.progress(frac, text=f"[{current}/{total}] {label}")
        status_text.caption(label)

    trace = TraceContext(trace_type="ingestion")
    trace.metadata["source_path"] = uploaded_file.name
    trace.metadata["collection"] = collection
    trace.metadata["source"] = "dashboard"

    pipeline = None
    try:
        pipeline = IngestionPipeline(settings, collection=collection)
        result = pipeline.run(
            file_path=tmp_path,
            trace=trace,
            on_progress=on_progress,
            source_path=uploaded_file.name,
        )
        if result.success:
            skipped = result.stages.get("integrity", {}).get("skipped", False)
            progress_bar.progress(1.0, text="⏭️ Already ingested" if skipped else "✅ Complete")
            status_text.success(f"已更新 **{uploaded_file.name}**，集合 **{collection}** 中已可查询。")
            if result.degraded:
                status_text.warning('部分模型处理已降级：' + '；'.join(result.warnings))
        else:
            status_text.error(f"Ingestion failed: {result.error}")
    except Exception as exc:
        status_text.error(f"Ingestion failed: {exc}")
    finally:
        try:
            if pipeline is not None:
                pipeline.close()
        finally:
            try:
                TraceCollector().collect(trace)
            finally:
                Path(tmp_path).unlink(missing_ok=True)


def render() -> None:
    """Render the Ingestion Manager page."""
    st.header("📥 Ingestion Manager")

    # ── Upload section ─────────────────────────────────────────────
    st.subheader("📤 Upload & Ingest")
    st.caption("Uploading the same filename to the same collection updates that document. Rename it to keep a separate copy.")

    col1, col2 = st.columns([3, 1])
    with col1:
        uploaded = st.file_uploader(
            "Select a file to ingest",
            type=["pdf", "md", "markdown"],
            key="ingest_uploader",
        )
    with col2:
        collection = st.text_input("Collection", key="ingest_collection")

    if uploaded is not None:
        if st.button("🚀 Start Ingestion", key="btn_ingest"):
            progress_bar = st.progress(0, text="Preparing…")
            status_text = st.empty()
            _run_ingestion(uploaded, collection.strip() or "default", progress_bar, status_text)

    st.divider()

    # ── Document management section ────────────────────────────────
    st.subheader("🗑️ 已发布文档")

    try:
        svc = DataService()
        collections = svc.list_collections()
        if "default" not in collections:
            collections.insert(0, "default")
        default_idx = collections.index("default")
        selected = st.selectbox(
            "Collection",
            collections,
            index=default_idx,
            key="manage_collection",
            help="Select which collection's documents to manage.",
        )
        docs = svc.list_documents(selected)
    except Exception as exc:
        st.error(f"Failed to load documents: {exc}")
        return

    if not docs:
        st.info(
            "**No documents ingested yet.** "
            "Upload a PDF or Markdown file above and click \"Start Ingestion\" to begin."
        )
        return

    for idx, doc in enumerate(docs):
        col_info, col_btn = st.columns([4, 1])
        with col_info:
            st.markdown(
                f"**{doc['source_path']}** — "
                f"collection: `{doc.get('collection', '—')}` | "
                f"chunks: {doc['chunk_count']} | "
                f"images: {doc['image_count']}"
            )
        with col_btn:
            if doc.get("can_delete", True) and st.button("🗑️ Delete", key=f"del_{selected}_{idx}"):
                try:
                    result = svc.delete_document(
                        source_path=doc["source_path"],
                        collection=selected,
                        source_hash=doc.get("source_hash"),
                    )
                    if result.success:
                        st.success(
                            f"Deleted: {result.chunks_deleted} chunks, "
                            f"{result.images_deleted} images removed."
                        )
                        st.rerun()
                    else:
                        st.warning(f"Partial delete. Errors: {result.errors}")
                except Exception as exc:
                    st.error(f"Delete failed: {exc}")
