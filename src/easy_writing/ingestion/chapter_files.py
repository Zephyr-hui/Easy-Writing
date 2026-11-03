"""本地解析单章文件，并为确认入库的原文保留来源定位。"""

import io
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from easy_writing.storage.database import StorageError


FILE_TYPES = ("txt", "md", "markdown", "docx", "pdf")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_CHAPTER_CHARS = 2_000_000
MAX_PAGES = 300
MAX_OCR_PAGES = 30
PDF_LOCK = Lock()  # PDFium 同一进程内串行访问。
LINES = re.compile(r"[^\r\n]+")


@dataclass(frozen=True)
class Location:
    unit: str
    number: int
    method: str


@dataclass(frozen=True)
class PagePreview:
    page: int
    method: str
    characters: int
    start: int | None


@dataclass(frozen=True)
class ChapterFile:
    filename: str
    format: str
    sha256: str
    title: str
    text: str
    locations: tuple[Location, ...] = ()
    pages: tuple[PagePreview, ...] = ()
    warnings: tuple[str, ...] = ()

    def source(self, content, paragraphs):
        mapping = {}
        if self.format in ("docx", "pdf"):
            original = [m.group() for m in LINES.finditer(self.text) if m.group().strip()]
            accepted = [m for m in LINES.finditer(content) if m.group().strip()]
            if content == self.text:
                locations = iter(self.locations)
            else:
                counts = Counter(original)
                edited_counts = Counter(m.group() for m in accepted)
                unique = {line: loc for line, loc in zip(original, self.locations, strict=True) if counts[line] == 1}
                locations = (unique.get(m.group()) if edited_counts[m.group()] == 1 else None for m in accepted)
            chunks = iter(paragraphs)
            chunk = next(chunks, None)
            for line, loc in zip(accepted, locations, strict=True):
                source = asdict(loc) if loc else {"method": "author_edited"}
                while chunk is not None and chunk.start < line.end():
                    mapping[chunk.paragraph_id] = source
                    chunk = next(chunks, None)
        return {"filename": self.filename, "format": self.format, "file_hash": self.sha256, "locations": mapping}


def validate_text(text):
    if not text.strip() or "\x00" in text:
        raise StorageError("请输入可用的非空章节正文。")
    if len(text) > MAX_CHAPTER_CHARS:
        raise StorageError("单章最多 200 万字符。")


def read_chapter_file(path, **options):
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise StorageError("无法读取章节文件，请检查路径与权限。") from None
    return parse_chapter_file(path.name, data, **options)


def parse_chapter_file(filename, data, *, ocr=False, ocr_data=None, ocr_language="chi_sim+eng"):
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    extension = Path(filename).suffix.lower().removeprefix(".")
    if extension not in FILE_TYPES:
        raise StorageError("请选择 TXT、Markdown、Word (.docx) 或 PDF。旧版 .doc 请先另存为 .docx。")
    if not data or len(data) > MAX_FILE_BYTES:
        raise StorageError("请选择非空且不超过 32 MiB 的章节文件。")
    title = Path(filename).stem[:300]
    warnings, pages, locations = (), (), ()
    if extension == "docx":
        try:
            text, locations, heading, warnings = _word(data)
        except RecursionError:
            raise StorageError("Word 正文嵌套结构过深，请重新导出后导入。") from None
        title = heading or title
    elif extension == "pdf":
        text, locations, pages, warnings = _pdf(data, ocr, ocr_data, ocr_language)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            raise StorageError("请将纯文本或 Markdown 保存为 UTF-8 编码。") from None
    if text.strip() or extension != "pdf":
        validate_text(text)
    return ChapterFile(filename, extension, sha256(data).hexdigest(), title, text, tuple(locations), tuple(pages), tuple(warnings))


def _word(data):
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            entry = archive.getinfo("word/document.xml")
            if entry.file_size > MAX_FILE_BYTES or entry.flag_bits & 1:
                raise StorageError("Word 正文过大或已加密，请使用已解锁的单章 .docx。")
            root = ElementTree.fromstring(archive.read(entry), forbid_dtd=True)
            names = archive.namelist()
    except (BadZipFile, KeyError, ElementTree.ParseError, DefusedXmlException, RuntimeError, ZlibError):
        raise StorageError("无法读取 Word 正文，文件可能损坏、已加密或格式不正确。") from None
    namespace = root.tag.removesuffix("document")
    if namespace not in ("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}",
                         "{http://purl.oclc.org/ooxml/wordprocessingml/main}"):
        raise StorageError("文件缺少有效的 Word 正文。")
    body = root.find(namespace + "body")
    if body is None:
        raise StorageError("Word 文件缺少正文。")
    skipped = {"del", "moveFrom", "drawing", "pict", "object", "txbxContent", "rPr", "pPr"}

    def inline(node):
        tag = node.tag.removeprefix(namespace)
        if tag in skipped:
            return ""
        if tag == "t":
            return node.text or ""
        if tag in ("br", "cr", "tab"):
            return "\t" if tag == "tab" else "\n"
        return "".join(inline(child) for child in node)

    def paragraphs(node):
        for child in node:
            tag = child.tag.removeprefix(namespace)
            if tag == "p":
                yield child
            elif tag in ("sdt", "sdtContent", "customXml", "ins", "moveTo"):
                yield from paragraphs(child)

    text, locations, heading, warnings = [], [], "", []
    for number, paragraph in enumerate(paragraphs(body), 1):
        value = inline(paragraph)
        text.append(value)
        locations.extend(Location("paragraph", number, "word") for m in LINES.finditer(value) if m.group().strip())
        style = paragraph.find(f"{namespace}pPr/{namespace}pStyle")
        if not heading and value.strip() and style is not None:
            label = style.get(namespace + "val", "").casefold()
            if label in ("title", "heading1", "标题", "标题1", "标题 1"):
                heading = value.strip().splitlines()[0][:300]
    tags = {node.tag.removeprefix(namespace) for node in body.iter()}
    if tags & {"tbl", "drawing", "pict", "object", "footnoteReference", "endnoteReference", "commentRangeStart"} or any(
        name.startswith(("word/comments", "word/footnotes", "word/endnotes")) for name in names
    ):
        warnings.append("检测到表格、图片、脚注或批注等非正文内容，请在预览中检查并补充。")
    if tags & {"ins", "del", "moveFrom", "moveTo"}:
        warnings.append("检测到修订痕迹，预览采用插入后的正文。")
    return "\n\n".join(text), locations, heading, warnings


