"""四入口本地工作台；付费操作只在表单明确提交后执行。"""

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

import streamlit as st
from pydantic import ValidationError

from easy_writing.config import ConfigurationError, load_settings
from easy_writing.ingestion.chapter_files import FILE_TYPES, parse_chapter_file, validate_text
from easy_writing.llm.base import LLMError
from easy_writing.services.checking import CheckingService
from easy_writing.services.conversation import ChatRequest, ConversationService, DEEP_BUDGET, NORMAL_BUDGET
from easy_writing.services.foreshadowing import ForeshadowingService, STATES
from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.planning import PlanningRequest, PlanningService
from easy_writing.services.tasks import TaskService
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import RECORD_TABLES, Repository, digest, uid
from easy_writing.usage import JsonlCallRecorder
from easy_writing.ui.clients import client as _client, recorder as _recorder, workbench_import_identity

PAGES = ("项目导入", "知识查看", "创作问答", "检查报告")
STATE_LABELS = {"candidate": "候选", "planted": "已埋设", "reinforced": "已强化", "partially_resolved": "部分回收", "resolved": "已回收", "cancelled": "已取消", "open": "开放解释"}
PLAN_LABELS = {"draft": "草稿", "adopted": "已采纳", "written": "已关联正文", "rejected": "已舍弃"}

WORKBENCH_CSS = """
<style>
section[data-testid="stMain"] .block-container {
    max-width: 1260px;
    padding-top: 6rem;
    padding-bottom: 3rem;
}
h1 { letter-spacing: -.04em; }
div[data-testid="stMetric"] {
    background: #fff;
    border: 1px solid #dedfd7;
    border-radius: 12px;
    padding: 16px;
}
section[data-testid="stSidebar"] { border-right: 1px solid #dadfd5; }
.welcome-features {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    grid-auto-rows: 1fr;
    gap: 16px;
}
.welcome-feature {
    box-sizing: border-box;
    min-height: 168px;
    padding: 24px;
    border: 1px solid #cbd1cd;
    border-radius: 8px;
}
.welcome-feature h3 {
    margin: 0 0 12px;
    padding: 0;
    font-size: 1.5rem;
    line-height: 1.4;
}
.welcome-feature p { margin: 0; line-height: 1.7; }
@media (max-width: 900px) {
    .welcome-features { grid-template-columns: 1fr; }
}
</style>
"""

WELCOME_CARDS = """
<div class="welcome-features">
    <article class="welcome-feature">
        <h3>01 · 导入正文</h3>
        <p>一章一章入库，保留原文出处与版本。</p>
    </article>
    <article class="welcome-feature">
        <h3>02 · 回到故事</h3>
        <p>查看人物认知、时间线与伏笔状态，核查前后冲突。</p>
    </article>
    <article class="welcome-feature">
        <h3>03 · 探索下一章</h3>
        <p>获得有依据的推进路线，由你决定如何写。</p>
    </article>
</div>
"""


def _submit(repo, project, namespace, kind, payload, operation):
    task_id = uid()
    st.session_state[namespace + ":" + kind] = task_id
    with st.spinner("正在处理本次提交…"):
        return TaskService(repo).perform(project, task_id, kind, payload, operation)


def _task_result(repo, project, namespace, kind):
    task_id = st.session_state.get(namespace + ":" + kind)
    if task_id:
        task = TaskService(repo).result(project, task_id)
        if task["status"] == "succeeded":
            return task["result"]
        st.warning("上次任务未成功完成；页面刷新不会重试。需要重试时请再次明确提交。")
        if task["result"]:
            with st.expander("未通过校验的草稿与用量（不能直接采纳）"):
                st.json(task["result"])
                if kind == "plan" and "rejected_proposal" in task["result"]:
                    with st.form("recheck_" + task_id):
                        reason = st.text_input("补齐知识后重新校验的原因")
                        if st.form_submit_button("仅重新校验草稿，不调用模型"):
                            new_id = uid()
                            result = PlanningService(repo).recheck(project, new_id, task_id, reason=reason)
                            st.session_state[namespace + ":plan"] = new_id
                            return result["result"]
    return None


def _provider_widget(key):
    return st.selectbox("模型服务", ["fake", "deepseek"], format_func=lambda x: "离线演示（固定模板）" if x == "fake" else "DeepSeek（发送本次资料并产生接口用量）", key=key)


