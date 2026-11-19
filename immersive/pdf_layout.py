"""Extract PDF text and compose fixed-size bilingual sheets with PyMuPDF."""

from __future__ import annotations

import html
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from contextlib import ExitStack, nullcontext

import pymupdf as pdf

from .pdf_cache import page_cache
from .pdf_cells import tagged_tables
from .pdf_figures import prose_view, protect_forms
from .pdf_fonts import PdfFonts
from .pdf_links import collect_links, copy_links
from .pdf_redact import redact_units

pdf.set_messages(stream=sys.stderr)


ADDRESS = re.compile(r'(?:https?://|www\.)\S+|\S+@\S+\.\S+', re.I)
MATH_FONT = re.compile(r'math|symbol|cmsy|cmmi|cmex|msam|msbm|tex_', re.I)
MATH_SIGN = re.compile(r'[=+×÷±∫∑∏∂∇∞≈≠≤≥<>∈∉⊂∪∩‖√^∣]|(?<![^\W\d_])[\u03b1-\u03c9](?![^\W\d_])')
MATH_NAMES = {'sin', 'cos', 'tan', 'log', 'exp', 'lim', 'min', 'max', 'sqrt', 'arg', 'argmin', 'argmax', 'det'}


@dataclass
class Line:
    spans: list[dict]
    rect: pdf.Rect
    angle: int
    source_line: str = ''

    @property
    def text(self):
        return ''.join(s['text'] for s in self.spans).strip()

    @property
    def style(self):
        return max(self.spans, key=lambda s: len(s['text'].strip()))


@dataclass
class Unit:
    id: str
    text: str
    lines: list[Line]
    rect: pdf.Rect
    kind: str = ''
    formulas: tuple = ()


def _bounds(rects):
    result = pdf.Rect()
    for rect in rects:
        result |= pdf.Rect(rect)
    return result


def _read_lines(block, skipped):
    """Keep separated table cells and column fragments in independent boxes."""
    for line in block['lines']:
        dx, dy = line['dir']
        degrees = math.degrees(math.atan2(-dy, dx))
        quarter = round(degrees / 90) * 90
        if abs(degrees - quarter) > 1 or line['wmode']:
            skipped.add('angled')
            yield None
            continue
        group = []
        for span in line['spans']:
            if not span['char_flags'] & 24 or span['alpha'] == 0:
                continue  # An OCR layer over a page image is not visible type.
            if not span['text']:
                continue
            if group and quarter == 0:
                gap = span['bbox'][0] - group[-1]['bbox'][2]
                if gap > 1.6 * span['size']:
                    yield Line(group, _bounds(s['bbox'] for s in group), 0)
                    yield None
                    group = []
            group.append(span)
        if group:
            yield Line(group, _bounds(s['bbox'] for s in group), quarter % 360)


def _math_evidence(spans, text):
    return bool(MATH_SIGN.search(text) or any(s['text'].strip() and MATH_FONT.search(s['font']) for s in spans))


def _unprotected_math(unit):
    if unit.kind:
        return bool(MATH_SIGN.search(re.sub(r'⟦f\d+⟧', ' ', unit.text)))
    return any(_math_evidence(line.spans, line.text) for line in unit.lines)


def _visual_lines(block, skipped):
    pending = None
    for line in _read_lines(block, skipped):
        # Fully justified publishers' PDFs sometimes expose every word on the
        # same baseline as a separate line, with literal spaces after each word.
        if pending and line and not pending.angle and not line.angle:
            size = pending.style['size']
            gap = line.rect.x0 - pending.rect.x1
            same_baseline = abs(line.style['origin'][1] - pending.style['origin'][1]) < .12 * size
            if same_baseline and 0 <= gap <= 1.3 * size and pending.spans[-1]['text'].endswith(' '):
                pending = Line(pending.spans + line.spans, pending.rect | line.rect, 0)
                continue
        if pending is not None:
            yield pending
        if line is None:
            yield None
        pending = line
    if pending is not None:
        yield pending


def _formula(spans, text):
    if not _math_evidence(spans, text):
        return False
    # Natural-language words distinguish 'the force F = ma' from 'F = ma'.
    # Retain common mathematical functions as part of the equation.
    if re.search(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]', text):
        return False
    words = (w for w in ''.join(c if c.isalpha() else ' ' for c in text).split() if len(w) >= 3)
    return all((word.isupper() and len(word) <= 5) or word.lower() in MATH_NAMES for word in words)


