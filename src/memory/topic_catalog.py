"""Single-process JSON catalog. Publish immutable snapshots only after atomic persistence."""
from dataclasses import asdict, dataclass, replace
from collections.abc import Mapping
import json
import os
from pathlib import Path
from threading import RLock
import tempfile
from uuid import uuid4

from ..frame.contracts import DomainError, freeze
from .contracts import Topic, TopicChange, TopicConflict, TopicSnapshot, UNSET
from .common import NormalizeTopicName


@dataclass(frozen=True)
class _SourceState:
    snapshot: TopicSnapshot
    children: Mapping[str | None, tuple[str, ...]]
    siblings: Mapping[tuple[str | None, str], str]


def _state(snapshot):
    children, siblings = {}, {}
    for topic in snapshot.topics.values():
        children.setdefault(topic.parent_id, []).append(topic.topic_id)
        siblings[topic.parent_id, NormalizeTopicName(topic.name)] = topic.topic_id
    return _SourceState(snapshot, freeze(children), freeze(siblings))


def _source_data(snapshot):
    return {"revision": snapshot.revision,
            "topics": [asdict(topic) for topic in snapshot.topics.values()],
            "assignments": [{"record_id": record, "topic_id": topic}
                            for record, topic in snapshot.assignments.items()]}


def _text(value, maximum, field):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DomainError("INVALID_REQUEST", f"Invalid topic {field}")
    return value.strip()


def _unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate catalog field")
        data[key] = value
    return data


