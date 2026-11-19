"""Append bilingual Word paragraphs without rebuilding unrelated OPC parts.

The run writer and OOXML element classes come from python-openxml/python-docx.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
from zipfile import ZipFile

from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Pt
from lxml import etree


STORIES = {"document.main", "header", "footer", "footnotes", "endnotes"}
WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml."
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class StrictDocument(ValueError):
    """The source uses ISO Strict namespaces and needs Word normalization."""


def _parts(package):
    types = etree.fromstring(package.read("[Content_Types].xml"))
    result = {}
    for item in types:
        mime = item.get("ContentType", "")
        if mime.startswith(WORD_TYPE) and mime.endswith("+xml"):
            kind = mime[len(WORD_TYPE):-4]
            result.setdefault(kind, []).append(item.get("PartName").lstrip("/"))
    if "document.main" not in result:
        raise ValueError("无法识别 Word 主文档部件。")
    return result


@dataclass
class Fragment:
    text: str
    props: object = None
    link: object = None
    fixed: object = None  # A run child or a complete OMML equation.
    style: tuple = ()


def _style(props, link):
    # Namespace prefixes and declarations are not formatting differences.
    metadata = {qn("w:noProof"), qn("w:lang"), qn("w:rPrChange")}
    properties = tuple(etree.tostring(el, method="c14n", exclusive=True) for el in props
                       if el.tag not in metadata) if props is not None else ()
    target = tuple(sorted(link.attrib.items())) if link is not None else ()
    return (properties, target)


def _reference_field(instruction):
    # Repeating a PAGE or REF is safe. Repeating a SEQ would advance the counter.
    kind = instruction.strip().split(maxsplit=1)
    if kind and kind[0].upper() in {"PAGE", "NUMPAGES", "SECTION", "SECTIONPAGES", "REF", "PAGEREF", "NOTEREF"}:
        field = OxmlElement("w:fldSimple", attrs={qn("w:instr"): instruction})
        field.append(OxmlElement("w:r"))
        return field
    return None


def _fragments(paragraph, fields):
    pieces = []

    def emit(text, props=None, link=None, fixed=None):
        if not text and fixed is None:
            return
        key = _style(props, link)
        if fixed is None and pieces and pieces[-1].fixed is None and pieces[-1].style == key:
            pieces[-1].text += text
        else:
            pieces.append(Fragment(text, props, link, fixed, key))

    def walk(el, props=None, link=None):
        tag = el.tag
        if tag == qn("w:fldChar"):
            event = el.get(qn("w:fldCharType"))
            if event == "begin":
                fields.append({"instruction": "", "result": False, "native": None})
            elif event == "separate" and fields:
                state = fields[-1]
                state["result"] = True
                native = _reference_field(state["instruction"])
                if native is not None and all(f["result"] for f in fields):
                    state["native"] = native
                    emit(state["instruction"].strip(), link=link, fixed=native)
            elif event == "end" and fields:
                fields.pop()
        elif tag == qn("w:instrText") and fields:
            fields[-1]["instruction"] += el.text or ""
        elif tag == qn("w:fldSimple"):
            cached = "".join(n.text or "" for n in el.iter(qn("w:t")))
            instruction = el.get(qn("w:instr"), "")
            native = _reference_field(instruction)
            if native is not None:
                native[0].add_t(cached)
                emit(cached or instruction.strip(), link=link, fixed=native)
            elif cached:
                emit(cached, props, link, _literal(cached))
        elif tag == qn("w:t"):
            value = el.text or ""
            if not fields:
                emit(value, props, link)
            elif value and all(f["result"] for f in fields):
                native = fields[-1]["native"]
                if native is not None:
                    native[0].add_t(value)
                else:
                    emit(value, props, link, _literal(value))
        elif tag in (qn("m:oMath"), qn("m:oMathPara")):
            emit("".join(n.text or "" for n in el.iter(qn("m:t"))), fixed=el)
        elif tag in (qn("w:tab"), qn("w:br"), qn("w:cr"), qn("w:ptab"), qn("w:sym")):
            # A hard page or column break belongs to the source once.
            if el.get(qn("w:type")) not in ("page", "column"):
                emit(etree.QName(el).localname, props, link, el)
        elif tag in (qn("w:noBreakHyphen"), qn("w:softHyphen")):
            emit("‑" if tag == qn("w:noBreakHyphen") else "\u00ad", props, link)
        elif tag in (qn("w:r"), qn("w:hyperlink"), qn("w:ins"), qn("w:moveTo"),
                     qn("w:smartTag"), qn("w:sdt"), qn("w:sdtContent"), qn("w:customXml")):
            if tag == qn("w:r"):
                props = el.find(qn("w:rPr"))
                hidden = props.find(qn("w:vanish")) if props is not None else None
                if hidden is not None and hidden.get(qn("w:val"), "1") not in ("0", "false", "off"):
                    return
            if tag == qn("w:hyperlink"):
                link = el
            for child in el:
                walk(child, props, link)

    for child in paragraph:
        walk(child)
    return pieces


def _literal(text):
    return OxmlElement("w:r").add_t(text)


class InlineLayout:
    """Expose only differing run styles, links, and immutable inline objects."""

    def __init__(self, pieces):
        self.pieces = pieces
        weights = Counter()
        for piece in pieces:
            if piece.fixed is None and piece.link is None:
                weights[piece.style] += len(piece.text.strip())
        default_style = weights.most_common(1)[0][0] if weights else None
        self.default = next((f for f in pieces if f.style == default_style and f.fixed is None), Fragment(""))
        self.prefix = ""
        literal = "".join(p.text for p in pieces if p.fixed is None)
        while re.search(r"⟦/?" + self.prefix + r"[0-9]+/?⟧", literal):
            self.prefix += "W"
        self.tokens = {}
        text = []
        notes = []
        for i, piece in enumerate(pieces, 1):
            label = self.prefix + str(i)
            if piece.fixed is not None:
                token = f"⟦{label}/⟧"
                self.tokens[token] = ("fixed", piece)
                text.append(token)
                notes.append(f"{token} = {piece.text}")
            elif piece.style != default_style:
                start, end = f"⟦{label}⟧", f"⟦/{label}⟧"
                self.tokens[start] = ("open", piece)
                self.tokens[end] = ("close", piece)
                text.extend((start, piece.text, end))
            else:
                text.append(piece.text)
        self.text = "".join(text)
        self.notes = "；".join(notes)

    def write(self, paragraph, translation):
        token_re = re.compile(r"(⟦/?" + self.prefix + r"[0-9]+/?⟧)")
        chunks = token_re.split(translation)
        if Counter(chunks[1::2]) != Counter(self.tokens.keys()):
            raise ValueError("译文中的 Word 格式或公式标记缺失、重复或多余。")
        active = None
        translated_text = []
        for index, chunk in enumerate(chunks):
            if index % 2 == 0:
                if chunk:
                    _write_piece(paragraph, active or self.default, chunk)
                    translated_text.append(chunk)
                continue
            kind, piece = self.tokens[chunk]
            if kind == "fixed" and active is None:
                _write_piece(paragraph, piece)
            elif kind == "open" and active is None:
                active = piece
            elif kind == "close" and active is piece:
                active = None
            else:
                raise ValueError(f"Word 标记 {chunk} 的配对位置有误。")
        if not "".join(translated_text).strip():
            raise ValueError("Word 段落的译文为空。")


def _write_piece(paragraph, piece, text=None):
    parent = paragraph
    if piece.link is not None:
        parent = OxmlElement("w:hyperlink", attrs=dict(piece.link.attrib))
        paragraph.append(parent)
    if piece.fixed is not None and (piece.fixed.tag == qn("w:fldSimple") or etree.QName(piece.fixed).namespace != WORD_NS):
        parent.append(deepcopy(piece.fixed))
        return
    run = OxmlElement("w:r")
    parent.append(run)
    if piece.props is not None:
        props = deepcopy(piece.props)
        changes = props.find(qn("w:rPrChange"))
        if changes is not None:
            props.remove(changes)
        run.append(props)
    if piece.fixed is not None:
        run.append(deepcopy(piece.fixed))
    else:
        run.text = text  # python-docx writes native tabs, line breaks and xml:space.


def _paragraphs(root, part):
    namespace = etree.QName(root).namespace
    if namespace == "http://purl.oclc.org/ooxml/wordprocessingml/main" and etree.QName(root).localname == "document":
        raise StrictDocument("此 Word 文件使用 Strict OOXML，需要转换为常规 .docx。")
    if namespace != WORD_NS:
        raise ValueError(f"Word 部件 {part} 的命名空间无法识别。")
    field_states = {}
    for i, paragraph in enumerate(root.iter(qn("w:p")), 1):
        ancestors = list(paragraph.iterancestors())
        if any(a.tag in (qn("w:del"), qn("w:moveFrom")) for a in ancestors):
            continue
        story = next((a for a in ancestors if a.tag in
                      (qn("w:txbxContent"), qn("w:footnote"), qn("w:endnote"))), root)
        pieces = _fragments(paragraph, field_states.setdefault(story, []))
        if any(f.fixed is None and any(c.isalpha() for c in f.text) for f in pieces):
            yield f"{part}#{i}", paragraph, InlineLayout(pieces)


def extract(path: Path) -> list[dict]:
    result = []
    with ZipFile(path) as package:
        parts = _parts(package)
        for kind in sorted(STORIES, key=lambda item: (item != "document.main", item)):
            names = parts.get(kind, [])
            for name in names:
                for key, _, layout in _paragraphs(parse_xml(package.read(name)), name):
                    row = {"id": key, "text": layout.text}
                    if layout.notes:
                        row["context"] = layout.notes
                    result.append(row)
    return result


def _val(el, tag):
    child = el.find(qn(tag)) if el is not None else None
    return child.get(qn("w:val")) if child is not None else None


class Numbering:
    def __init__(self, package, parts):
        self.styles = {}
        self.nums = {}
        self.levels = {}
        self.linked = {}
        for name in parts.get("styles", []):
            for style in parse_xml(package.read(name)).iter(qn("w:style")):
                self.styles[style.get(qn("w:styleId"))] = style
        for name in parts.get("numbering", []):
            numbering = parse_xml(package.read(name))
            for num in numbering.iter(qn("w:num")):
                self.nums[num.get(qn("w:numId"))] = _val(num, "w:abstractNumId")
            for abstract in numbering.iter(qn("w:abstractNum")):
                aid = abstract.get(qn("w:abstractNumId"))
                for level in abstract.findall(qn("w:lvl")):
                    key = (aid, level.get(qn("w:ilvl")))
                    self.levels[key] = level
                    self.linked[_val(level, "w:pStyle")] = key

    def continue_list(self, props, source_style):
        num_id = level = None
        node = props
        style_id = source_style
        visited = set()
        while node is not None:
            num = node.find(qn("w:numPr"))
            num_id = num_id or _val(num, "w:numId")
            level = level or _val(num, "w:ilvl")
            style = self.styles.get(style_id)
            if style is None or style_id in visited:
                break
            visited.add(style_id)
            node = style.find(qn("w:pPr"))
            style_id = _val(style, "w:basedOn")
            if node is None:
                node = style  # Continue to base style even if this style has no pPr.
        linked = self.linked.get(source_style) if source_style else None
        if num_id == "0" or (not num_id and linked is None):
            return
        definition = self.levels.get((self.nums.get(num_id), level or "0"))
        if definition is None and linked:
            definition = self.levels.get(linked)
        indent = props.get_or_add_ind()
        if definition is not None:
            inherited = definition.find(qn("w:pPr") + "/" + qn("w:ind"))
            if inherited is not None:
                for attr, value in inherited.attrib.items():
                    if attr not in indent.attrib:
                        indent.set(attr, value)
        for field in ("hanging", "hangingChars", "firstLine", "firstLineChars"):
            indent.attrib.pop(qn("w:" + field), None)
        indent.set(qn("w:firstLine"), "0")
        props.get_or_add_numPr().get_or_add_numId().val = 0


def _append(source, layout, translation, numbering):
    target = OxmlElement("w:p")
    if source.pPr is not None:
        target.append(deepcopy(source.pPr))
    props = target.get_or_add_pPr()
    for tag in ("w:sectPr", "w:pPrChange", "w:rPrChange", "w:ins", "w:del", "w:moveFrom", "w:moveTo", "w:numberingChange"):
        for old in list(props.iter(qn(tag))):
            old.getparent().remove(old)
    # Keep the paired paragraphs together in the same section.
    if source.pPr is not None and source.pPr.sectPr is not None:
        props._insert_sectPr(source.pPr.sectPr)
    numbering.continue_list(props, source.style)
    props.get_or_add_pageBreakBefore().val = False
    spacing = props.get_or_add_spacing()
    spacing.before = Pt(6)
    for attr in ("w:beforeLines", "w:beforeAutospacing"):
        spacing.attrib.pop(qn(attr), None)
    layout.write(target, translation)
    source.addnext(target)
    for row in source.iterancestors(qn("w:tr")):
        for height in row.xpath("./w:trPr/w:trHeight"):
            if height.get(qn("w:hRule")) == "exact":
                height.set(qn("w:hRule"), "atLeast")


def apply(path: Path, translations: dict[str, str], output: Path) -> list[str]:
    if Path(path).resolve() == Path(output).resolve():
        raise ValueError("请选择与原 Word 文档不同的输出文件。")
    warnings = []
    with ZipFile(path) as package:
        parts = _parts(package)
        numbering = Numbering(package, parts)
        updates = {}
        expected = set()
        for kind, names in parts.items():
            if kind not in STORIES:
                continue
            for name in names:
                root = parse_xml(package.read(name))
                # Materialize the original paragraphs before inserting new siblings.
                entries = list(_paragraphs(root, name))
                in_shapes = []
                for key, paragraph, layout in entries:
                    expected.add(key)
                    if key not in translations:
                        raise ValueError(f"缺少 Word 段落译文 {key}")
                    try:
                        _append(paragraph, layout, translations[key], numbering)
                    except ValueError as err:
                        raise ValueError(f"{key} — {err}") from err
                    if next(paragraph.iterancestors(qn("w:txbxContent")), None) is not None:
                        in_shapes.append(key)
                if in_shapes:
                    warnings.append("请核对文本框容量和译文可见性：" + "、".join(in_shapes))
                if entries:
                    updates[name] = etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)
        if expected != translations.keys():
            raise ValueError("译文中包含与 Word 原稿不匹配的段落 ID。")
        if any(n.startswith("_xmlsignatures/") for n in package.namelist()):
            warnings.append("双语 Word 副本中的原数字签名失效。")
        with ZipFile(output, "w") as target:
            target.comment = package.comment
            for info in package.infolist():
                data = updates[info.filename] if info.filename in updates else package.read(info)
                target.writestr(info, data)
    return warnings
