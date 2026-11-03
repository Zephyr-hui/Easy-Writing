"""把知识与路线资料转成工作台阅读文本。"""

import re
from collections import defaultdict


PLAN_STATUS = {"draft": "待采纳", "adopted": "已采纳", "written": "已写入正文", "rejected": "已舍弃"}
RECORD_LABELS = {"entity": "人物与实体", "event": "事件", "fact": "事实与关系", "thread": "伏笔",
                 "summary": "章节摘要", "link": "事件连接"}
REVIEW_STATUS = {"unconfirmed": "待核对", "confirmed": "已确认", "rejected": "已否决"}
THREAD_STATUS = {"candidate": "候选伏笔", "planted": "已埋下", "reinforced": "已强化",
                 "partially_resolved": "部分回收", "resolved": "已回收", "cancelled": "已取消", "open": "开放保留"}
LINK_TYPES = {"before": "先于", "overlaps": "时间重叠", "during": "发生于", "causes": "导致",
              "foreshadows": "铺垫", "resolves": "回收"}
MENTION_TYPES = {"occurrence": "实际发生", "recollection": "回忆", "retelling": "转述", "uncertain": "待核对"}
FINDING_STATUS = {"open": "待处理", "ignored": "暂不处理", "intentional": "有意安排", "resolved": "已处理"}
FINDING_CERTAINTY = {"confirmed_conflict": "已确认冲突", "needs_review": "待核查", "suggestion": "规则建议"}
REPAIR_TERMS = {"intentional": "有意安排", **THREAD_STATUS}


def record_title(kind, record):
    if kind == "fact":
        return f"{record.get('subject_name', '未命名对象')} · {record['predicate']}"
    if kind == "summary":
        return f"第 {record['chapter_no']} 章摘要"
    if kind == "link":
        return "事件关系 · " + LINK_TYPES.get(record["relation_type"], "待核对")
    return record.get("name") or record.get("title") or "知识记录"


def reference_label(ref):
    paragraph = ref.get("paragraph_id", "")
    location = f"第 {ref['chapter_no']} 章"
    if paragraph.startswith("p") and paragraph[1:].isdigit():
        location += f" · 原文片段 {int(paragraph[1:])}"
    return location


def knowledge_sections(kind, record, refs, endpoints=None):
    review = REVIEW_STATUS.get(record.get("review_status"), "")
    body = [review] if review else []
    if record.get("requires_review"):
        body.append("改稿后待复核")
    if kind == "entity":
        types = {"person": "人物", "item": "物品", "place": "地点", "concept": "概念"}
        body.append("类型：" + types.get(record.get("kind"), "其他实体"))
        if record.get("aliases"):
            body.append("别名：" + "、".join(record["aliases"]))
    elif kind == "event":
        body.extend(["故事时间：" + (record.get("story_time") or "未标注"), record["summary"]])
    elif kind == "fact":
        body.append("记录内容：" + (record.get("object_entity_name") or record["object_value"]))
        if record.get("certainty") == "inferred":
            body.append("这条事实为推断。")
    elif kind == "thread":
        body.extend([THREAD_STATUS.get(record.get("伏笔状态") or record.get("state"), "待核对"), record["description"]])
    elif kind == "summary":
        body.append(record["text"])
    elif kind == "link":
        names = endpoints or {}
        body.append(" · ".join((names.get(record["from_event_id"], "未找到起点事件"),
                                LINK_TYPES.get(record["relation_type"], "待核对"),
                                names.get(record["to_event_id"], "未找到终点事件"))))

    evidence = []
    for ref in refs:
        location = reference_label(ref)
        if not ref.get("is_latest_source", True):
            location += f" · 历史版本 {ref['revision']}"
        for source in ref.get("document_sources", ()):
            suffix = {"page": "页", "paragraph": "段"}.get(source.get("unit"))
            mark = f"第 {source['number']} {suffix}" if suffix else "作者校订" if source.get("method") == "author_edited" else ""
            location += " · " + source["filename"] + (f"（{mark}）" if mark else "")
        evidence.append(f"{location}\n「{ref['quote']}」")
    return [(record_title(kind, record), "\n".join(body)),
            ("原文依据", "\n\n".join(dict.fromkeys(evidence)) or "暂无可查看的原文依据。")]