def _evidence(repo, kind, record_id, allowed_ids=None):
    refs = repo.evidence(kind, record_id)
    if allowed_ids is not None:
        refs = [r for r in refs if r["id"] in allowed_ids]
    if not refs:
        st.info("这条记录没有原文引用，请核对作者修改历史。")
    for ref in refs:
        st.caption(f"第 {ref['chapter_no']} 章 · 版本 {ref['revision']} · {ref['paragraph_id']} · {'当前原文' if ref['is_latest_source'] else '历史原文'}")
        for source in ref["document_sources"]:
            unit = {"page": "页", "paragraph": "段"}.get(source.get("unit"))
            label = f"第 {source['number']} {unit}" if unit else "作者校订" if source.get("method") == "author_edited" else "文件来源"
            st.caption(f"{source['filename']} · {label}" + (" · OCR" if source.get("method") == "ocr" else ""))
        st.text(ref["quote"])


def _imports(repo, project, settings, ns, chat):
    st.subheader("把正文整理为可追溯知识")
    st.caption("一文件一章，支持 UTF-8 TXT / Markdown。新增正文需要明确提交才会提取。")
    chapters = repo.chapters(project)
    if chapters:
        st.dataframe([{k: c[k] for k in ("chapter_no", "title", "latest_revision", "knowledge_is_current")} for c in chapters], hide_index=True, width="stretch")
    with st.expander("先体验一章已标注样稿", expanded=not chapters):
        st.write("内置人物为林舟、林月，附密码认知与世界规则的人工标准标注。建议在专用演示项目中使用。")
        if st.button("导入内置演示章节", key="sample_import"):
            if chapters:
                raise StorageError("内置演示仅导入空项目，请新建演示项目以避免覆盖正文。")
            from easy_writing.ui.sample import import_sample
            _submit(repo, project, ns, "ingest", {"builtin_sample": 1}, lambda: import_sample(repo, project, _recorder(settings)))
            st.success("演示章节已入库，接下来可以查看知识或生成下一章路线。")
    upload = st.file_uploader("正文文件（每份文件一章）", type=list(FILE_TYPES), key=ns + ":chapter_file")
    document = None
    if upload is not None:
        use_ocr = upload.name.lower().endswith(".pdf") and st.checkbox("对没有可用文字层的 PDF 页启用本地 OCR", key=ns + ":chapter_ocr")
        data = upload.getvalue()
        key = (ns, upload.name, sha256(data).hexdigest(), use_ocr, settings.ocr_tessdata_path, settings.ocr_language)
        cached = st.session_state.get("chapter_preview")
        if cached is None or cached[0] != key:
            try:
                with st.spinner("正在本地解析章节文件…"):
                    parsed = parse_chapter_file(upload.name, data, ocr=use_ocr, ocr_data=settings.ocr_tessdata_path,
                                                ocr_language=settings.ocr_language)
                cached = (key, parsed, None)
            except StorageError as error:
                cached = (key, None, str(error))
            st.session_state["chapter_preview"] = cached
        _, document, error = cached
        if error:
            st.error(error)
        if document:
            for warning in document.warnings:
                st.warning(warning)
            if document.pages:
                with st.expander("PDF 页码与文字提取结果"):
                    methods = {"text": "文字层", "ocr": "本地 OCR", "missing": "待补充或空页"}
                    st.dataframe([{"页码": p.page, "方式": methods[p.method], "字符数": p.characters} for p in document.pages],
                                 hide_index=True)
                    number = st.selectbox("查看单页提取文本", [p.page for p in document.pages],
                                          key=f"{ns}:{document.sha256}:{use_ocr}:pdf_page")
                    page = document.pages[number - 1]
                    if page.start is None:
                        st.caption("本页暂无可用文字。")
                    else:
                        st.text(document.text[page.start:page.start + page.characters])
    else:
        st.session_state.pop("chapter_preview", None)
    with st.form("chapter_import"):
        no = st.number_input("章节号", min_value=1, value=max((c["chapter_no"] for c in chapters), default=0) + 1, step=1, key="import_chapter")
        title = st.text_input("章节标题", value=document.title if document else "", key="import_title" if document is None else f"{ns}:{document.sha256}:title")
        text = (st.text_area("正文预览（可校订）", value=document.text, height=280,
                             key=f"{ns}:{document.sha256}:{use_ocr}:preview")
                if document else st.text_area("或粘贴正文", height=180, key="chapter_text"))
        provider = _provider_widget("import_provider")
        fixture = st.file_uploader("离线提取响应（使用离线服务时需要）", type=["json"], key="fixture_file")
        retry = st.checkbox("重试此前失败的入库任务", key="import_retry")
        submitted = st.form_submit_button("确认导入并提取本章" if document else "导入并提取本章",
                                          type="primary", disabled=upload is not None and document is None)
    if submitted:
        content = text
        if document and document.format in ("txt", "md", "markdown") and text == document.text.replace("\r\n", "\n").replace("\r", "\n"):
            content = document.text  # 保留未校订文本的原有版本指纹。
        validate_text(content)
        if not title.strip():
            raise StorageError("请填写章节标题。")
        from easy_writing.ingestion.pipeline import IngestionPipeline
        def operation():
            if provider == "fake":
                if fixture is None:
                    raise StorageError("离线服务需要 blocks 格式提取响应；可先使用内置演示。")
                from easy_writing.ingestion.extractor import FixtureLLMClient
                raw = fixture.getvalue().decode("utf-8-sig")
                try:
                    blocks = json.loads(raw)["blocks"]
                    if not isinstance(blocks, dict):
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise StorageError("离线响应须是包含 blocks 对象的 JSON。") from None
                client = FixtureLLMClient(_recorder(settings), blocks)
                identity = workbench_import_identity(settings, raw)
            else:
                client = _client(settings, provider, output_tokens=4096)
                identity = workbench_import_identity(settings)
            try:
                return IngestionPipeline(repo, client, model_identity=identity).ingest(
                    project, int(no), title, content, retry=retry, document=document)
            finally:
                client.close()
        fixture_hash = digest(fixture.getvalue().decode("utf-8-sig")) if fixture else None
        _submit(repo, project, ns, "ingest", {"chapter": int(no), "title": title, "content_hash": digest(content), "provider": provider, "fixture_hash": fixture_hash, "retry": retry}, operation)
        st.success("本章已发布，可在知识查看中确认或修正。")
    result = _task_result(repo, project, ns, "ingest")
    if result:
        with st.expander("最近一次导入结果"):
            st.json(result)
    from easy_writing.ui.maintenance import render
    render(repo, project, settings, ns, _submit, chat)


