"""Stable public Cognee dataset names with private service identifiers."""
from enum import Enum
from uuid import UUID


class CogneeDataset(Enum):
    """Datasets that FunAgent is allowed to manage.

    Configuration, diagnostics and citations use ``dataset_name``.  The UUID is
    an implementation detail used only for Cognee API calls.
    """

    FUNAGENT_MEMORY = ("funagent-memory", "f1f5c81e-1c5c-59bf-b96a-bcd6fe154d32")

    def __init__(self, dataset_name, dataset_id):
        self.dataset_name = dataset_name
        self.dataset_id = str(UUID(dataset_id))

    @classmethod
    def Find(cls, dataset_name):
        return next((dataset for dataset in cls if dataset.dataset_name == dataset_name), None)
