"""Check the finished bilingual file before publishing it. No screenshots are written."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
import re
import unicodedata
from zipfile import ZipFile


def _normalized(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if c.isalnum())


def _target_anchors(text):
    # Formula tokens become native glyphs at layout time. Sample translated words
    # on one side of the formula instead of looking for token syntax in the PDF.
    text = max((_normalized(part) for part in re.split(r"⟦f\d+⟧", text)), key=len)
    if not text:
        return ()
    width = min(8, len(text))
    return tuple(dict.fromkeys(text[i:i + width] for i in (0, (len(text) - width) // 2, len(text) - width)))


def _pixels(page, area, scale):
    import pymupdf as pdf

    return page.get_pixmap(matrix=pdf.Matrix(scale, scale), clip=area,
                           colorspace=pdf.csGRAY, alpha=False, annots=True)


def _difference(reference, actual):
    # At fractional page widths the neighboring half can paint a seam pixel.
    width = min(reference.width, actual.width)
    height = min(reference.height, actual.height)
    if width < 3 or height < 3:
        return 0, 0
    src, dst = reference.samples, actual.samples
    if reference.width == actual.width and reference.height == actual.height and src == dst:
        return 0, 0
    different = ink = 0
    for row in range(1, height - 1):
        i, j = row * reference.stride + 1, row * actual.stride + 1
        a, b = src[i:i + width - 2], dst[j:j + width - 2]
        ink += len(a) - a.count(255)
        if a != b:
            different += sum(abs(x - y) > 42 for x, y in zip(a, b))
    return different, ink


def _check_pixels(reference, actual, side, page, warnings):
    diff, ink = _difference(reference, actual)
    if diff > max(20, .09 * ink):
        raise ValueError(f"第 {page} 页{side}与原件的可见内容明显不同。")
    if diff > max(12, .014 * ink):
        warnings.append(f"第 {page} 页{side}有局部渲染差异，请核对。")


def _missing_images(src, sheet, width):
    import pymupdf as pdf

    # Compare visible placements instead of xrefs. PDF grafting and font/image
    # optimization can renumber every object. Baked widget images may add entries.
    images = set()
    for item in src.get_image_info():
        box = pdf.Rect(item["bbox"]) * src.rotation_matrix
        if box.intersects(src.rect):
            images.add((item["width"], item["height"], tuple(box)))
    if not images:
        return 0
    right = defaultdict(list)
    for item in sheet.get_image_info():
        box = pdf.Rect(item["bbox"]) - (width, 0, width, 0)
        if box.intersects(src.rect):
            right[item["width"], item["height"]].append(box)
    return sum(not any(max(abs(a - b) for a, b in zip(orig, other)) < .5 for other in right[w, h])
               for w, h, orig in images)


def _visible_text(sheet, area):
    import pymupdf as pdf

    flags = pdf.TEXTFLAGS_DICT & ~pdf.TEXT_PRESERVE_IMAGES
    blocks = sheet.get_text("dict", clip=area, flags=flags)["blocks"]
    pieces, offsets, boxes = [], [], []
    length = 0
    replacement = False
    for block in blocks:
        for line in block.get("lines", ()):
            for span in line["spans"]:
                if not span["alpha"] or not span["char_flags"] & 24:
                    continue
                replacement |= "\ufffd" in span["text"]
                piece = _normalized(span["text"])
                if piece:
                    pieces.append(piece)
                    offsets.append(length)
                    boxes.append(pdf.Rect(span["bbox"]) - (area.x0, 0, area.x0, 0))
                    length += len(piece)
    return "".join(pieces), offsets, boxes, replacement


def _anchor_status(anchors, text, offsets, boxes, expected):
    import pymupdf as pdf

    anywhere = False
    region = pdf.Rect(expected) + (-5, -5, 5, 5) if expected else None
    for anchor in anchors:
        for candidate in {anchor, anchor[::-1]}:   # LTR or visual-order RTL extraction
            pos = text.find(candidate)
            while pos >= 0:
                anywhere = True
                if region is None or len(candidate) < 3:
                    return "ok"
                start = bisect_right(offsets, pos) - 1
                end = bisect_right(offsets, pos + len(candidate) - 1)
                if any(box.intersects(region) for box in boxes[start:end]):
                    return "ok"
                pos = text.find(candidate, pos + 1)
    return "displaced" if anywhere else "missing"


def _pdf(source, result, translations, rectangles):
    import pymupdf as pdf

    by_page = defaultdict(list)
    for key, value in translations.items():
        match = re.match(r"p(\d+)-", key)
        if match:
            by_page[int(match[1])].append((key, value))
    warnings = []
    with pdf.open(source) as original, pdf.open(result) as output:
        if not output.is_pdf or len(original) != len(output):
            raise ValueError("双语 PDF 页数与原件不一致。")
        for number, (src, sheet) in enumerate(zip(original, output), 1):
            w, h = src.rect.width, src.rect.height
            if abs(sheet.rect.width - 2 * w) > .03 or abs(sheet.rect.height - h) > .03:
                raise ValueError(f"第 {number} 页未按原尺寸左右并排。")
            # Integer half-page pixel width keeps the two raster origins aligned.
            scale = max(1, round(w * min(1, 420 / max(w, h)))) / w
            right = pdf.Rect(w, 0, 2 * w, h)
            reference = _pixels(src, src.rect, scale)
            _check_pixels(reference, _pixels(sheet, src.rect, scale), "左半页", number, warnings)
            right_raster = _pixels(sheet, right, scale)
            if not by_page[number]:
                _check_pixels(reference, right_raster, "右半页", number, warnings)
                continue
            # Only inspect the translated half; a misplaced translation must not
            # accidentally pass by matching text in the original half.
            text, offsets, boxes, replacement = _visible_text(sheet, right)
            if (not text or right_raster.is_unicolor) and any(_normalized(t) for _, t in by_page[number]):
                raise ValueError(f"第 {number} 页右半页没有可见的译文。")
            missing, displaced = [], []
            for key, translation in by_page[number]:
                anchors = _target_anchors(translation)
                if not anchors:
                    continue
                status = _anchor_status(anchors, text, offsets, boxes, rectangles.get(key))
                if status == "missing":
                    missing.append(key)
                elif status == "displaced":
                    displaced.append(key)
            if missing:
                warnings.append(f"第 {number} 页右半页未检出 {len(missing)} 段的译文片段，请核对 "
                                + "、".join(missing[:3]) + "。")
            if displaced:
                warnings.append(f"第 {number} 页有 {len(displaced)} 段译文仅在原文字框以外检出，请核对 "
                                + "、".join(displaced[:3]) + "。")
            if replacement and "\ufffd" not in src.get_text():
                warnings.append(f"第 {number} 页右半页出现新增的文字解码替代字符。")
            if missing_images := _missing_images(src, sheet, w):
                warnings.append(f"第 {number} 页右半页有 {missing_images} 处原图位置未匹配。")
        return {"pages": len(output), "warnings": warnings}


def _word(source, result, translations):
    from lxml import etree
    from .word import WORD_NS, _parts

    per_part = Counter(key.rsplit("#", 1)[0] for key in translations)
    with ZipFile(source) as before, ZipFile(result) as after:
        if set(before.namelist()) != set(after.namelist()):
            raise ValueError("双语 Word 的部件清单与原件不一致。")
        parts = set(per_part) | set(_parts(before)["document.main"])
        for name in before.namelist():
            if name not in parts and before.getinfo(name).CRC != after.getinfo(name).CRC:
                raise ValueError(f"双语 Word 的原始部件发生变化：{name}")
        paragraph = f"{{{WORD_NS}}}p"
        for name in sorted(parts):
            old = etree.fromstring(before.read(name))
            new = etree.fromstring(after.read(name))
            added = sum(1 for _ in new.iter(paragraph)) - sum(1 for _ in old.iter(paragraph))
            if added != per_part[name]:
                raise ValueError(f"双语 Word 的译文段数不一致：{name}")
    return {"translated_paragraphs": len(translations), "warnings": []}


def _plain(source, result, translations):
    from .plain import _load

    text, codec, bom, _, segments = _load(source)
    data = result.read_bytes()
    # A translation that does not fit the input encoding is written as UTF-8.
    # Prefer the original encoding when both interpretations are valid.
    candidates = ((codec, bom), ("utf-8", b""))
    for encoding, prefix in candidates:
        if not data.startswith(prefix):
            continue
        try:
            both = data[len(prefix):].decode(encoding)
        except UnicodeError:
            continue
        source_pos = target_pos = 0
        for segment in segments:
            original = text[source_pos:segment.position]
            if not both.startswith(original, target_pos):
                break
            target_pos += len(original)
            added = segment.render(translations[segment.id])
            if not both.startswith(added, target_pos):
                break
            target_pos += len(added)
            source_pos = segment.position
        else:
            if both[target_pos:] != text[source_pos:]:
                continue
            return {"translated_paragraphs": len(translations), "warnings": []}
    raise ValueError("双语文本中的原文顺序、编码或译文长度异常。")


def verify(source_layout: Path, staging: Path, translations: dict[str, str], rectangles=None) -> dict:
    suffix = source_layout.suffix.lower()
    if suffix == ".pdf":
        return _pdf(source_layout, staging, translations, rectangles or {})
    if suffix == ".docx":
        return _word(source_layout, staging, translations)
    if suffix in {".md", ".txt"}:
        return _plain(source_layout, staging, translations)
    raise ValueError(f"无法验收文件格式 {suffix}")
