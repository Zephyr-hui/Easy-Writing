import re
from dataclasses import dataclass

from easy_writing.storage.database import StorageError


@dataclass(frozen=True)
class Paragraph:
    paragraph_id: str
    scene_no: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Block:
    block_id: str
    paragraphs: tuple[Paragraph, ...]


def split_chapter(content: str, max_chars: int = 2400) -> tuple[list[Paragraph], list[Block]]:
    if not content.strip():
        raise StorageError("章节正文不能为空。")
    if not 200 <= max_chars <= 8000:
        raise StorageError("分块长度应在 200～8000 字符之间。")
    paragraphs = []
    scene = 1
    # 保留原始换行及空白的偏移；长段落按字符窗口拆开，不静默截断。
    for match in re.finditer(r"[^\r\n]+", content):
        if not match.group().strip():
            continue
        if match.group().strip() in ("***", "---", "* * *"):
            scene += 1
        # 定位片段长度固定，改变模型分块大小时不会改变同一版本的段落 ID。
        for start in range(match.start(), match.end(), 200):
            end = min(start + 200, match.end())
            paragraphs.append(Paragraph(f"p{len(paragraphs)+1:04d}", scene, start, end, content[start:end]))
    blocks = []
    pending = []
    size = 0
    for paragraph in paragraphs:
        if pending and (size + len(paragraph.text) > max_chars or paragraph.scene_no != pending[-1].scene_no):
            blocks.append(Block(f"b{len(blocks)+1:04d}", tuple(pending)))
            pending, size = [], 0
        pending.append(paragraph)
        size += len(paragraph.text)
    if pending:
        blocks.append(Block(f"b{len(blocks)+1:04d}", tuple(pending)))
    return paragraphs, blocks
