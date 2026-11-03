"""使用 Tk 原生窗口的小说工作台。所有耗时操作在工作线程中运行。"""

import json
import os
import queue
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from easy_writing.desktop.backend import DesktopBackend, expected_error
from easy_writing.desktop.library import CHAT_DB, WorkLibrary
from easy_writing.desktop.library_migration import migrate
from easy_writing.desktop.presentation import (
    FINDING_STATUS, PLAN_STATUS, RECORD_LABELS, archive_sections, export_sections, knowledge_sections, plan_sections,
    record_title, refreshed_sections, report_sections, revision_sections, route_title, timeline_sections,
)
from easy_writing.ingestion.chapter_files import FILE_TYPES
from easy_writing.services.archive import ArchiveService
from easy_writing.services.chapter_drafting import ChapterDraftRequest, ChapterDraftService, DemoChapterClient, DRAFT_MAX_CHARS, DRAFT_OUTPUT_TOKENS, prose_length
from easy_writing.services.checking import CheckingService
from easy_writing.services.conversation import ChatRequest, GUIDANCE_LIMIT
from easy_writing.services.foreshadowing import ForeshadowingService
from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.planning import PlanningRequest, PlanningService
from easy_writing.services.revisions import RevisionService
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import RECORD_TABLES, uid
from easy_writing.ui.clients import client, recorder
from easy_writing.ui.sample import import_sample


OFFLINE, DEEPSEEK = "离线演示", "DeepSeek"


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


