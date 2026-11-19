"""Identify imported PDF illustrations without decoding their fonts or changing the source."""

from collections import defaultdict
from contextlib import contextmanager
import logging
from pathlib import PurePosixPath
import re

import pymupdf as pdf


CAPTION = re.compile(r"^\s*(figure|fig\.?|图|table|表)\s*[A-Z]?\d+[a-z]?(?:[.:-]|[：、—–])", re.I)
FIGURE_PATH = re.compile(r"^(?:fig(?:ure)?s?|illustrations?|charts?|diagrams?)$", re.I)
FIGURE_NAME = re.compile(r"^(?:fig(?:ure)?[\d_.-]|(?:chart|diagram|illustration)(?:[\d_.-]|$))", re.I)
TABLE_NAME = re.compile(r"^(?:tables?|tab)[\d_.-]", re.I)

# Publishers' vector figures can contain megabytes of path commands. A paint
# operator at the top level already establishes that the Form contains art.
# Recognize only simple tokens here. Strings, dictionaries and complex arrays
# fall through to pypdf's full content parser.
_FORM_TOKEN = re.compile(rb"[\x00\t\n\f\r ]+|%[^\r\n]*|/[^\x00\t\n\f\r ()<>\[\]{}/%]*"
                         rb"|\[[-+.\d\x00\t\n\f\r ]*\]|([^\x00\t\n\f\r ()<>\[\]{}/%]+)|(.)", re.S)
_PAINT = {b"S", b"s", b"B", b"B*", b"b", b"b*", b"f", b"F", b"f*", b"sh", b"BI"}


def _paints_in_prefix(data):
    for token in _FORM_TOKEN.finditer(data, 0, min(8192, len(data))):
        if token[2] or (token.end() == 8192 and len(data) > 8192):
            break
        if token[1] in _PAINT:
            return True
    return False


def _named_figure(doc, xref):
    kind, value = doc.xref_get_key(xref, "PTEX.FileName")
    if kind != "string":
        return False
    path = PurePosixPath(value.replace("\\", "/"))
    return not TABLE_NAME.match(path.stem) and (
        any(FIGURE_PATH.fullmatch(part) for part in path.parts[:-1]) or bool(FIGURE_NAME.match(path.stem)))


def _captions(page):
    flags = pdf.TEXTFLAGS_DICT & ~pdf.TEXT_PRESERVE_IMAGES
    for block in page.get_text("dict", flags=flags)["blocks"]:
        for line in block.get("lines", ()):
            match = CAPTION.match("".join(span["text"] for span in line["spans"]))
            if match:
                yield match[1].lower() not in {"table", "表"}, pdf.Rect(line["bbox"])


def _near_figure(box, captions, page):
    if box.is_empty or box.width < 24 or box.height < 16 or box.get_area() > page.cropbox.get_area() * .65:
        return False
    nearby = []
    for figure, caption in captions:
        overlap = min(box.x1, caption.x1) - max(box.x0, caption.x0)
        gap = max(caption.y0 - box.y1, box.y0 - caption.y1)
        if (overlap > .6 * min(box.width, caption.width) and -.5 <= gap <= 1.5 * caption.height):
            nearby.append((gap, figure))
    return bool(nearby and min(nearby)[1])


def _placed_forms(reader_page, page, candidates):
    """Apply the PDF graphics-state transforms to the clipped Form BBox."""
    contents = reader_page.get_contents()
    if contents is None:
        return
    matrix, stack = pdf.Identity, []
    for args, op in contents.operations:
        if op == b"q":
            stack.append(matrix)
        elif op == b"Q":
            matrix = stack.pop() if stack else pdf.Identity
        elif op == b"cm" and len(args) == 6:
            matrix = pdf.Matrix(*(float(v) for v in args)) * matrix
        elif op == b"Do" and args and str(args[0])[1:] in candidates:
            xref, local_box = candidates[str(args[0])[1:]]
            yield xref, pdf.Rect(local_box) * matrix * page.transformation_matrix


def _contains_art(reader, xref, visited):
    from pypdf.generic import ContentStream, IndirectObject
    if xref in visited:
        return False
    visited.add(xref)
    form = reader.get_object(IndirectObject(xref, 0, reader))
    if _paints_in_prefix(form.get_data()):
        return True
    for args, op in ContentStream(form, reader).operations:
        if op in _PAINT or op == b"INLINE IMAGE":
            return True
        if op == b"Do" and args:
            resources = form.get("/Resources")
            if resources is None:
                continue
            children = resources.get_object().get("/XObject")
            if children is None or args[0] not in children.get_object():
                continue
            child = children.get_object()[args[0]]
            if child.get("/Subtype") == "/Image":
                return True
            if child.get("/Subtype") == "/Form" and _contains_art(reader, child.indirect_reference.idnum, visited):
                return True
    return False


def _figure_refs(path, doc):
    known = set()
    unidentified = defaultdict(list)
    candidates = {}
    for page in doc:
        forms = [(xref, name, box) for xref, name, parent, box in page.get_xobjects() if not parent]
        potential = {}
        for xref, name, box in forms:
            if _named_figure(doc, xref):
                known.add(xref)
            else:
                potential[name] = (xref, box)
        if potential:
            candidates[page.number] = potential
    if not candidates:
        return known

    # Only consult the page drawing stream if an independent figure caption exists.
    # Shared Forms used elsewhere as ordinary content must remain visible there.
    from pypdf import PdfReader
    with open(path, "rb") as stream:
        reader = None
        for number, forms in candidates.items():
            page = doc[number]
            captions = list(_captions(page))
            if not any(figure for figure, _ in captions):
                for xref, _ in forms.values():
                    unidentified[xref].append(False)
                continue
            try:
                if reader is None:
                    reader = PdfReader(stream)
                for xref, box in _placed_forms(reader.pages[number], page, forms):
                    illustration = _near_figure(box, captions, page) and _contains_art(reader, xref, set())
                    unidentified[xref].append(illustration)
            except Exception as error:
                logging.getLogger(__name__).debug("Incomplete PDF figure metadata: %s", error)
                for xref, _ in forms.values():
                    unidentified[xref].append(False)
    return known | {xref for xref, placements in unidentified.items() if all(placements)}


@contextmanager
def prose_view(path, doc):
    """Omit complete illustration streams only in a temporary view used to locate prose."""
    refs = _figure_refs(path, doc)
    if not refs:
        yield doc, refs
        return
    with pdf.open(path) as view:
        for xref in refs:
            view.update_stream(xref, b"")
        yield view, refs


@contextmanager
def protect_forms(doc, refs):
    """Keep MuPDF's text redactor out of figure streams, including overlapping native glyphs."""
    backups = {}
    try:
        for xref in refs:
            saved = doc.get_new_xref()
            doc.update_object(saved, "<<>>")
            doc.xref_copy(xref, saved)
            backups[xref] = saved
            # With PDF_REDACT_IMAGE_NONE, MuPDF retains an image invocation,
            # its transform, clipping and paint order even inside a redaction.
            doc.update_object(xref, "<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
                                    "/ColorSpace /DeviceGray /BitsPerComponent 8 >>")
            doc.update_stream(xref, b"\xff")
        yield
    finally:
        for xref, saved in backups.items():
            doc.xref_copy(saved, xref)
