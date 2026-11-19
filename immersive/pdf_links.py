"""Keep original PDF actions and put translated-side hit areas on visible labels."""

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import re
from unicodedata import normalize
from urllib.parse import unquote

import pymupdf as pdf


_SECTION = re.compile(r"(?:sec(?:tion)?\.?|第)\s*(\d+(?:\.\d+)*)(?:节)?", re.I)
_FIG_TABLE = re.compile(r"((?i:fig(?:ure)?|table)s?\.?|图|表)\s*(S?\d+(?:\.\d+)*)(\([a-z]\))?")


@lru_cache(maxsize=4096)
def _norm(text):
    return "".join(c for c in normalize("NFKC", text)
                   if not c.isspace() and c not in "\u200b\u200c\u00ad\ufeff")


def _named_target(doc, details):
    page_no = details.get("page", -1)
    if not 0 <= page_no < len(doc):
        return None
    page = doc[page_no]
    operands = details.get("dest", "").split()
    point = None
    if details.get("to") is not None:
        point = pdf.Point(details["to"])
    elif operands:
        try:
            if operands[0] == "/FitR" and len(operands) == 5:
                x0, y0, x1, y1 = map(float, operands[1:])
                point = pdf.Point(min(x0, x1), max(y0, y1))
            elif operands[0] in ("/FitH", "/FitBH") and len(operands) == 2 and operands[1] != "null":
                point = pdf.Point(page.mediabox.x0, float(operands[1]))
            elif operands[0] in ("/FitV", "/FitBV") and len(operands) == 2 and operands[1] != "null":
                point = pdf.Point(float(operands[1]), page.mediabox.y1)
        except ValueError:
            return None
    # resolve_names() returns native PDF bottom-left coordinates. Stored
    # click rectangles from get_links() already use displayed page coordinates.
    to = point * page.transformation_matrix * page.rotation_matrix if point is not None else pdf.Point(0, 0)
    return {"kind": pdf.LINK_GOTO, "page": page_no, "to": to, "zoom": details.get("zoom", 0)}


def collect_links(doc):
    """Capture link metadata before baking or rotating source pages."""
    pages = [p.get_links() for p in doc]
    if not any(x["kind"] == pdf.LINK_NAMED for row in pages for x in row):
        return pages
    names = doc.resolve_names()
    sections = defaultdict(list)
    for _, title, _, action in doc.get_toc(False):
        number = re.match(r"^(\d+(?:\.\d+)*)\.?\s+\S", title.strip())
        if number:
            sections[number[1]].append(action)
    for pno, links in enumerate(pages):
        for link in links:
            if link["kind"] != pdf.LINK_NAMED:
                continue
            name = link.get("nameddest", "")
            dest = _named_target(doc, names.get(name, link))
            if not dest and sections:
                # Some publishers reference a missing name from a literal
                # "Section 3.6". A unique numbered bookmark is explicit evidence.
                label = doc[pno].get_textbox(link["from"]).splitlines()
                match = _SECTION.fullmatch(label[0].strip().strip(".,;:()")) if label else None
                candidates = sections.get(match[1], []) if match else []
                if len(candidates) == 1:
                    action = candidates[0]
                    dest = _named_target(doc, names.get(action.get("nameddest"), {}))
                    if dest is None and action.get("kind") == pdf.LINK_GOTO and isinstance(action.get("to"), pdf.Point):
                        dest = {k: action[k] for k in ("kind", "page", "to", "zoom") if k in action}
            if dest:
                link.update(dest)
    return pages


@dataclass
class TextLine:
    text: str
    boxes: list
    bbox: pdf.Rect


class TextIndex:
    def __init__(self):
        self.lines = []
        self.offsets = []
        self.text = ""

    def append(self, chars):
        text, boxes = [], []
        for char, box in chars:
            for c in _norm(char):
                text.append(c)
                boxes.append(box)
        if text:
            xs, ys, xe, ye = zip(*boxes)
            bbox = pdf.Rect(min(xs), min(ys), max(xe), max(ye))
            self.lines.append(TextLine("".join(text), boxes, bbox))
            self.offsets.extend((len(self.lines)-1, i) for i in range(len(boxes)))

    def anchor(self, rect, allow_short_label=False):
        best, complete = "", False
        for line in self.lines:
            if not line.bbox.intersects(rect):
                continue
            text = "".join(c for c, (x0, y0, x1, y1) in zip(line.text, line.boxes)
                           if rect.x0 - .15 <= (x0+x1)/2 <= rect.x1 + .15
                           and rect.y0 - .15 <= (y0+y1)/2 <= rect.y1 + .15)
            if len(text) > len(best):
                # Some PDF buttons cover most of a short heading, cutting off
                # only trailing glyphs. A complete translated label in the
                # original button can still use that whole button rectangle.
                nearly_whole = (allow_short_label and len(line.text) <= 32 and
                                line.text.startswith(text) and len(text) >= .8 * len(line.text))
                best, complete = text, text == line.text or nearly_whole
        return best, complete

    def find(self, label, numeric_boundary):
        matches = []
        for hit in re.finditer(re.escape(label), self.text):
            if numeric_boundary and self.text[hit.end():hit.end()+1].isdigit():
                continue
            if label[0].isdigit() and hit.start() and self.text[hit.start()-1].isdigit():
                continue
            pieces = []
            for line, char in self.offsets[hit.start():hit.end()]:
                box = self.lines[line].boxes[char]
                if pieces and pieces[-1][0] == line:
                    pieces[-1][1].include_rect(box)
                else:
                    pieces.append((line, pdf.Rect(box)))
            # Font ascenders and descenders often visually share the same
            # leading. A narrower box prevents links on adjacent lines crossing.
            matches.append([pdf.Rect(b.x0, b.y0+.15*b.height, b.x1, b.y1-.15*b.height) for _, b in pieces])
        return matches