def _knowledge(repo, project, settings, ns):
    st.subheader("知识与出处")
    mode = st.radio("查看内容", ["知识记录", "人物与时间线", "伏笔状态"], horizontal=True, key="knowledge_mode")
    if mode == "知识记录":
        labels = {"entity": "人物与实体", "fact": "事实", "event": "事件", "thread": "伏笔", "summary": "摘要", "link": "连接"}
        kind = st.selectbox("知识类型", list(RECORD_TABLES), format_func=lambda x: labels[x], key="knowledge_kind")
        offset = st.number_input("从第几条开始", min_value=0, step=50, key="knowledge_offset")
        rows = repo.records(project, kind, limit=50, offset=int(offset))
        if not rows:
            st.info("此范围没有当前有效记录。请先导入章节，或调整分页位置。")
            return
        st.dataframe(rows, hide_index=True, width="stretch")
        record_id = st.selectbox("查看或修改记录", [r["id"] for r in rows], format_func=lambda rid: next((r.get("name") or r.get("title") or r.get("subject_name", "") + " · " + r.get("predicate", "") or rid) for r in rows if r["id"] == rid), key="record_id")
        row = next(r for r in rows if r["id"] == record_id)
        with st.expander("原文证据", expanded=True):
            _evidence(repo, kind, record_id)
        with st.form("knowledge_edit"):
            allowed = {"entity": ["name"], "fact": ["predicate", "object_value"], "event": ["title", "summary", "story_time"], "thread": ["title", "description"], "summary": ["text"], "link": []}
            fields = {"name": "名称", "predicate": "关系或属性", "object_value": "对象值", "title": "标题", "summary": "摘要", "story_time": "正文中的时间表达", "description": "描述", "text": "摘要正文"}
            field = st.selectbox("修改字段", ["仅修改确认状态"] + allowed[kind], format_func=lambda x: fields.get(x, x), key="edit_field")
            value = st.text_area("修改后的内容", key="edit_value")
            status = st.selectbox("确认状态", ["confirmed", "unconfirmed", "rejected"], format_func=lambda x: {"confirmed": "作者确认", "unconfirmed": "待确认", "rejected": "已否决"}[x], key="edit_status")
            reason = st.text_input("修改原因", key="edit_reason")
            if st.form_submit_button("保存知识修改"):
                repo.edit(kind, record_id, field=None if field == "仅修改确认状态" else field, value=value or None, status=status, reason=reason)
                st.success("修改已保存；关联摘要和旧报告会按依据标记失效。")
        if kind == "fact":
            events = TimelineService(repo).timeline(project)["events"]
            ids = [None] + [e["id"] for e in events]
            names = {None: "未设置", **{e["id"]: e["title"] for e in events}}
            with repo.db.connect() as conn:
                existing_mode = conn.execute("SELECT mode FROM fact_modes WHERE fact_id=?", (record_id,)).fetchone()
            with st.form("fact_context_" + record_id):
                start = st.selectbox("事实生效事件", ids, index=ids.index(row["effective_from_event_id"]) if row["effective_from_event_id"] in ids else 0, format_func=lambda x: names[x])
                end = st.selectbox("事实结束事件", ids, index=ids.index(row["effective_until_event_id"]) if row["effective_until_event_id"] in ids else 0, format_func=lambda x: names[x])
                fact_mode = st.selectbox("命题模式", ["assertion", "state"], index=1 if existing_mode and existing_mode[0] == "state" else 0, format_func=lambda x: "一般命题" if x == "assertion" else "单值状态")
                reason = st.text_input("事实时间标注原因")
                if st.form_submit_button("保存事实时间"):
                    KnowledgeService(repo).bind_fact(record_id, from_event=start, until_event=end, mode=fact_mode, reason=reason)
                    st.success("事实生效范围已保存。")
            people = KnowledgeService(repo).characters(project)
            if events and people:
                with st.form("fact_learning"):
                    actor = st.selectbox("获知人物", [p["id"] for p in people], format_func=lambda x: next(p["name"] for p in people if p["id"] == x))
                    event = st.selectbox("获知事件", [e["id"] for e in events], format_func=lambda x: names[x])
                    reason = st.text_input("获知标注原因")
                    if st.form_submit_button("保存获知渠道"):
                        KnowledgeService(repo).learn(record_id, actor, event, reason=reason)
                        st.success("获知渠道已保存（作者视图）。")
    elif mode == "人物与时间线":
        graph = TimelineService(repo)
        st.dataframe(KnowledgeService(repo).characters(project), hide_index=True, width="stretch")
        result = graph.timeline(project)
        st.caption("未知时间保留偏序；正文倒叙不会自动转换成故事时间。")
        st.dataframe([{k: e[k] for k in ("id", "title", "story_time", "time")} for e in result["events"]], hide_index=True, width="stretch")
        if result["events"]:
            events = {e["id"]: e["title"] for e in result["events"]}
            with st.form("event_time"):
                event = st.selectbox("标注时间的事件", list(events), format_func=lambda x: events[x])
                tick = st.number_input("故事时间刻度", value=0.0)
                reason = st.text_input("时间标注原因")
                if st.form_submit_button("保存事件时间"):
                    graph.set_time(event, start=float(tick), reason=reason)
                    st.success("事件时间已保存（作者视图）。")
    else:
        service = ForeshadowingService(repo)
        offset = st.number_input("伏笔起始位置", min_value=0, step=50)
        result = service.list(project, limit=50, offset=int(offset))
        st.caption(f"共 {result['total']} 条 · 下一页位置 {result['next_offset']} · 未回收本身不等于错误")
        st.json(result["state_counts"], expanded=False)
        for thread in result["threads"]:
            with st.expander(f"{thread['title']} · {STATE_LABELS.get(thread['state'], thread['state'])}"):
                st.write(thread["description"])
                if thread["state_needs_review"]:
                    st.warning("状态依据已过期，需要核查后修正。")
                _evidence(repo, "thread", thread["id"])
                with st.form("thread_" + thread["id"]):
                    state = st.selectbox("新状态", STATES, format_func=lambda x: STATE_LABELS[x])
                    event = st.text_input("关联事件 ID（可选）")
                    reason = st.text_input("状态修改原因")
                    correction = st.checkbox("这是作者对已有判断的修正")
                    if st.form_submit_button("确认伏笔状态"):
                        service.transition(project, thread["id"], state, event_id=event or None, reason=reason, correction=correction)
                        st.success("伏笔状态已保存。")


