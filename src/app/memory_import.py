"""Upload through the application API; never open memory stores in this process."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--file', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    def request(path, data=None, content_type=None):
        req = Request(args.server.rstrip('/') + path, data=data,
                      headers={'Content-Type': content_type} if content_type else {})
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    try:
        raw = Path(args.file).read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('File exceeds 2 MiB')
        boundary = uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="source_id"\r\n\r\n{args.source_id}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="import.json"\r\n'
                'Content-Type: application/json\r\n\r\n').encode() + raw + f'\r\n--{boundary}--\r\n'.encode()
        try:
            result = request('/memory/imports', body, 'multipart/form-data; boundary=' + boundary)
        except HTTPError:
            # A server rejection is definitive, including when an older receipt exists.
            raise
        except (URLError, TimeoutError):
            result = request('/memory/imports?' + urlencode({'source_id': args.source_id}))
            if not result or result['input_sha256'] != hashlib.sha256(raw).hexdigest():
                raise ValueError('Submission unconfirmed; check latest import before retrying')
        while result['status'] == 'running':
            time.sleep(1)
            result = request('/memory/imports/' + result['import_id'])
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if result['status'] == 'completed' else 1
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
