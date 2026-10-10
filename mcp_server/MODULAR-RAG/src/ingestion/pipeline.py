"""Public ingestion entry: every document uses the structured pipeline."""
from src.core.settings import load_settings
from src.ingestion.pipeline_result import PipelineResult as PipelineResult
from src.ingestion.structured_pipeline import StructuredIngestionPipeline

IngestionPipeline = StructuredIngestionPipeline


def run_pipeline(file_path, settings=None, collection='default', force=False, trace=None):
    with IngestionPipeline(settings or load_settings(), collection=collection, force=force) as pipeline:
        return pipeline.run(file_path, trace=trace)
