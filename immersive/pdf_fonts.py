"""Build small in-memory subsets of locally installed reading fonts once per PDF."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import re


_HAN = re.compile(r"[\u3400-\u9fff]")
_LATIN = re.compile(r"[\x20-\x7e\u00c0-\u024f\u0370-\u03ff]+")
_TAGS = re.compile(r"(<[^>]+>|&(?:#\d+|#x[\da-fA-F]+|[A-Za-z]+);)")


def _subset(file, characters):
    from fontTools import subset
    from fontTools.ttLib import TTFont

    with TTFont(file, fontNumber=0, lazy=True, recalcTimestamp=False) as face:
        # Restricted, bitmap-only, and no-subsetting fonts use the renderer's fallback.
        if "OS/2" in face and face["OS/2"].fsType & 0x302:
            return None
        options = subset.Options()
        options.drop_tables += ["MERG", "meta"]
        options.hinting = False
        options.layout_features = ["kern", "liga"]
        sub = subset.Subsetter(options=options)
        sub.populate(unicodes=characters)
        sub.subset(face)
        data = BytesIO()
        face.save(data)
        return data.getvalue()


@dataclass
class PdfFonts:
    archive: object
    faces: frozenset[str]
    declarations: str

    @classmethod
    def for_targets(cls, targets):
        root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if not root:
            return None
        directory = Path(root) / "Fonts"
        families = {"Song": "simsun.ttc", "Hei": "simhei.ttf", "Latin": "times.ttf",
                    "LatinBold": "timesbd.ttf", "LatinItalic": "timesi.ttf", "LatinBoldItalic": "timesbi.ttf"}
        if not (directory / families["Song"]).is_file():
            return None
        chars = set()
        for text in targets:
            chars.update(text)
        if not any(_HAN.fullmatch(c) for c in chars):
            return None
        try:
            from fontTools.ttLib import TTLibError
        except ImportError:
            return None
        import pymupdf as pdf

        archive = pdf.Archive()
        faces = []
        # Include ASCII for entity-decoded punctuation and rendered formula markup.
        codepoints = set(map(ord, chars)) | set(range(32, 127))
        codepoints.update(range(0x370, 0x400))    # Greek in expanded math placeholders
        codepoints.update(range(0x2070, 0x20A0))  # superscripts and subscripts
        for name, filename in families.items():
            if not (file := directory / filename).is_file():
                continue
            try:
                data = _subset(file, codepoints)
            except (OSError, TTLibError):
                continue
            if data:
                archive.add((data, name + ".ttf"))
                faces.append(name)
        if "Song" not in faces:
            return None
        css = "".join(f"@font-face{{font-family:{name};src:url({name}.ttf);}}" for name in faces)
        return cls(archive, frozenset(faces), css)

    def styled(self, markup, *, bold=False, italic=False, monospace=False):
        # MuPDF's HTML shaper uses its own CJK fallback for uncovered codepoints.
        # Explicit text spans keep Latin abbreviations and units in the source style.
        cjk = "Hei" if bold and "Hei" in self.faces else "Song"
        latin = "Latin" + ("Bold" if bold else "") + ("Italic" if italic else "")
        if monospace:
            latin = "monospace"
        elif latin not in self.faces:
            latin = "serif"
        css = self.declarations + f"div,sub,sup{{font-family:{cjk};}} .latin-text{{font-family:{latin};}}"
        if latin in self.faces:
            css += ".latin-text{font-weight:normal;font-style:normal;}"
        pieces = _TAGS.split(markup)
        for i in range(0, len(pieces), 2):
            pieces[i] = _LATIN.sub(lambda m: '<span class="latin-text">' + m[0] + "</span>", pieces[i])
        return "".join(pieces), css
