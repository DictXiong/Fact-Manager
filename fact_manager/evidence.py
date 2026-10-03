"""Original-file inspection alongside immutable RAGFlow text snapshots."""

from functools import lru_cache
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

VERSION = "original-evidence-1"


@lru_cache(maxsize=128)
def _hash(path, size, mtime, ctime):
    result = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def integrity(source):
    try:
        path = Path(source["original_path"])
        stat = path.stat()
        return (
            _hash(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            == source["file_hash"]
        )
    except OSError:
        return False


def _xml(archive, name):
    if archive.getinfo(name).file_size > 8 * 1024 * 1024:
        raise ValueError("Original XML exceeds the inspection bound")
    return ET.fromstring(archive.read(name))


@lru_cache(maxsize=128)
def _native_cached(path, suffix, local, size, mtime, ctime, chunk_key):
    source = {
        "original_path": path,
        "name": "original" + suffix,
        "dataset_id": "local" if local else "remote",
    }
    chunk = (
        {"positions": chunk_key[0], "content": "" if chunk_key[1] else "nonempty"}
        if chunk_key is not None
        else None
    )
    return _native_uncached(source, chunk)


def native(source, chunk=None):
    """Cache bounded parsing, while keeping each caller's inspection independent."""
    path = Path(source["original_path"])
    suffix = Path(source["name"]).suffix.lower()
    local = source["dataset_id"] == "local"
    try:
        stat = path.stat()
    except OSError:
        return _native_uncached(source, chunk)
    chunk_key = (
        (chunk["positions"], not chunk["content"].strip())
        if chunk is not None and suffix in (".pdf", ".pptx") and not local
        else None
    )
    return deepcopy(
        _native_cached(
            str(path),
            suffix,
            local,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            chunk_key,
        )
    )


def _native_uncached(source, chunk=None):
    """Return native text/structure; image contents are never presented as understood."""
    from pypdf.errors import PdfReadError

    suffix = Path(source["name"]).suffix.lower()
    result = {"format": suffix.lstrip("."), "text": "", "warnings": []}
    try:
        if source["dataset_id"] == "local":
            result["format"] = "text"
            result["text"] = Path(source["original_path"]).read_text(encoding="utf-8")[
                :48000
            ]
        elif suffix == ".docx":
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            with zipfile.ZipFile(source["original_path"]) as archive:
                root = _xml(archive, "word/document.xml")
            paragraphs = [
                "".join(t.text or "" for t in p.findall(".//w:t", ns))
                for p in root.findall(".//w:p", ns)
            ]
            result["text"] = "\n".join(p for p in paragraphs if p.strip())[:48000]
            tables = []
            budget = 0
            for table in root.findall(".//w:tbl", ns)[:30]:
                rows = []
                for row in table.findall("w:tr", ns)[:40]:
                    cells = [
                        "".join(t.text or "" for t in cell.findall(".//w:t", ns))[:2000]
                        for cell in row.findall("w:tc", ns)[:20]
                    ]
                    if budget + sum(map(len, cells)) > 24000:
                        break
                    budget += sum(map(len, cells))
                    rows.append(cells)
                tables.append(rows)
            result["tables"] = tables
            if root.findall(".//w:drawing", ns):
                result["warnings"].append(
                    "Word含图片或绘图；本检查只读取原生文字与表格，图片含义需回看原文件。"
                )
        elif suffix == ".pptx":
            from .workflow import presentation_layout

            if chunk is not None:
                layout = presentation_layout(source, chunk)
                result["layout"] = layout
                result["text"] = (
                    "\n".join(b["text"] for b in layout["blocks"]) if layout else ""
                )
                if not layout:
                    result["warnings"].append(
                        "无法读取本页PPT文本框布局，需要人工回看原页。"
                    )
            with zipfile.ZipFile(source["original_path"]) as archive:
                images = sum(
                    1
                    for n in archive.namelist()
                    if n.startswith("ppt/media/") and not n.endswith("/")
                )
            result["embedded_media_count"] = images
            if images:
                result["warnings"].append(
                    "PPT包含嵌入媒体；图片文字、客户Logo与拓扑连线未由原生文本解析证明。"
                )
            if chunk is not None and not chunk["content"].strip():
                result["warnings"].append("该页解析文本为空，不能视为已完成内容识别。")
        elif suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(source["original_path"])
            if reader.is_encrypted:
                raise ValueError("Encrypted PDF requires manual inspection")
            result["page_count"] = len(reader.pages)
            positions = json.loads(chunk["positions"]) if chunk is not None else []
            numbers = sorted(
                {int(pos[0]) for pos in positions if pos and int(pos[0]) >= 1}
            )
            if not numbers:
                numbers = list(range(1, min(len(reader.pages), 8) + 1))
            pages = []
            for page_number in numbers[:8]:
                if page_number > len(reader.pages):
                    continue
                text = (reader.pages[page_number - 1].extract_text() or "")[:12000]
                pages.append({"page": page_number, "text": text})
                if not text.strip():
                    result["warnings"].append(
                        f"PDF第{page_number}页无原生文本；需依赖OCR并核对原页。"
                    )
            result["pages"] = pages
            result["text"] = "\n".join(p["text"] for p in pages)[:48000]
            result["warnings"].append(
                "PDF图表及连线关系须回看原页；原生文字和OCR可能存在列顺序差异。"
            )
        else:
            result["warnings"].append(
                "该格式尚无原生结构检查器，保留RAGFlow快照及原文件供人工核验。"
            )
    except (
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        ET.ParseError,
        ImportError,
        PdfReadError,
    ) as error:
        result["warnings"].append(
            "原文件结构读取失败："
            + type(error).__name__
            + "；不能仅凭分块认为解析完整。"
        )
    return result


def inspect(store, source_id, chunk_id=None):
    source = store.source(source_id)
    if not integrity(source):
        raise ValueError(
            "Original file missing or SHA256 mismatch; stop and restore the immutable snapshot"
        )
    chunks = store.chunks(source_id)
    chunk = (
        next((c for c in chunks if c["id"] == chunk_id), None)
        if chunk_id is not None
        else None
    )
    if chunk_id is not None and chunk is None:
        raise ValueError("Unknown evidence chunk")
    from .core import digest

    snapshot = [
        {
            "id": c["id"],
            "content": c["content"],
            "positions": json.loads(c["positions"]),
        }
        for c in chunks
    ]
    if digest(sorted(snapshot, key=lambda c: c["id"])) != source["chunks_hash"]:
        raise ValueError(
            "Parsed snapshot hash mismatch; restore the immutable evidence"
        )
    original = native(source, chunk)
    empty = [c["id"] for c in chunks if not c["content"].strip()]
    warnings = list(original["warnings"])
    if empty:
        warnings.append(f"{len(empty)}个分块没有可供引用的文字，需人工检查对应页面。")
    return {
        "source_id": source_id,
        "source_name": source["name"],
        "file_sha256": source["file_hash"],
        "chunks_sha256": source["chunks_hash"],
        "version": VERSION,
        "integrity": "verified",
        "chunk_id": chunk_id,
        "parsed_text": (
            chunk["content"]
            if chunk
            else "\n".join(c["content"] for c in chunks)[:48000]
        ),
        "positions": json.loads(chunk["positions"]) if chunk else [],
        "native": original,
        "warnings": warnings,
        "empty_chunk_ids": empty,
        "review_status": "evidence_only",
    }