def _prose_parts(line):
    """Keep display equations and distinct inline math runs at their PDF positions."""
    if line is None or not _math_evidence(line.spans, line.text):
        yield line
        return
    if _formula(line.spans, line.text):
        yield None
        return
    protected = [_formula([s], s['text']) for s in line.spans]
    if not any(protected):
        yield line  # Formula and prose share one PDF span; flag the unit for review.
        return
    # Subscripts, variables and parentheses frequently use adjacent non-math fonts.
    variable = re.compile(r'[\s\d\W_]*[A-Za-zα-ωΑ-Ω]{0,2}[\s\d\W_]*')
    for i, span in enumerate(line.spans):
        if i and protected[i - 1] and variable.fullmatch(span['text']):
            protected[i] = True
    for i in reversed(range(len(protected) - 1)):
        if protected[i + 1] and variable.fullmatch(line.spans[i]['text']):
            protected[i] = True
    group = []
    for span, fixed in zip(line.spans, protected):
        if fixed:
            if group:
                yield Line(group, _bounds(s['bbox'] for s in group), line.angle, line.text)
                group = []
            yield None
        else:
            group.append(span)
    if group:
        yield Line(group, _bounds(s['bbox'] for s in group), line.angle, line.text)


def _continues(lines, following):
    prev = lines[-1]
    if prev.angle or following.angle or prev.source_line or following.source_line:
        return False
    if ADDRESS.fullmatch(prev.text) or ADDRESS.fullmatch(following.text) or prev.text.endswith((':', '：')):
        return False
    style, next_style = prev.style, following.style
    size = max(style['size'], 1)
    step = next_style['origin'][1] - style['origin'][1]
    if not 0.65 * size < step < 1.85 * size:
        return False
    if abs(next_style['size'] - size) > 0.18 * size:
        return False
    if (next_style['flags'] ^ style['flags']) & (pdf.TEXT_FONT_BOLD | pdf.TEXT_FONT_ITALIC):
        return False
    delta_x = following.rect.x0 - prev.rect.x0
    if abs(delta_x) > 2.3 * size or delta_x > size * .8:
        return False
    # Short table keys, glossary variables, and list labels are independent.
    if min(len(prev.text), len(following.text)) <= 3:
        return False
    return True


def _join(lines):
    out = lines[0].text
    for line in lines[1:]:
        if out.endswith('\u00ad'):
            out = out[:-1] + line.text
        elif out.endswith('-') or (out and ord(out[-1]) >= 0x2e80 and ord(line.text[0]) >= 0x2e80):
            out += line.text
        else:
            out += ' ' + line.text
    return out.replace('\u00ad', '')


def _units(page, blocks=None):
    """Original segmentation, kept for resuming saved pN-tN jobs."""
    result, skipped = [], set()
    flags = pdf.TEXTFLAGS_DICT & ~pdf.TEXT_PRESERVE_IMAGES
    if blocks is None:
        blocks = page.get_text('dict', flags=flags)['blocks']
    for block in blocks:
        if block['type'] != 0:
            continue
        groups, current = [], []
        lines = (part for line in _visual_lines(block, skipped) for part in _prose_parts(line))
        for line in lines:
            if line is None or not line.text:
                current = []
                continue
            if not current or not _continues(current, line):
                current = []
                groups.append(current)
            current.append(line)
        # PDF exporters commonly put an entire keyword list in a single text block.
        # Independent, capitalized short labels retain their individual baselines.
        split_groups = []
        for group in groups:
            if len(group) >= 3 and all(len(l.text) <= 48 and l.text[0].isupper() and not l.text.endswith(('.', ',', '-')) for l in group):
                split_groups.extend([line] for line in group)
            else:
                split_groups.append(group)
        for group in split_groups:
            text = _join(group)
            if not any(c.isalpha() for c in text):
                continue  # Preserve numbers, rulers, and standalone symbols verbatim.
            if re.fullmatch(r'[A-Za-zα-ωΑ-Ω]', text):
                continue  # Also includes fraction denominators and summation bounds.
            if ADDRESS.fullmatch(text):
                continue
            rect = _bounds(line.rect for line in group)
            if rect.is_empty or rect.is_infinite:
                continue
            key = f'p{page.number + 1}-t{len(result) + 1}'
            result.append(Unit(key, text, group, rect))
    return result, skipped


def _spatial_lines(lines):
    """Put fragments from one tagged cell onto their native baselines."""
    ordered = sorted(lines, key=lambda l: (l.style['origin'][1], l.rect.x0))
    rows = []
    for line in ordered:
        if rows and abs(line.style['origin'][1] - rows[-1][0].style['origin'][1]) < .4 * line.style['size']:
            rows[-1].append(line)
        else:
            rows.append([line])
    united = []
    for row in rows:
        spans = sorted((s for l in row for s in l.spans), key=lambda s: s['bbox'][0])
        united.append(Line(spans, _bounds(s['bbox'] for s in spans), 0))
    return united


