import unittest

import pymupdf as pdf

from immersive.pdf_fonts import PdfFonts


class ChineseTypography(unittest.TestCase):
    def test_local_chinese_and_latin_runs_retain_text_and_embedded_faces(self):
        self.assertIsNone(PdfFonts.for_targets(["Temperature at 20 °C."]))
        phrases = ["测量 TC-4 的压力。", "标题 Report"]
        fonts = PdfFonts.for_targets(phrases)
        if fonts is None or not {"Song", "Hei", "Latin", "LatinBold"} <= fonts.faces:
            self.skipTest("Windows reading fonts and fontTools are optional")
        with pdf.open() as document:
            page = document.new_page()
            for i, phrase in enumerate(phrases):
                markup, css = fonts.styled("<div>" + phrase + "</div>", bold=bool(i))
                page.insert_htmlbox(pdf.Rect(10, 30 + i * 30, 350, 60 + i * 30),
                                    markup, css=css, archive=fonts.archive)
            data = document.tobytes(garbage=4, deflate=True)
        with pdf.open(stream=data, filetype="pdf") as output:
            spans = [s for b in output[0].get_text("dict")["blocks"]
                     for line in b["lines"] for s in line["spans"]]
            for term, face in [("测量", "SimSun"), ("TC-4", "TimesNewRoman"),
                               ("标题", "SimHei"), ("Report", "Bold")]:
                self.assertTrue(any(term in s["text"] and face in s["font"] for s in spans))