def _pdf(data, use_ocr, ocr_data, ocr_language):
    import pypdfium2 as pdfium
    from pypdfium2 import raw

    text, locations, pages = [], [], []
    warnings = ["请核对 PDF 阅读顺序、分页断句、页眉页脚和双栏文字。"]
    text_length = 0
    with PDF_LOCK:
        try:
            document = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as error:
            if error.err_code in (raw.FPDF_ERR_PASSWORD, raw.FPDF_ERR_SECURITY):
                raise StorageError("PDF 已加密或受保护，请先解锁后再导入。") from None
            raise StorageError("无法打开 PDF，请检查文件格式和完整性。") from None
        with document:
            if raw.FPDF_GetSecurityHandlerRevision(document) >= 0:
                raise StorageError("PDF 已加密或受保护，请先解锁后再导入。")
            if len(document) > MAX_PAGES:
                raise StorageError(f"单章 PDF 最多 {MAX_PAGES} 页。")
            try:
                ocr_pages = 0
                for index in range(len(document)):
                    page = document[index]
                    try:
                        layer = page.get_textpage()
                        try:
                            if layer.count_chars() > MAX_CHAPTER_CHARS:
                                raise StorageError("PDF 正文超过单章字符上限。")
                            value = layer.get_text_bounded(errors="replace")
                        finally:
                            layer.close()
                        method = "text"
                        if not _usable_pdf_text(value):
                            method, value = "missing", ""
                            if use_ocr:
                                ocr_pages += 1
                                if ocr_pages > MAX_OCR_PAGES:
                                    raise StorageError(f"一次最多识别 {MAX_OCR_PAGES} 个扫描页，请先补齐文件文字层。")
                                value = _recognize(page, ocr_data, ocr_language)
                                method = "ocr" if _usable_pdf_text(value) else "missing"
                    finally:
                        page.close()
                    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip() if method != "missing" else ""
                    start = None
                    if value:
                        start = text_length + (2 if text else 0)
                        text_length = start + len(value)
                        if text_length > MAX_CHAPTER_CHARS:
                            raise StorageError("PDF 正文超过单章字符上限。")
                        text.append(value)
                        locations.extend(Location("page", index + 1, method) for m in LINES.finditer(value) if m.group().strip())
                    pages.append(PagePreview(index + 1, method, len(value), start))
            except pdfium.PdfiumError:
                raise StorageError("PDF 页面读取失败，请重新导出该文件。") from None
    missing = [str(page.page) for page in pages if page.method == "missing"]
    if missing:
        warnings.append("没有可用文字的页：" + "、".join(missing) + "。可开启本地 OCR，或在预览中补齐。")
    if any(page.method == "ocr" for page in pages):
        warnings.append("标记为 OCR 的页面需要核对识别结果。")
    return "\n\n".join(text), locations, pages, warnings


def _usable_pdf_text(value):
    return any(char.isprintable() and not char.isspace() and char != "\ufffd" for char in value)


def _recognize(page, data_path, language):
    import numpy as np

    try:
        import tesserocr
    except (ImportError, OSError):
        raise StorageError("本地 OCR 组件未安装，请安装项目的 ocr 可选依赖。") from None
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", language):
        raise StorageError("OCR_LANGUAGE 应为语言代码，例如 chi_sim+eng。")
    width, height = page.get_size()
    if width <= 0 or height <= 0:
        raise StorageError("PDF 页面尺寸无效。")
    scale = min(4, math.sqrt(10_000_000 / (width * height)), 4000 / max(width, height))
    bitmap = page.render(scale=scale, grayscale=True, may_draw_forms=False)
    try:
        pixels = bitmap.to_numpy()
        ink = pixels < 180
        if not ink.any():
            ink = pixels < 245
        rows = np.flatnonzero(ink.any(axis=1))
        columns = np.flatnonzero(ink.any(axis=0))
        if not rows.size:
            return ""
        pixels = np.pad(pixels[rows[0]:rows[-1] + 1, columns[0]:columns[-1] + 1], 25, constant_values=255)
        height, width = pixels.shape
        options = {"lang": language}
        if data_path is not None:
            options["path"] = str(data_path.resolve())
        try:
            with tesserocr.PyTessBaseAPI(**options) as engine:
                engine.SetImageBytes(pixels.tobytes(), width, height, 1, width)
                engine.SetSourceResolution(max(70, int(scale * 72)))
                if not engine.Recognize(timeout=15000):
                    raise StorageError("本地 OCR 页面识别超时或失败，请在预览中补充正文。")
                return engine.GetUTF8Text()
        except RuntimeError:
            raise StorageError("无法运行本地 OCR，请检查 OCR_TESSDATA_PATH 和语言数据。") from None
    finally:
        bitmap.close()