def _formula_marks(lines):
    """Mark small script glyphs and technical fonts before translating flowing prose."""
    protected, parts = [], []
    literal_tokens = set(re.findall(r'⟦f\d+⟧', ''.join(line.text for line in lines)))
    next_token = 1
    for line in lines:
        size = max(line.style['size'], 1)
        base = line.style['origin'][1]
        words, cluster = [], []
        def flush():
            nonlocal next_token
            if cluster:
                while f'⟦f{next_token}⟧' in literal_tokens:
                    next_token += 1
                token = f'⟦f{next_token}⟧'
                next_token += 1
                content = ''.join(value for _, value in cluster)
                markup = ''.join(html.escape(value) if tag == '' else
                                 f'<{tag}>{html.escape(value)}</{tag}>' for tag, value in cluster)
                protected.append((token, content, markup))
                words.append(token)
                cluster.clear()
        for span in line.spans:
            delta = span['origin'][1] - base
            script = span['size'] < .9 * size and abs(delta) > .09 * size
            if script or MATH_FONT.search(span['font']):
                cluster.append((('sub' if delta > 0 else 'sup') if script else '', span['text']))
            else:
                flush()
                words.append(span['text'])
        flush()
        raw = ''.join(words).strip()
        if not raw:
            continue
        if parts and not parts[-1].endswith(('-', '\u00ad')) and not (
                ord(raw[0]) >= 0x2e80 and ord(parts[-1][-1]) >= 0x2e80):
            parts.append(' ')
        parts.append(raw)
    return ''.join(parts).replace('\u00ad', ''), tuple(protected)


def _flow_groups(lines):
    """Return complete paragraph / bibliography groups when baselines indicate flowing prose."""
    while lines and lines[0] is not None and _formula(lines[0].spans, lines[0].text):
        lines = lines[1:]  # A superscript/denominator from the preceding display can share this block.
    while lines and lines[-1] is not None and _formula(lines[-1].spans, lines[-1].text):
        lines = lines[:-1]
    if len(lines) < 2 or any(line is None or line.angle for line in lines):
        return None
    group = []
    groups = []
    left = min(line.rect.x0 for line in lines)
    hanging_refs = any(re.match(r'^\[\d{1,4}\]\s', a.text)
                       and b.rect.x0 > a.rect.x0 + a.style['size']
                       for a, b in zip(lines, lines[1:]))
    for line in lines:
        if not line.text:
            return None
        size = max(line.style['size'], 1)
        if (line.rect.x0 - left > 3.5 * size or
                (len(line.text) <= 3 and _formula(line.spans, line.text))):
            return None
        if group:
            prev = group[-1]
            step = line.style['origin'][1] - prev.style['origin'][1]
            if not .65 * size <= step <= 2.2 * size:
                return None  # A table, formula stack, distant rows, or a second column.
            if line.text.startswith(('• ', '● ', '▪ ')) or (hanging_refs and re.match(r'^\[\d{1,4}\]\s', line.text)):
                groups.append(group)
                group = []
        group.append(line)
    groups.append(group)
    if not any(len(line.text) >= 45 for line in lines):
        return None
    return groups


def _glossary(block):
    """Recognize aligned term/definition pairs, including minus-glyph word separators."""
    lines = [Line(line['spans'], _bounds(s['bbox'] for s in line['spans']), 0)
             for line in block['lines'] if line['spans'] and line['dir'][0] > .995]
    if len(lines) < 4:
        return None
    size = median(line.style['size'] for line in lines)
    starts = Counter(round(line.rect.x0) for line in lines)
    anchors = sorted(x for x, count in starts.items() if count >= 3)
    if not anchors:
        anchors = sorted(x for x, count in starts.items() if count >= 2)
    if len(anchors) != 2 or anchors[1] - anchors[0] < 2.5 * size:
        return None
    rows = []
    for line in sorted(lines, key=lambda l: (l.style['origin'][1], l.rect.x0)):
        if not rows or line.style['origin'][1] - rows[-1][0].style['origin'][1] > .45 * size:
            rows.append([])
        rows[-1].append(line)
    pairs = []
    for row in rows:
        left = [l for l in row if l.rect.x0 < anchors[1] - 1]
        right = [l for l in row if l not in left]
        pairs.append((left, right))
    abbreviations = sum(bool(right and left and re.fullmatch(r'[\wα-ωΑ-Ω]{1,8}', ''.join(l.text for l in left)))
                        for left, right in pairs)
    short_math_list = len(pairs) == 2 and abbreviations == 2 and any(
        re.search(r'[α-ωΑ-Ω]', l.text) for left, _ in pairs for l in left)
    if not short_math_list and (abbreviations < 3 or abbreviations < .65 * len(pairs)):
        return None
    result = []
    for left, right in pairs:
        terms = ''.join(l.text for l in left)
        if left and not (right and re.fullmatch(r'[\wα-ωΑ-Ω]{1,8}', terms)):
            result.append(_spatial_lines(left))
        if right:
            result.append(_spatial_lines(right))
    return result


