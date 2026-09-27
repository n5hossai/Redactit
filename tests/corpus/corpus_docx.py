"""Hand-built minimal OOXML .docx files for the corpus.

WHY zipfile by hand instead of python-docx: python-docx does not expose
comments, tracked-change authors or footnotes well enough to place PII
in them exactly where the plan requires. All parts below are the minimum
OOXML needed for Word to open the file; ZipInfo timestamps are fixed so
the same seed produces byte-identical bytes.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

DOCX_VARIANTS = [
    "body",
    "header_footer",
    "footnote",
    "comments",
    "tracked_changes",
    "core_props",
    "split_runs",
]

_FIXED_TIME = (2024, 1, 1, 0, 0, 0)
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s: str) -> str:
    return _esc(s).replace('"', "&quot;")


def _p(text: str) -> str:
    """A plain paragraph holding one run."""
    return f'<w:p><w:r><w:t xml:space="preserve">{_esc(text)}</w:t></w:r></w:p>'


CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>
"""

PACKAGE_RELS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>
"""

DOCUMENT_RELS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>
<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>
"""


def _document_xml(body_xml: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>'
        f"{body_xml}"
        '<w:sectPr><w:headerReference w:type="default" r:id="rId1"/>'
        '<w:footerReference w:type="default" r:id="rId2"/>'
        '<w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
        "</w:body></w:document>"
    )
    return xml.encode("utf-8")


def _header_xml(text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:hdr xmlns:w="{_W}">{_p(text)}</w:hdr>'
    ).encode("utf-8")


def _footer_xml(text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:ftr xmlns:w="{_W}">{_p(text)}</w:ftr>'
    ).encode("utf-8")


def _footnotes_xml(text: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:footnotes xmlns:w="{_W}">'
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        '<w:footnote w:type="continuationSeparator" w:id="0">'
        "<w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>"
        f'<w:footnote w:id="1">{_p(text)}</w:footnote>'
        "</w:footnotes>"
    )
    return xml.encode("utf-8")


def _comments_xml(comments: list[tuple[str, str]]) -> bytes:
    body = "".join(
        f'<w:comment w:id="{i}" w:author="{_esc_attr(author)}" '
        f'w:date="2024-01-01T00:00:00Z" w:initials="XX">{_p(text)}</w:comment>'
        for i, (author, text) in enumerate(comments, start=1)
    )
    xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:comments xmlns:w="{_W}">{body}</w:comments>'
    return xml.encode("utf-8")


def _core_xml(creator: str, last_modified_by: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<cp:coreProperties "
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:creator>{_esc(creator)}</dc:creator>"
        f"<cp:lastModifiedBy>{_esc(last_modified_by)}</cp:lastModifiedBy>"
        '<dcterms:created xsi:type="dcterms:W3CDTF">2024-01-01T00:00:00Z</dcterms:created>'
        '<dcterms:modified xsi:type="dcterms:W3CDTF">2024-01-01T00:00:00Z</dcterms:modified>'
        "</cp:coreProperties>"
    )
    return xml.encode("utf-8")


def _write_docx(path: Path, parts: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            zf.writestr(info, data)


def build_docx(variant: str, vf, path: Path) -> list[dict]:
    entries: list[dict] = []

    def seed(entity_type: str, value: str, location: str) -> str:
        entries.append({"entity_type": entity_type, "value": value, "location": location})
        return value

    # non-sensitive defaults; a variant overrides only the part it targets
    header_text = vf.filler_sentence()
    footer_text = vf.filler_sentence()
    footnote_text = vf.filler_sentence()
    creator = "Redactit Corpus"
    last_modified_by = "Redactit Corpus"
    comments: list[tuple[str, str]] = []
    body_paragraphs = [_p(vf.filler_paragraph())]

    if variant == "body":
        person = seed("PERSON", vf.person(), "body")
        email = seed("EMAIL", vf.email(), "body")
        address = seed("ADDRESS", vf.address(), "body")
        body_paragraphs = [
            _p(f"Prepared for {person} ({email})."),
            _p(f"Mailing address: {address}."),
            _p(vf.filler_paragraph()),
        ]

    elif variant == "header_footer":
        h_person = seed("PERSON", vf.person(), "header")
        f_term = seed("COMPANY_TERM", vf.company_term(), "footer")
        header_text = f"Prepared for {h_person}"
        footer_text = f"{f_term} - confidential"

    elif variant == "footnote":
        ssn = seed("US_SSN", vf.us_ssn(), "footnote")
        footnote_text = f"Internal reference SSN {ssn}."
        body_paragraphs = [
            '<w:p><w:r><w:t xml:space="preserve">See the note below.</w:t></w:r>'
            '<w:r><w:footnoteReference w:id="1"/></w:r></w:p>'
        ]

    elif variant == "comments":
        c_person = seed("PERSON", vf.person(), "comment")
        c_email = seed("EMAIL", vf.email(), "comment")
        c_author = seed("PERSON", vf.person(), "comment_author")
        comments = [(c_author, f"Please double check {c_person} at {c_email}.")]
        body_paragraphs = [
            '<w:p><w:commentRangeStart w:id="1"/>'
            '<w:r><w:t xml:space="preserve">Reviewed section.</w:t></w:r>'
            '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p>'
        ]

    elif variant == "tracked_changes":
        ins_author = seed("PERSON", vf.person(), "revision_author")
        del_author = seed("PERSON", vf.person(), "revision_author")
        ins_value = seed("IBAN", vf.iban(), "tracked_insert")
        del_value = seed("CREDIT_CARD", vf.credit_card(), "tracked_delete")
        body_paragraphs = [
            '<w:p><w:r><w:t xml:space="preserve">Account number update: </w:t></w:r>'
            f'<w:ins w:id="10" w:author="{_esc_attr(ins_author)}" w:date="2024-01-01T00:00:00Z">'
            f'<w:r><w:t xml:space="preserve">{_esc(ins_value)}</w:t></w:r></w:ins>'
            f'<w:del w:id="11" w:author="{_esc_attr(del_author)}" w:date="2024-01-01T00:00:00Z">'
            f'<w:r><w:delText xml:space="preserve">{_esc(del_value)}</w:delText></w:r></w:del>'
            "</w:p>"
        ]

    elif variant == "core_props":
        creator = seed("PERSON", vf.person(), "core_props")
        last_modified_by = seed("PERSON", vf.person(), "core_props")

    elif variant == "split_runs":
        value = vf.uk_nino()
        mid = len(value) // 2
        seed("UK_NINO", value, "split_runs")
        body_paragraphs = [
            "<w:p>"
            f'<w:r><w:t xml:space="preserve">{_esc(value[:mid])}</w:t></w:r>'
            f'<w:r><w:t xml:space="preserve">{_esc(value[mid:])}</w:t></w:r>'
            "</w:p>"
        ]

    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": PACKAGE_RELS,
        "word/document.xml": _document_xml("".join(body_paragraphs)),
        "word/_rels/document.xml.rels": DOCUMENT_RELS,
        "word/header1.xml": _header_xml(header_text),
        "word/footer1.xml": _footer_xml(footer_text),
        "word/footnotes.xml": _footnotes_xml(footnote_text),
        "word/comments.xml": _comments_xml(comments),
        "docProps/core.xml": _core_xml(creator, last_modified_by),
    }
    _write_docx(path, parts)
    return entries
