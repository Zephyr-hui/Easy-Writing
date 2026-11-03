"""从不可变原文与已完成分块显式恢复入库。"""

import json

from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.storage.database import StorageError


class RecoveryService:
    def __init__(self, repo):
        self.repo = repo

    def jobs(self, project):
        with self.repo.db.connect() as conn:
            self.repo.require(conn, "projects", project)
            return [dict(r) for r in conn.execute("""SELECT j.*,c.chapter_no,c.latest_version_id=v.id AS source_is_current,
                s.job_id IS NOT NULL AS resumable FROM ingestion_jobs j JOIN chapter_versions v ON v.id=j.version_id
                JOIN chapters c ON c.id=v.chapter_id LEFT JOIN ingestion_specs s ON s.job_id=j.id
                WHERE c.project_id=? ORDER BY j.created_at DESC,j.id DESC""", (project,))]

    def resume(self, project, job_id, client, *, lease_seconds=900):
        with self.repo.db.connect() as conn:
            row = conn.execute("""SELECT j.*,c.chapter_no,c.title,c.latest_version_id,c.project_id,c.published_generation_id AS active_generation,v.content,
                s.model_identity,s.max_chars,s.configuration_json FROM ingestion_jobs j JOIN chapter_versions v ON v.id=j.version_id
                JOIN chapters c ON c.id=v.chapter_id LEFT JOIN ingestion_specs s ON s.job_id=j.id WHERE j.id=?""", (job_id,)).fetchone()
            if not row or row["project_id"] != project:
                raise StorageError("入库任务不属于本项目。")
            if row["latest_version_id"] != row["version_id"]:
                raise StorageError("任务原文已被新版本替换，不能通过恢复回滚新稿。")
            if row["status"] == "published" and row["active_generation"] != job_id:
                raise StorageError("同一原文已有新的提取代次，不能通过恢复覆盖当前知识。")
            if not row["model_identity"]:
                raise StorageError("旧任务未保存恢复规格，请使用原文件和原提取配置显式 --retry。")
            expected = json.loads(row["configuration_json"])
            settings = getattr(client, "settings", None)
            actual = ({"provider": "deepseek", "base_url": settings.base_url, "model": settings.extraction_model,
                       "thinking": settings.extraction_thinking} if settings is not None else {"provider": "fake"})
            if settings is not None and "max_output_tokens" in expected:
                actual["max_output_tokens"] = settings.max_output_tokens
            if actual != expected:
                raise StorageError("模型配置与原入库任务不一致，拒绝混合提取器继续。")
            pipeline = IngestionPipeline(self.repo, client, model_identity=row["model_identity"], max_chars=row["max_chars"], lease_seconds=lease_seconds)
            if pipeline.signature != row["signature"]:
                raise StorageError("提取器或提示版本已变化，不能混用旧分块缓存，请明确重新导入。")
        return pipeline.ingest(project, row["chapter_no"], row["title"], row["content"], retry=True)

    def output_budget(self, project, job_id, fallback):
        with self.repo.db.connect() as conn:
            row = conn.execute("""SELECT s.configuration_json FROM ingestion_specs s JOIN ingestion_jobs j ON j.id=s.job_id
                JOIN chapter_versions v ON v.id=j.version_id JOIN chapters c ON c.id=v.chapter_id
                WHERE s.job_id=? AND c.project_id=?""", (job_id, project)).fetchone()
            return json.loads(row[0]).get("max_output_tokens", fallback) if row else fallback