def _retain_script_base(page, lines, unit):
    """Keep an equation base printed in the end of a prose span next to its native script."""
    last = unit.lines[-1].spans[-1]
    trailing = re.search(r'\s([A-Za-zΑ-Ωα-ω])$', last['text'])
    if not trailing or len(last['text']) < 6:
        return
    following = next((l['spans'][idx+1] for l in lines for idx,s in enumerate(l['spans'][:-1]) if s is last), None)
    if (not following or following['size'] >= last['size'] * .85
            or abs(following['origin'][1] - last['origin'][1]) < .1 * last['size']
            or abs(following['origin'][0] - last['bbox'][2]) > .35 * last['size']):
        return
    rect = pdf.Rect(last['bbox'])
    raw = page.get_text('rawdict', clip=rect + (-1,-1,1,1), flags=pdf.TEXTFLAGS_RAWDICT & ~pdf.TEXT_PRESERVE_IMAGES)
    chars = [c for b in raw['blocks'] for l in b['lines'] for s in l['spans'] for c in s['chars']
             if c['c'] == trailing[1] and abs(c['origin'][1] - last['origin'][1]) < .2]
    if not chars:
        return
    base = max(chars, key=lambda c: c['origin'][0])
    if rect.x1 - base['bbox'][2] > .3 * last['size']:
        return
    rect.x1 = base['bbox'][0] - .1
    end = unit.lines[-1]
    spans = [*end.spans[:-1], {**last, 'bbox': tuple(rect), 'text': last['text'][:trailing.start()]}]
    unit.lines[-1] = Line(spans, _bounds(s['bbox'] for s in spans), end.angle, end.source_line)
    unit.rect = _bounds(l.rect for l in unit.lines)
    unit.text = _join(unit.lines)
    unit.kind = 'before-equation'
    unit.rect.x1 = rect.x1


def _fast_units(page, cells, prefix='b'):
    skipped = set()
    flags = pdf.TEXTFLAGS_DICT & ~pdf.TEXT_PRESERVE_IMAGES
    blocks = page.get_text('dict', flags=flags)['blocks']
    entries = []
    cell_lines = [[] for _ in cells]
    cell_order = {}
    for bi, block in enumerate(blocks):
        if block['type'] != 0:
            continue
        remaining = []
        for li, line in enumerate(block['lines']):
            other = []
            inside = {}
            for span in line['spans']:
                if not span['text'] or not span['char_flags'] & 24 or not span['alpha']:
                    continue
                rect = pdf.Rect(span['bbox'])
                center_x = (rect.x0 + rect.x1) / 2
                # Compare against native ink, not ascender/descender bounds shared by tight rows.
                center_y = span['origin'][1] - .32 * span['size']
                owner = next((i for i, cell in enumerate(cells)
                              if cell.ink.x0 - 1 <= center_x <= cell.ink.x1 + 1
                              and cell.ink.y0 - 1 <= center_y <= cell.ink.y1 + 1), None)
                if owner is None:
                    other.append(span)
                else:
                    inside.setdefault(owner, []).append(span)
            for i, spans in inside.items():
                cell_lines[i].append(Line(spans, _bounds(s['bbox'] for s in spans), 0))
                cell_order.setdefault(i, (bi, li))
            if other:
                remaining.append({**line, 'spans': other})
        if not remaining:
            continue
        clean = {**block, 'lines': remaining}
        defs = _glossary(clean)
        if defs:
            for idx, group in enumerate(defs):
                text = _join(group)
                if any(c.isalpha() for c in text):
                    if re.search(r'[A-Za-z]{2}\s*−\s*[A-Za-z]{2}|\b[A-Za-z]\s*−\s*th\b', text):
                        text = re.sub(r'(?<=[A-Za-z])\s*−\s*(?=[A-Za-z])', '-', text)
                        marks = ()
                    else:
                        text, marks = _formula_marks(group)
                    entries.append(((bi, idx), Unit('', text, group, _bounds(l.rect for l in group), 'glossary', marks)))
            continue
        lines = list(_visual_lines(clean, skipped))
        flows = _flow_groups(lines)
        if flows:
            for idx, group in enumerate(flows):
                text, marks = _formula_marks(group)
                if any(c.isalpha() for c in re.sub(r'⟦f\d+⟧', '', text)) and not ADDRESS.fullmatch(text):
                    entries.append(((bi, idx), Unit('', text, group, _bounds(l.rect for l in group), 'prose', marks)))
        else:
            units, ignored = _units(page, [clean])
            for unit in units:
                _retain_script_base(page, remaining, unit)
            skipped.update(ignored)
            entries.extend(((bi, idx), u) for idx, u in enumerate(units))
    for i, members in enumerate(cell_lines):
        if not members:
            continue
        lines = _spatial_lines(members)
        text = _join(lines)
        if not any(c.isalpha() for c in text) or _formula([s for line in lines for s in line.spans], text):
            continue
        box = _bounds(l.rect for l in lines)
        size = lines[0].style['size']
        box.x1 = max(box.x1, min(page.cropbox.width, cells[i].right - size * .25))
        if cells[i].bottom:
            box.y1 = max(box.y1, cells[i].bottom - .4 * size)
        entries.append((cell_order[i], Unit('', text, lines, box, 'cell')))
    entries.sort(key=lambda item: item[0])
    typical_size = median(u.lines[0].style['size'] for _, u in entries) if entries else 0
    for idx, (_, u) in enumerate(entries, 1):
        u.id = f'p{page.number+1}-{prefix}{idx}'
        if (u.kind == 'prose' and len(u.lines) <= 3 and u.rect.y0 < .3 * page.cropbox.height
                and u.rect.width > .45 * page.cropbox.width and u.lines[0].style['size'] > typical_size * 1.4):
            u.kind = 'title'
    return [unit for _, unit in entries], skipped


