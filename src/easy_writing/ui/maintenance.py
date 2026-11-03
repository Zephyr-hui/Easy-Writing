"""四入口内的改稿维护面板，操作均由明确提交触发。"""

from pathlib import Path

import streamlit as st

from easy_writing.maintenance_cli import embedding_provider, resume_client
from easy_writing.services.archive import ArchiveService
from easy_writing.services.recovery import RecoveryService
from easy_writing.services.revisions import RevisionService
from easy_writing.services.statistics import StatisticsService
from easy_writing.storage.repository import digest, uid


def render(repo, project, settings, namespace, submit, chat):
    with st.expander("改稿与数据维护"):
        service = RevisionService(repo)
        offset = st.number_input("改稿复核起始位置", min_value=0, step=25, key="revision_offset")
        status = service.status(project, limit=25, offset=int(offset))
        st.caption(f"待复核 {status['pending_total']} 项 · 待刷新摘要 {status['stale_summary_count']} 份 · 下一页位置 {status['next_offset']}")
        st.json(status, expanded=False)
        with st.form("revision_refresh"):
            embedding = st.selectbox("刷新检索索引", ["none", "bge", "fake"], format_func=lambda x: {"none": "只刷新摘要、视图与检查", "bge": "同时增量更新本地中文语义索引", "fake": "同时更新离线测试索引"}[x])
            if st.form_submit_button("刷新当前派生资料"):
                result = service.refresh(project, provider=embedding_provider(embedding, settings))
                st.session_state[namespace + ":check_run"] = result["check_run_id"]
                st.success("当前资料已刷新；存在未完成复核的章节仍保持隔离。")
                st.json(result)
        if status["reviews"]:
            with st.form("revision_resolve"):
                selected = st.selectbox("复核项目", [r["id"] for r in status["reviews"]], format_func=lambda x: next(r["target_type"] + " · " + r["target_id"][:12] + " · " + r["reason"] for r in status["reviews"] if r["id"] == x))
                reason = st.text_input("已对照当前正文复核的依据")
                if st.form_submit_button("记录作者复核决定"):
                    service.resolve(project, selected, reason=reason)
                    st.success("复核决定已记录。所有相关待复核项处理完后，该章恢复参与查询。")
        with st.form("revision_dependency"):
            st.caption("明确后文依赖时登记关联；未知的语义依赖不会被自动猜测。")
            source_kind = st.selectbox("上游记录类型", ["fact", "event", "thread", "entity", "summary", "link"])
            source = st.text_input("上游记录 ID")
            target_kind = st.selectbox("受影响记录类型", ["fact", "event", "thread", "summary", "link"])
            target = st.text_input("受影响记录 ID")
            reason = st.text_input("为何依赖上游记录")
            if st.form_submit_button("保存改稿依赖"):
                service.depend(project, source_kind, source, target_kind, target, reason=reason)
                st.success("依赖已保存。")

        jobs = RecoveryService(repo).jobs(project)
        st.markdown("**入库恢复**")
        st.dataframe([{k: j[k] for k in ("id", "chapter_no", "status", "completed_blocks", "total_blocks", "source_is_current", "resumable")} for j in jobs[:50]], hide_index=True, width="stretch")
        resumable = [j for j in jobs if j["source_is_current"] and j["resumable"] and j["status"] != "published"]
        if resumable:
            with st.form("resume_job"):
                job = st.selectbox("继续入库的任务", [j["id"] for j in resumable])
                provider = st.selectbox("原提取服务", ["fake", "deepseek"], format_func=lambda x: "离线响应" if x == "fake" else "DeepSeek（仅未完成分块）")
                fixture = st.file_uploader("离线恢复响应 JSON", type=["json"], key="resume_fixture")
                submitted = st.form_submit_button("明确继续未完成分块")
            if submitted:
                content = fixture.getvalue() if fixture else None
                def operation():
                    recovery = RecoveryService(repo)
                    recovered_settings = settings.model_copy(update={"max_output_tokens": recovery.output_budget(project, job, settings.max_output_tokens)})
                    client = resume_client(recovered_settings, provider, content)
                    try:
                        return RecoveryService(repo).resume(project, job, client)
                    finally:
                        client.close()
                submit(repo, project, namespace, "ingest", {"resume_job": job, "provider": provider, "fixture_hash": digest(content.decode("utf-8-sig")) if content else None}, operation)
                st.success("恢复任务已完成；已完成分块与正式发布记录不会重复写入。")
        st.markdown("**备份、导出与恢复**")
        st.caption("知识库和会话库分别备份。恢复写入新文件，再从左侧选择对应数据库。")
        with st.form("data_archive"):
            action = st.selectbox("数据操作", ["backup", "export", "restore", "chat_backup", "chat_restore"],
                                  format_func=lambda x: {"backup": "小说知识库备份", "export": "当前作品知识导出",
                                                         "restore": "恢复小说知识库", "chat_backup": "独立会话库备份",
                                                         "chat_restore": "恢复独立会话库"}[x])
            source = st.text_input("恢复时使用的备份文件路径")
            destination = st.text_input("新输出文件路径", placeholder="data/backups/novel-backup.db 或 data/exports/novel.json")
            if st.form_submit_button("执行数据操作"):
                if not destination.strip():
                    from easy_writing.storage.database import StorageError
                    raise StorageError("请填写新的输出文件路径。")
                archive = chat.db.archive() if action.startswith("chat_") else ArchiveService(repo)
                if action in ("backup", "chat_backup"):
                    result = archive.backup(destination)
                elif action == "export":
                    result = archive.export(project, destination)
                else:
                    result = archive.restore(source, destination)
                st.success("操作完成，已有文件保持原状。")
                st.json(result)
        if st.button("汇总本项目可归属的接口用量", key="usage_summary"):
            st.json(StatisticsService(repo).summarize([settings.usage_log], project=project, chat=chat))
