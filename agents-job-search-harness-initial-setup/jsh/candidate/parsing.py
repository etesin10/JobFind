"""Résumé file parsing → ordered lines (no external Office/Word dependency).

- DOCX: read with the standard library (zip + XML). Includes tables and text boxes. Rejects DOCTYPE/entities
  (XML bomb protection) and oversized parts (zip bomb protection).
- PDF: text-based PDFs via ``pypdf`` (pure Python, same on Windows and macOS). Scanned/image PDFs have no
  text layer: they are reported clearly (OCR is intentionally not bundled).
- TXT / pasted text: decoded as UTF-8 (fallback Windows-1252 / Mac Roman by user choice not needed here).
- Legacy .doc (binary Word 97-2003) is NOT supported: the user is asked to save it as .docx.
"""
from __future__ import annotations

import io
import re
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_XML_BYTES = 30 * 1024 * 1024
MAX_PDF_PAGES = 40
MAX_TEXT_CHARS = 200_000
SUPPORTED = {".docx": "docx", ".pdf": "pdf", ".txt": "txt", ".md": "txt"}

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
BULLET_CHARS = "•●▪■◦○‣∙·-–—*>"


class ResumeParseError(ValueError):
    """User-facing (Spanish) reason why a résumé could not be read."""


@dataclass
class Line:
    text: str
    bullet: bool = False
    heading: bool = False  # styled heading (Word Heading/Title styles)


@dataclass
class ParsedResume:
    kind: str
    lines: list[Line]
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(("• " if ln.bullet else "") + ln.text for ln in self.lines)


def detect_kind(file_name: str) -> str:
    ext = Path(file_name).suffix.lower()
    if ext == ".doc":
        raise ResumeParseError("Los archivos .doc (Word 97-2003) no son compatibles. Ábralo en Word, LibreOffice "
                               "o Google Docs y guárdelo como .docx, o pegue el texto.")
    if ext not in SUPPORTED:
        raise ResumeParseError(f"Formato no compatible: '{ext or 'sin extensión'}'. Use .docx, .pdf (con texto) "
                               "o .txt, o pegue el texto.")
    return SUPPORTED[ext]


def parse_resume(data: bytes, file_name: str) -> ParsedResume:
    if len(data) > MAX_FILE_BYTES:
        raise ResumeParseError("El archivo es demasiado grande (máximo 10 MB).")
    kind = detect_kind(file_name)
    if kind == "docx":
        parsed = parse_docx(data)
    elif kind == "pdf":
        parsed = parse_pdf(data)
    else:
        parsed = parse_text(_decode_text(data), kind="txt")
    if not any(ln.text.strip() for ln in parsed.lines):
        raise ResumeParseError("No se encontró texto en el archivo.")
    return parsed


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _clean(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\t", " ")
    return re.sub(r"[ ]{2,}", "  ", text).strip()


def _split_bullet(text: str) -> tuple[str, bool]:
    stripped = text.lstrip()
    if stripped[:1] and stripped[0] in BULLET_CHARS and (len(stripped) == 1 or stripped[1] in " \t"):
        return stripped[1:].strip(), True
    return text.strip(), False


# ----------------------------------------------------------------------------- plain text
def parse_text(text: str, kind: str = "paste") -> ParsedResume:
    text = text[:MAX_TEXT_CHARS]
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        t, bullet = _split_bullet(_clean(raw))
        if t:
            lines.append(Line(t, bullet=bullet))
    return ParsedResume(kind, lines)


# ----------------------------------------------------------------------------- DOCX
MAX_XML_DEPTH = 200


def parse_docx(data: bytes) -> ParsedResume:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, EOFError) as exc:
        raise ResumeParseError("El archivo .docx está dañado o no es un documento de Word.") from exc
    with zf:
        try:
            info = zf.getinfo("word/document.xml")
        except KeyError as exc:
            raise ResumeParseError("El archivo .docx no contiene un documento de Word válido.") from exc
        if info.file_size > MAX_XML_BYTES:
            raise ResumeParseError("El documento es demasiado grande para procesarse.")
        try:
            # ZipFile.read stops at the declared size, so a lying header cannot inflate memory use.
            xml = zf.read(info)
        except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError, OSError) as exc:
            raise ResumeParseError("El archivo .docx está dañado o protegido y no se puede leer.") from exc
        has_media = any(n.startswith("word/media/") for n in zf.namelist())
    upper = xml.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ResumeParseError("El documento contiene definiciones XML no permitidas.")
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, RecursionError, ValueError) as exc:
        raise ResumeParseError("No se pudo leer el contenido del .docx.") from exc

    # Iterative pre-order walk (no recursion limit issues). Each <w:p> owns the text of its runs; a paragraph
    # nested inside another (text boxes) gets its own line, placed right after its parent.
    bufs: list[list[str]] = []
    props: list[tuple[bool, bool]] = []
    body = root.find(W + "body")
    stack: list[tuple[Any, int | None, int]] = [(body if body is not None else root, None, 0)]
    while stack:
        node, owner, depth = stack.pop()
        if depth > MAX_XML_DEPTH:
            raise ResumeParseError("La estructura del documento es demasiado profunda para procesarse.")
        tag = node.tag
        if tag == W + "p":
            owner = len(bufs)
            bufs.append([])
            ppr = node.find(W + "pPr")
            style, bullet = "", False
            if ppr is not None:
                st = ppr.find(W + "pStyle")
                style = (st.get(W + "val") or "") if st is not None else ""
                bullet = ppr.find(W + "numPr") is not None or "list" in style.lower()
            props.append((bullet, bool(re.match(r"(?i)(heading|title|t[ií]tulo)", style))))
        elif owner is not None:
            if tag == W + "t":
                bufs[owner].append(node.text or "")
            elif tag in (W + "tab", W + "br", W + "cr"):
                bufs[owner].append(" ")
        for child in reversed(list(node)):
            stack.append((child, owner, depth + 1))

    lines = []
    total = 0
    for buf, (bullet, heading) in zip(bufs, props):
        text, glyph = _split_bullet(_clean("".join(buf)))
        if text:
            total += len(text)
            lines.append(Line(text, bullet=bullet or glyph, heading=heading))
    if total > MAX_TEXT_CHARS:
        raise ResumeParseError("El documento tiene demasiado texto para un currículum (máximo ~200 000 caracteres).")
    warnings = []
    if has_media and total < 200:
        warnings.append("El documento parece contener principalmente imágenes; puede faltar texto.")
    return ParsedResume("docx", lines, warnings)