def _open(path):
    doc = pdf.open(path)
    if not doc.is_pdf or doc.needs_pass:
        doc.close()
        raise ValueError('请提供能直接打开的、已解锁的 PDF 文件。')
    return doc


def extract(path: Path) -> list[dict]:
    records = []
    with (_open(path) as original, prose_view(path, original) as (doc, figures),
          tagged_tables(path, original) as locate):
        prefix = 'g' if figures else 'b'
        for page in doc:
            units, _ = _fast_units(page, locate(original[page.number]), prefix)
            for unit in units:
                context = f'PDF 第 {page.number + 1} 页'
                source_lines = list(dict.fromkeys(l.source_line for l in unit.lines if l.source_line))
                if source_lines:
                    context += '。独立的公式已保留原位，只翻译 text 中的片段。原行：' + ' '.join(source_lines)
                if _unprotected_math(unit):
                    context += '。正文与行内数学共用文字层，请保留完整算式、变量和上下标。'
                if unit.formulas:
                    context += '。保留公式标记：' + '；'.join(f'{t}={v}' for t, v, _ in unit.formulas)
                record = {'id': unit.id, 'text': unit.text, 'context': context,
                          '_rect': list(unit.rect * page.rotation_matrix)}
                if unit.rect.y1 < page.cropbox.height * .075 or unit.rect.y0 > page.cropbox.height * .94:
                    # Equal text in the same running header/footer position can share a translation.
                    record['repeat_key'] = f"{unit.rect.x0 / page.cropbox.width:.2f},{unit.rect.y0 / page.cropbox.height:.2f}"
                records.append(record)
    return records


class PdfFitError(ValueError):
    """A segment that needs a different translation before the PDF can be delivered."""


class LabelSpace:
    """Find unoccupied space immediately to the right of an unboxed short label."""

    def __init__(self, source_page, units):
        self.page = source_page
        self.columns = [u.rect for u in units if u.kind == 'cell' or len(u.lines) > 1]
        self.ink = None
        self.reserved = []

    def wider_box(self, unit, size, target):
        box = pdf.Rect(unit.rect)
        if unit.kind == 'cell' or self.page.rotation:
            return box
        if self.ink is None:
            self.ink = [(kind, pdf.Rect(rect)) for kind, rect in self.page.get_bboxlog()
                        if kind != 'ignore-text']
        edge = min(self.page.cropbox.width, box.x0 + size * min(24, len(target) + 2))
        obstacles = [(kind, r) for kind, r in self.ink
                     if kind not in {'fill-text', 'stroke-text'} or r.x1 > box.x1 + 1.25]
        obstacles.extend(('reserved', r) for r in self.columns + self.reserved)
        for _, occupied in obstacles:
            if occupied.y1 <= box.y0 or occupied.y0 >= box.y1 or occupied.x1 < box.x1 - .25:
                continue
            edge = min(edge, occupied.x0 - max(1, size * .2))
        box.x1 = max(box.x1, edge)
        return box


