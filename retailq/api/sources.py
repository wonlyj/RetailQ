"""Conversation-authorized immutable citation and archived source access."""
from hashlib import sha256
from pathlib import Path
import re

from fastapi import APIRouter, Request

from retailq.api.source_schemas import PublicCitation, SourceAnchor, SourceBundle, SourceDocumentView
from retailq.errors import ServiceError

router = APIRouter()


def public_citation(item):
    values = item.model_dump() if hasattr(item, 'model_dump') else dict(item)
    return PublicCitation(**{key: (str(values[key]) if key == 'chunk_id' else values[key])
                            for key in PublicCitation.model_fields})


def _repository(request):
    service = getattr(request.app.state, 'chat_service', None)
    if service is None: raise ServiceError('database_not_configured', 503, '数据库未配置。')
    return service.repository


def load_source_document(root: Path, item):
    if item.source_key == 'review':
        if item.archive or item.source_version or item.line_start is not None or item.line_end is not None:
            raise ServiceError('citation_invalid', 409, '审核知识来源格式无效。')
        return SourceDocumentView(source_key='review', source_version='', section_path=item.section_path,
                                  text=item.text, anchor=SourceAnchor(start=None, end=None))
    key, digest = item.source_key, item.source_version
    if (not key or '\\' in key or key.startswith('/') or any(p in ('', '.', '..') for p in key.split('/'))
            or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ServiceError('citation_invalid', 409, '来源标识无效。')
    expected = f'sources/{sha256(key.encode()).hexdigest()}/{digest}.md'
    if item.archive != expected or item.line_start is None or item.line_end is None:
        raise ServiceError('citation_invalid', 409, '来源归档定位无效。')
    directory = (root / 'corpus').resolve()
    target = (directory / expected).resolve()
    if not target.is_relative_to(directory): raise ServiceError('citation_invalid', 409, '来源路径无效。')
    try:
        content = target.read_bytes()
    except FileNotFoundError:
        raise ServiceError('citation_not_found', 404, '引用的历史来源归档不存在。') from None
    except OSError:
        raise ServiceError('citation_invalid', 409, '引用的历史来源归档不可读取。') from None
    try:
        text = content.decode('utf-8')
        lines = text.splitlines()
        if sha256(content).hexdigest() != digest or not 1 <= item.line_start <= item.line_end <= len(lines): raise ValueError()
        section = '\n'.join(lines[item.line_start-1:item.line_end])
        if any(line.strip() not in section for line in item.text.splitlines() if line.strip()): raise ValueError()
    except (UnicodeDecodeError, ValueError):
        raise ServiceError('citation_invalid', 409, '引用的历史来源归档与快照不一致。') from None
    return SourceDocumentView(source_key=key, source_version=digest, section_path=item.section_path,
                              text=text, anchor=SourceAnchor(start=item.line_start, end=item.line_end))


@router.get('/api/conversations/{conversation_id}/tool-calls/{tool_call_id}/sources', response_model=SourceBundle)
def sources(request: Request, conversation_id: int, tool_call_id: str, user_id: str):
    items = _repository(request).load_citation_bundle(conversation_id, user_id, tool_call_id)
    return SourceBundle(citations=[public_citation(item) for item in items])


@router.get('/api/conversations/{conversation_id}/tool-calls/{tool_call_id}/sources/{number}/document', response_model=SourceDocumentView)
def document(request: Request, conversation_id: int, tool_call_id: str, number: int, user_id: str):
    items = _repository(request).load_citation_bundle(conversation_id, user_id, tool_call_id)
    if not 1 <= number <= len(items): raise ServiceError('citation_not_found', 404, '未找到引用来源。')
    root = Path(request.app.state.settings.knowledge_data_dir)
    if not root.is_absolute(): root = Path(__file__).resolve().parents[2] / root
    return load_source_document(root, items[number-1])


def _workflow_repository(request):
    repository=getattr(request.app.state,'workflow_repository',None)
    if repository is None: raise ServiceError('workflow_not_configured',503,'工作流尚未配置。')
    return repository


@router.get('/api/conversations/{conversation_id}/workflow-turns/{turn_id}/sources',response_model=SourceBundle)
def workflow_sources(request:Request,conversation_id:int,turn_id:str,user_id:str):
    items=_workflow_repository(request).load_citations(conversation_id,user_id,'wf_'+turn_id)
    return SourceBundle(citations=[public_citation(item) for item in items])


@router.get('/api/conversations/{conversation_id}/workflow-turns/{turn_id}/sources/{number}/document',response_model=SourceDocumentView)
def workflow_document(request:Request,conversation_id:int,turn_id:str,number:int,user_id:str):
    items=_workflow_repository(request).load_citations(conversation_id,user_id,'wf_'+turn_id)
    if not 1<=number<=len(items): raise ServiceError('citation_not_found',404,'未找到引用来源。')
    root=Path(request.app.state.settings.knowledge_data_dir)
    if not root.is_absolute():root=Path(__file__).resolve().parents[2]/root
    return load_source_document(root,items[number-1])
