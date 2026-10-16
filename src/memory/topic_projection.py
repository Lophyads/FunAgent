"""Pure projections of catalog values; no storage, authorization or model calls."""
from dataclasses import asdict


def TopicData(topic):
    return asdict(topic)


def TopicPath(snapshot, topic_id):
    path = []
    while topic_id is not None:
        topic = snapshot.topics[topic_id]
        path.append(TopicData(topic))
        topic_id = topic.parent_id
    return list(reversed(path))


def RecordTopic(snapshot, record_id):
    topic_id = snapshot.assignments.get(record_id)
    return {"topic_id": topic_id, "topic_path": TopicPath(snapshot, topic_id)}


def PlanningTopics(snapshots, sources):
    result = []
    for snapshot in snapshots:
        used = set()
        for topic_id in snapshot.assignments.values():
            while topic_id is not None and topic_id not in used:
                used.add(topic_id)
                topic_id = snapshot.topics[topic_id].parent_id
        nodes = {key: {"name": topic.name, "description": topic.description, "children": []}
                 for key, topic in snapshot.topics.items() if key in used}
        roots = []
        for key, node in nodes.items():
            parent = snapshot.topics[key].parent_id
            (roots if parent is None else nodes[parent]["children"]).append(node)
        result.append({"source_id": snapshot.source_id, "kind": sources[snapshot.source_id]["kind"], "nodes": roots})
    return result
