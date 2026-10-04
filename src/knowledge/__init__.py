"""Knowledge preparation contracts and replaceable implementations."""

from importlib import import_module

from .models import KnowledgeContext, KnowledgeItem, KnowledgePlan, KnowledgeRequest

__all__ = [
    "KnowledgeContext", "KnowledgeItem", "KnowledgePlan", "KnowledgeRequest",
    "KnowledgeProvider", "KnowledgeRetriever", "NoopKnowledgeProvider",
    "RetrievalKnowledgeProvider", "RetrievalPlanner", "McpKnowledgeRetriever", "render_knowledge",
]

_MODULES = {
    "KnowledgeProvider": ".provider", "KnowledgeRetriever": ".provider",
    "NoopKnowledgeProvider": ".provider", "RetrievalKnowledgeProvider": ".provider",
    "render_knowledge": ".provider", "RetrievalPlanner": ".planner", "McpKnowledgeRetriever": ".mcp",
}


def __getattr__(name):
    if name not in _MODULES:
        raise AttributeError(name)
    value = getattr(import_module(_MODULES[name], __name__), name)
    globals()[name] = value
    return value
