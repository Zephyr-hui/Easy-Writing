import io
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest

from easy_writing.cli import main
from easy_writing.ingestion import chapter_files
from easy_writing.ingestion.chapter_files import parse_chapter_file
from easy_writing.ingestion.chunker import split_chapter
from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository


def word_file(body):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml",
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         f"<w:body>{body}</w:body></w:document>")
    return output.getvalue()


def word_paragraph(value):
    return f"<w:p><w:r><w:t>{escape(value)}</w:t></w:r></w:p>"


def pdf_file(*pages):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>"
               b"\nstream\n\x00\nendstream"]
    kids = []
    for text in pages:
        page_id = len(objects) + 1
        kids.append(f"{page_id} 0 R")
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents {page_id + 1} 0 R "
                        "/Resources << /Font << /F1 3 0 R >> /XObject << /Scan 4 0 R >> >> >>").encode())
        stream = (f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode() if text is not None else b"q 50 0 0 50 20 20 cm /Scan Do Q")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def response(text, paragraph="p0001"):
    evidence = [{"paragraph_id": paragraph, "quote": text}]
    return {"summary": text, "summary_evidence": evidence,
            "events": [{"key": "e1", "title": "找到钥匙", "summary": text, "evidence": evidence}]}


def test_word_preview_order_heading_revision_and_conservative_edit_mapping():
    raw = word_file(
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>第一章</w:t></w:r></w:p>'
        + word_paragraph("林舟找到钥匙。") + word_paragraph("***") +
        '<w:tbl><w:tr><w:tc>' + word_paragraph("表格内容") + '</w:tc></w:tr></w:tbl>'
        '<w:p><w:del><w:r><w:delText>旧字</w:delText></w:r></w:del>'
        '<w:ins><w:r><w:t>他向前走。</w:t></w:r></w:ins></w:p>'
    )
    doc = parse_chapter_file("第一章.DOCX", raw)
    assert doc.title == "第一章" and doc.sha256 == sha256(raw).hexdigest()
    assert doc.text.split("\n\n") == ["第一章", "林舟找到钥匙。", "***", "他向前走。"]
    assert len(doc.warnings) == 2
    accepted = doc.text.replace("他向前走。", "他向前走了一步。")
    paragraphs, _ = split_chapter(accepted)
    refs = doc.source(accepted, paragraphs)["locations"]
    assert refs["p0002"] == {"unit": "paragraph", "number": 2, "method": "word"}
    assert refs["p0004"] == {"method": "author_edited"}


def test_pdf_mixed_pages_keep_actual_page_numbers_and_use_ocr_selectively(monkeypatch):
    data = pdf_file("First page", None, "Last page")
    doc = parse_chapter_file("a.pdf", data)
    assert [p.method for p in doc.pages] == ["text", "missing", "text"]
    assert [ref.number for ref in doc.locations] == [1, 3]
    calls = []

    def recognize(page, path, language):
        calls.append(language)
        return "林舟在扫描页找到钥匙。"

    monkeypatch.setattr(chapter_files, "_recognize", recognize)
    doc = parse_chapter_file("a.pdf", data, ocr=True)
    assert calls == ["chi_sim+eng"]
    assert [p.method for p in doc.pages] == ["text", "ocr", "text"]
    assert doc.text.split("\n\n") == ["First page", "林舟在扫描页找到钥匙。", "Last page"]
    page = doc.pages[1]
    assert doc.text[page.start:page.start + page.characters] == "林舟在扫描页找到钥匙。"
    paragraphs, _ = split_chapter(doc.text)
    assert doc.source(doc.text, paragraphs)["locations"]["p0002"] == {"unit": "page", "number": 2, "method": "ocr"}


@pytest.mark.parametrize("name,data,match", [
    ("bad.docx", b"bad", "无法读取 Word"),
    ("bad.pdf", b"bad", "无法打开 PDF"),
    ("old.doc", b"bad", "旧版"),
    ("bad.txt", b"\xff", "UTF-8"),
])
def test_bad_chapter_files_raise_actionable_error(name, data, match):
    with pytest.raises(StorageError, match=match):
        parse_chapter_file(name, data)