class DesktopWindow:
    def __init__(self, root, data_dir):
        self.root, self.data_dir = root, Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.preferences = self.data_dir / "desktop.json"
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="easy-writing")
        self.progress = queue.SimpleQueue()
        self.future = None
        self.backend = self.library = self.repo = self.chat = self.project = self.work = self.document = None
        self.legacy_paths = {}
        self.preferred_work = None
        self.chat_popup = None
        self.projects = self.sessions = self.records = self.findings = []
        self.turns = self.plans = self.reports = self.people = self.events = []
        self.can_adopt = False
        self.root.title("Easy Writing · 小说工作台")
        self.root.geometry("1120x780")
        self.root.minsize(820, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.report_callback_exception = lambda _, error, __: self.error(expected_error(error))
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("TNotebook.Tab", padding=(16, 7))
        shell = ttk.Frame(root, padding=16)
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="Easy Writing", style="Title.TLabel").pack(side="left", padx=(0, 18))
        self.project_pick = self.combo(header, (), self.select_project, width=24)
        self.project_pick.pack(side="left", padx=4)
        self.button(header, "新建作品", self.new_project)
        self.button(header, "创建离线演示作品", self.sample)
        self.provider = self.combo(header, (OFFLINE, DEEPSEEK), width=14)
        self.provider.set(DEEPSEEK)
        self.provider.pack(side="right")
        ttk.Label(header, text="选用 DeepSeek 并提交时发送本次相关资料").pack(side="right", padx=8)
        self.status = tk.StringVar(value="就绪")
        ttk.Label(shell, textvariable=self.status).pack(side="bottom", fill="x", pady=(8, 0))
        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill="both", expand=True)
        self.make_import(self.tab("项目导入"))
        self.make_knowledge(self.tab("知识查看"))
        self.make_chat(self.tab("创作问答"))
        self.make_plans(self.tab("下一章路线"))
        self.make_checks(self.tab("检查报告"))
        self.make_settings(self.tab("数据与设置"))
        self.load_preferences()
        self.open_workspace()

    def tab(self, title):
        page = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(page, text=title)
        return page

    @staticmethod
    def row(parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=4)
        return frame

    @staticmethod
    def button(parent, title, action):
        button = ttk.Button(parent, text=title, command=action)
        button.pack(side="left", padx=(0, 5))
        return button

    @staticmethod
    def combo(parent, values, action=None, width=20):
        widget = ttk.Combobox(parent, values=values, state="readonly", width=width)
        if action:
            widget.bind("<<ComboboxSelected>>", lambda _: action())
        return widget

    @staticmethod
    def entry(parent, label, default="", width=20):
        ttk.Label(parent, text=label).pack(side="left", padx=(0, 5))
        variable = tk.StringVar(value=default)
        ttk.Entry(parent, textvariable=variable, width=width).pack(side="left", padx=(0, 12))
        return variable

    @staticmethod
    def toggle(parent, label):
        state = tk.BooleanVar()
        ttk.Checkbutton(parent, text=label, variable=state).pack(side="left", padx=(0, 10))
        return state

    @staticmethod
    def text(parent, *, height=10, expand=True):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=expand, pady=4)
        view = tk.Text(frame, wrap="word", height=height, undo=True, padx=8, pady=6,
                       font=("Microsoft YaHei UI", 10))
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=view.yview)
        view.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        view.pack(side="left", fill="both", expand=True)
        return view

    @staticmethod
    def show(view, value, *, editable=False):
        view.configure(state="normal")
        view.delete("1.0", "end")
        view.insert("1.0", value if isinstance(value, str) else pretty(value))
        if not editable:
            view.configure(state="disabled")

    def long_field(self, parent, label, *, width, guidance=False, constraints=False):
        value, preview = tk.StringVar(), tk.StringVar(value="点击编辑")
        value.trace_add("write", lambda *_: preview.set(" ".join(value.get().split()) or "点击编辑"))
        ttk.Label(parent, text=label).pack(side="left", padx=(0, 4))
        entry = ttk.Entry(parent, textvariable=preview, width=width, state="readonly")
        entry.pack(side="left", padx=(0, 8))

        def edit(_):
            if self.project and not self.future:
                self.edit_long_field(label, value, guidance=guidance, constraints=constraints)
            return "break"

        for event in ("<Button-1>", "<Return>", "<space>"):
            entry.bind(event, edit)
        return value

    def edit_long_field(self, label, value, *, guidance=False, constraints=False):
        popup = tk.Toplevel(self.root)
        popup.title(label)
        popup.transient(self.root)
        popup.geometry("620x340")
        editor = self.text(popup)
        self.show(editor, value.get(), editable=True)
        limit = 9600 if constraints and not guidance else GUIDANCE_LIMIT
        counter = tk.StringVar()
        ttk.Label(popup, textvariable=counter).pack(anchor="w", padx=8)
        actions = self.row(popup)

        def count(_=None):
            counter.set(f"{len(editor.get('1.0', 'end-1c'))} / {limit} 字"
                        + ("，每行一条，最多 8 条" if constraints and not guidance else ""))

        def save():
            text = editor.get("1.0", "end-1c")
            try:
                if len(text) > limit:
                    raise StorageError(f"内容最多 {limit} 字。")
                if constraints and not guidance:
                    self.chapter_constraints(text)
                if guidance:
                    goal = text if value is self.chat_goal else self.chat_goal.get()
                    rules = text if value is self.chat_constraints else self.chat_constraints.get()
                    self.chat.save_guidance(self.project, goal, rules)
            except Exception as error:
                messagebox.showerror(label, expected_error(error), parent=popup)
                return
            value.set(text)
            popup.destroy()

        self.button(actions, "保存", save)
        self.button(actions, "取消", popup.destroy)
        popup.bind("<Escape>", lambda _: popup.destroy())
        editor.bind("<<Modified>>", lambda _: (count(), editor.edit_modified(False)) if editor.edit_modified() else None)
        count()
        popup.wait_visibility()
        popup.grab_set()
        editor.focus_set()

    @staticmethod
    def chapter_constraints(text):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 8 or any(len(line) > 1200 for line in lines):
            raise StorageError("章节约束最多 8 条，每行一条且最多 1200 字。")
        return lines

    @staticmethod
    def show_sections(view, sections):
        view.configure(state="normal")
        view.delete("1.0", "end")
        view.tag_configure("section_title", font=("Microsoft YaHei UI", 12, "bold"),
                           foreground="#17384a", background="#eaf0f3", spacing1=10, spacing3=6)
        for i, (title, text) in enumerate(sections):
            if i:
                view.insert("end", "\n\n")
            mark = f"section_{i}"
            view.mark_set(mark, "end-1c")
            view.mark_gravity(mark, "left")
            view.insert("end", title + "\n", "section_title")
            view.insert("end", text)
        view.yview_moveto(0)
        view.configure(state="disabled")

    @staticmethod
    def selected(combo, entries):
        index = combo.current()
        return entries[index] if 0 <= index < len(entries) else None

    def error(self, message):
        self.status.set(message)
        messagebox.showerror("Easy Writing", message, parent=self.root)

    def run(self, label, operation, done=lambda _: None):
        if self.future:
            self.status.set("当前任务正在运行，请等待结果。")
            return
        self.status.set(label)
        self.future = self.executor.submit(operation)
        self.root.after(65, self.poll, done)

    def poll(self, done):
        while not self.progress.empty():
            self.status.set(self.progress.get())
        if not self.future.done():
            self.root.after(65, self.poll, done)
            return
        future, self.future = self.future, None
        try:
            value = future.result()
            self.status.set("操作完成")
            done(value)
        except Exception as error:
            self.error(expected_error(error))

    def close(self):
        if self.future:
            self.status.set("请等当前任务结束后再关闭窗口。")
            return
        self.executor.shutdown(wait=False)
        if self.library:
            self.library.close()
        self.root.destroy()

    def load_preferences(self):
        try:
            values = json.loads(self.preferences.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        if isinstance(values, dict):
            for key, variable in self.paths.items():
                if isinstance(values.get(key), str):
                    variable.set(values[key])
            legacy = values.get("legacy", values)
            if isinstance(legacy, dict):
                self.legacy_paths = {key: legacy[key] for key in ("knowledge", "chat") if isinstance(legacy.get(key), str)}
            self.preferred_work = values.get("last_work")

    def save_preferences(self):
        values = dict(self.applied_paths) | {"legacy": self.legacy_paths, "last_work": self.work}
        temporary = self.preferences.with_name("desktop.pending.json")
        temporary.write_text(pretty(values), encoding="utf-8")
        os.replace(temporary, self.preferences)

    def open_workspace(self):
        if self.future:
            return
        values = {key: var.get().strip() for key, var in self.paths.items()}
        location = Path(values["works"] or self.data_dir / "works").resolve()
        current = self.library

        def open_files():
            backend = DesktopBackend(self.data_dir, values["env"] or None)
            library = current if current and current.root == location else WorkLibrary(location)
            try:
                return backend, library, library.works()
            except BaseException:
                if library is not current:
                    library.close()
                raise

        def ready(result):
            self.backend, self.library, rows = result
            previous = self.work or self.preferred_work
            if current and current is not self.library:
                current.close()
                self.clear_chapter()
                self.clear_chat_popup()
                self.close_draft_popup()
                self.show(self.question, "", editable=True)
                self.plan_goal.set("")
                self.plan_constraints.set("")
                self.draft_min_chars.set("")
                self.draft_max_chars.set("")
                self.repo = self.chat = self.project = self.work = None
            self.active_interpreter = values["python"]
            self.applied_paths = values | {"works": str(location)}
            self.paths["works"].set(str(location))
            self.refresh_projects(previous, rows=rows)
            if not rows and self.legacy_knowledge().is_file():
                self.status.set("发现旧数据库，可在「数据与设置」中按作品迁移。")

        self.run("正在打开本地数据库…", open_files, ready)

    def refresh_projects(self, selected=None, *, rows=None):
        self.projects = self.library.works() if rows is None else rows
        self.project_pick.configure(values=[f"{i}. {p['name']}" for i, p in enumerate(self.projects, 1)])
        self.project_pick.set("")
        if self.projects:
            self.project_pick.current(next((i for i, p in enumerate(self.projects) if p["work_id"] == selected), 0))
        self.select_project()

    def select_project(self):
        if self.future:
            current = next((i for i, p in enumerate(self.projects) if p["work_id"] == self.work), None)
            if current is not None:
                self.project_pick.current(current)
            return
        selected = self.selected(self.project_pick, self.projects)
        work = selected["work_id"] if selected else None

        def ready(pair):
            if work != self.work or pair[0] is not self.repo:
                self.clear_chapter()
                self.clear_chat_popup()
                self.show(self.question, "", editable=True)
                self.plan_goal.set("")
                self.plan_constraints.set("")
                self.draft_min_chars.set("")
                self.draft_max_chars.set("")
                self.close_draft_popup()
            self.repo, self.chat = pair
            self.work, self.project = work, selected["id"] if selected else None
            goal, constraints = self.chat.guidance(self.project) if self.chat else ("", "")
            self.chat_goal.set(goal)
            self.chat_constraints.set(constraints)
            self.db_label.set(f"当前作品目录  {selected['folder']}" if selected else f"作品根目录  {self.library.root}")
            self.save_preferences()
            self.refresh_data()

        if not selected:
            ready((None, None))
        elif work == self.work and self.repo:
            ready((self.repo, self.chat))
        else:
            library = self.library
            self.run("正在打开作品的两个数据库…", lambda: library.open(work), ready)

    def refresh_data(self):
        if not self.project:
            self.sessions, self.turns, self.records, self.people, self.events = [], [], [], [], []
            self.reports, self.findings = [], []
            self.show(self.chapter_list, "先创建或打开一个作品。")
            for view in (self.knowledge_detail, self.chat_log, self.check_detail, self.plan_detail):
                self.show(view, "")
            for combo in (self.session_pick, self.knowledge_pick, self.report_pick, self.finding_pick, self.plan_pick, self.route_pick):
                combo.configure(values=())
                combo.set("")
            self.adopt_button.configure(state="disabled")
            self.draft_button.configure(state="disabled")
            self.advice_button.configure(state="disabled")
            self.refresh_drafts()
            return
        chapters = self.repo.chapters(self.project)
        self.chapter_no.set(str(max((c["chapter_no"] for c in chapters), default=0) + 1))
        self.show(self.chapter_list, "\n".join(f"{c['chapter_no']:>3}  {c['title']}  ·  v{c['latest_revision']}  ·  "
                                              + ("已入库" if c["knowledge_is_current"] else "待入库") for c in chapters))
        self.refresh_knowledge()
        self.refresh_scope()
        self.refresh_sessions()
        self.refresh_checks()
        self.refresh_plans()
        self.refresh_drafts()

    def new_project(self):
        if not self.library or self.future:
            return
        title = simpledialog.askstring("新建作品", "作品名称", parent=self.root)
        if title:
            library = self.library
            self.run("正在创建作品与数据库…", lambda: library.create(title), lambda row: self.refresh_projects(row["work_id"]))

    def sample(self):
        if not self.library or self.future:
            return
        library, cfg = self.library, self.backend.settings

        def create():
            names = {p["name"] for p in library.works()}
            title = "离线演示 · 旧宅"
            if title in names:
                title += " · " + uid()[:6]
            return library.create(title, initialize=lambda repo, project: import_sample(
                repo, project, recorder(cfg.model_copy(update={"usage_log": repo.db.path.parent / "usage.jsonl"}))))

        self.run("正在创建并导入内置样章…", create, lambda row: self.refresh_projects(row["work_id"]))

    def rename_work(self):
        if not self.work or self.future:
            return
        library, work = self.library, self.work
        info = library.info(work)
        title = simpledialog.askstring("重命名作品", "作品名称", initialvalue=info["name"], parent=self.root)
        if title:
            self.run("正在保存作品名称…", lambda: library.rename(work, title), lambda _: self.refresh_projects(work))

    def delete_work(self):
        if not self.work or self.future:
            return
        library, work = self.library, self.work
        info = library.info(work)
        if not messagebox.askyesno("删除作品", f"将「{info['name']}」移到作品回收站？\n\n"
                                   f"目录：{info['folder']}\n正文、知识及独立会话库将随作品一起移动。",
                                   parent=self.root, default="no"):
            return
        self.run("正在将作品移到回收站…", lambda: library.delete(work), lambda _: self.refresh_projects())

    def restore_work(self):
        if not self.library or self.future:
            return
        library = self.library
        rows = library.works(recycled=True)
        if not rows:
            self.status.set("作品回收站为空。")
            return
        choices = "\n".join(f"{i}. {row['name']} · {row['work_id'][:8]}" for i, row in enumerate(rows, 1))
        pick = simpledialog.askinteger("恢复作品", "选择要恢复的作品编号\n\n" + choices,
                                       minvalue=1, maxvalue=len(rows), parent=self.root)
        if pick is not None:
            self.run("正在恢复作品目录…", lambda: library.restore(rows[pick - 1]["work_id"]),
                     lambda row: self.refresh_projects(row["work_id"]))

    @property
    def provider_id(self):
        return "deepseek" if self.provider.get() == DEEPSEEK else "fake"

    def make_import(self, page):
        top = self.row(page)
        self.button(top, "选择并预览单章文件", self.choose_chapter)
        self.button(top, "清空预览", self.clear_chapter)
        self.ocr = self.toggle(top, "扫描页使用本地 OCR")
        self.retry = self.toggle(top, "继续此前失败的入库任务")
        self.preview_status = tk.StringVar(value="请选择单章文件，或粘贴正文。")
        ttk.Label(page, textvariable=self.preview_status, wraplength=1000).pack(anchor="w")
        details = self.row(page)
        self.chapter_no = self.entry(details, "章节号", "1", 7)
        self.chapter_title = self.entry(details, "章节标题", width=38)
        self.button(details, "离线响应 JSON…", self.choose_fixture)
        self.fixture = ""
        self.chapter_text = self.text(page)
        actions = self.row(page)
        self.button(actions, "确认导入并提取本章", self.import_chapter)
        self.button(actions, "查看 PDF 分页提取结果", self.preview_pages)
        self.chapter_list = self.text(page, height=5, expand=False)

    def clear_chapter(self):
        if not self.future:
            self.document = None
            self.fixture = ""
            self.chapter_title.set("")
            self.preview_status.set("请选择单章文件，或粘贴正文。")
            self.show(self.chapter_text, "", editable=True)

    def choose_fixture(self):
        self.fixture = filedialog.askopenfilename(parent=self.root, filetypes=[("离线 JSON", "*.json")]) or self.fixture
        if self.fixture:
            self.status.set("离线响应：" + Path(self.fixture).name)

    def choose_chapter(self):
        if not self.project or self.future:
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("章节文件", " ".join("*." + kind for kind in FILE_TYPES))])
        if not path:
            return
        ocr, backend = self.ocr.get(), self.backend

        def show_document(document):
            self.document = document
            self.chapter_title.set(document.title)
            self.show(self.chapter_text, document.text, editable=True)
            self.preview_status.set(document.filename + (" · " + " ".join(document.warnings) if document.warnings else " · 请确认正文预览"))

        self.run("正在本地解析文件…", lambda: backend.preview(path, ocr), show_document)

    def preview_pages(self):
        if not self.document or not self.document.pages:
            self.status.set("当前文件没有 PDF 分页资料。")
            return
        document = self.document
        popup = tk.Toplevel(self.root)
        popup.title("PDF 分页预览")
        popup.geometry("730x490")
        pick = self.combo(popup, [f"第 {p.page} 页 · " + {"text": "文字层", "ocr": "OCR", "missing": "待补充"}[p.method]
                                 for p in document.pages], width=35)
        pick.pack(anchor="w", padx=8, pady=8)
        view = self.text(popup)

        def select(_=None):
            p = document.pages[pick.current()]
            self.show(view, "暂无可用文字。" if p.start is None else document.text[p.start:p.start + p.characters])

        pick.bind("<<ComboboxSelected>>", select)
        pick.current(0)
        select()

    def import_chapter(self):
        if not self.project or self.future:
            return
        try:
            chapter = int(self.chapter_no.get())
        except ValueError:
            return self.error("章节号必须是整数。")
        title, text = self.chapter_title.get(), self.chapter_text.get("1.0", "end-1c")
        provider = self.provider_id
        backend, repo, project, document, fixture, retry = self.backend, self.repo, self.project, self.document, self.fixture, self.retry.get()
        if document and document.format in ("txt", "md", "markdown") and text == document.text.replace("\r\n", "\n").replace("\r", "\n"):
            text = document.text

        def submit():
            return backend.ingest(repo, project, chapter, title, text, document, provider, fixture, retry,
                                  lambda p: self.progress.put(f"知识提取 {p['completed_blocks']} / {p['total_blocks']}"))

        def finished(result):
            self.clear_chapter()
            self.refresh_data()
            self.status.set("已复用发布结果" if result["cached"] else "章节已发布")

        self.run("正在导入章节…", submit, finished)

    def make_knowledge(self, page):
        line = self.row(page)
        self.kind_pick = self.combo(line, [RECORD_LABELS[k] for k in RECORD_TABLES], self.refresh_knowledge)
        self.kind_pick.current(0)
        self.kind_pick.pack(side="left", padx=(0, 8))
        self.knowledge_offset = self.entry(line, "起始位置", "0", 6)
        self.button(line, "刷新列表", self.refresh_knowledge)
        self.record_buttons = (self.button(line, "确认当前记录", lambda: self.edit_record("confirmed")),
                               self.button(line, "否决当前记录", lambda: self.edit_record("rejected")))
        for button in self.record_buttons:
            button.configure(state="disabled")
        views = self.row(page)
        self.button(views, "故事时间线", lambda: self.knowledge_view("story"))
        self.button(views, "正文揭示顺序", lambda: self.knowledge_view("narrative"))
        self.button(views, "人物关系", lambda: self.knowledge_view("relations"))
        self.knowledge_pick = self.combo(page, (), self.show_knowledge, width=66)
        self.knowledge_pick.pack(fill="x", pady=4)
        self.knowledge_detail = self.text(page)

    def refresh_knowledge(self):
        if not self.project:
            return
        kind = list(RECORD_TABLES)[self.kind_pick.current()]
        try:
            offset = max(0, int(self.knowledge_offset.get()))
        except ValueError:
            offset = 0
        self.records = self.repo.records(self.project, kind, limit=100, offset=offset)
        names = [f"{i} · {record_title(kind, item)}" for i, item in enumerate(self.records, offset + 1)]
        self.knowledge_pick.configure(values=names)
        self.knowledge_pick.set("")
        if names:
            self.knowledge_pick.current(0)
        self.show_knowledge()

    def show_knowledge(self):
        item = self.selected(self.knowledge_pick, self.records)
        kind = list(RECORD_TABLES)[self.kind_pick.current()]
        for button in self.record_buttons:
            button.configure(state="normal" if self.project and item else "disabled")
        if not self.project or not item:
            self.show(self.knowledge_detail, "没有可查看的知识。")
            return
        refs = self.repo.evidence(kind, item["id"])
        if kind == "thread":
            item = dict(item)
            rows = ForeshadowingService(self.repo).list(self.project, limit=500)["threads"]
            item["伏笔状态"] = next((row["state"] for row in rows if row["id"] == item["id"]), None)
        endpoints = None
        if kind == "link":
            with self.repo.db.connect() as conn:
                endpoints = dict(conn.execute("SELECT id,title FROM events WHERE id IN (?,?)",
                                              (item["from_event_id"], item["to_event_id"])).fetchall())
        self.show_sections(self.knowledge_detail, knowledge_sections(kind, item, refs, endpoints))

    def knowledge_view(self, mode):
        if not self.project or self.future:
            return
        repo, project = self.repo, self.project

        def ready(result):
            for button in self.record_buttons:
                button.configure(state="disabled")
            self.show_sections(self.knowledge_detail, timeline_sections(mode, result))

        self.run("正在整理知识视图…", lambda: KnowledgeService(repo).relations(project) if mode == "relations"
                 else TimelineService(repo).timeline(project, order=mode), ready)

    def edit_record(self, status):
        item = self.selected(self.knowledge_pick, self.records)
        if self.future or not self.project or not item:
            return
        reason = simpledialog.askstring("作者复核", "复核原因", parent=self.root)
        if reason:
            kind, repo = list(RECORD_TABLES)[self.kind_pick.current()], self.repo
            self.run("正在记录作者复核…", lambda: repo.edit(kind, item["id"], status=status, reason=reason),
                     lambda _: self.refresh_knowledge())

    def make_chat(self, page):
        row = self.row(page)
        self.session_pick = self.combo(row, (), self.show_turns, width=32)
        self.session_pick.pack(side="left", padx=4)
        self.button(row, "新建会话", self.new_session)
        self.button(row, "重命名会话…", self.rename_session)
        self.button(row, "删除会话…", self.delete_session)
        self.button(row, "查看本轮证据与用量", self.chat_details)
        self.button(row, "查看较早对话", self.older_turns)
        self.chat_offset = 0
        self.chat_log = self.text(page)
        fields = self.row(page)
        self.through = self.entry(fields, "截止章", "0", 5)
        self.chat_actor = self.combo(fields, (), width=21)
        self.chat_actor.pack(side="left", padx=(0, 6))
        self.chat_point = self.combo(fields, (), width=21)
        self.chat_point.pack(side="left", padx=(0, 6))
        self.deep = self.toggle(fields, "深度对话")
        self.bge = self.toggle(fields, "本地 BGE")
        options = self.row(page)
        self.chat_goal = self.long_field(options, "作品目标", width=24, guidance=True)
        self.chat_constraints = self.long_field(options, "作品约束", width=27, guidance=True)
        self.semantic_summary = self.toggle(options, "语义摘要")
        self.pending = self.toggle(options, "允许待入库原文")
        self.question = self.text(page, height=3, expand=False)
        self.button(self.row(page), "发送问题", self.send_question)

    def refresh_scope(self):
        if not self.project:
            return
        self.people = KnowledgeService(self.repo).characters(self.project)
        self.events = TimelineService(self.repo).timeline(self.project)["events"]
        self.chat_actor.configure(values=["作者／自动视角"] + [p["name"] + " · " + p["id"][:6] for p in self.people])
        self.chat_actor.current(0)
        event_names = ["故事时点不限"] + [event["title"] + " · " + event["id"][:6] for event in self.events]
        self.chat_point.configure(values=event_names)
        self.chat_point.current(0)
        self.plan_point.configure(values=[f"{i} · {e['title']}" for i, e in enumerate(self.events, 1)])
        self.plan_point.set("")
        if self.events:
            self.plan_point.current(0)

    def refresh_sessions(self, session_id=None):
        self.sessions = self.chat.sessions(self.project)
        self.session_pick.configure(values=[f"{i}. {r['title']}" for i, r in enumerate(self.sessions, 1)])
        self.session_pick.set("")
        if self.sessions:
            self.session_pick.current(next((i for i, row in enumerate(self.sessions) if row["id"] == session_id), 0))
        self.show_turns()

    def show_turns(self, *, offset=None):
        self.chat_offset = offset or 0
        session = self.selected(self.session_pick, self.sessions)
        self.turns = self.chat.turns(self.project, session["id"], offset=self.chat_offset) if self.project and session else []
        lines = []
        for turn in self.turns:
            lines.append("你\n" + turn["request"]["question"])
            result = turn["result"]
            if not result:
                lines.append("助手\n本轮未完成，可明确提交新问题。")
                continue
            label = {"creative": "助手 · 创作建议", "clarify": "助手 · 需要确认"}.get(result["answer"]["confidence"], "助手")
            lines.append(label + "\n" + result["answer"]["answer"])
        if self.turns and not self.chat_offset:
            latest = self.turns[-1]
            scope = (latest["result"] or {}).get("scope", {})
            self.through.set(str(scope.get("through_chapter") or 0))
            self.chat_actor.current(next((i for i, p in enumerate(self.people, 1) if p["id"] == scope.get("actor")), 0))
            self.chat_point.current(next((i for i, e in enumerate(self.events, 1) if e["id"] == scope.get("at_event")), 0))
        elif not self.turns:
            self.through.set("0")
            if self.project:
                self.chat_actor.current(0)
                self.chat_point.current(0)
        self.show(self.chat_log, "\n\n".join(lines) or "发送第一条问题，即可创建会话。")
        self.chat_log.see("end")

    def older_turns(self):
        if len(self.turns) < 30:
            self.show_turns()
        else:
            self.show_turns(offset=self.chat_offset + 30)

    def chat_details(self):
        if not self.turns:
            return
        self.clear_chat_popup()
        popup = tk.Toplevel(self.root)
        self.chat_popup = popup
        popup.title("对话证据、记忆与用量")
        popup.geometry("760x530")
        choices = self.combo(popup, [t["request"]["question"][:50] for t in self.turns], width=60)
        choices.pack(fill="x", padx=8, pady=8)
        view = self.text(popup)
        turns = list(self.turns)
        project, chat = self.project, self.chat

        def select(_=None):
            turn = turns[choices.current()]
            result = turn["result"] or {}
            scope = result.get("scope", {})
            stale = result.get("snapshot") != chat.snapshot(project, scope.get("through_chapter")) if result else False
            self.show(view, {"旧证据": stale, "记忆": turn["summary"], "摘要用量": turn["summary_usage"],
                             **{key: value for key, value in result.items() if key != "memory"}})

        choices.bind("<<ComboboxSelected>>", select)
        choices.current(len(turns) - 1)
        select()

    def clear_chat_popup(self):
        if self.chat_popup and self.chat_popup.winfo_exists():
            self.chat_popup.destroy()
        self.chat_popup = None

    def new_session(self):
        if not self.project or self.future:
            return
        title = simpledialog.askstring("新建会话", "会话名称", parent=self.root)
        if title:
            chat, project = self.chat, self.project
            self.run("正在建立会话…", lambda: chat.create(project, title), self.refresh_sessions)

    def rename_session(self):
        session = self.selected(self.session_pick, self.sessions)
        if not session or self.future:
            return
        title = simpledialog.askstring("重命名会话", "会话名称", initialvalue=session["title"], parent=self.root)
        if title:
            chat, project, session_id = self.chat, self.project, session["id"]
            self.run("正在保存会话名称…", lambda: chat.rename(project, session_id, title),
                     lambda _: self.refresh_sessions(session_id))

    def delete_session(self):
        session = self.selected(self.session_pick, self.sessions)
        if not session or self.future:
            return
        if not messagebox.askyesno("删除会话", f"永久删除「{session['title']}」 · {session['id'][:8]}？\n\n"
                                   "该会话的问答、证据引用、记忆、摘要及用量记录将一起删除。",
                                   parent=self.root, default="no"):
            return
        chat, project, session_id = self.chat, self.project, session["id"]

        def done(_):
            self.clear_chat_popup()
            self.show(self.question, "", editable=True)
            self.refresh_scope()
            self.refresh_sessions()

        self.run("正在删除所选会话…", lambda: chat.delete(project, session_id), done)

    def send_question(self):
        if not self.project or self.future:
            return
        question = self.question.get("1.0", "end-1c").strip()
        provider = self.provider_id
        try:
            actor_index, event_index = self.chat_actor.current() - 1, self.chat_point.current() - 1
            request = ChatRequest(question=question, provider=provider, deep=self.deep.get(),
                                  through_chapter=int(self.through.get()) or None,
                                  actor=self.people[actor_index]["id"] if actor_index >= 0 else None,
                                  at_event=self.events[event_index]["id"] if event_index >= 0 else None,
                                  goal=self.chat_goal.get(), constraints=self.chat_constraints.get(), project_guidance=True,
                                  embedding="bge" if self.bge.get() else "none", allow_pending=self.pending.get(),
                                  semantic_summary=provider == "deepseek" and self.semantic_summary.get())
        except ValueError:
            return self.error("请填写非空问题（最多 750 字）、有效截止章及创作约束。")
        selected = self.selected(self.session_pick, self.sessions)
        repo, backend, chat, project, interpreter = self.repo, self.backend, self.chat, self.project, self.active_interpreter
        session_id = selected["id"] if selected else None

        def send():
            nonlocal session_id
            session_id = session_id or chat.create(project, question[:50])
            if request.embedding == "bge":
                backend.semantic_ask(repo, chat, project, session_id, request, interpreter)
            else:
                backend.ask(chat, project, session_id, request)
            return session_id

        def done(current):
            self.question.delete("1.0", "end")
            self.deep.set(False)
            self.refresh_sessions(current)

        self.run("正在理解问题、按需查询并生成答复…", send, done)

    def make_plans(self, page):
        row = self.row(page)
        self.plan_no = self.entry(row, "下一章章号", "2", 6)
        self.plan_goal = self.long_field(row, "本章目标", width=27)
        self.plan_point = self.combo(row, (), width=23)
        self.plan_point.pack(side="left", padx=(0, 6))
        self.button(row, "生成并保存路线", self.generate_plan)
        row = self.row(page)
        self.plan_constraints = self.long_field(row, "本章约束", width=33, constraints=True)
        self.plan_pick = self.combo(row, (), self.show_plan, width=34)
        self.plan_pick.pack(side="left")
        row = self.row(page)
        ttk.Label(row, text="定位或选择路线").pack(side="left", padx=(0, 6))
        self.route_pick = self.combo(row, (), self.select_route, width=37)
        self.route_pick.pack(side="left", padx=(0, 6))
        self.adopt_button = self.button(row, "采纳所选路线…", self.adopt_plan)
        self.adopt_button.configure(state="disabled")
        self.draft_button = self.button(row, "生成下一章内容…", self.submit_draft)
        self.draft_button.configure(state="disabled")
        self.can_draft = False
        self.draft_popup = None
        row = self.row(page)
        ttk.Label(row, text="正文目标字数（可选）").pack(side="left", padx=(0, 8))
        self.draft_min_chars = self.entry(row, "最少", width=7)
        self.draft_max_chars = self.entry(row, "最多", width=7)
        ttk.Label(row, text=f"两项均填或留空 · 上限 {DRAFT_MAX_CHARS} 字").pack(side="left")
        row = self.row(page)
        self.draft_pick = self.combo(row, (), self.select_draft, width=44)
        self.draft_pick.pack(side="left", padx=(0, 6))
        self.draft_view = self.button(row, "查看章节草稿", self.view_draft)
        self.draft_retry = self.button(row, "重试章节生成", self.retry_draft)
        self.drafts = []
        self.plan_detail = self.text(page)

    def refresh_plans(self, selected=None):
        self.plans = PlanningService(self.repo).list(self.project)["plans"]
        self.plan_pick.configure(values=[f"{i}. {p['created_at'][:19].replace('T', ' ')} · {PLAN_STATUS.get(p['status'], '待处理')}"
                                         for i, p in enumerate(self.plans, 1)])
        self.plan_pick.set("")
        if self.plans:
            self.plan_pick.current(next((i for i, p in enumerate(self.plans) if p["id"] == selected), 0))
        self.show_plan()

    def show_plan(self):
        row = self.selected(self.plan_pick, self.plans)
        self.route_pick.set("")
        self.adopt_button.configure(state="disabled")
        self.draft_button.configure(state="disabled")
        if not row:
            self.route_pick.configure(values=())
            self.can_adopt = self.can_draft = False
            self.show(self.plan_detail, "尚无下一章路线。")
            return
        result = PlanningService(self.repo).get(self.project, row["id"])
        routes = result["response"]["routes"]
        self.route_pick.configure(values=[route_title(i, route) for i, route in enumerate(routes)])
        if result["selected_route"] is not None:
            self.route_pick.current(result["selected_route"])
        self.can_adopt = result["status"] == "draft" and not result["stale"]
        self.can_draft = result["status"] in ("draft", "adopted") and not result["stale"]
        self.show_sections(self.plan_detail, plan_sections(result))
        if self.route_pick.current() >= 0:
            self.select_route()

    def select_route(self):
        index = self.route_pick.current()
        self.adopt_button.configure(state="normal" if self.can_adopt and index >= 0 else "disabled")
        self.draft_button.configure(state="normal" if self.can_draft and index >= 0 else "disabled")
        if index >= 0:
            self.plan_detail.yview(f"section_{index + 1}")

    def generate_plan(self):
        if not self.project or self.future:
            return
        event = self.selected(self.plan_point, self.events)
        if not event or not self.people:
            return self.error("请先导入并确认人物与故事时点。")
        try:
            request = PlanningRequest(chapter_no=int(self.plan_no.get()), goal=self.plan_goal.get(), at_event=event["id"],
                                      actors=[person["id"] for person in self.people[:2]],
                                      constraints=self.chapter_constraints(self.plan_constraints.get()))
        except StorageError as error:
            return self.error(str(error))
        except ValueError:
            return self.error("请检查章节号、目标和约束。")
        provider, project, repo = self.provider_id, self.project, self.repo
        cfg = self.backend.settings_for(repo)
        def generate():
            adapter = client(cfg, provider, planning=True)
            try:
                return PlanningService(repo).generate(project, uid(), request, adapter)["result"]["plan_id"]
            finally:
                adapter.close()

        self.run("正在生成下一章路线…", generate, self.refresh_plans)

    def adopt_plan(self):
        plan = self.selected(self.plan_pick, self.plans)
        index = self.route_pick.current()
        if self.future or not plan or not self.project or not self.can_adopt or index < 0:
            return
        reason = simpledialog.askstring("作者决定", f"采纳「{self.route_pick.get()}」的原因", parent=self.root)
        if reason:
            project, repo = self.project, self.repo
            self.run("正在保存作者决定…", lambda: PlanningService(repo).decide(project, plan["id"], "adopted",
                                                                                  reason=reason, route_index=index),
                     lambda _: self.refresh_plans(plan["id"]))

    def submit_draft(self):
        plan = self.selected(self.plan_pick, self.plans)
        index = self.route_pick.current()
        if not self.project or self.future or not plan or not self.can_draft or index < 0:
            return
        service = ChapterDraftService(self.repo)
        project = self.project
        saved = PlanningService(self.repo).get(project, plan["id"])
        chapter = saved["request"]["chapter_no"]
        try:
            lower, upper = self.draft_min_chars.get().strip(), self.draft_max_chars.get().strip()
            request = ChapterDraftRequest(route_index=index, provider=self.provider_id, goal=self.plan_goal.get().strip(),
                                          constraints=self.chapter_constraints(self.plan_constraints.get()),
                                          min_chars=int(lower) if lower else None, max_chars=int(upper) if upper else None)
        except StorageError as error:
            return self.error(str(error))
        except ValueError:
            return self.error(f"请将两个字数框同时留空，或填写 1～{DRAFT_MAX_CHARS} 的整数。最少字数不能超过最多字数。")
        length = f"\n正文目标 {request.min_chars}～{request.max_chars} 字" if request.min_chars is not None else ""
        if not messagebox.askyesno("生成下一章内容", f"为第 {chapter} 章生成标题和正文？\n\n{self.route_pick.get()}{length}",
                                   parent=self.root, default="no"):
            return

        def submitted(draft):
            self.plan_goal.set("")
            self.plan_constraints.set("")
            self.refresh_drafts(draft)
            self.generate_draft(draft)

        self.run("正在校验路线并保存生成请求…", lambda: service.submit(project, plan["id"], request), submitted)

    def refresh_drafts(self, selected=None):
        self.drafts = ChapterDraftService(self.repo).list(self.project) if self.project else []
        labels = {"pending": "待生成", "running": "生成中／曾中断", "failed": "生成失败", "succeeded": "草稿已保存"}
        self.draft_pick.configure(values=[f"{i}. 第 {row['request']['chapter_no']} 章 · {row['request']['route_title']} · {labels[row['status']]}"
                                          for i, row in enumerate(self.drafts, 1)])
        self.draft_pick.set("")
        if self.drafts:
            self.draft_pick.current(next((i for i, row in enumerate(self.drafts) if row["id"] == selected), 0))
        self.select_draft()

    def select_draft(self):
        row = self.selected(self.draft_pick, self.drafts)
        self.draft_view.configure(state="normal" if row and row["status"] == "succeeded" else "disabled")
        self.draft_retry.configure(state="normal" if row and row["retryable"] else "disabled")

    def retry_draft(self):
        row = self.selected(self.draft_pick, self.drafts)
        if not row or not row["retryable"] or self.future:
            return
        provider = "DeepSeek" if row["request"]["provider"] == "deepseek" else "离线演示"
        if messagebox.askyesno("重试章节生成", f"使用原请求及原模型模式「{provider}」重试？",
                               parent=self.root, default="no"):
            self.generate_draft(row["id"])

    def generate_draft(self, draft):
        service, project = ChapterDraftService(self.repo), self.project
        cfg = self.backend.settings_for(self.repo)

        def generate():
            try:
                maximum = service.get(project, draft)["request"].get("max_chars") or 0
                output_tokens = max(DRAFT_OUTPUT_TOKENS, cfg.max_output_tokens, maximum * 3 // 2 + 1024)
                service.generate(project, draft, lambda provider: DemoChapterClient() if provider == "fake" else
                                 client(cfg, provider, planning=True, output_tokens=output_tokens))
            except Exception as error:
                return error

        def done(error):
            self.refresh_drafts(draft)
            if error:
                self.error(expected_error(error))
            else:
                self.view_draft()

        self.run("正在生成章节标题与正文…", generate, done)

    def close_draft_popup(self):
        if self.draft_popup and self.draft_popup.winfo_exists():
            self.draft_popup.destroy()
        self.draft_popup = None

    def view_draft(self):
        row = self.selected(self.draft_pick, self.drafts)
        if not row or row["status"] != "succeeded" or self.future:
            return
        result = ChapterDraftService(self.repo).get(self.project, row["id"])["result"]
        self.close_draft_popup()
        popup = self.draft_popup = tk.Toplevel(self.root)
        popup.title(f"第 {row['request']['chapter_no']} 章 · 草稿")
        popup.geometry("780x600")
        ttk.Label(popup, text=result["title"], font=("Microsoft YaHei UI", 14, "bold"), wraplength=730).pack(pady=8)
        count = prose_length(result["body"])
        minimum, maximum = row["request"].get("min_chars"), row["request"].get("max_chars")
        length = f"正文 {count} 字（含标点，不含空白）"
        if minimum is not None:
            length += f" · 目标 {minimum}～{maximum} 字"
            if not minimum <= count <= maximum:
                length += " · 超出目标范围" if count > maximum else " · 未达到目标字数"
        ttk.Label(popup, text=length).pack(pady=(0, 5))
        self.show(self.text(popup), result["body"])

        def copy():
            popup.clipboard_clear()
            popup.clipboard_append(result["title"] + "\n\n" + result["body"])

        self.button(self.row(popup), "复制标题与正文", copy)

    def make_checks(self, page):
        row = self.row(page)
        self.button(row, "运行一致性与伏笔检查", self.check)
        self.report_pick = self.combo(row, (), self.show_report, width=38)
        self.report_pick.pack(side="left", padx=(0, 6))
        self.check_offset = self.entry(row, "疑点起始位置", "0", 6)
        self.button(row, "刷新报告", self.show_report)
        row = self.row(page)
        self.finding_pick = self.combo(row, (), self.select_finding, width=40)
        self.finding_pick.pack(side="left", padx=5)
        self.decision_pick = self.combo(row, ("待处理", "暂不处理", "有意安排", "已处理"), width=12)
        self.decision_pick.current(0)
        self.decision_pick.pack(side="left", padx=5)
        self.button(row, "保存处理决定", self.decide_finding)
        self.advice_button = self.button(row, "生成模型建议", self.suggest_report)
        self.advice_button.configure(state="disabled")
        self.check_detail = self.text(page)

    def refresh_checks(self, selected=None):
        self.reports = CheckingService(self.repo).runs(self.project)["runs"]
        self.report_pick.configure(values=[f"{i}. {r['created_at'][:19].replace('T', ' ')}"
                                           for i, r in enumerate(self.reports, 1)])
        self.report_pick.set("")
        if self.reports:
            self.report_pick.current(next((i for i, row in enumerate(self.reports) if row["id"] == selected), 0))
        self.show_report()

    def show_report(self):
        row = self.selected(self.report_pick, self.reports)
        try:
            offset = max(0, int(self.check_offset.get()))
        except ValueError:
            offset = 0
        report = CheckingService(self.repo).report(self.project, row["id"], offset=offset) if row else None
        self.findings = report["findings"] if report else []
        self.finding_pick.configure(values=[f"{i}. {f['title']} · {FINDING_STATUS[f['status']]}"
                                            for i, f in enumerate(self.findings, offset + 1)])
        self.finding_pick.set("")
        if self.findings:
            self.finding_pick.current(0)
            self.select_finding(scroll=False)
        self.advice_button.configure(state="normal" if report and not report["stale"] else "disabled")
        if report:
            self.show_sections(self.check_detail, report_sections(report, offset))
            self.check_detail.yview("1.0")
        else:
            self.show(self.check_detail, "尚无检查报告。")

    def select_finding(self, *, scroll=True):
        index = self.finding_pick.current()
        if index >= 0:
            self.decision_pick.current(tuple(FINDING_STATUS).index(self.findings[index]["status"]))
            if scroll:
                self.check_detail.yview(f"section_{index + 2}")

    def suggest_report(self):
        row = self.selected(self.report_pick, self.reports)
        if not self.project or not row or self.future:
            return
        if self.provider_id != "deepseek":
            return self.error("请先在窗口右上角选用 DeepSeek。")
        self.generate_report_advice(row["id"])

    def generate_report_advice(self, run):
        service, project = CheckingService(self.repo), self.project
        cfg = self.backend.settings_for(self.repo)

        def suggest():
            with client(cfg, "deepseek", planning=True, output_tokens=2048) as adapter:
                service.suggest(project, run, adapter)

        self.run("正在根据检查报告生成修改建议…", suggest, lambda _: self.show_report())

    def decide_finding(self):
        finding = self.selected(self.finding_pick, self.findings)
        if self.future or not self.project or not finding:
            return
        reason = simpledialog.askstring("处理疑点", "作者决定的依据", parent=self.root)
        if reason:
            project, repo = self.project, self.repo
            decision = ("open", "ignored", "intentional", "resolved")[self.decision_pick.current()]
            self.run("正在保存检查决定…", lambda: CheckingService(repo).decide(project, finding["id"], decision, reason=reason),
                     lambda _: self.show_report())

    def check(self):
        if self.project and not self.future:
            repo, project = self.repo, self.project
            use_model = self.provider_id == "deepseek"

            def done(result):
                self.check_offset.set("0")
                self.refresh_checks(result["id"])
                if use_model:
                    self.generate_report_advice(result["id"])

            self.run("正在检查当前知识…", lambda: CheckingService(repo).run(project), done)

    def make_settings(self, page):
        self.paths = {}
        for key, label, filters in (
            ("works", "作品根目录", None),
            ("env", "本机 .env 配置", [("配置文件", ".env *.env"), ("所有文件", "*")]),
            ("python", "BGE 本机 Python", [("Python", "python.exe"), ("所有文件", "*")]),
        ):
            row = self.row(page)
            variable = self.entry(row, label, width=58)
            self.paths[key] = variable

            def choose(var=variable, options=filters):
                if options is None:
                    path = filedialog.askdirectory(parent=self.root, initialdir=var.get() or self.data_dir)
                else:
                    path = filedialog.askopenfilename(parent=self.root, filetypes=options)
                if path:
                    var.set(path)

            self.button(row, "选择…", choose)
        self.button(self.row(page), "应用路径并打开作品目录", self.open_workspace)
        self.db_label = tk.StringVar()
        ttk.Label(page, textvariable=self.db_label, wraplength=1000).pack(anchor="w", pady=6)
        ttk.Label(page, text=f"本机数据目录  {self.data_dir}", wraplength=1000).pack(anchor="w", pady=4)
        row = self.row(page)
        self.button(row, "重命名作品…", self.rename_work)
        self.button(row, "删除作品…", self.delete_work)
        self.button(row, "恢复回收站作品…", self.restore_work)
        self.button(row, "迁移旧库／导入备份…", self.migrate_workspace)
        row = self.row(page)
        for chat, label in ((False, "小说库"), (True, "会话库")):
            self.button(row, f"备份{label}", lambda kind=chat: self.archive(kind, False))
            self.button(row, f"恢复{label}到新文件", lambda kind=chat: self.archive(kind, True))
        row = self.row(page)
        self.button(row, "导出当前作品知识", self.export_knowledge)
        self.button(row, "查看改稿复核", lambda: self.revisions(False))
        self.button(row, "刷新派生资料", lambda: self.revisions(True))
        self.archive_detail = self.text(page, height=6)

    def legacy_knowledge(self):
        return Path(self.legacy_paths.get("knowledge") or self.backend.settings.database_path)

    def migrate_workspace(self):
        if not self.library or self.future:
            return
        options = [("SQLite 数据库", "*.db *.sqlite *.sqlite3 *.bak"), ("所有文件", "*")]
        old = self.legacy_knowledge()
        knowledge = filedialog.askopenfilename(parent=self.root, title="选择旧小说知识库或其备份",
                                               initialdir=old.parent, initialfile=old.name, filetypes=options)
        if not knowledge:
            return
        configured_chat = self.legacy_paths.get("chat") or self.backend.settings.chat_database_path
        old_chat = Path(configured_chat) if configured_chat and Path(knowledge) == old else Path(knowledge).with_name(CHAT_DB)
        chat = filedialog.askopenfilename(parent=self.root, title="选择与旧知识库配套的会话库或其备份",
                                         initialdir=old_chat.parent, initialfile=old_chat.name, filetypes=options)
        if not chat and not messagebox.askyesno("迁移旧库", "未选择独立会话库。继续迁移小说知识库及其中可能包含的旧会话？",
                                               parent=self.root, default="no"):
            return
        library, previous = self.library, self.work

        def done(result):
            self.legacy_paths = {"knowledge": knowledge} | ({"chat": chat} if chat else {})
            lines = [f"源库备份目录　{result['backup']}"]
            for key, label in (("imported", "已迁移"), ("skipped", "已处理"), ("failed", "迁移失败")):
                for row in result[key]:
                    lines.append(f"{label}　{row['name']} · {row['work_id'][:8]}" + (f"　{row['error']}" if key == "failed" else ""))
            self.show(self.archive_detail, "\n".join(lines))
            self.refresh_projects(previous)

        self.run("正在备份、拆分并校验旧库…", lambda: migrate(library, knowledge, chat or None), done)

    def export_knowledge(self):
        if not self.project or self.future:
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="选择知识导出文件", defaultextension=".json")
        if path:
            repo, project = self.repo, self.project
            self.run("正在导出当前作品…", lambda: ArchiveService(repo).export(project, path),
                     lambda result: self.show_sections(self.archive_detail, export_sections(result)))

    def revisions(self, refresh):
        if not self.project or self.future:
            return
        service, project = RevisionService(self.repo), self.project
        if refresh:
            self.run("正在刷新派生资料…", lambda: service.refresh(project),
                     lambda result: self.show_sections(self.archive_detail, refreshed_sections(result)))
        else:
            self.run("正在读取改稿复核…", lambda: service.status(project, describe=True),
                     lambda result: self.show_sections(self.archive_detail, revision_sections(result)))

    def archive(self, chat, restore):
        if not self.repo or self.future:
            return
        service = self.chat.db.archive() if chat else ArchiveService(self.repo)
        source = filedialog.askopenfilename(parent=self.root, title="选择备份") if restore else None
        if restore and not source:
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="选择未占用的新文件", defaultextension=".db")
        if destination:
            self.run("正在校验并写入新文件…", lambda: service.restore(source, destination) if restore else service.backup(destination),
                     lambda result: self.show_sections(self.archive_detail, archive_sections(result, chat=chat, restore=restore)))
