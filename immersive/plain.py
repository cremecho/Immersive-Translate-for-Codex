"""Paragraph-aligned TXT and Markdown edits. Insert into source offsets, never reserialize it."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from pathlib import Path


def _decode(data: bytes):
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(bom):
            return data[len(bom):].decode(encoding), encoding, bom, []
    if b"\0" in data:
        for parity, encoding in ((1, "utf-16-le"), (0, "utf-16-be")):
            part = data[parity::2]
            if part and part.count(0) > len(part) / 2:
                return data.decode(encoding), encoding, b"", []
        raise ValueError("输入含有无法识别的空字节。请将文本保存为带 BOM 的 Unicode 文件。")
    for encoding in ("utf-8", "gb18030", "cp1252"):
        try:
            body = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        warnings = [] if encoding == "utf-8" else [f"原文按 {encoding} 解码。"]
        return body, encoding, b"", warnings
    raise ValueError("无法识别文本编码。请将原文保存为 UTF-8 或带 BOM 的 Unicode 文件。")


@dataclass
class _Line:
    body: str
    ending: str
    start: int

    @property
    def end(self):
        return self.start + len(self.body)


def _lines(body):
    lines = []
    start = 0
    for match in re.finditer(r"\r\n|\r|\n", body):
        lines.append(_Line(body[start:match.start()], match[0], start))
        start = match.end()
    if start < len(body):
        lines.append(_Line(body[start:], "", start))
    return lines


@dataclass
class _Segment:
    id: str
    position: int
    lead: str
    text: str
    context: str
    # Plain text can contain literal placeholder-like text. Only MD uses markers.
    symbol: str = ""
    protected: list[str] = field(default_factory=list)
    table: bool = False

    def render(self, translated):
        if not isinstance(translated, str) or not translated.strip():
            raise ValueError(f"{self.id} 缺少译文")
        if not self.symbol:
            return self.lead + re.sub(r"\r\n?|\n", lambda _: self.lead, translated.strip())
        pattern = rf"⟦{self.symbol}(\d+)⟧"
        expected = [str(i) for i in range(1, len(self.protected) + 1)]
        if re.findall(pattern, translated) != expected:
            raise ValueError(f"{self.id} 的占位符应各出现一次并保持原顺序：{self.text}")
        # Escape translated literals independently of trusted markup. This also
        # keeps a translated table cell containing a pipe inside that same cell.
        pieces = re.split(pattern, translated.strip())
        for i in range(0, len(pieces), 2):
            literal = re.sub(r"\s*[\r\n]+\s*", " ", pieces[i])
            pieces[i] = re.sub(r"([\\`*_{}\[\]<>~])", r"\\\1", literal)
        for i in range(1, len(pieces), 2):
            pieces[i] = self.protected[int(pieces[i]) - 1]
        result = "".join(pieces)
        if self.table:
            result = re.sub(r"(?<!\\)\|", r"\\|", result)
        # Block-opening punctuation only needs quoting at the start of a line.
        result = re.sub(r"^(#{1,6}|[-+]|\d{1,9}[.)])(?=[ \t])",
                        lambda m: m[0][:-1] + "\\" + m[0][-1], result)
        return self.lead + result


_INDENT = re.compile(r"^[ \t\u3000]*")
_LIST_START = re.compile(
    r"^(?:[-+*•][ \t]+|\d{1,4}[.)](?=[ \t\u3000\u3400-\u9fff])|"
    r"(?:\d{1,4}|[一二三四五六七八九十百]+)、|[（(][\d一二三四五六七八九十百]+[）)])"
)
_SENTENCE_END = re.compile(r"""[.!?。！？…]["'”’」』）)\]]*$""")
_ABBREVIATION = re.compile(
    r"\b(?:mr|mrs|ms|dr|prof|fig|eq|no|sec|vol|e\.g|i\.e|et al|[a-z](?:\.[a-z])+|[A-Z])\.$",
    re.IGNORECASE,
)