def _conversation(repo, project, settings, ns, chat):
    sessions = {row["id"]: row["title"] for row in chat.sessions(project)}
    session_key = ns + ":chat_session"
    if st.button("新建会话", key=ns + ":new_chat"):
        session = chat.create(project, f"创作会话 {len(sessions) + 1}")
        st.session_state[session_key] = session
        st.rerun()
    session = st.selectbox("继续会话", list(sessions), format_func=sessions.get, key=session_key) if sessions else None
    if not session:
        st.caption("发送第一条问题即可开始新会话。")
    key = ns + ":chat:" + (session or "new")
    offset = st.number_input("历史消息偏移量（0 为最近 30 轮）", min_value=0, step=30, key=key + ":offset") if session else 0
    turns = chat.turns(project, session, offset=int(offset)) if session else []
    latest = (chat.turns(project, session, limit=1) if offset else turns[-1:]) if session else []
    defaults = latest[0]["request"] if latest else {}
    latest_result = (latest[0]["result"] or {}) if latest else {}
    remembered = latest_result.get("memory", {})
    current_goal = remembered.get("goal", defaults.get("goal")) or ""
    current_constraints = remembered.get("constraints", defaults.get("constraints")) or ""
    last_chapter = latest_result.get("scope", {}).get("through_chapter", defaults.get("through_chapter"))
    snapshots = {}
    for turn in turns:
        with st.chat_message("user"):
            st.text(turn["request"]["question"])
            if turn["request"]["deep"]:
                st.caption("深度对话")
        with st.chat_message("assistant"):
            result = turn["result"]
            if result is None:
                st.warning("本轮未完成。刷新不会重试，可在下方明确再次发送。")
                continue
            st.write(result["answer"]["answer"])
            chapter = result["scope"]["through_chapter"]
            if chapter not in snapshots:
                snapshots[chapter] = chat.snapshot(project, chapter)
            if result["snapshot"] != snapshots[chapter]:
                st.warning("本轮知识依据已变化。新问题会重新检索当前资料。")
            coverage = result.get("context", {}).get("packet", {}).get("coverage", {})
            if coverage.get("not_current") or coverage.get("missing_ranges") or coverage.get("quarantined_chapters"):
                st.caption("本轮范围内有尚未入库、缺失或待复核的章节，可展开查看覆盖情况。")
            metrics = result["metrics"]
            if metrics.get("llm_turns"):
                st.caption(f"模型调用 {metrics['llm_turns']} 次 · 工具调用 {metrics['tool_calls']} 次 · 局部原文读取 {metrics['source_read_calls']} 次")
            reason = {"no_new_evidence": "继续补查未取得新证据", "llm_budget": "已达到模型调用上限",
                      "tool_budget": "已达到工具调用上限", "input_budget": "已达到输入预算，请缩小章节或事件范围",
                      "context_length": "接口的上下文长度限制仍未满足，请缩小问题范围"}.get(metrics.get("stop_reason"))
            if reason:
                st.caption(reason)
            with st.expander("依据、范围与调用用量"):
                st.json({k: v for k, v in result.items() if k != "memory"}, expanded=False)
            with st.expander("本轮保存的记忆"):
                if turn["summary"] is not None:
                    st.json(turn["summary"], expanded=False)
                if turn["summary_usage"]:
                    st.json(turn["summary_usage"], expanded=False)
                if turn["summary_status"] in ("pending", "failed", "running") or (
                    turn["summary_status"] is None and turn["request"]["provider"] == "deepseek"
                ):
                    st.caption("可单独补做语义摘要。DeepSeek 摘要最多再发起 1 次请求。")
                    if st.button("补做本轮摘要", key=key + ":summary:" + turn["id"]):
                        chat.summarize(project, session, turn["id"],
                                       client_factory=lambda: _client(settings, turn["request"]["provider"], output_tokens=384))
                        st.rerun()

    clear_draft = st.session_state.pop("chat_clear_draft", False)
    if st.session_state.get("chat_draft_scope") != key or clear_draft:
        st.session_state["research_question"] = ""
        st.session_state["chat_draft_scope"] = key
    people = {p["id"]: p["name"] for p in KnowledgeService(repo).characters(project)}
    events = {e["id"]: e["title"] for e in TimelineService(repo).timeline(project)["events"]}
    focus_labels = {i: f"人物 · {name} ({i[:8]})" for i, name in people.items()}
    focus_labels.update({i: f"事件 · {name} ({i[:8]})" for i, name in events.items()})
    if st.session_state.pop(key + ":reset_deep", False):
        st.session_state[key + ":deep"] = False
    with st.form(key + ":send"):
        question = st.text_area("你的问题", key="research_question", max_chars=750, placeholder="例如：钥匙由谁持有？她当时知道这件事吗？")
        deep = st.checkbox("深度对话（仅本次提问）", key=key + ":deep")
        normal_budget = "／".join(map(str, NORMAL_BUDGET))
        deep_budget = "／".join(map(str, DEEP_BUDGET))
        st.caption(f"每轮上限（模型／工具／局部原文）：普通 {normal_budget} 次，深度 {deep_budget} 次。选用 DeepSeek 时，问题、相关对话和必要资料会发送至接口。")
        provider = _provider_widget(key + ":provider")
        semantic_summary = st.checkbox("使用 DeepSeek 补充本轮语义摘要（最多额外 1 次调用）")
        embedding = st.selectbox("检索方式", ["none", "bge"], format_func=lambda x: {"none": "关键词检索", "bge": "本地中文语义检索"}[x], key=key + ":embedding")
        chapter = st.number_input("正文截止章（0 为全部当前章节）", min_value=0, step=1, value=last_chapter or 0)
        actor_options = [None, *people]
        actor = st.selectbox("限定人物认知", actor_options, index=actor_options.index(defaults.get("actor")) if defaults.get("actor") in people else 0,
                             format_func=lambda x: "作者视图／按问题识别" if x is None else focus_labels[x])
        event_options = [None, *events]
        point = st.selectbox("故事时点", event_options, index=event_options.index(defaults.get("at_event")) if defaults.get("at_event") in events else 0,
                             format_func=lambda x: "不限定时点" if x is None else focus_labels[x])
        focus = st.selectbox("明确查询对象", [None, *focus_labels], format_func=lambda x: "按问题和会话识别" if x is None else focus_labels[x])
        goal = st.text_input("创作目标", value=current_goal, max_chars=200)
        constraints = st.text_area("作者约束", value=current_constraints, max_chars=300)
        allow_pending = st.checkbox("允许核查尚未入库章节的已保存原文片段")
        submitted = st.form_submit_button("提交问答", type="primary")
    if submitted:
        request = ChatRequest(question=question, provider=provider, embedding=embedding, through_chapter=int(chapter) or None,
                              actor=actor, at_event=point, focus=focus, goal=goal, constraints=constraints, deep=deep,
                              allow_pending=allow_pending, semantic_summary=semantic_summary and provider == "deepseek")
        model = None
        if embedding == "bge":
            from easy_writing.retrieval.embeddings import BGEEmbeddingProvider
            cache_key = "bge:" + str((settings.embedding_model, settings.embedding_cache, settings.embedding_revision))
            if cache_key not in st.session_state:
                st.session_state[cache_key] = BGEEmbeddingProvider(settings.embedding_model, cache_folder=settings.embedding_cache, revision=settings.embedding_revision)
            model = st.session_state[cache_key]
        if not session:
            session = chat.create(project, question.strip()[:50])
            st.session_state[session_key] = session
        st.session_state[key + ":reset_deep"] = True
        with st.spinner("正在逐步核查关联事件…" if deep else "正在检索小说知识…"):
            chat.send(project, session, uid(), request, embedding=model,
                      client_factory=lambda: _client(settings, provider, output_tokens=1536 if deep else 1024),
                      summary_client_factory=lambda: _client(settings, provider, output_tokens=384),
                      recorder=JsonlCallRecorder(settings.retrieval_log, secrets=(settings.api_key.get_secret_value(),)))
        st.session_state["chat_clear_draft"] = True
        st.rerun()
    if current_goal:
        def open_planning():
            st.session_state["creation_mode"] = "下一章路线"
            st.session_state["planning_goal"] = current_goal
            st.session_state["planning_constraints"] = current_constraints
        st.button("将创作目标和约束带入下一章路线", on_click=open_planning)


