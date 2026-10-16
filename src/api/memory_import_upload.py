"""Bounded in-memory multipart adapter, exclusively for memory imports."""
from email.parser import BytesParser
from email.policy import default
from ..frame.contracts import DomainError
from ..memory.memory_import import MAX_FILE


def ParseUpload(content_type, raw):
    def invalid():
        raise DomainError('INVALID_REQUEST', 'Invalid import form')
    if '\r' in content_type or '\n' in content_type:
        invalid()
    message = BytesParser(policy=default).parsebytes(
        b'Content-Type: ' + content_type.encode('ascii') + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw)
    if message.get_content_type() != 'multipart/form-data' or not message.is_multipart() or message.defects:
        invalid()
    fields = {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if (part.defects or part.is_multipart() or part.get_content_disposition() != 'form-data'
                or len(part.get_all('Content-Disposition', [])) != 1
                or name not in {'source_id', 'file'} or name in fields
                or part.get('Content-Transfer-Encoding') is not None):
            invalid()
        value = part.get_payload(decode=True)
        if not isinstance(value, bytes):
            invalid()
        if name == 'source_id':
            if part.get_filename() is not None:
                invalid()
            try:
                value = value.decode('utf-8')
            except UnicodeError:
                invalid()
            if not value.strip() or len(value) > 128:
                invalid()
        elif part.get_filename() is None:
            invalid()
        elif len(value) > MAX_FILE:
            raise DomainError('PAYLOAD_TOO_LARGE', 'Import file exceeds 2 MiB')
        fields[name] = value
    if set(fields) != {'source_id', 'file'}:
        invalid()
    return fields
