"""Standalone offline review: python -m src.api.trace_cli --directory ... task."""
import argparse
import json
from pathlib import Path
import sys

from ..frame.contracts import DomainError
from ..observability.trace_reader import TraceReader


def Show(reader, task_id, format='view', output=None):
    output = output or sys.stdout
    review = reader.Read(task_id)
    if format == 'path':
        text = review['path']
    elif format == 'markdown':
        text = reader.Markdown(review)
    elif format == 'json':
        text = reader.JSON(review)
        if text is None:
            raise DomainError('NOT_FOUND', 'No readable saved trace')
    else:
        text = json.dumps(reader.View(review), ensure_ascii=False, indent=2)
    print(text, file=output)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Review saved traces offline; no application startup.')
    parser.add_argument('--directory', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    task = commands.add_parser('task')
    task.add_argument('task_id')
    task.add_argument('--format', choices=('view', 'json', 'markdown', 'path'), default='view')
    listing = commands.add_parser('list')
    listing.add_argument('--session-id')
    listing.add_argument('--since', type=float)
    listing.add_argument('--until', type=float)
    listing.add_argument('--limit', type=int, default=50)
    listing.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        reader = TraceReader(args.directory)
        if args.command == 'task':
            Show(reader, args.task_id, args.format)
        else:
            print(json.dumps(reader.List(session_id=args.session_id, since=args.since, until=args.until,
                                         limit=args.limit, offset=args.offset), ensure_ascii=False, indent=2))
    except DomainError as error:
        print(error.code + ': ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