def _creation(repo, project, settings, ns, chat):
    st.subheader("以已有故事为依据，探索下一步")
    section = st.radio("创作任务", ["创作问答", "下一章路线", "已保存计划"], horizontal=True, key="creation_mode")
    if section == "创作问答":
        _conversation(repo, project, settings, ns, chat)
    elif section == "下一章路线":
        people = KnowledgeService(repo).characters(project)
        events = TimelineService(repo).timeline(project)["events"]
        if not people or not events:
            st.info("先导入章节并确认人物、事实与故事时间，再生成下一章路线。")
            return
        with st.form("planning"):
            no = st.number_input("计划章节号", min_value=1, value=max((c["chapter_no"] for c in repo.chapters(project)), default=0) + 1, step=1)
            goal = st.text_area("下一章目标", key="planning_goal", placeholder="例如：让兄妹进入旧宅，并强化月牙划痕的悬念。")
            actors = st.multiselect("涉及人物", [p["id"] for p in people], default=[p["id"] for p in people[:2]], format_func=lambda x: next(p["name"] for p in people if p["id"] == x))
            point = st.selectbox("从哪个故事时点继续", [e["id"] for e in events], format_func=lambda x: next(e["title"] for e in events if e["id"] == x))
            constraints = st.text_area("限制条件（每行一条）", key="planning_constraints")
            provider = _provider_widget("planning_provider")
            submitted = st.form_submit_button("生成并保存路线", type="primary")
        if submitted:
            request = PlanningRequest(chapter_no=int(no), goal=goal, actors=actors, constraints=[line.strip() for line in constraints.splitlines() if line.strip()], at_event=point)
            task_id = uid()
            client = _client(settings, provider, planning=True)
            st.session_state[ns + ":plan"] = task_id
            try:
                with st.spinner("正在整理两到三条路线…"):
                    result = PlanningService(repo).generate(project, task_id, request, client)
            finally:
                client.close()
            st.session_state[ns + ":selected_plan"] = result["result"]["plan_id"]
        task = _task_result(repo, project, ns, "plan")
        if task:
            _show_plan(repo, project, task["plan_id"])
    else:
        offset = st.number_input("计划起始位置", min_value=0, step=50)
        plans = PlanningService(repo).list(project, offset=int(offset))
        if not plans["plans"]:
            st.info("尚无已保存计划。")
            return
        selected = st.selectbox("已保存计划", [p["id"] for p in plans["plans"]], format_func=lambda x: next(p["created_at"][:19] + " · " + PLAN_LABELS[p["status"]] for p in plans["plans"] if p["id"] == x))
        _show_plan(repo, project, selected)


