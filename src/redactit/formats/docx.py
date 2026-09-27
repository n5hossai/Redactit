"""DOCX to Markdown for redaction: every part of the file a reader or reviewer could see.

Body (final text, tables as Markdown rows), headers, footers, footnotes, endnotes,
comments, tracked insertions and deletions, every author named anywhere, and text in any
other content part (text boxes, charts, SmartArt, custom XML). Document properties are
dropped: the output is Markdown, so nothing outside these sections is ever emitted.
"""

from __future__ import annotations

import io
import re
import zipfile

from defusedxml import ElementTree

from redactit.types import RedactitError

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# A crafted file stops here instead of filling memory (zip bombs expand ~1000x).
MAX_UNCOMPRESSED, MAX_PARTS = 200 * 2**20, 2000
# Parts that hold layout and settings, not document text.
_NO_TEXT = re.compile(r"word/(styles|stylesWithEffects|settings|webSettings|fontTable|numbering)\.xml$|word/theme/")
_HANDLED = re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$")


class DocxError(RedactitError, ValueError):
    pass


def docx_to_markdown(data: bytes) -> str:
    parts = _read_parts(data)
    if "word/document.xml" not in parts:
        raise DocxError("not a Word document: word/document.xml is missing")
    by_name = lambda pattern: [parts[n] for n in sorted(parts) if re.fullmatch(pattern, n)]  # noqa: E731
    sections = {
        "Body": _blocks(parts["word/document.xml"]),
        "Headers and footers": [b for root in by_name(r"word/(header|footer)\d*\.xml") for b in _blocks(root)],
        "Notes": [b for root in by_name(r"word/(footnotes|endnotes)\.xml") for note in root
                  if note.get(W + "type") not in ("separator", "continuationSeparator") for b in _blocks(note)],
        "Comments": [f"- {c.get(W + 'author', '')}: {' '.join(_blocks(c))}" for root in by_name(r"word/comments\.xml")
                     for c in root.iter(W + "comment")],
        "Tracked changes": [line for root in parts.values() for line in _revisions(root)],
        # Anyone who commented, revised or reformatted, in any part, including word/people.xml.
        "Authors": [", ".join(sorted({v for root in parts.values() for el in root.iter()
                                      for k, v in el.attrib.items() if k.endswith("}author") and v}))],
        "Other text": [t for name, root in parts.items() if not _HANDLED.match(name) and not _NO_TEXT.match(name)
                       for t in [" ".join(s.strip() for s in root.itertext() if s.strip())] if t],
    }
    return "\n\n".join(f"## {title}\n\n" + "\n\n".join(lines) for title, lines in sections.items()
                       if any(line.strip() for line in lines)) + "\n"


def _read_parts(data: bytes) -> dict:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise DocxError("not a Word document: the file is not a zip package") from e
    infos = [i for i in z.infolist() if i.filename.endswith(".xml")]
    if len(z.infolist()) > MAX_PARTS or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise DocxError("refusing an oversized or suspicious package")
    try:
        # defusedxml refuses DTDs and entity expansion (billion-laughs attacks).
        return {i.filename: ElementTree.fromstring(z.read(i)) for i in infos if i.filename.startswith(("word/", "customXml/"))}
    except Exception as e:  # malformed XML or a forbidden construct
        raise DocxError(f"unreadable Word XML ({type(e).__name__})") from e


def _blocks(el) -> list[str]:
    """Paragraphs and tables under `el`, in document order."""
    out = []
    for child in el:
        if child.tag == W + "p":
            out.append(_text(child))
        elif child.tag == W + "tbl":
            out.append(_table(child))
        elif child.tag not in (W + "sectPr", W + "tblPr"):
            out += _blocks(child)  # body, content controls, custom XML wrappers
    return [b for b in out if b.strip()]


def _text(el) -> str:
    """Final-view text: insertions in, deletions out (those go to "Tracked changes")."""
    out = []
    for child in el:
        tag = child.tag
        if tag == W + "t":
            out.append(child.text or "")
        elif tag == W + "tab":
            out.append("\t")
        elif tag in (W + "br", W + "cr"):
            out.append("\n")
        elif tag in (W + "del", W + "moveFrom", W + "delText"):
            continue
        elif tag == W + "txbxContent":  # a text box inside this paragraph
            out.append("\n" + "\n".join(_blocks(child)) + "\n")
        else:
            out.append(_text(child))
    return "".join(out)


def _table(tbl) -> str:
    rows = []
    for tr in tbl.iter(W + "tr"):
        cells = [" ".join(_blocks(tc)).replace("\n", " ").replace("|", "/") for tc in tr.iter(W + "tc")]
        rows.append("| " + " | ".join(cells) + " |")
    if rows:  # a Markdown header delimiter, so the first row reads as column headers
        rows.insert(1, "|" + "---|" * (rows[0].count("|") - 1))
    return "\n".join(rows)


def _revisions(root) -> list[str]:
    lines = []
    for kind, tag in (("Inserted", "ins"), ("Moved", "moveTo")):
        for el in root.iter(W + tag):
            if text := _text(el).strip():
                lines.append(f"- {kind} by {el.get(W + 'author', '')}: {text}")
    for kind, tag in (("Deleted", "del"), ("Moved away", "moveFrom")):
        for el in root.iter(W + tag):
            text = "".join(t.text or "" for t in el.iter() if t.tag in (W + "delText", W + "t")).strip()
            if text:
                lines.append(f"- {kind} by {el.get(W + 'author', '')}: {text}")
    return lines
