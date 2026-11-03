"""第六阶段验收：有依据的路线、显式采纳、持久任务与零重发。"""

import argparse
import json
import sys
from pathlib import Path

from easy_writing.config import load_settings
from easy_writing.services.checking import CheckingService
from easy_writing.services.planning import DemoPlanningClient, PlanningService
from easy_writing.services.tasks import TaskService
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository, uid
from easy_writing.ui.sample import import_sample
from easy_writing.usage import JsonlCallRecorder


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stage6_demo/novel.db"))
    parser.add_argument("--provider", choices=("fake", "deepseek"), default="fake")
    args = parser.parse_args()
    repo = Repository(Database(args.db))
    project = repo.create_project("第六阶段样稿-" + uid()[:8])["id"]
    settings = load_settings()
    recorder = JsonlCallRecorder(args.db.parent / "stage6_calls.jsonl", secrets=(settings.api_key.get_secret_value(),))
    sample = TaskService(repo).perform(project, uid(), "ingest", {"sample": 1}, lambda: import_sample(repo, project, recorder))["result"]
    actors = [p["id"] for p in repo.records(project, "entity") if p["kind"] == "person"]
    request = {"chapter_no": 2, "goal": "让林舟和林月进入旧宅，并强化月牙划痕的悬念", "actors": actors,
        "constraints": ["林月使用密码之前，必须先由已知密码的林舟明确告知", "铁门不可穿墙；不要完全解释月牙划痕"], "at_event": sample["event_id"]}
    if args.provider == "deepseek":
        from easy_writing.llm.deepseek import DeepSeekClient
        client = DeepSeekClient(settings.model_copy(update={"max_retries": 0, "max_output_tokens": 4096,
            "extraction_model": settings.analysis_model, "extraction_thinking": "disabled"}), recorder)
    else:
        client = DemoPlanningClient()
    before = {kind: repo.records(project, kind) for kind in ("event", "fact", "thread")}
    service, task_id = PlanningService(repo), uid()
    try:
        first = service.generate(project, task_id, request, client)
        replayed = service.generate(project, task_id, request, client)
    finally:
        client.close()
    assert replayed["cached"] and first["result"] == replayed["result"]
    plan_id = first["result"]["plan_id"]
    adopted = service.decide(project, plan_id, "adopted", route_index=0, reason="样稿作者选择第一条路线，尚未写入正文")
    assert adopted["status"] == "adopted" and len(adopted["response"]["routes"]) in (2, 3)
    try:
        service.decide(project, plan_id, "written", version_id=repo.chapters(project)[0]["latest_version_id"], reason="验证错章不能当作已写入")
    except StorageError:
        written_guard = True
    else:
        raise AssertionError("不能把第一章当成计划第二章的正文")
    for kind, rows in before.items():
        assert repo.records(project, kind) == rows
    report = CheckingService(repo).run(project)
    packet = adopted["context"]
    secret = next(f["key"] for f in packet["facts"] if f["predicate"] == "密码")
    moon = next(a for a in packet["actors"] if a["name"] == "林月")
    assert secret not in moon["known_keys"]
    acceptance = {"project_id": project, "database": str(args.db.resolve()), "plan_id": plan_id,
        "route_count": len(adopted["response"]["routes"]), "task_replay_used_cache": True,
        "adoption_did_not_publish_facts": True, "written_guard_verified": written_guard,
        "actor_knowledge_separated": True, "world_rule_count": len(packet["world_rules"]),
        "check_run_id": report["id"], "metrics": adopted["metrics"],
        "evaluation_basis": "已标注样稿、结构化引用与行动校验；自然语言效果仍需作者审阅"}
    args.db.parent.mkdir(parents=True, exist_ok=True)
    name = "stage6_live_acceptance.json" if args.provider == "deepseek" else "stage6_acceptance.json"
    (args.db.parent / name).write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.db.parent / ("stage6_live_plan.json" if args.provider == "deepseek" else "stage6_plan.json")).write_text(json.dumps(adopted, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(acceptance, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