def _show_plan(repo, project, plan_id):
    plan = PlanningService(repo).get(project, plan_id)
    st.caption(f"第 {plan['request']['chapter_no']} 章 · {PLAN_LABELS[plan['status']]} · 所有路线都是未来计划")
    if plan["stale"]:
        st.warning("计划依据已变化。草稿须重新生成后采纳；已采纳计划写入正文时仍需作者核对。")
    for i, route in enumerate(plan["response"]["routes"]):
        with st.expander(f"路线 {i + 1} · {route['title']}", expanded=True):
            st.write(route["synopsis"])
            st.markdown("**必要铺垫**")
            for preparation in route["preparations"]:
                st.write("• " + preparation)
            for index, step in enumerate(route["steps"], 1):
                st.write(f"{index}. {step['action']}")
            st.markdown("**人物弧线与伏笔影响**")
            for arc in route["character_arcs"]:
                actor = next(a["name"] for a in plan["context"]["actors"] if a["id"] == arc["actor_id"])
                st.write(f"{actor}：{arc['basis']} → {arc['proposed_shift']}")
            st.json(route["thread_impacts"], expanded=False)
            for key in route["citations"]:
                record = next((r for field in ("facts", "threads", "world_rules") for r in plan["context"][field] if r["key"] == key), None)
                if record:
                    st.caption("依据 " + key)
                    st.json(record, expanded=False)
                    source_key = record.get("source_fact_key", key)
                    kind, record_id = source_key.split(":", 1)
                    if kind in ("fact", "thread"):
                        _evidence(repo, kind, record_id, {e["evidence_id"] for e in record["evidence"]})
    if plan["response"]["caveats"]:
        st.info("；".join(plan["response"]["caveats"]))
    with st.expander("资料包与用量"):
        st.json({"context": plan["context"], "metrics": plan["metrics"], "history": plan["history"]})
    with st.form("plan_decide_" + plan_id):
        action = st.selectbox("作者决定", ["adopted", "rejected", "written"], format_func=lambda x: {"adopted": "采纳路线", "rejected": "舍弃计划", "written": "关联已写入并入库的正文"}[x])
        index = st.selectbox("选择路线", list(range(len(plan["response"]["routes"]))), format_func=lambda x: f"路线 {x + 1}")
        version = st.text_input("正文版本 ID（仅关联正文时需要）")
        reason = st.text_input("作者决定原因")
        if st.form_submit_button("保存计划决定"):
            PlanningService(repo).decide(project, plan_id, action, route_index=index, version_id=version or None, reason=reason)
            st.success("作者决定已保存。计划内容不会直接发布为已发生知识。")