def _paragraph_starts(previous, current):
    indent = _INDENT.match(current)[0]
    following = current[len(indent):]
    if _LIST_START.match(following):
        return True
    if "\u3000" in indent:
        return True
    if len(indent.expandtabs()) >= 2 and len(indent.expandtabs()) > len(_INDENT.match(previous)[0].expandtabs()):
        return True
    tail = previous.rstrip()
    if not _SENTENCE_END.search(tail) or _ABBREVIATION.search(tail):
        return False
    return not following[0].islower()


def _txt(lines, eol):
    segments = []
    # In a file with blank paragraph separators, internal physical newlines
    # are soft wraps. With only single newlines, infer likely paragraph starts.
    occupied = [n for n, line in enumerate(lines) if line.body.strip()]
    infer_starts = bool(occupied) and occupied[-1] - occupied[0] + 1 == len(occupied)
    start = 0
    while start < len(lines):
        if not lines[start].body.strip():
            start += 1
            continue
        end = start + 1
        while end < len(lines) and lines[end].body.strip():
            if infer_starts and _paragraph_starts(lines[end - 1].body, lines[end].body):
                break
            end += 1
        text = "\n".join(line.body.strip() for line in lines[start:end])
        indent = _INDENT.match(lines[start].body)[0]
        last = lines[end - 1]
        segments.append(_Segment(f"txt-{start + 1}", last.end, (last.ending or eol) + indent,
                                 text, f"第 {start + 1} 至 {end} 行"))
        start = end
    return segments


# Additional inline constructs commonly used in technical Markdown. CommonMark
# syntax (including fenced code, links and emphasis) is parsed by markdown-it-py.
_EXTRA_INLINE = re.compile(
    r"!\[\[[^\]\n]+\]\]|\$\$(?:(?!\$\$)[\s\S])+?\$\$"
    r"|\$(?![\s$])(?:\\.|[^$\n\\])+?(?<!\s)\$(?!\d)"
    r"|\\\((?:(?!\\\))[\s\S])*?\\\)|\\\[(?:(?!\\\])[\s\S])*?\\\]"
)


def _extra_rule(state, silent):
    match = _EXTRA_INLINE.match(state.src, state.pos, state.posMax)
    if not match:
        return False
    if not silent:
        kind = "image" if match[0].startswith("![[") else "literal"
        state.push(kind, "", 0).content = match[0]
    state.pos = match.end()
    return True


_DISPLAY = re.compile(
    r"^(?:\$\$|\\\[|\\begin\{(equation\*?|align(?:at|ed)?\*?|gather(?:ed)?\*?|"
    r"multline\*?|flalign\*?|eqnarray\*?|displaymath|math|cases|array|[bpvVB]?matrix)\})"
)


def _display_rule(state, start, limit, silent):
    """Consume display math as one opaque block, including blank lines inside it."""
    if state.sCount[start] - state.blkIndent >= 4:
        return False

    def line(n):
        return state.src[state.bMarks[n] + state.tShift[n]:state.eMarks[n]].rstrip()

    first = line(start)
    open_mark = _DISPLAY.match(first)
    if not open_mark:
        return False
    if open_mark[1]:
        close = "\\end{" + open_mark[1] + "}"
    else:
        close = "$$" if open_mark[0] == "$$" else "\\]"
    for row in range(start, limit):
        content = first[open_mark.end():] if row == start else line(row)
        if row > start and content and state.sCount[row] < state.blkIndent:
            break
        if close in content:
            if not content.endswith(close):
                return False
            if not silent:
                token = state.push("display_math", "", 0)
                token.map = [start, row + 1]
                state.line = row + 1
            return True
    return False