def _styled_math_latin(markup):
    # Some MuPDF fallback math fonts draw these glyphs but extract e.g. italic
    # "rank" as unrelated modifier letters. Ordinary Latin + matching font style
    # carries the same mathematical glyph, with searchable Unicode underneath.
    def run(match):
        name = unicodedata.name(match[0][0], '')
        style = 'BoldItalic' if 'BOLD ITALIC' in name else 'Italic' if 'ITALIC' in name else 'Bold'
        return f'<span class="math-{style}">{unicodedata.normalize("NFKC", match[0])}</span>'
    return re.sub(r'[𝐀-𝐳]+|[𝐴-𝑔𝑖-𝑧]+|[𝑨-𝒛]+', run, markup)


def _typeset(page, unit, target, fonts=None, *, fit_space=None, fit_warnings=None):
    lines, box = unit.lines, pdf.Rect(unit.rect)
    if not unit.kind and len(lines) == 1 and len(unit.text) <= 40 and not lines[0].angle:
        # Publisher PDFs sometimes draw whitespace between words in a much
        # larger font. Anchor a compact label to its letters' actual row.
        letters = _bounds(s['bbox'] for s in lines[0].spans if s['text'].strip())
        if 0 < letters.height < .8 * box.height:
            box.y0, box.y1 = letters.y0, letters.y1
    styles = Counter()
    for line in lines:
        for span in line.spans:
            styles[(round(span['size'], 2), span['flags'], span['color'])] += len(span['text'].strip())
    (size, flags, color), _ = styles.most_common(1)[0]
    size = max(size, 1)
    align = 'left'
    indent = 0
    leading = 1.08
    if len(lines) > 1:
        origins = [line.style['origin'][1] for line in lines]
        leading = max(.9, min(1.65, median(b - a for a, b in zip(origins, origins[1:])) / size))
        indent = max(0, lines[0].rect.x0 - box.x0)
        if len(lines) >= 3 and sum(box.x1 - l.rect.x1 < size for l in lines[:-1]) >= .75 * (len(lines) - 1):
            align = 'justify'
    if abs((box.x0 + box.x1 - page.cropbox.width) / 2) < size and box.width < page.cropbox.width * .8:
        align = 'center'
    family = 'monospace' if flags & pdf.TEXT_FONT_MONOSPACED else 'serif' if flags & pdf.TEXT_FONT_SERIFED else 'sans-serif'
    if re.search(r'[\u4e00-\u9fff]', target):
        if align == 'justify':
            align = 'left'
        if unit.kind == 'prose' and len(lines) > 2:
            size *= 1.15  # A translated CJK paragraph is generally shorter and can use a larger face.
        if unit.kind == 'title':
            size *= 1.17
            flags |= pdf.TEXT_FONT_BOLD
    if unit.kind == 'before-equation':
        align = 'right'
    css = (f'* {{margin:0; padding:0; font-family:{family}; font-size:{size}pt; '
           f'color:#{color:06x}; line-height:{leading}; text-align:{align}; '
           f'font-weight:{"bold" if flags & pdf.TEXT_FONT_BOLD else "normal"}; '
           f'font-style:{"italic" if flags & pdf.TEXT_FONT_ITALIC else "normal"};}} '
           f'div {{text-indent:{indent}pt;}}')
    content = '<div>' + '<br>'.join(html.escape(s) for s in target.splitlines()) + '</div>'
    if unit.formulas:
        expected = {token: markup for token, _, markup in unit.formulas}
        if Counter(re.findall(r'⟦f\d+⟧', target)) != Counter(re.findall(r'⟦f\d+⟧', unit.text)):
            raise PdfFitError(f'{unit.id} 的公式标记缺失或重复，请保留每个 ⟦fN⟧。')
        content = re.sub(r'⟦f\d+⟧', lambda m: expected.get(m[0], m[0]), content)
        css += 'sub,sup{font-size:70%;} sub .latin-text,sup .latin-text{font-size:inherit;}'
    if re.search(r'[𝐀-𝒛]', content):
        content = _styled_math_latin(content)
        for style in ('Bold', 'Italic', 'BoldItalic'):
            selector = f'.math-{style},.math-{style} .latin-text'
            face = 'Latin' + style
            if fonts and face in fonts.faces:
                css += f'{selector}{{font-family:{face};font-weight:normal;font-style:normal;}}'
            else:
                weight = 'bold' if 'Bold' in style else 'normal'
                slope = 'italic' if 'Italic' in style else 'normal'
                css += f'{selector}{{font-family:serif;font-weight:{weight};font-style:{slope};}}'
    if fonts:
        content, font_css = fonts.styled(content, bold=bool(flags & pdf.TEXT_FONT_BOLD),
                                         italic=bool(flags & pdf.TEXT_FONT_ITALIC),
                                         monospace=bool(flags & pdf.TEXT_FONT_MONOSPACED))
        css += font_css
    minimum = min(.7, 4 / size)
    options = dict(css=css, rotate=lines[0].angle, archive=fonts.archive if fonts else None)
    label = (fit_space is not None and len(lines) == 1 and not lines[0].angle and not unit.formulas
             and unit.kind in {'', 'cell', 'glossary'} and len(unit.text) <= 32 and box.width <= 12 * size)
    spare, scale = page.insert_htmlbox(box, content, scale_low=.7 if label else minimum, **options)
    if spare < 0 and label:
        wider = fit_space.wider_box(unit, size, target)
        wider.y0, wider.y1 = box.y0, box.y1
        if wider.width > box.width + 1:
            spare, scale = page.insert_htmlbox(wider, content, scale_low=minimum, **options)
            if spare >= 0:
                fit_space.reserved.append(wider)
                if fit_warnings is not None:
                    fit_warnings.append(f'{unit.id} 已在同一行的空白内扩排，保留完整译文。')
        elif unit.kind == 'cell':
            # Identifiers can wrap at their own word/separator boundaries. Keep
            # every character and the tagged table's exact cell dimensions.
            parts = re.split(r'(<[^>]+>|&(?:#\d+|#x[\da-fA-F]+|[A-Za-z]+);)', content)
            for i in range(0, len(parts), 2):
                parts[i] = re.sub(r'(?<=[a-z])(?=[A-Z])|(?<=[_/-])(?=[A-Za-z0-9])',
                                  '\u200b', parts[i])
            wrapped = ''.join(parts)
            if wrapped != content:
                spare, scale = page.insert_htmlbox(box, wrapped, scale_low=minimum, **options)
                if spare >= 0 and fit_warnings is not None:
                    fit_warnings.append(f'{unit.id} 已按名称边界在表格单元内换行，保留完整译文。')
        if spare < 0 and wider == box and minimum < .7:
            spare, scale = page.insert_htmlbox(box, content, scale_low=minimum, **options)
    if spare < 0:
        raise PdfFitError(f'{unit.id} 的完整译文无法在 {box.width:.1f} × {box.height:.1f} 磅原位清晰排下，'
                          f'请精简此段译文后重试。')
    return scale