def _checks(repo, project, settings, ns):
    st.subheader("检查证据，保留作者判断")
    service = CheckingService(repo)
    if st.button("运行当前知识检查", type="primary"):
        report = service.run(project)
        st.session_state[ns + ":check_run"] = report["id"]
    runs = service.runs(project)
    if not runs["runs"]:
        st.info("尚无检查报告。检查在本机执行，不发起模型请求。")
        return
    ids = [r["id"] for r in runs["runs"]]
    current = st.session_state.get(ns + ":check_run")
    run_id = st.selectbox("检查报告", ids, index=ids.index(current) if current in ids else 0, format_func=lambda x: next(r["created_at"][:19] + " · " + x[:8] for r in runs["runs"] if r["id"] == x))
    offset = st.number_input("问题起始位置", min_value=0, step=25)
    report = service.report(project, run_id, limit=25, offset=int(offset))
    cols = st.columns(3)
    cols[0].metric("待处理疑点", report["open_issues"])
    cols[1].metric("全部问题 / 建议", report["finding_total"])
    cols[2].metric("扫描伏笔", report["examined"]["threads"])
    if report["stale"]:
        st.warning("报告依据已过期，请重新运行检查后处理问题。")
    st.caption(f"下一页位置：{report['next_offset']}；覆盖范围与未更新章节可在下方查看。")
    for finding in report["findings"]:
        with st.expander(f"{finding['title']} · {finding['status']}", expanded=True):
            st.caption({"confirmed_conflict": "明确结构化冲突", "needs_review": "待核查疑点", "suggestion": "建议"}[finding["certainty"]])
            for side in finding["evidence_sides"]:
                st.markdown("**" + side["label"] + "**")
                for evidence in side["evidence"]:
                    st.caption(f"第 {evidence['chapter_no']} 章 · {evidence['paragraph_id']}")
                    st.text(evidence["quote"])
            st.write("判断条件：" + "；".join(finding["conditions"]))
            st.write("处理建议：" + "；".join(finding["repair_suggestions"]))
            with st.form("decision_" + finding["id"]):
                status = st.selectbox("处理状态", ["open", "ignored", "intentional", "resolved"], format_func=lambda x: {"open": "待处理", "ignored": "暂不处理", "intentional": "有意安排", "resolved": "作者标记已处理"}[x])
                reason = st.text_input("处理原因")
                if st.form_submit_button("保存问题决定", disabled=report["stale"]):
                    service.decide(project, finding["id"], status, reason=reason)
                    st.success("处理记录已保存。")
    with st.expander("检查范围与未覆盖内容"):
        st.json({k: v for k, v in report.items() if k != "findings"})


