"""尝试级用量统计；缺失用量与未归属请求单独列出。"""

import json
from collections import defaultdict
from pathlib import Path


class StatisticsService:
    def __init__(self, repo):
        self.repo = repo

    def summarize(self, paths, *, project=None, chat=None):
        ids = set()
        if project:
            with self.repo.db.connect() as conn:
                self.repo.require(conn, "projects", project)
                ids.update(r[0] for r in conn.execute("""SELECT a.call_id FROM ingestion_attempts a JOIN ingestion_jobs j ON j.id=a.job_id
                    JOIN chapter_versions v ON v.id=j.version_id JOIN chapters c ON c.id=v.chapter_id WHERE c.project_id=?""", (project,)))
                ids.update(r[0] for r in conn.execute("""SELECT b.call_id FROM extraction_blocks b JOIN ingestion_jobs j ON j.id=b.job_id
                    JOIN chapter_versions v ON v.id=j.version_id JOIN chapters c ON c.id=v.chapter_id WHERE c.project_id=?""", (project,)))
                for r in conn.execute("SELECT result_json FROM work_tasks WHERE project_id=? AND result_json IS NOT NULL", (project,)):
                    self._ids(json.loads(r[0]), ids)
                for r in conn.execute("SELECT metrics_json FROM chapter_plans WHERE project_id=?", (project,)):
                    self._ids(json.loads(r[0]), ids)
            if chat:
                with chat.db.connect() as conn:
                    for r in conn.execute("""SELECT t.result_json,a.usage_json FROM turns t
                        JOIN sessions s ON s.library_id=t.library_id AND s.id=t.session_id
                        LEFT JOIN summary_attempts a ON a.turn_seq=t.seq
                        WHERE s.library_id=? AND s.project_id=?""", (chat.library, project)):
                        for value in r:
                            if value:
                                self._ids(json.loads(value), ids)
        groups, seen, malformed, missing, unattributed = defaultdict(list), set(), 0, [], 0
        for path in dict.fromkeys(str(Path(p).resolve()) for p in paths):
            file = Path(path)
            if not file.is_file():
                missing.append(path)
                continue
            with file.open(encoding="utf-8-sig") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict) or not isinstance(row.get("call_id"), str) or type(row.get("attempt")) is not int:
                            raise ValueError
                    except (ValueError, TypeError):
                        malformed += 1
                        continue
                    key = (row["call_id"], row["attempt"], row.get("provider"))
                    if key in seen:
                        continue
                    seen.add(key)
                    if project and row["call_id"] not in ids:
                        unattributed += 1
                        continue
                    groups[str(row.get("provider", "unknown")), str(row.get("model", "unknown"))].append(row)
        summaries = []
        for (provider, model), rows in sorted(groups.items()):
            paid = provider == "deepseek"
            entry = {"provider": provider, "model": model, "attempts": len(rows), "unique_calls": len({r["call_id"] for r in rows}),
                "failed_attempts": sum(r.get("status") != "success" for r in rows), "real_model_attempts": len(rows) if paid else 0}
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                values = [r.get("usage", {}).get(field) for r in rows if isinstance(r.get("usage", {}), dict)]
                known = [v for v in values if type(v) is int and v >= 0]
                entry["known_" + field] = sum(known) if known else None
                entry["missing_" + field + "_attempts"] = len(rows) - len(known)
            summaries.append(entry)
        return {"scope": "project_attributable_calls" if project else "all_supplied_logs", "project_id": project,
            "groups": summaries, "malformed_lines": malformed, "missing_logs": missing,
            "unattributed_attempts": unattributed, "amount_estimated": False,
            "note": "统计已提供日志中的每次尝试；未知用量不按零费用计算，缺失日志及未能归属请求另列。"}

    @staticmethod
    def _ids(value, result):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("call_id", "original_call_id") and isinstance(item, str):
                    result.add(item)
                elif key == "llm_call_ids" and isinstance(item, list):
                    result.update(x for x in item if isinstance(x, str))
                elif key in ("metrics", "result"):
                    StatisticsService._ids(item, result)
