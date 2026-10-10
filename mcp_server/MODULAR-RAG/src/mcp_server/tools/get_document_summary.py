"""Return document metadata from current evidence, using exact document identities."""
import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from mcp import types

from src.observability.dashboard.services.data_service import DataService

TOOL_NAME = 'get_document_summary'
TOOL_DESCRIPTION = 'Get title, summary, keywords and source information by document ID or full source hash.'
TOOL_INPUT_SCHEMA = {'type':'object', 'properties':{
    'doc_id':{'type':'string', 'description':'Exact document ID or full source hash.'},
    'collection':{'type':'string', 'description':'Published logical collection.'}}, 'required':['doc_id']}


@dataclass
class DocumentSummary:
    doc_id: str
    title: str
    summary: str
    tags: list[str] = field(default_factory=list)
    source_path: str | None = None
    chunk_count: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class GetDocumentSummaryConfig:
    default_collection: str = 'default'
    summary_max_length: int = 500


class DocumentNotFoundError(Exception):
    def __init__(self, doc_id, collection=None):
        self.doc_id, self.collection = doc_id, collection
        super().__init__(f"Document '{doc_id}' not found in collection '{collection}'")


class GetDocumentSummaryTool:
    def __init__(self, settings=None, config=None, data_service=None):
        self.config = config or GetDocumentSummaryConfig()
        self.data_service = data_service or DataService(settings=settings)

    def get_document_summary(self, doc_id, collection=None):
        if not isinstance(doc_id,str) or not doc_id.strip():
            raise ValueError('doc_id cannot be empty')
        target = collection or self.config.default_collection
        document = self.data_service.get_document_bundle(doc_id, target)
        if document is None:
            raise DocumentNotFoundError(doc_id,target)
        metadata = [u.attributes.get('llm_metadata',{}) for u in document.search_units]
        title = next((m['title'] for m in metadata if m.get('title')), Path(document.source_path).stem)
        summaries = list(dict.fromkeys(m['summary'] for m in metadata if m.get('summary')))
        summary = '\n'.join(summaries) or '\n\n'.join(e.raw_text for e in document.elements)
        tags = list(dict.fromkeys(k for m in metadata for k in m.get('keywords',[])))
        return DocumentSummary(document.doc_id,title,summary[:self.config.summary_max_length],tags,
            document.source_path,len(document.search_units),{'source_hash':document.source_hash,
            'page_count':document.page_count,'parser_version':document.parser_version})

    @staticmethod
    def format_response(summary):
        return json.dumps(summary.to_dict(),ensure_ascii=False,indent=2)

    @staticmethod
    def format_error(error):
        return str(error)

    async def execute(self, doc_id, collection=None):
        try:
            summary = await asyncio.to_thread(self.get_document_summary,doc_id,collection)
            return types.CallToolResult(content=[types.TextContent(type='text',text=self.format_response(summary))],isError=False)
        except Exception as exc:
            return types.CallToolResult(content=[types.TextContent(type='text',text=self.format_error(exc))],isError=True)


def register_tool(protocol_handler):
    tool = GetDocumentSummaryTool()
    protocol_handler.register_tool(name=TOOL_NAME,description=TOOL_DESCRIPTION,
        input_schema=TOOL_INPUT_SCHEMA,handler=tool.execute)
