from __future__ import annotations

from pathlib import Path

from docx import Document
from pypdf import PdfReader


class UnsupportedFileType(ValueError):
    pass


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(path)
    if suffix == ".docx":
        return extract_docx_text(path)
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    raise UnsupportedFileType("Only PDF, DOCX, TXT, and MD files are supported.")


def extract_pdf_text(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise UnsupportedFileType("This file could not be read as a PDF. It may be corrupted.") from exc
    return "\n\n".join(page.strip() for page in pages if page.strip())


def extract_docx_text(path: Path) -> str:
    try:
        document = Document(str(path))
        paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs]
    except Exception as exc:
        raise UnsupportedFileType("This file could not be read as a DOCX. It may be corrupted.") from exc
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
