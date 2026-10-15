"""Plain-text navigation for explicit Pixiv page/chapter markers.

Never interpret HTML or fetch embedded image/link references. The source asset
is immutable; this is a bounded reading view shared by the API and progress validation.
"""
import re

MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_SECTIONS = 5000
_MARKER = re.compile(r"\[newpage\]|\[chapter:([^\[\]\r\n]{1,512})\]")


def parse_sections(text):
    sections = []
    title = "正文"
    start = 0
    for match in _MARKER.finditer(text):
        content = text[start:match.start()]
        # A heading immediately after a page break changes the upcoming page's
        # title. Do not create fake blank chapters for adjacent markers.
        if content.strip():
            sections.append({"index": len(sections), "title": title, "text": content})
        if len(sections) >= MAX_SECTIONS:
            raise ValueError("正文分节超过 5000 节限制")
        title = match.group(1).strip() if match.group(1) is not None else f"第 {len(sections) + 1} 节"
        title = title or f"第 {len(sections) + 1} 节"
        start = match.end()
    content = text[start:]
    if content.strip() or not sections:
        sections.append({"index": len(sections), "title": title, "text": content})
    return sections


def text_sections(path):
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("文本超过 16 MiB 阅读解析限制，请下载原始文件")
    with path.open("rb") as stream:
        content = stream.read(MAX_TEXT_BYTES + 1)
    if len(content) > MAX_TEXT_BYTES:
        raise ValueError("文本超过 16 MiB 阅读解析限制，请下载原始文件")
    try:
        return parse_sections(content.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ValueError("文本不是有效的 UTF-8 内容") from exc
