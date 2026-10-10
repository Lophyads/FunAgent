"""Query the current published collection or an explicitly requested completed build."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from mcp import types

from src.core.query_engine.query_entrypoint import query_collection
from src.core.response.mcp_response import MCPToolResponse
from src.core.response.structured_response import StructuredMCPResponse
from src.core.settings import load_settings

logger = logging.getLogger(__name__)
TOOL_NAME = 'query_knowledge_hub'
TOOL_DESCRIPTION = 'Search a knowledge collection with semantic and keyword retrieval. Returns original evidence, citations and images.'
TOOL_INPUT_SCHEMA = {'type':'object', 'properties':{
    'query':{'type':'string', 'description':'Search question or keywords.'},
    'top_k':{'type':'integer', 'default':5, 'minimum':1, 'maximum':20},
    'collection':{'type':'string', 'description':'Published logical collection name.'},
    'build_id':{'type':'string', 'description':'Optional completed build to preview.'}}, 'required':['query']}


@dataclass
class QueryKnowledgeHubConfig:
    default_top_k: int = 5
    max_top_k: int = 20
    default_collection: str = 'default'
    enable_rerank: bool = True


class QueryKnowledgeHubTool:
    def __init__(self, settings=None, config=None):
        self._settings = settings
        self.config = config or QueryKnowledgeHubConfig()

    @property
    def settings(self):
        if self._settings is None:
            self._settings = load_settings()
        return self._settings

    async def execute(self, query, top_k=None, collection=None, build_id=None):
        if not isinstance(query, str) or not query.strip():
            raise ValueError('Query cannot be empty')
        if top_k is not None and (type(top_k) is not int or top_k < 1):
            raise ValueError('top_k must be a positive integer')
        count = min(top_k or self.config.default_top_k, self.config.max_top_k)
        target = collection or self.config.default_collection
        try:
            bundle = await asyncio.to_thread(query_collection, self.settings, query, target, count,
                use_rerank=self.config.enable_rerank, build_id=build_id)
            return StructuredMCPResponse(bundle)
        except Exception as exc:
            logger.exception('query_knowledge_hub failed')
            return MCPToolResponse(content=f'查询失败: {exc}',
                metadata={'query':query, 'collection':target, 'error':str(exc)}, is_empty=True)


_tool_instance = None


def get_tool_instance(settings=None):
    global _tool_instance
    if _tool_instance is None:
        _tool_instance = QueryKnowledgeHubTool(settings)
    return _tool_instance


async def query_knowledge_hub_handler(query, top_k=5, collection=None, build_id=None):
    try:
        response = await get_tool_instance().execute(query, top_k, collection, build_id)
        return types.CallToolResult(content=response.to_mcp_content(),
            structuredContent=response.to_structured_content(), isError='error' in response.metadata)
    except ValueError as exc:
        return types.CallToolResult(content=[types.TextContent(type='text', text=f'参数错误: {exc}')], isError=True)
    except Exception:
        logger.exception('query_knowledge_hub handler failed')
        return types.CallToolResult(content=[types.TextContent(type='text', text='内部错误: 查询处理失败')], isError=True)


def register_tool(protocol_handler):
    protocol_handler.register_tool(name=TOOL_NAME, description=TOOL_DESCRIPTION,
        input_schema=TOOL_INPUT_SCHEMA, handler=query_knowledge_hub_handler)
