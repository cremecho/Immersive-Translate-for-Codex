from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

import pymupdf as pdf
from docx import Document

from immersive import pdf_layout, plain, word
from immersive.verify import verify


class DeliveryChecks(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.gettempdir()) / ("translation-qa-" + uuid.uuid4().hex)
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work)

    def test_pdf_visuals_and_page_specific_text(self):
        source, good = self.work / "source.pdf", self.work / "dual.pdf"
        with pdf.open() as d:
            p = d.new_page(width=241.3, height=150.7)
            p.insert_text((18, 29), "Calibration report", fontsize=13)
            p.add_rect_annot(pdf.Rect(10, 40, 70, 50)).update()
            p = d.new_page(width=180.5, height=120.25)
            scan = pdf.Pixmap(pdf.csGRAY, pdf.IRect(0, 0, 8, 12), 0)
            scan.clear_with(70)
            p.insert_image(pdf.Rect(20, 22, 80, 110), pixmap=scan)
            p.set_rotation(90)
            d.save(source)
        values = {u["id"]: "Informe de calibración" for u in pdf_layout.extract(source)}
        pdf_layout.apply(source, values, good)
        self.assertEqual(verify(source, good, values), {"pages": 2, "warnings": []})
        with self.assertRaisesRegex(ValueError, "左右并排"):
            verify(source, source, values)

        bad = self.work / "bad.pdf"
        with pdf.open(good) as d:
            d[0].draw_rect(pdf.Rect(8, 7, 170, 40), color=None, fill=(1, 1, 1))
            d.save(bad)
        with self.assertRaisesRegex(ValueError, "左半页"):
            verify(source, bad, values)
        bad_scan = self.work / "bad-scan.pdf"
        with pdf.open(good) as d:
            p = d[1]
            p.draw_rect(pdf.Rect(p.rect.width / 2, 0, p.rect.width, p.rect.height),
                        color=None, fill=(1, 1, 1))
            d.save(bad_scan)
        with self.assertRaisesRegex(ValueError, "右半页"):
            verify(source, bad_scan, values)
        different = dict.fromkeys(values, "輸出に存在しない翻訳")
        self.assertIn(next(iter(values)), verify(source, good, different)["warnings"][0])

    def test_word_and_editable_source_preservation(self):
        src, out = self.work / "draft.docx", self.work / "dual.docx"
        d = Document()
        d.add_paragraph("Report")
        d.save(src)
        words = {u["id"]: "Informe" for u in word.extract(src)}
        word.apply(src, words, out)
        self.assertEqual(verify(src, out, words)["translated_paragraphs"], 1)
        with self.assertRaisesRegex(ValueError, "译文段数"):
            verify(src, src, words)
        for kind in ["txt", "md"]:
            src, out = (self.work / (n + "." + kind) for n in ("draft", "dual"))
            src.write_text("Report\n\nTemperature", encoding="utf-8")
            values = {u["id"]: "résultat" for u in plain.extract(src)}
            plain.apply(src, values, out)
            self.assertEqual(verify(src, out, values)["translated_paragraphs"], 2)
            out.write_text("result\n\nTempérature", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify(src, out, values)

    def test_two_columns_in_rotated_cropped_page(self):
        source, output = self.work / "labels.pdf", self.work / "dual.pdf"
        with pdf.open() as d:
            p = d.new_page(width=300, height=190)
            p.insert_text((52, 90), "Temperature", fontsize=10)
            p.insert_text((183, 90), "Pressure", fontsize=10)
            p.set_cropbox(pdf.Rect(35, 25, 265, 160))
            p.set_rotation(90)
            d.save(source)
        units = pdf_layout.extract(source)
        by_text = {"Temperature": "Temperatura", "Pressure": "Presión"}
        values = {u["id"]: by_text[u["text"]] for u in units}
        # PDF extraction is in unrotated crop-box coordinates. The bilingual
        # sheet places the visible, rotated page upright in each half.
        with pdf.open(source) as d:
            page = d[0]
            rectangles = {u["id"]: list(page.search_for(u["text"])[0] * page.rotation_matrix) for u in units}
        pdf_layout.apply(source, values, output)
        checks = verify(source, output, values, rectangles=rectangles)
        self.assertEqual(checks["warnings"], [])
        swapped = dict(zip(rectangles, reversed(rectangles.values())))
        checks = verify(source, output, values, rectangles=swapped)
        self.assertTrue(any("文字框以外" in warning for warning in checks["warnings"]))