def _parser():
    from markdown_it import MarkdownIt
    from markdown_it.rules_inline import backtick, link

    md = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    md.inline.ruler.before("text", "immersive_literal", _extra_rule)
    md.block.ruler.before("fence", "immersive_math_block", _display_rule,
                          {"alt": ["paragraph", "reference", "blockquote", "list"]})

    def exact_code(state, silent):
        start = state.pos
        before = len(state.tokens)
        success = backtick(state, silent)
        if success and not silent and len(state.tokens) > before:
            if state.tokens[-1].type == "code_inline":
                state.tokens[-1].meta["source"] = state.src[start:state.pos].replace("\n", " ")
        return success

    def exact_link(state, silent):
        start = state.pos
        success = link(state, silent)
        if success and not silent:
            end = state.pos
            end_label = state.md.helpers.parseLinkLabel(state, start, True)
            tail = state.src[end_label:end]
            # Translating a shortcut/collapsed reference changes its implicit
            # lookup key. Make that same reference explicit in the translation.
            if tail in ("]", "][]"):
                tail = "][" + state.src[start + 1:end_label] + "]"
            state.tokens[-1].meta["source"] = tail
        return success

    md.inline.ruler.at("backticks", exact_code)
    md.inline.ruler.at("link", exact_link)
    return md


_BARE_URL = re.compile(r"(?:https?://|mailto:)[^\s<>]+")


def _inline(token, task=False):
    symbol = "m"
    while re.search(rf"⟦{symbol}\d+⟧", token.content):
        symbol += "m"
    pieces, protected, meanings, prose = [], [], [], []

    def protect(raw, meaning=None):
        protected.append(raw)
        mark = f"⟦{symbol}{len(protected)}⟧"
        pieces.append(mark)
        meanings.append(mark + " " + (meaning or raw).replace("\n", " ")[:120])

    children = token.children or []
    index = 0
    while index < len(children):
        part = children[index]
        index += 1
        kind = part.type
        if kind == "image":
            continue
        if kind == "link_open":
            if part.markup == "autolink":
                protect("<" + children[index].content + ">")
                index += 2
                continue
            if index + 1 < len(children) and children[index].type == "image" and children[index+1].type == "link_close":
                index += 2
                continue
            protect("[", "链接文字开始")
        elif kind == "link_close":
            protect(part.meta["source"], "链接文字结束")
        elif kind in ("text", "text_special"):
            content = part.content
            if task and index == 1:
                content = re.sub(r"^\[[ xX]\][ \t]+", "", content)
            end = 0
            for match in _BARE_URL.finditer(content):
                pieces.append(content[end:match.start()])
                prose.append(pieces[-1])
                protect(match[0])
                end = match.end()
            pieces.append(content[end:])
            prose.append(pieces[-1])
        elif kind == "softbreak":
            pieces.append(" ")
        elif kind == "hardbreak":
            protect("<br>", "换行")
        elif kind == "html_inline":
            if not re.match(r"<(?:img\b|!--)", part.content, re.I):
                protect(part.content)
        elif kind in ("literal", "code_inline"):
            protect(part.meta.get("source", part.content))
        elif part.markup:
            protect(part.markup, kind)
    if not any(letter.isalpha() for text in prose for letter in text):
        return None
    return "".join(pieces).strip(), symbol, protected, "；".join(meanings)


_PREFIX = re.compile(r"^[ \t]*(?:(?:>[ \t]?|(?:[-+*]|\d{1,9}[.)])[ \t]+)[ \t]*)*")
_LIST_MARKER = re.compile(r"(?:[-+*]|\d{1,9}[.)])(?=[ \t])")


def _cell_ends(line, prefix):
    # Matching positions in the untouched row preserves escaped delimiters,
    # optional outer pipes, cell padding and any blockquote/list markers.
    start = len(prefix)
    end = len(line.rstrip(" \t"))
    if start < end and line[start] == "|":
        start += 1
    for match in re.finditer(r"(?<!\\)\|", line[start:end]):
        yield start + match.start()
    if end and (line[end - 1] != "|" or line[max(0, end - 2):end] == "\\|"):
        yield end