def test_word_rejects_dtd_and_oversized_upload(monkeypatch):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE document [<!ENTITY x "test">]><document>&x;</document>')
    with pytest.raises(StorageError, match="无法读取 Word"):
        parse_chapter_file("bad.docx", output.getvalue())
    monkeypatch.setattr(chapter_files, "MAX_FILE_BYTES", 5)
    with pytest.raises(StorageError, match="32 MiB"):
        parse_chapter_file("bad.pdf", b"123456")


def test_cli_previews_before_confirmation_and_preserves_origin_on_cached_import(tmp_path, capsys):
    path = tmp_path / "第一章.docx"
    text = "林舟找到钥匙。"
    path.write_bytes(word_file(word_paragraph(text)))
    assert main(["ingest", str(path), "--preview"]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == text
    assert not Path("data/easy_writing.db").exists()
    repo = Repository(Database(Path("data/easy_writing.db")))
    project = repo.create_project("Word 导入")["id"]
    fixture = tmp_path / "response.json"
    fixture.write_text(json.dumps({"blocks": {"b0001": response(text)}}, ensure_ascii=False), encoding="utf-8")
    args = ["ingest", str(path), "--project", project, "--chapter", "1", "--fixture", str(fixture)]
    assert main(args) == 1
    assert "--confirm" in capsys.readouterr().err
    assert not repo.chapters(project)
    assert main(args + ["--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 1
    assert main(args + ["--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    event = repo.records(project, "event")[0]
    source = repo.evidence("event", event["id"])[0]
    assert source["quote"] == text and source["revision"] == 1
    assert source["document_sources"] == [{"filename": path.name, "format": "docx", "sha256": sha256(path.read_bytes()).hexdigest(),
                                            "unit": "paragraph", "number": 1, "method": "word"}]


def test_cli_pdf_review_creates_new_version_and_marks_edited_evidence(tmp_path, capsys):
    path = tmp_path / "第一章.pdf"
    path.write_bytes(pdf_file("First page", "Second page"))
    assert main(["ingest", str(path), "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert [page["page"] for page in preview["pages"]] == [1, 2]
    repo = Repository(Database(Path("data/easy_writing.db")))
    project = repo.create_project("PDF 导入")["id"]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"blocks": {"b0001": response("Second page", "p0002")}}), encoding="utf-8")
    args = ["ingest", str(path), "--project", project, "--chapter", "1", "--fixture", str(fixture), "--confirm"]
    assert main(args) == 0
    capsys.readouterr()
    first = repo.records(project, "event")[0]
    assert repo.evidence("event", first["id"])[0]["document_sources"][0]["number"] == 2
    corrected = tmp_path / "reviewed.txt"
    corrected.write_text("First page\n\n作者重新校订第二页。", encoding="utf-8")
    fixture.write_text(json.dumps({"blocks": {"b0001": response("作者重新校订第二页。", "p0002")}}), encoding="utf-8")
    assert main(args + ["--reviewed-text", str(corrected)]) == 0
    assert len(repo.chapters(project)) == 1
    current = repo.records(project, "event")[0]
    ref = repo.evidence("event", current["id"])[0]
    assert ref["revision"] == 2
    assert ref["document_sources"][0]["method"] == "author_edited"
    assert "number" not in ref["document_sources"][0]


def test_ui_upload_preview_is_read_only_until_explicit_confirmation(monkeypatch, tmp_path, recorder):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from easy_writing.ui import workbench
    from test_workbench import APP, click

    text = "林舟找到钥匙。"
    upload = SimpleNamespace(name="第一章.docx", getvalue=lambda: word_file(word_paragraph(text)))
    uploader = workbench.st.file_uploader
    monkeypatch.setattr(workbench.st, "file_uploader", lambda label, **kwargs:
                        upload if label.startswith("正文文件") else uploader(label, **kwargs))
    monkeypatch.setattr(workbench, "_client", lambda *args, **kwargs: FixtureLLMClient(recorder, {"b0001": response(text)}))
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.text_input(key="project_name").set_value("导入预览")
    click(at, "创建项目")
    assert next(t for t in at.text_area if "正文预览" in t.label).value == text
    repo = Repository(Database(tmp_path / "data/easy_writing.db"))
    project = repo.projects()[0]["id"]
    assert repo.chapters(project) == [] and recorder.events == []
    at.selectbox(key="import_provider").set_value("deepseek")
    click(at, "确认导入并提取本章")
    assert len(repo.chapters(project)) == 1 and len(recorder.events) == 1
    at.run()
    assert len(recorder.events) == 1
