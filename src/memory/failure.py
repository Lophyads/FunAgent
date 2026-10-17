"""Pure, bounded diagnostics. Never project remote messages or exception text."""
from dataclasses import asdict, replace
import re

from ..frame.contracts import ControlError, DomainError
from .contracts import MemoryFailure, MemoryIssue, MemoryRef, TOPIC_SYNC_MESSAGE

STAGES = frozenset({'request', 'embedding', 'search', 'plan', 'index', 'verify', 'topic', 'rerank'})
MESSAGES = {
    'CONFIG_CREDENTIAL': '远端记忆服务认证失败，请检查服务端凭据配置。',
    'DENIED': '记忆来源拒绝访问。',
    'UNSUPPORTED': '记忆来源不支持此操作。',
    'NOT_FOUND': '记忆记录不存在。',
    'CONFLICT': '记忆记录或版本冲突，请刷新核对。',
    'INVALID_REQUEST': '记忆请求无效。',
    'NOT_READY': '记忆来源尚未就绪。',
    'MEMORY_STORAGE': '记忆存储操作失败。',
    'MEMORY_SOURCE': '记忆来源请求失败。',
    'MEMORY_TIMEOUT': '记忆来源请求超时。',
    'MEMORY_PROTOCOL': '记忆来源响应无效。',
    'EMBEDDING_SOURCE': '向量化服务请求失败。',
    'EMBEDDING_PROTOCOL': '向量化服务响应无效。',
    'EMBEDDING_DIMENSION_MISMATCH': '实际向量维度与配置不一致。',
    'EMBEDDING_PROFILE_MISMATCH': '存量索引与当前向量配置不兼容。',
    'INDEX_NOT_READY': '索引尚未确认完成。',
    'INDEX_STATE_UNKNOWN': '无法确认当前正文版本的索引状态。',
    'RETRIEVAL_UNVERIFIED': '检索协议无法确认结果完整性，请升级协议。',
    'TOPIC_SYNC_FAILED': TOPIC_SYNC_MESSAGE,
    'PLAN_INVALID': '记忆检索规划响应无效。',
    'PLAN_FAILED': '记忆检索规划失败。',
    'RERANK_FAILED': '记忆重排失败，使用原候选顺序。',
    'INSPECTION_SOURCE': '浏览服务请求失败。',
    'INSPECTION_TIMEOUT': '浏览服务请求超时。',
    'INSPECTION_PROTOCOL': '浏览服务响应无效。',
    'INSPECTION_TOO_LARGE': '浏览响应超过容量限制。',
}
CODES = frozenset(MESSAGES)
IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


def _identifier(value):
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def ValidateIssue(issue):
    if (not isinstance(issue, MemoryIssue) or not isinstance(issue.code, str) or issue.code not in CODES or
            not isinstance(issue.stage, str) or issue.stage not in STAGES or
            not isinstance(issue.effect, str) or issue.effect not in {'none', 'known', 'unknown'} or type(issue.retryable) is not bool or
            issue.source_id is not None and not _identifier(issue.source_id) or
            issue.http_status is not None and (type(issue.http_status) is not int or not 100 <= issue.http_status <= 599) or
            any(value is not None and (type(value) is not int or not 0 <= value <= 1000000)
                for value in (issue.expected_dimensions, issue.actual_dimensions))):
        raise DomainError('MEMORY_PROTOCOL', 'Invalid memory diagnostic')
    return issue


def ValidateFailure(error):
    ValidateIssue(error.issue)
    if (len(error.related) > 16 or any(not isinstance(i, MemoryIssue) for i in error.related) or
            error.version is not None and not _identifier(error.version) or
            error.ref is not None and (not isinstance(error.ref, MemoryRef) or
                not _identifier(error.ref.source_id) or not _identifier(error.ref.record_id)) or
            error.version is not None and error.ref is None or
            error.ref is not None and error.issue.source_id not in {None, error.ref.source_id}):
        raise DomainError('MEMORY_PROTOCOL', 'Invalid memory failure reference')
    for issue in error.related:
        ValidateIssue(issue)


def FailureMessage(issue):
    ValidateIssue(issue)
    if issue.effect == 'unknown':
        return '提交结果尚未确认，请刷新核对，请勿自动重复提交。'
    if issue.effect == 'known':
        if issue.code == 'TOPIC_SYNC_FAILED':
            return TOPIC_SYNC_MESSAGE
        if issue.code in {'INDEX_NOT_READY', 'INDEX_STATE_UNKNOWN'}:
            return '正文已保存，索引未确认完成。请刷新核对，请勿自动重复提交。'
        return '正文已经变化，但操作未完整确认。请刷新核对，请勿自动重复提交。'
    return MESSAGES[issue.code]


def IssueData(issue):
    ValidateIssue(issue)
    return {key: value for key, value in asdict(issue).items() if value is not None}


def FailureData(error):
    ValidateFailure(error)
    result = dict(code=error.code, message=FailureMessage(error.issue), effect=error.issue.effect,
                  issues=[IssueData(i) for i in (error.issue, *error.related)])
    if error.ref is not None:
        result['ref'] = asdict(error.ref)
    if error.version is not None:
        result['version'] = error.version
    return result


def IssueWarnings(issues):
    return tuple(dict.fromkeys(f'{i.source_id}:{i.code}' if i.source_id else i.code
                              for i in issues if ValidateIssue(i)))


def ReadIssues(values):
    """Decode bounded wire diagnostics; external source/effect never grant authority."""
    if not isinstance(values, list) or len(values) > 16:
        raise DomainError('MEMORY_PROTOCOL', 'Invalid memory diagnostics')
    fields = set(MemoryIssue.__dataclass_fields__)
    issues = []
    for value in values:
        if not isinstance(value, dict) or set(value) - fields or not {'code', 'stage'} <= set(value):
            raise DomainError('MEMORY_PROTOCOL', 'Invalid memory diagnostic')
        # Validate before stripping the untrusted binding and mutation claim.
        issue = MemoryIssue(**value)
        issues.append(replace(issue, source_id=None, effect='none'))
    return tuple(dict.fromkeys(issues))


def BindIssue(issue, source_id, *, read=False):
    """A driver cannot assign diagnostics to a different authorized source."""
    ValidateIssue(issue)
    return replace(issue, source_id=source_id, effect='none' if read else issue.effect)


def ErrorIssue(error, *, source_id=None, stage='request', effect='none'):
    if isinstance(error, ControlError):
        raise error
    if isinstance(error, MemoryFailure):
        return BindIssue(error.issue, source_id, read=effect == 'none') if source_id else error.issue
    code = error.code if isinstance(error, DomainError) and error.code in CODES else 'MEMORY_SOURCE'
    if code.startswith('EMBEDDING_'):
        stage = 'embedding'
    return MemoryIssue(code=code, stage=stage, source_id=source_id, effect=effect)


def HTTPIssue(status, *, source, stage, mutation=False, code=None):
    """Only definite authorization rejection proves no mutation at this layer."""
    code = code or ('CONFIG_CREDENTIAL' if status == 401 else 'DENIED' if status == 403 else source)
    return MemoryIssue(code=code, stage=stage, http_status=status,
                       retryable=status in {408, 429, 502, 503, 504},
                       effect='unknown' if mutation and status not in {401, 403} else 'none')