class JsonTopicCatalog:
    def __init__(self, path):
        self._path = Path(path)
        self._lock = RLock()
        if not self._path.exists():
            self._save({})
            self._sources = {}
        else:
            self._sources = self._load()

    def Snapshot(self, source_id):
        _text(source_id, 128, "source")
        with self._lock:
            state = self._sources.get(source_id)
            return state.snapshot if state is not None else TopicSnapshot(source_id)

    @staticmethod
    def _validate(topics):
        siblings = set()
        for key, topic in topics.items():
            _text(key, 128, "id")
            _text(topic.name, 64, "name")
            _text(topic.description, 240, "description")
            if topic.parent_id is not None and (not isinstance(topic.parent_id, str) or topic.parent_id not in topics):
                raise DomainError("NOT_FOUND", "Parent topic does not exist in this source")
            sibling = (topic.parent_id, NormalizeTopicName(topic.name))
            if sibling in siblings:
                raise TopicConflict("A topic with this name already exists under this parent")
            siblings.add(sibling)
        # Iterative walk supports deep trees without depending on Python's recursion limit.
        visited = set()
        for key in topics:
            path, node = set(), key
            while node is not None and node not in visited:
                if node in path:
                    raise TopicConflict("A topic cannot be moved into its own descendants")
                path.add(node)
                node = topics[node].parent_id
            visited.update(path)

    def _load(self):
        try:
            data = json.loads(self._path.read_text(encoding="utf8"), object_pairs_hook=_unique_object)
            if (not isinstance(data, dict) or set(data) != {"schema_version", "sources"}
                    or type(data["schema_version"]) is not int or data["schema_version"] != 1
                    or not isinstance(data["sources"], dict)):
                raise ValueError()
            sources = {}
            for name, source in data["sources"].items():
                _text(name, 128, "source")
                if (not isinstance(source, dict) or set(source) != {"revision", "topics", "assignments"}
                        or type(source["revision"]) is not int or source["revision"] < 0
                        or not isinstance(source["topics"], list) or not isinstance(source["assignments"], list)):
                    raise ValueError()
                topics, assignments = {}, {}
                for row in source["topics"]:
                    if not isinstance(row, dict) or set(row) != {"topic_id", "parent_id", "name", "description"}:
                        raise ValueError()
                    topic = Topic(**row)
                    if topic.topic_id in topics:
                        raise ValueError()
                    topics[topic.topic_id] = topic
                self._validate(topics)
                for row in source["assignments"]:
                    if not isinstance(row, dict) or set(row) != {"record_id", "topic_id"}:
                        raise ValueError()
                    _text(row["record_id"], 128, "record")
                    if row["record_id"] in assignments or row["topic_id"] not in topics:
                        raise ValueError()
                    assignments[row["record_id"]] = row["topic_id"]
                sources[name] = _state(TopicSnapshot(name, source["revision"], topics, assignments))
            return sources
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError, DomainError):
            raise DomainError("MEMORY_STORAGE", "Topic catalog is invalid or unsupported; original file preserved") from None

    def _save(self, sources):
        temporary = None
        try:
            data = {"schema_version": 1, "sources": {name: _source_data(state.snapshot)
                                                     for name, state in sources.items()}}
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf8", dir=self._path.parent,
                                             prefix=self._path.name + ".", suffix=".tmp", delete=False) as file:
                temporary = file.name
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self._path)
        except (OSError, ValueError, TypeError):
            raise DomainError("MEMORY_STORAGE", "Topic catalog could not be saved") from None
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _revision(snapshot, expected):
        if type(expected) is not int or expected < 0:
            raise DomainError("INVALID_REQUEST", "Expected revision must be a nonnegative integer")
        if expected != snapshot.revision:
            raise TopicConflict("Topic catalog changed; refresh before saving")

    @staticmethod
    def _topic(snapshot, topic_id):
        _text(topic_id, 128, "id")
        if topic_id not in snapshot.topics:
            raise DomainError("NOT_FOUND", "Topic does not exist in this source")
        return snapshot.topics[topic_id]

    def _commit(self, snapshot, topics, assignments):
        self._validate(topics)
        if topics == snapshot.topics and assignments == snapshot.assignments:
            return snapshot.revision
        changed = _state(TopicSnapshot(snapshot.source_id, snapshot.revision + 1, topics, assignments))
        sources = self._sources | {snapshot.source_id: changed}
        self._save(sources)
        self._sources = sources
        return changed.snapshot.revision

    def CreateTopic(self, source_id, parent_id, name, description):
        with self._lock:
            snapshot = self.Snapshot(source_id)
            if parent_id is not None:
                _text(parent_id, 128, "parent")
            topic = Topic("topic_" + uuid4().hex, parent_id, _text(name, 64, "name"),
                          _text(description, 240, "description"))
            state = self._sources.get(source_id)
            if state is not None and (parent_id, NormalizeTopicName(topic.name)) in state.siblings:
                raise TopicConflict("A topic with this name already exists under this parent")
            revision = self._commit(snapshot, dict(snapshot.topics) | {topic.topic_id: topic}, dict(snapshot.assignments))
            return TopicChange(topic, revision)

    def UpdateTopic(self, source_id, topic_id, expected_revision, *, name=UNSET, description=UNSET, parent_id=UNSET):
        with self._lock:
            snapshot = self.Snapshot(source_id)
            self._revision(snapshot, expected_revision)
            topic = self._topic(snapshot, topic_id)
            changes = {key: value for key, value in dict(name=name, description=description, parent_id=parent_id).items()
                       if value != UNSET}
            if not changes:
                raise DomainError("INVALID_REQUEST", "Topic update requires at least one field")
            if "name" in changes:
                changes["name"] = _text(name, 64, "name")
            if "description" in changes:
                changes["description"] = _text(description, 240, "description")
            topic = replace(topic, **changes)
            revision = self._commit(snapshot, dict(snapshot.topics) | {topic_id: topic}, dict(snapshot.assignments))
            return TopicChange(topic, revision)

    def DeleteTopic(self, source_id, topic_id, expected_revision):
        with self._lock:
            snapshot = self.Snapshot(source_id)
            self._revision(snapshot, expected_revision)
            self._topic(snapshot, topic_id)
            if self._sources[source_id].children.get(topic_id):
                raise TopicConflict("Move or delete child topics before deleting this topic")
            return self._commit(snapshot, {key: topic for key, topic in snapshot.topics.items() if key != topic_id},
                                {key: topic for key, topic in snapshot.assignments.items() if topic != topic_id})

    def Assign(self, source_id, record_id, topic_id, expected_revision=None):
        _text(record_id, 128, "record")
        with self._lock:
            snapshot = self.Snapshot(source_id)
            if expected_revision is not None:
                self._revision(snapshot, expected_revision)
            self._topic(snapshot, topic_id)
            current = snapshot.assignments.get(record_id)
            # Automatic retries preserve an existing human classification.
            if expected_revision is None and current is not None:
                return snapshot.revision
            return self._commit(snapshot, dict(snapshot.topics), dict(snapshot.assignments) | {record_id: topic_id})

    def Unassign(self, source_id, record_id):
        _text(record_id, 128, "record")
        with self._lock:
            snapshot = self.Snapshot(source_id)
            return self._commit(snapshot, dict(snapshot.topics),
                                {key: topic for key, topic in snapshot.assignments.items() if key != record_id})