def _place(sheet, half, document, pno):
    # Page Form XObjects keep paths, embedded type, and original-resolution images.
    page = document[pno]
    if not page.get_contents():
        return
    angle = page.rotation
    if angle:
        page.set_rotation(0)
    try:
        sheet.show_pdf_page(half, document, pno, rotate=-angle)
    finally:
        if angle:
            page.set_rotation(angle)


def _spread(dual, left, number, right, translated_number):
    width, height = left[number].rect.width, left[number].rect.height
    sheet = dual.new_page(width=2 * width, height=height)
    _place(sheet, pdf.Rect(0, 0, width, height), left, number)
    _place(sheet, pdf.Rect(width, 0, 2 * width, height), right, translated_number)


def _spread_snapshot(dual, left, index, snapshot):
    if snapshot is None:
        _spread(dual, left, index, left, index)
    else:
        _spread(dual, left, index, snapshot, 0)


def _render_page(page, target_page, units, changed, figures, fonts, warnings, errors):
    fit_space = LabelSpace(page, units)
    rotation = target_page.rotation
    target_page.set_rotation(0)
    try:
        refs = figures.intersection(xref for xref, *_ in target_page.get_xobjects()) if figures else ()
        # Restore protected forms before the finished page is committed to its checkpoint.
        with protect_forms(target_page.parent, refs):
            redact_units(target_page, [unit for unit, target in changed])
        for unit, target in changed:
            try:
                scale = _typeset(target_page, unit, target, fonts, fit_space=fit_space, fit_warnings=warnings)
            except PdfFitError as error:
                errors.append(str(error))
                continue
            if _unprotected_math(unit):
                warnings.append(f'{unit.id} 的行内数学与正文共用文字层，已随译文重排，请核对算式及上下标。')
            if scale < .7:
                warnings.append(f'{unit.id} 的译文缩至原字号的 {scale:.0%}，请检查可读性。')
    finally:
        target_page.set_rotation(rotation)


