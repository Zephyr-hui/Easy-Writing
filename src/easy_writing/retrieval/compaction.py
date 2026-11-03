"""重组已取证的上下文，丢弃冗余文本并保留可校验的出处。"""

import json

from easy_writing.retrieval.context import estimate_tokens
from easy_writing.services.chat_memory import encode


def compact_messages(system, initial, supplements, memory, seen, level):
    packet = initial | {"records": [], "sources": [], "paths": [], "truncated": True}
    sources = {}
    records = {}
    nodes, links, roots = {}, {}, []
    for source_packet in (initial, *supplements):
        for record in source_packet.get("records", []):
            records.setdefault(record["key"], record)
        for source in source_packet.get("sources", []):
            key = source["citation"], source["version_id"], source["paragraph_id"]
            sources.setdefault(key, source)
        for path in source_packet.get("paths", []):
            roots.append(path["root"])
            nodes.update((node["key"], node) for node in path["nodes"])
            links.update((link["id"], link) for link in path["links"])
    # 同时保留首批最相关证据与最后补查所得。
    values = list(records.values())
    count, text_size = ((7, 200), (4, 120), (2, 70))[level - 1]
    selection = values[:min(2, count)] + values[max(2, len(values) - (count - min(2, count))):]
    for record in selection:
        refs = [{k: v for k, v in ref.items() if k != "quote"} for ref in record.get("evidence", [])[:1]]
        start, end, total = record["text_window"]
        packet["records"].append(record | {"text": record["text"][:text_size], "evidence": refs,
                                           "text_window": [start, min(end, start + text_size), total]})
    for source in list(sources.values())[:max(1, 3 - level)]:
        text = source["text"][:text_size]
        packet["sources"].append(source | {"text": text, "truncated": source.get("truncated", False) or len(text) < len(source["text"])})
    if nodes:
        max_links = (12, 6, 2)[level - 1]
        edges = list(links.values())
        if len(edges) > max_links:
            half = max_links // 2
            edges = edges[:half] + edges[-(max_links - half):]
        used = {"event:" + link[end] for link in edges for end in ("source", "target")}
        used.update(list(nodes)[:2])
        visible = [node for key, node in nodes.items() if key in used]
        packet["paths"] = [{"root": roots[0], "nodes": visible, "links": edges, "truncated": True}]
    compact = {key: value for key, value in memory.items() if key not in ("recent", "summary", "rolling")}
    compact["directives"] = compact.get("directives", [])[-max(1, 4 - level):]
    compact["rolling"] = []
    for rollup in memory.get("rolling", [])[:max(1, 3 - level)]:
        compact["rolling"].append({k: v for k, v in rollup.items() if k in ("turn", "focus", "goal", "constraints")}
                                  | {k: rollup.get(k, [])[:1] for k in ("proposals", "unresolved")})
    if level == 1:
        compact["recent"] = memory.get("recent", [])[-1:]
    progress = []
    for signature in seen[-(5 if level == 1 else 2):]:
        name, args = json.loads(signature)
        progress.append({"工具": name, "对象": str(args.get("event_id") or args.get("query") or "")[:75]})
    return [{"role": "system", "content": system},
            {"role": "user", "content": encode(packet)},
            {"role": "user", "content": encode({"会话记忆": compact, "已完成的补查": progress})}], packet


def request_size(messages, tools, output_reserve):
    return estimate_tokens({"messages": messages, "tools": tools}) + output_reserve
