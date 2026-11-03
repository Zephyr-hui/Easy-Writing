"""第五阶段入口，全部本地运行，不调用 LLM。"""

import argparse
from pathlib import Path

from easy_writing.services.checking import CheckingService, DECISIONS
from easy_writing.services.foreshadowing import ForeshadowingService, STATES

COMMANDS = {"thread", "check", "check-rule", "knowledge-use"}


def register_commands(subparsers):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path)
    common.add_argument("--env-file", type=Path)
    common.add_argument("--project", required=True)

    def group(name, help_text):
        return subparsers.add_parser(name, help=help_text).add_subparsers(dest="action", required=True)

    def page(parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--offset", type=int, default=0)

    threads = group("thread", "伏笔全量分页、作者补录与生命周期确认")
    listing = threads.add_parser("list", parents=[common])
    listing.add_argument("--state", choices=STATES)
    listing.add_argument("--chapter", type=int)
    page(listing)
    create = threads.add_parser("create", parents=[common])
    create.add_argument("--title", required=True)
    create.add_argument("--description", required=True)
    create.add_argument("--event", required=True)
    create.add_argument("--reason", required=True)
    transition = threads.add_parser("transition", parents=[common])
    transition.add_argument("--id", required=True)
    transition.add_argument("--state", choices=STATES, required=True)
    transition.add_argument("--event")
    transition.add_argument("--evidence")
    transition.add_argument("--correction", action="store_true")
    transition.add_argument("--reason", required=True)

    checks = group("check", "运行一致性检查，查看报告和作者处理记录")
    run = checks.add_parser("run", parents=[common])
    run.add_argument("--chapter", type=int)
    listing = checks.add_parser("list", parents=[common])
    page(listing)
    show = checks.add_parser("show", parents=[common])
    show.add_argument("--run", required=True)
    show.add_argument("--status", choices=DECISIONS)
    page(show)
    decide = checks.add_parser("decide", parents=[common])
    decide.add_argument("--finding", required=True)
    decide.add_argument("--status", choices=DECISIONS, required=True)
    decide.add_argument("--reason", required=True)
    page(checks.add_parser("history", parents=[common]))

    for name, label in (("check-rule", "明确世界规则的结构化约束"), ("knowledge-use", "标注人物实际使用某条知识的事件")):
        actions = group(name, label)
        add = actions.add_parser("add", parents=[common])
        add.add_argument("--fact", required=True)
        add.add_argument("--reason", required=True)
        if name == "check-rule":
            add.add_argument("--subject", required=True)
            add.add_argument("--predicate", required=True)
            add.add_argument("--operator", choices=["require", "forbid"], required=True)
            add.add_argument("--value", required=True)
            add.add_argument("--from-event")
            add.add_argument("--until-event")
        else:
            add.add_argument("--actor", required=True)
            add.add_argument("--event", required=True)
            add.add_argument("--evidence")
        page(actions.add_parser("list", parents=[common]))
        for action in ("enable", "disable"):
            edit = actions.add_parser(action, parents=[common])
            edit.add_argument("--id", required=True)
            edit.add_argument("--reason", required=True)


def handle_command(args, repo):
    if args.command == "thread":
        service = ForeshadowingService(repo)
        if args.action == "list":
            return service.list(args.project, state=args.state, through_chapter=args.chapter, limit=args.limit, offset=args.offset)
        if args.action == "create":
            return service.create(args.project, args.title, args.description, event_id=args.event, reason=args.reason)
        return service.transition(args.project, args.id, args.state, reason=args.reason, event_id=args.event, evidence_id=args.evidence, correction=args.correction)
    service = CheckingService(repo)
    if args.command == "check":
        if args.action == "run":
            return service.run(args.project, through_chapter=args.chapter)
        if args.action == "show":
            return service.report(args.project, args.run, limit=args.limit, offset=args.offset, status=args.status)
        if args.action == "list":
            return service.runs(args.project, limit=args.limit, offset=args.offset)
        if args.action == "decide":
            return service.decide(args.project, args.finding, args.status, reason=args.reason)
        return service.decision_history(args.project, limit=args.limit, offset=args.offset)
    kind = "rule" if args.command == "check-rule" else "use"
    if args.action == "list":
        return service.annotations(args.project, kind, limit=args.limit, offset=args.offset)
    if args.action in ("enable", "disable"):
        return service.enable_annotation(args.project, kind, args.id, args.action == "enable", reason=args.reason)
    if kind == "rule":
        return service.add_rule(args.project, args.fact, args.subject, args.predicate, args.operator, args.value,
            reason=args.reason, from_event=args.from_event, until_event=args.until_event)
    return service.record_use(args.project, args.fact, args.actor, args.event, reason=args.reason, evidence_id=args.evidence)