def timeline_sections(mode, result):
    if mode == "relations":
        sections = []
        for fact in result["relations"]:
            sources = list(dict.fromkeys(reference_label(ref) for ref in fact["evidence"]))
            detail = REVIEW_STATUS.get(fact.get("review_status"), "")
            if fact.get("time_uncertain"):
                detail += " · 时间待核对"
            sections.append((f"{fact['subject_name']} · {fact['predicate']} · {fact['object_name']}",
                             "\n".join(filter(None, [detail, "出处：" + "、".join(sources) if sources else ""]))))
        return sections or [("人物关系", "暂无可查看的关系。")]
    if mode == "narrative":
        chapters = defaultdict(list)
        for item in result["mentions"]:
            kind = MENTION_TYPES.get(item["mention_kind"], "待核对")
            chapters[item["chapter_no"]].append(f"{item['title']}（{kind}） · {reference_label(item)}")
        return [(f"第 {chapter} 章", "\n".join(mentions)) for chapter, mentions in chapters.items()] or [
            ("正文揭示顺序", "暂无事件提及。")]

    events = {event["id"]: event for event in result["events"]}
    if not events:
        return [("故事时间线", "暂无可查看的事件。")]
    sections = [("故事时间线", "分组依据已确认的先后关系。缺少关联的事件仍可能无法互相排序。")]
    for number, group in enumerate(result["unordered_groups"], 1):
        items = []
        for key in group:
            event = events[key]
            time = event.get("story_time") or "时间未标注"
            annotation = event.get("time")
            if annotation and annotation.get("start_tick") is not None:
                start, end = annotation["start_tick"], annotation.get("end_tick")
                time = f"故事时间点 {start}" if end is None or end == start else f"故事时间点 {start} 至 {end}"
            lines = [f"{event['title']}（{time}）", event["summary"]]
            if event["mentions"]:
                lines.append("首次提及：" + reference_label(event["mentions"][0]))
            items.append("\n".join(lines))
        sections.append((f"事件组 {number}", "\n\n".join(items)))
    links = [f"{events[link['source']]['title']} · {LINK_TYPES.get(link['relation_type'], '关联')} · {events[link['target']]['title']}"
             for link in result["links"] if link["review_status"] == "confirmed"]
    if links:
        sections.append(("已确认的事件关系", "\n".join(links)))
    return sections


def route_title(index, route):
    return f"路线 {index + 1} · {route['title']}"


def revision_sections(status):
    sections = [("改稿复核", f"待复核 {status['pending_total']} 条，需刷新摘要 {status['stale_summary_count']} 章。\n"
                             f"本作品登记了 {status['dependency_count']} 条知识依赖。")]
    if not status["reviews"]:
        sections.append(("待复核记录", "当前没有待复核项。"))
    for i, review in enumerate(status["reviews"], 1):
        lines = []
        for side, label in (("source", "变更来源"), ("target", "受影响记录")):
            record = review[f"{side}_record"]
            kind = RECORD_LABELS.get(review[f"{side}_type"], "知识记录")
            details = [kind]
            if record:
                if "chapter_no" in record:
                    details.append(f"第 {record['chapter_no']} 章")
                    if not record["is_current"]:
                        details.append(f"历史版本 {record['revision']}")
                if record["title"]:
                    details.append(record["title"])
            else:
                details.append("当前无法定位")
            lines.append(label + "　" + " · ".join(details))
        lines.extend([review["reason"], f"复核编号（供命令行处理）　{review['id']}"])
        sections.append((f"待复核 {i}", "\n".join(lines)))
    if status["next_offset"] is not None:
        sections.append(("显示范围", f"此处显示前 {len(status['reviews'])} 条待复核记录。"))
    coverage = status["coverage"]
    loaded = "、".join(map(str, coverage["loaded_chapters"]))
    scope = [f"已入库　第 {loaded} 章" if loaded else "暂无已入库章节。"]
    for key, message in (("not_current", "知识尚未更新"), ("quarantined_chapters", "已暂时移出模型的知识查询范围")):
        chapters = "、".join(map(str, coverage[key]))
        if chapters:
            scope.append(f"第 {chapters} 章{message}。")
    gaps = [str(r["start"]) if r["start"] == r["end"] else f"{r['start']}–{r['end']}"
            for r in coverage["missing_chapter_ranges"]]
    if gaps:
        scope.append("缺少的章　" + "、".join(gaps))
    sections.append(("章节范围", "\n".join(scope)))
    views = []
    for view in status["derived_views"]:
        label = "故事时间线" if view["name"] == "timeline" else f"情节线「{view['plot_title']}」" if view.get("plot_title") else "派生视图"
        views.append(label + " · " + ("待刷新" if view["stale"] else "已更新"))
    if views:
        sections.append(("派生资料状态", "\n".join(views)))
    return sections