def apply(path: Path, translations: dict[str, str], output: Path, *, source_texts=None,
          cache_dir=None, source_hash=None, stats=None) -> list[str]:
    """Compose a bilingual PDF. A job may supply its cache directory and already verified source SHA-256."""
    warnings, errors = [], []
    pending = set(translations)
    if source_texts is not None and pending != source_texts.keys():
        raise ValueError('保存的原文段号与译文不一致，请核对翻译任务。')
    by_page = defaultdict(list)
    for key in translations:
        if match := re.fullmatch(r'p(\d+)-[btg]\d+', key):
            by_page[int(match[1]) - 1].append(key)
    legacy = any(re.fullmatch(r'p\d+-t\d+', k) for k in translations)
    figure_aware = not translations or any(re.fullmatch(r'p\d+-g\d+', k) for k in translations)
    has_cjk = any(re.search(r'[\u3400-\u9fff]', value) for value in translations.values())
    mode = (legacy, figure_aware, has_cjk)
    counts = {} if stats is None else stats
    counts.update(pages_rendered=0, pages_reused=0, pages_original=0)
    with (page_cache(cache_dir, path, source_hash, mode) as cache, _open(path) as original,
          pdf.open() as dual, ExitStack() as resources):
        right = prose = None
        links = collect_links(original)
        widths = [p.rect.width for p in original]
        if cache:
            # Source positions and segmentation must be read from the unbaked original.
            left = resources.enter_context(_open(path))
            left.bake()
        for page in original:
            i = page.number
            ids = by_page[i]
            if cache:
                key = cache.key(i, ids, translations, source_texts)
                hit = cache.read(i, key)
                if hit is not None:
                    page_warnings, snapshot = hit
                    warnings.extend(page_warnings)
                    pending.difference_update(ids)
                    with snapshot if snapshot is not None else nullcontext():
                        counts['pages_original' if snapshot is None else 'pages_reused'] += 1
                        if not errors:
                            _spread_snapshot(dual, left, i, snapshot)
                    continue

            if prose is None:
                prose, figures = (resources.enter_context(prose_view(path, original))
                                  if figure_aware else (original, set()))
                locate = None if legacy else resources.enter_context(tagged_tables(path, original))
            units, skipped = (_units(page) if legacy else
                              _fast_units(prose[i], locate(page), 'g' if figures else 'b'))
            for unit in units:
                if unit.id not in pending or (source_texts is not None and source_texts[unit.id] != unit.text):
                    raise ValueError(f'PDF 重新提取的 {unit.id} 与任务记录不一致，请使用原提取环境或重新开始任务。')
                pending.remove(unit.id)
            if extra := set(ids).intersection(pending):
                raise ValueError(f'PDF 重新提取后缺少已保存的段号 {min(extra)}，请核对提取环境。')
            page_warnings = []
            if not units and not skipped:
                page_warnings.append(f'第 {i+1} 页没有可翻译的可见文字，右页保留原貌。')
            if 'angled' in skipped:
                page_warnings.append(f'第 {i+1} 页的斜排或直排文字保留原貌。')
            changed = [(u, translations[u.id]) for u in units if translations[u.id] != u.text]
            if changed:
                for unit, value in changed:
                    if not value.strip():
                        raise ValueError(f'PDF 译文为空（{unit.id}）。')
                if right is None:
                    right = resources.enter_context(_open(path))
                    right.bake()
                    fonts = PdfFonts.for_targets(translations.values())
                before = len(errors)
                _render_page(page, right[i], units, changed, figures, fonts, page_warnings, errors)
                if len(errors) > before:
                    continue
                counts['pages_rendered'] += 1
            else:
                counts['pages_original'] += 1
            warnings.extend(page_warnings)
            if cache:
                with (pdf.open() if changed else nullcontext(None)) as snapshot:
                    if snapshot is not None:
                        snapshot.insert_pdf(right, from_page=i, to_page=i, links=False, annots=False, widgets=False)
                        snapshot.subset_fonts()
                    cache.save(i, key, page_warnings, snapshot)
                    if not errors:
                        _spread_snapshot(dual, left, i, snapshot)

        if errors:
            raise PdfFitError('请一次修订以下译文后重新生成：\n' + '\n'.join(errors))
        if pending:
            raise ValueError(f'PDF 重新提取后缺少已保存的段号 {min(pending)}，请核对提取环境。')
        if cache is None:
            original.bake()
            if right is not None:
                right.subset_fonts()
            for i in range(len(original)):
                _spread(dual, original, i, right if right is not None else original, i)
        warnings.extend(copy_links(dual, links, widths))
        if toc := original.get_toc():
            dual.set_toc(toc)
        dual.set_metadata(original.metadata)
        dual.save(output, garbage=4, deflate=True, use_objstms=1)
    return warnings