def _indexes(sheet, width):
    sides = [TextIndex(), TextIndex()]
    flags = pdf.TEXTFLAGS_RAWDICT & ~pdf.TEXT_PRESERVE_IMAGES
    for block in sheet.get_text("rawdict", flags=flags)["blocks"]:
        for line in block.get("lines", ()):
            chars = [[], []]
            for span in line["spans"]:
                if not span.get("alpha", 255) or not span.get("char_flags", 24) & 24:
                    continue
                for char in span["chars"]:
                    box = char["bbox"]
                    chars[(box[0]+box[2])/2 >= width].append((char["c"], box))
            for side, group in zip(sides, chars):
                side.append(group)
    for side in sides:
        side.text = "".join(line.text for line in side.lines)
    return sides


def _labels(action, anchor):
    label = anchor.strip(".,;:()。，、；：")
    values = []
    if action["kind"] == pdf.LINK_URI:
        uri = action["uri"]
        if uri.lower().startswith("mailto:"):
            values.append(unquote(uri[7:].split("?")[0]))
        else:
            values += [uri, re.sub(r"^https?://", "", uri, flags=re.I)]
            doi = re.match(r"https?://(?:dx\.)?doi\.org/(10\.\S+)", uri, re.I)
            if doi:
                values.append(unquote(doi[1]))
    section = _SECTION.fullmatch(label)
    figure = _FIG_TABLE.fullmatch(label)
    if section:
        values.extend(["第"+section[1]+"节", "Section"+section[1]])
    elif figure:
        kind, num, sub = figure.groups()
        prefix = "图" if kind.lower().startswith(("fig", "图")) else "表"
        values.append(prefix + num + (sub or ""))
    if anchor:
        values.extend([anchor, anchor.replace("–", "-"), anchor.replace("–", "—")])
    return dict.fromkeys(_norm(x) for x in values if x)


def _action(link):
    kind = link["kind"]
    if kind == pdf.LINK_LAUNCH and re.fullmatch(r"www\.[\w.-]+(?:/[^\s]*)?", link.get("file", ""), re.I):
        return {"kind": pdf.LINK_URI, "uri": "https://" + link["file"]}
    if kind == pdf.LINK_GOTO and not isinstance(link.get("to"), pdf.Point):
        return None
    fields = {
        pdf.LINK_URI: ("uri",), pdf.LINK_LAUNCH: ("file",),
        pdf.LINK_GOTO: ("page", "to", "zoom"), pdf.LINK_GOTOR: ("file", "page", "to", "zoom"),
        pdf.LINK_NAMED: ("nameddest",),
    }
    if kind not in fields or (kind == pdf.LINK_NAMED and not link.get("nameddest")):
        return None
    return {"kind": kind, **{k: link[k] for k in fields[kind] if k in link}}


def _distance(box, other):
    return abs(box.y0 - other.y0) + abs(box.x0 - other.x0)


def copy_links(dual, links_by_page, widths):
    """Use translated glyphs for hits, preserve whole-box labels and image buttons."""
    warnings = []
    for n, links in enumerate(links_by_page):
        if not links:
            continue
        sheet = dual[n]
        width = widths[n]
        left, right = _indexes(sheet, width)
        placements = []
        missing = 0
        for link in links:
            action = _action(link)
            if not action or (action["kind"] == pdf.LINK_GOTO and not 0 <= action["page"] < len(widths)):
                missing += 1
                continue
            rect = link["from"]
            sheet.insert_link({**action, "from": rect})
            if action["kind"] == pdf.LINK_NAMED:
                missing += 1  # Still unresolved in the original PDF.
                continue
            if action["kind"] == pdf.LINK_GOTO:
                action = {**action, "to": action["to"] + (widths[action["page"]], 0)}
            projected = rect + (width, 0, width, 0)
            anchor, complete = left.anchor(rect, allow_short_label=True)
            for label in _labels(action, anchor):
                matches = right.find(label, label[-1].isdigit())
                if not matches:
                    continue
                best = min(matches, key=lambda m: _distance(m[0], projected))
                if len(matches) > 1 and len(label) <= 6 and _distance(best[0], projected) > 32:
                    continue
                placements.append((0, action, best))
                break
            else:
                right_label, whole = right.anchor(projected)
                if (not anchor and not right_label) or (complete and whole):
                    placements.append((1, action, [projected]))
                else:
                    missing += 1
        used = []
        for _, action, boxes in sorted(placements, key=lambda p: p[0]):
            for box in boxes:
                if any(rect == box or (other != action and (box & rect).get_area() > .02)
                       for rect, other in used):
                    continue
                sheet.insert_link({**action, "from": box})
                used.append((box, action))
        if missing:
            warnings.append(f"第 {n+1} 页有 {missing} 处链接的目标或译侧锚点无法可靠定位。")
    return warnings
