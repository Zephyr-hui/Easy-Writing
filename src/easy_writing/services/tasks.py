"""持久任务门闩：重运行只读取结果，失败和中断不自动重发。"""

import json

from easy_writing.services.review_scope import page_bounds, text_value
from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import digest, now


class TaskService:
    def __init__(self, repo):
        self.repo = repo

    def perform(self, project_id, task_id, kind, payload, operation, *, on_start=None):
        text_value(task_id, "任务编号", 100)
        if kind not in ("ingest", "research", "plan"):
            raise StorageError("任务类型无效。")
        fingerprint = digest(json.dumps([kind, payload], ensure_ascii=False, sort_keys=True))
        with self.repo.db.transaction() as conn:
            self.repo.require(conn, "projects", project_id)
            previous = conn.execute("SELECT * FROM work_tasks WHERE id=?", (task_id,)).fetchone()
            if previous:
                if previous["project_id"] != project_id or previous["kind"] != kind or previous["request_hash"] != fingerprint:
                    raise StorageError("任务编号已绑定其他项目或请求，请明确新建任务。")
                if previous["status"] == "succeeded":
                    return {"task_id": task_id, "cached": True, "result": json.loads(previous["result_json"])}
                raise StorageError("任务仍在执行或曾失败 / 中断；不会自动重发，请查看任务记录，确需重试时明确新建任务。")
            conn.execute("INSERT INTO work_tasks VALUES(?,?,?,?,?,?,?,?,?)", (task_id, project_id, kind, fingerprint, "running", None, None, now(), None))
            if on_start is not None:
                on_start(conn)
        try:
            result = operation()
            serialized = json.dumps(result, ensure_ascii=False)
        except BaseException as error:
            # 错误记录不保存异常原文、请求或密钥。
            code = getattr(error, "code", "task_failed")
            if code not in ("authentication", "balance", "timeout", "rate_limit", "invalid_json", "evidence_mismatch"):
                code = "task_failed"
            review = getattr(error, "review_result", None)
            with self.repo.db.transaction() as conn:
                conn.execute("UPDATE work_tasks SET status='failed',error_code=?,result_json=?,finished_at=? WHERE id=?", (code, json.dumps(review, ensure_ascii=False) if isinstance(review, dict) else None, now(), task_id))
            raise
        with self.repo.db.transaction() as conn:
            conn.execute("UPDATE work_tasks SET status='succeeded',result_json=?,finished_at=? WHERE id=?", (serialized, now(), task_id))
        return {"task_id": task_id, "cached": False, "result": result}

    def list(self, project_id, *, limit=50, offset=0):
        page_bounds(limit, offset)
        with self.repo.db.connect() as conn:
            self.repo.require(conn, "projects", project_id)
            total = conn.execute("SELECT COUNT(*) FROM work_tasks WHERE project_id=?", (project_id,)).fetchone()[0]
            rows = [dict(r) for r in conn.execute("SELECT id,kind,status,error_code,created_at,finished_at FROM work_tasks WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (project_id, limit, offset))]
            return {"tasks": rows, "total": total, "next_offset": offset + limit if offset + limit < total else None}

    def result(self, project_id, task_id):
        with self.repo.db.connect() as conn:
            row = self.repo.require(conn, "work_tasks", task_id)
            if row["project_id"] != project_id:
                raise StorageError("任务不属于本项目。")
            return {"task_id": task_id, "status": row["status"], "error_code": row["error_code"],
                    "result": json.loads(row["result_json"]) if row["result_json"] else None}