def _markdown(lines, eol, warnings):
    md = _parser()
    parse_lines = [line.body for line in lines]
    if parse_lines and parse_lines[0].strip() in ("---", "+++"):
        boundary = parse_lines[0].strip()
        for index, line in enumerate(parse_lines[1:], 1):
            if line.strip() == boundary or (boundary == "---" and line.strip() == "..."):
                parse_lines[:index + 1] = [""] * (index + 1)
                break
    tokens = md.parse("\n".join(parse_lines))
    stack, segments = [], []
    col = 0
    for token in tokens:
        if token.nesting == -1:
            stack.pop()
            continue
        if token.nesting == 1:
            stack.append(token)
            if token.type == "tr_open":
                col = 0
                row = lines[token.map[0]].body
                ends = list(_cell_ends(row, _PREFIX.match(row)[0]))
            continue
        if token.type == "html_block":
            if re.search(r"[^\W\d_]", re.sub(r"<!--[\s\S]*?-->|<[^>]*>", "", token.content)):
                warnings.append(f"第 {token.map[0] + 1} 行的原生 HTML 块保留原样。")
        if token.type != "inline" or not token.map:
            continue
        start, end = token.map
        parent = stack[-1]
        table = parent.type in ("td_open", "th_open")
        if table:
            col += 1
        in_list = any(p.type == "list_item_open" for p in stack)
        parts = _inline(token, task=in_list and parent.type == "paragraph_open")
        if not parts:
            continue
        text, symbol, protected, hints = parts
        if table:
            if col > len(ends):
                raise ValueError(f"第 {start + 1} 行表格无法匹配第 {col} 列")
            pos = lines[start].start + len(lines[start].body[:ends[col - 1]].rstrip(" \t"))
            key, lead = f"md-{start + 1}-c{col}", "<br>"
        else:
            if parent.type == "heading_open":
                end = parent.map[1]  # Include the underline of a setext heading.
            last = lines[end - 1]
            nl = last.ending or eol
            physical = lines[start].body
            prefix = _PREFIX.match(physical)[0]
            continuation = _LIST_MARKER.sub(lambda m: " " * len(m[0]), prefix)
            heading = "#" * int(parent.tag[1:]) + " " if parent.type == "heading_open" else ""
            if in_list and not heading:
                lead = nl + continuation + "<br>"
            else:
                lead = nl + continuation.rstrip() + nl + continuation + heading
            key, pos = f"md-{start + 1}", last.end
        context = f"第 {start + 1} 行，{parent.tag}"
        if hints:
            context += "。" + hints
        segments.append(_Segment(key, pos, lead, text, context, symbol, protected, table))
    return segments


def _load(path):
    body, encoding, bom, warnings = _decode(Path(path).read_bytes())
    lines = _lines(body)
    eol = next((line.ending for line in lines if line.ending), "\n")
    if Path(path).suffix.lower() == ".md":
        segments = _markdown(lines, eol, warnings)
    else:
        segments = _txt(lines, eol)
    return body, encoding, bom, warnings, segments


def extract(path: Path) -> list[dict]:
    return [{"id": seg.id, "text": seg.text, "context": seg.context} for seg in _load(path)[-1]]


def apply(path: Path, translations: dict[str, str], output: Path) -> list[str]:
    body, encoding, bom, warnings, segments = _load(path)
    if set(translations) != {seg.id for seg in segments}:
        raise ValueError("文本段落编号与原文不匹配。")
    pieces, last = [], 0
    for seg in segments:
        pieces.append(body[last:seg.position])
        pieces.append(seg.render(translations[seg.id]))
        last = seg.position
    pieces.append(body[last:])
    result = "".join(pieces)
    try:
        encoded = bom + result.encode(encoding)
    except UnicodeEncodeError:
        encoded = result.encode("utf-8")
        warnings.append(f"译文包含 {encoding} 无法表示的字符，双语文件已保存为 UTF-8。")
    Path(output).write_bytes(encoded)
    return warnings
