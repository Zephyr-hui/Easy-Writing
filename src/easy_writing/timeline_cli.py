"""第三阶段本地命令；全部为数据库操作，不发起模型请求。"""

import argparse
from pathlib import Path

from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.timeline import RELATIONS, TimelineService

COMMANDS = {"timeline", "characters", "relations", "state", "query", "event-time", "event-link",
            "event-neighbors", "event-merge", "plot", "fact-context", "learn"}


def register_commands(subparsers):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path)
    common.add_argument("--env-file", type=Path)
    def command(name, help_text):
        return subparsers.add_parser(name, parents=[common], help=help_text)
    def scoped(parser):
        parser.add_argument("--project", required=True)
        parser.add_argument("--chapter", type=int, help="正文揭示边界；留空为作者视角")
    timeline = command("timeline", "查看故事偏序或正文提及顺序")
    scoped(timeline)
    timeline.add_argument("--order", choices=["story", "narrative"], default="story")
    scoped(command("characters", "查看当前可见人物与别名"))
    relations = command("relations", "查看有事实依据的人物关系")
    scoped(relations)
    relations.add_argument("--at-event")
    for name, help_text in (("state", "查询指定时点的历史状态或角色认知"), ("query", "基于知识与关键词离线回答问题")):
        parser = command(name, help_text)
        scoped(parser)
        parser.add_argument("--subject")
        parser.add_argument("--predicate")
        parser.add_argument("--actor")
        parser.add_argument("--at-event")
        parser.add_argument("--limit", type=int, default=10 if name == "query" else 100)
        if name == "query":
            parser.add_argument("--question", required=True)
        else:
            parser.add_argument("--offset", type=int, default=0)
            parser.add_argument("--confirmed-only", action="store_true")
    event_time = command("event-time", "标注事件的数值时间或清除时间")
    event_time.add_argument("--event", required=True)
    event_time.add_argument("--start", type=float)
    event_time.add_argument("--end", type=float)
    event_time.add_argument("--frame", default="main")
    event_time.add_argument("--revealed-at", type=int)
    event_time.add_argument("--reason", required=True)
    link = command("event-link", "建立或调整候选/确认事件连接")
    link.add_argument("--source", required=True)
    link.add_argument("--target", required=True)
    link.add_argument("--relation", choices=sorted(RELATIONS), required=True)
    link.add_argument("--status", choices=["confirmed", "unconfirmed", "rejected"], default="confirmed")
    link.add_argument("--revealed-at", type=int)
    link.add_argument("--reason", required=True)
    neighbors = command("event-neighbors", "展开前驱、后继与关联路径")
    scoped(neighbors)
    neighbors.add_argument("--event", required=True)
    neighbors.add_argument("--direction", choices=["in", "out", "both"], default="both")
    neighbors.add_argument("--relation", choices=sorted(RELATIONS))
    neighbors.add_argument("--depth", type=int, default=1)
    neighbors.add_argument("--max-nodes", type=int, default=100)
    merge = command("event-merge", "对齐同一事件的不同提及，保留来源")
    merge.add_argument("--source", required=True)
    merge.add_argument("--target", required=True)
    merge.add_argument("--revealed-at", type=int)
    merge.add_argument("--reason", required=True)
    context = command("fact-context", "设置事实生效边界及状态/命题模式")
    context.add_argument("--id", required=True)
    context.add_argument("--from-event")
    context.add_argument("--until-event")
    context.add_argument("--mode", choices=["state", "assertion"], default="state")
    context.add_argument("--reason", required=True)
    learn = command("learn", "记录角色何时获知某条事实")
    learn.add_argument("--fact", required=True)
    learn.add_argument("--actor", required=True)
    learn.add_argument("--event", required=True)
    learn.add_argument("--revealed-at", type=int)
    learn.add_argument("--reason", required=True)
    plot = subparsers.add_parser("plot", help="剧情线与核心/普通节点角色")
    actions = plot.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", parents=[common])
    create.add_argument("--project", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--description", default="")
    listing = actions.add_parser("list", parents=[common])
    listing.add_argument("--project", required=True)
    view = actions.add_parser("view", parents=[common])
    view.add_argument("--plot", required=True)
    view.add_argument("--expand", action="store_true")
    view.add_argument("--max-paths", type=int, default=100)
    view.add_argument("--max-depth", type=int, default=30)
    role = actions.add_parser("role", parents=[common])
    role.add_argument("--plot", required=True)
    role.add_argument("--event", required=True)
    role.add_argument("--role", choices=["core", "ordinary"], required=True)
    role.add_argument("--reason", required=True)


def handle_command(args, repo):
    graph, knowledge = TimelineService(repo), KnowledgeService(repo)
    if args.command == "timeline":
        return graph.timeline(args.project, order=args.order, through_chapter=args.chapter)
    if args.command == "characters":
        return knowledge.characters(args.project, through_chapter=args.chapter)
    if args.command == "relations":
        return knowledge.relations(args.project, at_event=args.at_event, through_chapter=args.chapter)
    if args.command in ("state", "query"):
        options = dict(subject=args.subject, predicate=args.predicate, actor=args.actor, at_event=args.at_event,
                       through_chapter=args.chapter, limit=args.limit)
        if args.command == "query":
            return knowledge.answer(args.project, args.question, **options)
        return knowledge.facts(args.project, confirmed_only=args.confirmed_only, offset=args.offset, **options)
    if args.command == "event-time":
        return graph.set_time(args.event, start=args.start, end=args.end, frame=args.frame, revealed_at_chapter=args.revealed_at, reason=args.reason)
    if args.command == "event-link":
        return graph.link(args.source, args.target, args.relation, status=args.status, revealed_at_chapter=args.revealed_at, reason=args.reason)
    if args.command == "event-neighbors":
        return graph.neighbors(args.project, args.event, direction=args.direction, relation=args.relation,
                               depth=args.depth, max_nodes=args.max_nodes, through_chapter=args.chapter)
    if args.command == "event-merge":
        return graph.merge_events(args.source, args.target, reason=args.reason, revealed_at_chapter=args.revealed_at)
    if args.command == "fact-context":
        return knowledge.bind_fact(args.id, from_event=args.from_event, until_event=args.until_event, mode=args.mode, reason=args.reason)
    if args.command == "learn":
        return knowledge.learn(args.fact, args.actor, args.event, revealed_at_chapter=args.revealed_at, reason=args.reason)
    if args.action == "create":
        return graph.create_plot(args.project, args.name, args.description)
    if args.action == "list":
        return graph.plots(args.project)
    if args.action == "role":
        return graph.set_role(args.plot, args.event, args.role, reason=args.reason)
    return graph.plot_view(args.plot, expanded=args.expand, max_paths=args.max_paths, max_depth=args.max_depth)
