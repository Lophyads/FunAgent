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
        result.append({"source_id": snapshot.source_id, "kind": sources[snapshot.source_id]["kind"],
                       "source_description": sources[snapshot.source_id].get("source_description", ""),
                       "nodes": roots})
    return result


def MemoryTopicPaths(topics):
    """Full paths visible to the planner, restricted to fact-memory sources."""
    paths = set()
    pending = [(node, ()) for source in topics if source["kind"] == "episodic" for node in source["nodes"]]
    while pending:
        node, parents = pending.pop()
        names = (*parents, node["name"])
        paths.add(" / ".join(names))
        pending.extend((child, names) for child in node["children"])
    return paths


def ResolveMemoryTopics(snapshots, sources, selected):
    """Resolve selected paths and their descendants within each source snapshot."""
    selected = set(selected)
    allowed = {}
    for snapshot in snapshots:
        if sources[snapshot.source_id]["kind"] != "episodic":
            continue
        roots = {key for key in snapshot.topics
                 if " / ".join(topic["name"] for topic in TopicPath(snapshot, key)) in selected}
        children = {}
        for key, topic in snapshot.topics.items():
            children.setdefault(topic.parent_id, []).append(key)
        pending, included = list(roots), set()
        while pending:
            key = pending.pop()
            if key not in included:
                included.add(key)
                pending.extend(children.get(key, ()))
        allowed[snapshot.source_id] = frozenset(included)
    return allowed
