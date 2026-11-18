"""Document extraction.

`extract_document` is a READ tool. It posts a local file to the OCR adapter
and returns each field with its confidence. A field under the review threshold
is marked for review. The tool does not guess a value the service did not
return.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.adapters.ocr import OcrAdapter, build_ocr_adapter, render_read
from app.tools.registry import Capability, Risk, ToolError, tool

# A scan of a statement, not a disk image. Larger files stay on disk.
MAX_BYTES = 8 * 1024 * 1024

_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_override: OcrAdapter | None = None


def set_ocr_adapter(adapter: OcrAdapter | None) -> None:
    """Test hook. Pass None to go back to the environment."""
    global _override
    _override = adapter


def _current() -> OcrAdapter:
    if _override is not None:
        return _override
    return build_ocr_adapter()


@tool(
    name="extract_document",
    description=(
        "Extract fields from a local PDF or image via the OCR service. "
        "Each field includes its value, confidence, page, and bbox. "
        "Fields marked for review are below the confidence threshold and are not confirmed facts. "
        "If the service is unavailable, say so. Do not invent fields."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to a local PDF or image.",
            },
        },
        "required": ["path"],
    },
    capability=Capability.READ,
    risk=Risk.SAFE,
)
async def extract_document(path: str, **_: Any) -> str:
    file = Path(path)
    if not file.is_file():
        raise ToolError("path is not a file")
    try:
        size = file.stat().st_size
    except OSError as exc:
        raise ToolError("could not read the file") from exc
    if size == 0:
        raise ToolError("file is empty")
    if size > MAX_BYTES:
        raise ToolError("file is larger than 8 MiB")
    try:
        content = file.read_bytes()
    except OSError as exc:
        raise ToolError("could not read the file") from exc
    read = await _current().extract(file.name, content, _content_type(file))
    return render_read(read)


def _content_type(file: Path) -> str:
    return _CONTENT_TYPES.get(file.suffix.lower(), "application/octet-stream")
