"""Turn an uploaded file into page- or section-sized text.

PDF reading uses pypdf when it is installed. A missing package fails that one
upload. Markdown, text, Word, and CSV stay on the standard library.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass

_DOCX_TEXT = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.DOTALL)


class ExtractError(Exception):
    """The file could not be read. The message is safe to show the owner."""


@dataclass(frozen=True)
class Segment:
    text: str
    page: int | None
    section: str


def extract(filename: str, data: bytes) -> list[Segment]:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix in {"md", "markdown", "txt"}:
        return _markdown(data.decode("utf-8", errors="replace"))
    if suffix == "csv":
        return _csv(data.decode("utf-8", errors="replace"))
    if suffix == "docx":
        return _docx(data)
    if suffix == "pdf":
        return _pdf(data)
    raise ExtractError("This file type is not supported.")


def _markdown(text: str) -> list[Segment]:
    section = "document"
    buffer: list[str] = []
    segments: list[Segment] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if body:
            segments.append(Segment(body, None, section))

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            section = line.lstrip("#").strip() or "document"
            continue
        buffer.append(line)
    flush()
    if not segments and text.strip():
        segments.append(Segment(text.strip(), None, "document"))
    return segments


def _csv(text: str) -> list[Segment]:
    rows = list(csv.reader(io.StringIO(text)))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return []
    header = rows[0]
    body = rows[1:] if len(rows) > 1 else rows
    start = 2 if len(rows) > 1 else 1
    segments: list[Segment] = []
    for offset, row in enumerate(body):
        pairs = [
            f"{header[i] if i < len(header) else f'column {i + 1}'}: {cell}"
            for i, cell in enumerate(row)
        ]
        segments.append(Segment(", ".join(pairs), None, f"row {start + offset}"))
    return segments


def _docx(data: bytes) -> list[Segment]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            xml = package.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ExtractError("This Word file could not be read.") from exc
    # The owner's file, read as text. A full XML parser is unnecessary for w:t runs.
    text = xml.decode("utf-8", errors="replace")
    lines = [item.strip() for item in _DOCX_TEXT.findall(text) if item.strip()]
    if not lines:
        return []
    return [Segment("\n".join(lines), None, "document")]


def _pdf(data: bytes) -> list[Segment]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ExtractError("PDF reading is not available.") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ExtractError("This PDF could not be read.") from exc
    segments: list[Segment] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise ExtractError("This PDF could not be read.") from exc
        cleaned = text.strip()
        if cleaned:
            segments.append(Segment(cleaned, number, f"page {number}"))
    return segments