def refreshed_sections(result):
    rebuilt = "、".join(map(str, result["rebuilt_summary_chapters"]))
    deferred = "、".join(map(str, result["deferred_summary_chapters"]))
    lines = [f"已重建第 {rebuilt} 章的摘要。" if rebuilt else "本次没有需要重建的可用摘要。",
             f"已更新 {len(result['views'])} 个派生视图，并生成一份本地检查报告。"]
    if deferred:
        lines.append(f"第 {deferred} 章仍有待复核记录，摘要暂缓重建。")
    return [("派生资料刷新完成", "\n".join(lines))]


def export_sections(result):
    counts = [f"{RECORD_LABELS.get(kind, '知识记录')} {count} 条" for kind, count in result["record_counts"].items()]
    source = "已包含章节原文。" if result["includes_sources"] else "已导出知识记录及其出处。"
    return [("作品知识已导出", "\n".join([f"文件位置　{result['export_path']}", "、".join(counts), source,
                                         "完整恢复作品请使用数据库备份。"]))]


def archive_sections(result, *, chat, restore):
    label = "会话库" if chat else "小说库"
    if restore:
        path, meta = result["restored_path"], result["verified"]
        title = f"{label}已恢复到新文件"
        next_step = "如需将恢复的文件加入作品目录，可使用「迁移旧库／导入备份」选择配套的两个文件。"
    else:
        path, meta = result["backup_path"], result["metadata"]
        title = f"{label}备份完成"
        next_step = f"校验清单　{path}.json"
    return [(title, f"文件位置　{path}\n数据库版本　{meta['schema_version']}\n数据完整性和关联校验均已通过。\n{next_step}")]


def report_sections(report, offset=0):
    coverage, examined = report["coverage"], report["examined"]
    chapters = coverage["loaded_chapters"]
    visible = "、".join(map(str, chapters)) if len(chapters) <= 16 else "、".join(map(str, chapters[:16])) + f" 等 {len(chapters)} 章"
    lines = [f"检查时间　{report['created_at'][:19].replace('T', ' ')}",
             f"已入库范围　{('第 ' + visible + ' 章') if visible else '暂无已入库章节'}",
             f"检查了 {examined['facts']} 条事实、{examined['events']} 个事件和 {examined['threads']} 条伏笔。",
             f"发现 {report['finding_total']} 项记录，其中 {report['open_issues']} 项疑点待处理。"]
    if report.get("stale"):
        lines.insert(0, "本报告基于旧版知识，请重新检查当前内容。")
    for key, label in (("not_current", "知识未更新的章"), ("quarantined_chapters", "待复核而暂时隔离的章")):
        if coverage[key]:
            lines.append(label + "　" + "、".join(map(str, coverage[key])))
    if coverage["missing_chapter_ranges"]:
        gaps = [str(r["start"]) if r["start"] == r["end"] else f"{r['start']}–{r['end']}"
                for r in coverage["missing_chapter_ranges"]]
        lines.append("缺少的章　" + "、".join(gaps))
    if coverage["pending_review_count"]:
        lines.append(f"另有 {coverage['pending_review_count']} 条知识记录待复核。")
    if report.get("temporal_dependent_checks_skipped"):
        lines.append("时间线存在冲突，部分依赖时间顺序的检查已跳过。")
    if any(report["skipped_annotations"].values()):
        skipped = report["skipped_annotations"]
        lines.append(f"跳过 {skipped['uses']} 条认知标注和 {skipped['rules']} 条世界规则标注，需核对相关依据。")
    sections = [("检查概览", "\n".join(lines))]
    advice = report.get("model_advice")
    sections.append(("模型建议 · 供作者参考", "\n\n".join([advice["overview"], *("• " + s for s in advice["suggestions"])])
                     if advice else "选用 DeepSeek 后可生成修改与核查建议。"))
    for i, finding in enumerate(report["findings"], offset + 1):
        details = [FINDING_CERTAINTY[finding["certainty"]] + " · " + FINDING_STATUS[finding["status"]]]
        for side in finding["evidence_sides"]:
            details.extend(["", side["label"] + (f" · {side['value']}" if side.get("value") else "")])
            details.extend(f"{reference_label(ref)}\n「{ref['quote']}」" for ref in side["evidence"])
        if finding["conditions"]:
            details.extend(["", "判断依据", *("• " + line for line in finding["conditions"])])
        if finding["repair_suggestions"]:
            details.extend(["", "处理建议", *("• " + re.sub(r"\b[a-z_]+\b", lambda m: REPAIR_TERMS.get(m[0], m[0]), line)
                                               for line in finding["repair_suggestions"])])
        if finding["decision_reason"]:
            details.extend(["", "作者备注", finding["decision_reason"]])
        sections.append((f"{i}. {finding['title']}", "\n".join(details)))
    if not report["findings"]:
        sections.append(("检查疑点", "这一页没有疑点。" if report["finding_total"] else "在已检查的知识中未发现规则疑点。"))
    if report.get("next_offset") is not None:
        sections.append(("后续疑点", f"将疑点起始位置设为 {report['next_offset']} 并刷新，即可查看下一页。"))
    states = "、".join(f"{THREAD_STATUS.get(key, '待核对')} {count} 条" for key, count in report["thread_state_counts"].items())
    sections.append(("伏笔与检查范围", "\n".join([states or "暂无可检查的伏笔。", "尚未回收的伏笔可由作者继续保留。",
                                                  *report["limits"]])))
    return sections