# ----------------------------------------------------------------------------- PDF
MAX_PDF_SECONDS = 20.0


def parse_pdf(data: bytes) -> ParsedResume:
    try:
        from pypdf import PdfReader
        from pypdf.errors import DependencyError, PyPdfError
    except ImportError as exc:  # pragma: no cover - dependency is bundled
        raise ResumeParseError("El lector de PDF no está disponible en esta instalación.") from exc
    started = time.monotonic()
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ResumeParseError("El PDF está protegido con contraseña. Quite la protección o use .docx.")
        pages = reader.pages
        if len(pages) > MAX_PDF_PAGES:
            raise ResumeParseError(f"El PDF tiene demasiadas páginas (máximo {MAX_PDF_PAGES}).")
        chunks = []
        total = 0
        for page in pages:
            txt = page.extract_text() or ""
            total += len(txt)
            if total > MAX_TEXT_CHARS:
                raise ResumeParseError("El PDF tiene demasiado texto para un currículum.")
            if time.monotonic() - started > MAX_PDF_SECONDS:
                raise ResumeParseError("El PDF es demasiado complejo para leerse. Use la versión .docx o pegue "
                                       "el texto.")
            chunks.append(txt)
    except ResumeParseError:
        raise
    except (DependencyError, NotImplementedError) as exc:
        raise ResumeParseError("El PDF usa un cifrado no compatible (aunque no pida contraseña). Expórtelo de "
                               "nuevo sin protección, use la versión .docx o pegue el texto.") from exc
    except (PyPdfError, ValueError, KeyError, TypeError, IndexError, AttributeError, ZeroDivisionError,
            RecursionError, OSError, zlib.error) as exc:
        raise ResumeParseError("No se pudo leer el PDF (archivo dañado, demasiado complejo o formato no "
                               "compatible).") from exc
    text = "\n".join(chunks)
    if len(text.strip()) < 40:
        raise ResumeParseError("El PDF no tiene texto seleccionable (parece escaneado o una imagen). "
                               "El reconocimiento de texto (OCR) no está incluido: use la versión .docx, "
                               "exporte el PDF desde Word/Google Docs, o pegue el texto.")
    parsed = parse_text(text, kind="pdf")
    parsed.warnings.append("PDF: el orden del texto puede variar en diseños de varias columnas; revise las "
                           "propuestas antes de aprobarlas.")
    return parsed
