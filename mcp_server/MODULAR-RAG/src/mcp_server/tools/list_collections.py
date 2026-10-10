"""List published logical knowledge collections, excluding physical/staging stores."""
import asyncio
import json
from dataclasses import asdict, dataclass

from mcp import types

from src.observability.dashboard.services.data_service import DataService

TOOL_NAME = 'list_collections'
TOOL_DESCRIPTION = 'List published knowledge collections and their chunk counts.'
TOOL_INPUT_SCHEMA = {'type':'object', 'properties':{
    'include_stats':{'type':'boolean', 'default':True}}, 'required':[]}


@dataclass
class CollectionInfo:
    name: str
    count: int | None = None
    metadata: dict | None = None

    def to_dict(self):
        return {key:value for key,value in asdict(self).items() if value is not None}


@dataclass
class ListCollectionsConfig:
    include_stats_default: bool = True


class ListCollectionsTool:
    def __init__(self, settings=None, config=None, data_service=None):
        self.config = config or ListCollectionsConfig()
        self.data_service = data_service or DataService(settings=settings)

    def list_collections(self, include_stats=True):
        result = []
        for name in self.data_service.list_collections():
            try:
                stats = self.data_service.get_collection_stats(name) if include_stats else None
            except Exception as exc:
                result.append(CollectionInfo(name, metadata={'status':'unavailable', 'error':str(exc)}))
                continue
            result.append(CollectionInfo(name, stats['chunk_count'] if stats else None,
                                         stats if stats else None))
        return result

    def format_response(self, collections, include_stats=True):
        return json.dumps({'collections':[c.to_dict() for c in collections], 'count':len(collections)},
                          ensure_ascii=False, indent=2)

    async def execute(self, include_stats=None):
        enabled = self.config.include_stats_default if include_stats is None else include_stats
        try:
            collections = await asyncio.to_thread(self.list_collections, enabled)
            return types.CallToolResult(content=[types.TextContent(type='text',
                text=self.format_response(collections, enabled))], isError=False)
        except Exception as exc:
            return types.CallToolResult(content=[types.TextContent(type='text',text=f'集合列表读取失败: {exc}')], isError=True)


def register_tool(protocol_handler):
    tool = ListCollectionsTool()
    protocol_handler.register_tool(name=TOOL_NAME, description=TOOL_DESCRIPTION,
        input_schema=TOOL_INPUT_SCHEMA, handler=tool.execute)