def plan_sections(plan):
    request, context = plan["request"], plan["context"]
    actors = {person["id"]: person["name"] for person in context["actors"]}
    entries = {entry["key"]: entry for kind in ("facts", "threads", "world_rules") for entry in context[kind]}
    operators = {"require": "必须为", "forbid": "禁止为"}
    names = {entry["subject_id"]: entry["subject"] for entry in entries.values() if "subject_id" in entry} | actors

    def describe(key):
        item = entries.get(key)
        if not item:
            return "待核对的依据"
        if "title" in item:
            return item["title"]
        operator = operators.get(item.get("operator"), "")
        return f"{item['subject']} · {item['predicate']}：{operator}{item['value']}"

    overview = ["创作目标：" + request["goal"], "状态：" + PLAN_STATUS.get(plan["status"], "待处理")]
    if request["constraints"]:
        overview.append("约束：" + "；".join(request["constraints"]))
    if plan.get("stale"):
        overview.append("计划依据已有变化，请先核对当前知识。")
    if plan.get("history"):
        overview.append("最近处理原因：" + plan["history"][-1]["reason"])
    sections = [(f"第 {request['chapter_no']} 章 · 写作方案", "\n".join(overview))]
    impacts = {"deepen": "深化", "partial_payoff": "部分回收", "payoff": "回收", "leave_open": "保留现状"}
    for i, route in enumerate(plan["response"]["routes"]):
        steps = []
        for n, step in enumerate(route["steps"], 1):
            steps.append(f"{n}. {step['action']}")
            participants = "、".join(actors[a] for a in step["actors"] if a in actors)
            if participants:
                steps.append("   涉及人物：" + participants)
            for use in step["uses"]:
                steps.append(f"   {actors.get(use['actor_id'], '人物')}使用线索：{describe(use['fact_key'])}")
            for learn in step["learns"]:
                steps.append(f"   {actors.get(learn['actor_id'], '人物')}从{actors.get(learn['source_actor_id'], '知情人')}获知：{describe(learn['fact_key'])}")
            for assertion in step["assertions"]:
                subject = names.get(assertion["subject_id"])
                if subject is None:
                    subject = describe(assertion["subject_id"]) if assertion["subject_id"] in entries else "相关设定"
                steps.append(f"   设定条件：{subject} · {assertion['predicate']}：{assertion['value']}")
        lines = [route["synopsis"], "", "必要铺垫", *["• " + text for text in route["preparations"]], "", "场景步骤", *steps]
        if route["thread_impacts"]:
            lines.extend(["", "伏笔安排", *(f"• {describe(t['thread_key'])}（{impacts.get(t['action'], '待核对')}）：{t['explanation']}"
                                              for t in route["thread_impacts"])])
        lines.extend(["", "人物变化"])
        for arc in route["character_arcs"]:
            lines.append(f"• {actors.get(arc['actor_id'], '人物')}：{arc['basis']} → {arc['proposed_shift']}")
        lines.extend(["", "参考依据"])
        for key in route["citations"]:
            sources = list(dict.fromkeys(reference_label(ref) for ref in entries.get(key, {}).get("evidence", ())))
            lines.append("• " + describe(key) + ("（" + "；".join(sources) + "）" if sources else ""))
        title = route_title(i, route) + (" · 已选" if i == plan.get("selected_route") else "")
        sections.append((title, "\n".join(lines)))
    if plan["response"]["caveats"]:
        sections.append(("需留意", "\n".join("• " + item for item in plan["response"]["caveats"])))
    return sections
