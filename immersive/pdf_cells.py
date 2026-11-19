"""Locate tagged PDF table cells using their marked-content IDs and native text bounds."""

from collections import defaultdict
from contextlib import closing, contextmanager
from dataclasses import dataclass
import logging

import pymupdf as pdf


@dataclass
class Cell:
    row: int
    table: int


def _cell_index(reader):
    """Read the structure once. Missing parent pointers in producer PDFs are common."""
    root = reader.root_object.get("/StructTreeRoot")
    pages = {page.indirect_reference.idnum: i for i, page in enumerate(reader.pages)}
    index = defaultdict(dict)
    visited = set()
    stack = [(root, None, None, None, None)]
    while stack:
        node, page, table, row, cell = stack.pop()
        if hasattr(node, "idnum"):
            ref = (node.idnum, node.generation)
            if ref in visited:
                continue
            visited.add(ref)
            node = node.get_object()
        if isinstance(node, list):
            stack.extend((child, page, table, row, cell) for child in reversed(node))
        elif isinstance(node, dict):
            if "/Pg" in node:
                page = pages.get(getattr(node.raw_get("/Pg"), "idnum", None))
            tag = node.get("/S")
            if tag == "/Table":
                table = id(node)
            if tag == "/TR":
                row = id(node)
            if tag in ("/TD", "/TH") and table is not None and row is not None:
                cell = Cell(row, table)
            if "/Stm" in node:
                continue  # Form XObjects have their own MCID namespace.
            stack.append((node.get("/K", node.get("/MCID")), page, table, row, cell))
        elif isinstance(node, int) and cell is not None and page is not None:
            index[page][int(node)] = cell
    return index


@contextmanager
def tagged_tables(path, doc):
    """Provide table coordinates if the PDF contains a usable structure tree."""
    source = None
    locator = lambda page: []
    try:
        if doc.xref_get_key(doc.pdf_catalog(), "StructTreeRoot")[0] != "null":
            from pypdf import PdfReader
            import pypdfium2 as pdfium

            with open(path, "rb") as stream:
                marks = _cell_index(PdfReader(stream))
            if marks:
                source = pdfium.PdfDocument(str(path))
                def locator(page):
                    try:
                        return _positions(source, marks.get(page.number, {}), page)
                    except (pdfium.PdfiumError, ValueError, IndexError) as error:
                        logging.getLogger(__name__).debug("Malformed PDF cell bounds: %s", error)
                        return []
    except Exception as exc:
        # PDF structure metadata is supplemental; malformed tags must never hide text.
        logging.getLogger(__name__).debug("Using geometric PDF segmentation: %s", exc)
    try:
        yield locator
    finally:
        if source is not None:
            source.close()


@dataclass
class CellBox:
    ink: pdf.Rect
    right: float
    bottom: float


def _positions(source, marks, page):
    if not marks or page.rotation:
        return []
    from pypdfium2 import raw

    ink = {}
    with closing(source[page.number]) as native:
        for obj in native.get_objects(max_depth=1, filter=[raw.FPDF_PAGEOBJ_TEXT]):
            cell = marks.get(raw.FPDFPageObj_GetMarkedContentID(obj))
            if cell is None:
                continue
            bounds = pdf.Rect(obj.get_bounds()) * page.transformation_matrix
            if not bounds.is_empty:
                key = (cell.table, cell.row, id(cell))
                ink[key] = ink.get(key, pdf.Rect()) | bounds
    tables = defaultdict(lambda: defaultdict(list))
    for (table, row, _), rect in ink.items():
        tables[table][row].append(rect)
    cells = []
    for rows in tables.values():
        ordered = sorted(rows.values(), key=lambda row: min(r.y0 for r in row))
        edge = max(rect.x1 for row in ordered for rect in row)
        for i, row in enumerate(ordered):
            row.sort(key=lambda rect: rect.x0)
            bottom = min(r.y0 for r in ordered[i+1]) if i+1 < len(ordered) else 0
            for j, rect in enumerate(row):
                cells.append(CellBox(rect, row[j+1].x0 if j+1 < len(row) else edge, bottom))
    return cells