def main():
    st.set_page_config(page_title="Easy Writing · 小说工作台", page_icon="✒️", layout="wide")
    st.markdown(WORKBENCH_CSS, unsafe_allow_html=True)
    try:
        settings = load_settings()
        with st.sidebar:
            st.markdown("### ✒ Easy Writing")
            st.caption("让长篇故事始终有据可循")
            with st.form("database_open"):
                database = st.text_input("本地数据库", value=str(settings.database_path), key="db_input")
                chat_path = st.text_input("会话数据库（留空使用默认位置）", value=str(settings.chat_database_path or ""))
                if st.form_submit_button("打开数据库"):
                    st.session_state["active_database"] = str(Path(database).resolve())
                    st.session_state["active_chat_database"] = str(Path(chat_path).resolve()) if chat_path.strip() else None
            db_path = Path(st.session_state.get("active_database", settings.database_path))
            repo = Repository(Database(db_path))
            chat = ConversationService(repo, st.session_state.get("active_chat_database", settings.chat_database_path))
            projects = repo.projects()
            with st.form("project_create"):
                project_name = st.text_input("新项目名称", key="project_name")
                if st.form_submit_button("创建项目"):
                    created = repo.create_project(project_name)
                    st.session_state["preferred_project"] = created["id"]
                    st.session_state["active_project"] = created["id"]
                    projects = repo.projects()
            if not projects:
                st.info("先创建一个小说项目。")
                project = None
            else:
                ids = [p["id"] for p in projects]
                preferred = st.session_state.get("preferred_project")
                project = st.selectbox("当前作品", ids, index=ids.index(preferred) if preferred in ids else 0, format_func=lambda x: next(p["name"] for p in projects if p["id"] == x), key="active_project")
                page = st.radio("工作区", PAGES, key="page")
            st.caption("数据保存在本机；选用 DeepSeek 并提交时，本次相关资料会发送至接口。")
        if project is None:
            st.caption("EASY WRITING / 开始一个故事")
            st.title("写下故事，也记住故事")
            st.write("在左侧创建小说项目，逐章整理人物、事件与伏笔，让下一次创作有据可循。")
            st.markdown(WELCOME_CARDS, unsafe_allow_html=True)
            return
        ns = str(db_path.resolve()) + ":" + str(chat.db.path) + ":" + project
        st.caption("EASY WRITING / " + page)
        st.title(next(p["name"] for p in projects if p["id"] == project))
        chapters = repo.chapters(project)
        cols = st.columns(3)
        cols[0].metric("已保存章节", len(chapters))
        cols[1].metric("当前知识已入库", sum(c["knowledge_is_current"] for c in chapters))
        cols[2].metric("创作计划", PlanningService(repo).list(project, limit=1)["total"])
        from easy_writing.services.revisions import RevisionService
        revision_status = RevisionService(repo).status(project, limit=1)
        if revision_status["pending_total"] or revision_status["coverage"].get("not_current"):
            st.warning(f"改稿维护：{revision_status['pending_total']} 项待复核；{len(revision_status['coverage']['not_current'])} 章新稿尚未发布知识。受影响章节暂不用于回答，请在项目导入的维护面板处理。")
        if page == "项目导入":
            _imports(repo, project, settings, ns, chat)
        elif page == "创作问答":
            _creation(repo, project, settings, ns, chat)
        else:
            {"知识查看": _knowledge, "检查报告": _checks}[page](repo, project, settings, ns)
        with st.expander("本项目任务记录"):
            task_offset = st.number_input("任务起始位置", min_value=0, step=50, key="task_offset")
            tasks = TaskService(repo).list(project, offset=int(task_offset))
            st.caption(f"共 {tasks['total']} 个任务 · 下一页位置 {tasks['next_offset']}")
            st.dataframe(tasks["tasks"], hide_index=True, width="stretch")
            if tasks["tasks"]:
                task_id = st.selectbox("查看已保存的任务结果", [t["id"] for t in tasks["tasks"]], key="task_history_id")
                st.json(TaskService(repo).result(project, task_id), expanded=False)
    except (StorageError, ConfigurationError, LLMError) as error:
        st.error(str(error))
    except ValidationError:
        st.error("输入或模型返回结构无效，请核对必填项和任务记录。不会自动重试。")
    except (OSError, sqlite3.Error, UnicodeError):
        st.error("本地文件、数据库或 UTF-8 正文无法处理，请核对路径、权限与编码。")
