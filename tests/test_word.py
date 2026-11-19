import base64
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import unittest
from uuid import uuid4
from zipfile import ZipFile

from docx import Document
from docx.enum.table import WD_ROW_HEIGHT_RULE
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from immersive import word


@contextmanager
def workspace_tmp():
    # Inherit the workspace ACL on Windows, including in restricted Codex jobs.
    folder = Path.cwd() / (".test-word-" + uuid4().hex)
    folder.mkdir()
    try:
        yield folder
    finally:
        for file in folder.iterdir():
            file.unlink()
        folder.rmdir()


def specimen(path):
    doc = Document()
    doc.add_heading("Inspection report", 1)
    paragraph = doc.add_paragraph("Read the ")
    paragraph.add_run("results").bold = True
    paragraph.add_run(" and compare ")
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), doc.part.relate_to("https://example.org", RT.HYPERLINK, is_external=True))
    link.append(OxmlElement("w:r"))
    link[0].add_t("this reference")
    paragraph._p.append(link)
    paragraph.add_run(".")
    doc.add_paragraph("Inspect the coating", "List Number")
    p = doc.add_paragraph("Measured heat ")
    formula = OxmlElement("m:oMath")
    mr = OxmlElement("m:r")
    mt = OxmlElement("m:t")
    mt.text = "Q"
    mr.append(mt)
    formula.append(mr)
    p._p.append(formula)
    p.add_run(" after sampling.")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), " SEQ Figure ")
    field.append(OxmlElement("w:r"))
    field[0].add_t("1")
    doc.add_paragraph("Figure ")._p.append(field)
    table = doc.add_table(1, 2)
    table.style = "Table Grid"
    table.cell(0, 0).text = "Material"
    table.cell(0, 1).text = "Steel"
    table.rows[0].height = Pt(16)
    table.rows[0].height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
    # A single original bitmap plus caption; the image has no translation unit.
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2qkAAAAASUVORK5CYII=")
    doc.add_picture(BytesIO(tiny_png), width=Inches(.1))
    doc.sections[0].header.paragraphs[0].text = "Working paper"
    doc.save(path)


def chinese(rows):
    phrases = {"Inspection report": "检验报告", "Read the ": "请阅读", "results": "结果",
               " and compare ": "并对比", "this reference": "此参考文献", ".": "。",
               "Inspect the coating": "检查涂层", "Measured heat ": "采样后测量热量",
               " after sampling.": "。", "Material": "材料", "Steel": "钢", "Figure ": "图 ",
               "Working paper": "工作稿"}
    # Longest first so that the full stop inside a phrase stays with its phrase.
    output = {}
    for row in rows:
        value = row["text"]
        for en in sorted(phrases, key=len, reverse=True):
            value = value.replace(en, phrases[en])
        output[row["id"]] = value
    return output


class WordBilingualTest(unittest.TestCase):
    def test_native_objects_formatting_and_package_parts(self):
        with workspace_tmp() as folder:
            src, out = (Path(folder) / n for n in ("source.docx", "dual.docx"))
            specimen(src)
            rows = word.extract(src)
            self.assertEqual(8, len(rows))
            translations = chinese(rows)
            word.apply(src, translations, out)
            with ZipFile(src) as original, ZipFile(out) as edited:
                self.assertEqual(original.namelist(), edited.namelist())
                for name in original.namelist():
                    if name not in ("word/document.xml", "word/header1.xml"):
                        self.assertEqual(original.read(name), edited.read(name), name)
            doc = Document(out)
            paragraphs = doc.paragraphs
            self.assertEqual(["Inspection report", "检验报告"], [p.text for p in paragraphs[:2]])
            self.assertEqual(paragraphs[0].style.style_id, paragraphs[1].style.style_id)
            self.assertEqual("请阅读结果并对比此参考文献。", paragraphs[3].text)
            self.assertTrue(next(r for r in paragraphs[3].runs if r.text == "结果").bold)
            self.assertEqual("https://example.org", paragraphs[3].hyperlinks[0].address)
            self.assertEqual("0", paragraphs[5]._p.xpath("./w:pPr/w:numPr/w:numId/@w:val")[0])
            self.assertEqual(2, len(doc.element.xpath("//m:oMath")))
            self.assertEqual(1, len(doc.element.xpath("//w:fldSimple")))
            self.assertEqual(1, len(doc.inline_shapes))
            self.assertEqual("Material\n材料", doc.tables[0].cell(0, 0).text)
            self.assertEqual(WD_ROW_HEIGHT_RULE.AT_LEAST, doc.tables[0].rows[0].height_rule)
            self.assertEqual(["Working paper", "工作稿"], [p.text for p in doc.sections[0].header.paragraphs])
            bad = translations.copy()
            key = next(k for k, v in bad.items() if "⟦" in v)
            bad[key] = "破坏了标记"
            with self.assertRaisesRegex(ValueError, "标记"):
                word.apply(src, bad, out)

    def test_sections_and_literal_format_markers(self):
        with workspace_tmp() as folder:
            src, out = (Path(folder) / n for n in ("source.docx", "dual.docx"))
            doc = Document()
            p = doc.add_paragraph("Literal ⟦1⟧ ")
            p.add_run("bold").bold = True
            p._p.get_or_add_pPr().get_or_add_sectPr()
            doc.add_paragraph("Next chapter")
            doc.save(src)
            segments = word.extract(src)
            self.assertIn("⟦W2⟧", segments[0]["text"])
            word.apply(src, {r["id"]: r["text"] for r in segments}, out)
            output = Document(out)
            self.assertIsNone(output.paragraphs[0]._p.pPr.sectPr)
            self.assertIsNotNone(output.paragraphs[1]._p.pPr.sectPr)

    def test_strict_namespace_signals_normalization_on_main_document(self):
        with workspace_tmp() as folder:
            original, strict = (folder / name for name in ("original.docx", "strict.docx"))
            specimen(original)
            with ZipFile(original) as source, ZipFile(strict, "w") as target:
                for info in source.infolist():
                    data = source.read(info)
                    if info.filename in ("word/document.xml", "word/header1.xml"):
                        data = data.replace(word.WORD_NS.encode(), b"http://purl.oclc.org/ooxml/wordprocessingml/main")
                    target.writestr(info, data)
            with self.assertRaises(word.StrictDocument):
                word.extract(strict)

    def test_header_repeats_live_page_number(self):
        with workspace_tmp() as folder:
            src, out = (folder / name for name in ("page.docx", "dual.docx"))
            doc = Document()
            p = doc.sections[0].header.paragraphs[0]
            p.add_run("Page ")
            for child in ("begin", "instruction", "separate", "value", "end"):
                run = p.add_run()._r
                if child == "value":
                    run.add_t("1")
                elif child == "instruction":
                    instr = OxmlElement("w:instrText")
                    instr.text = " PAGE "
                    run.append(instr)
                else:
                    run.append(OxmlElement("w:fldChar", attrs={qn("w:fldCharType"): child}))
            doc.save(src)
            rows = word.extract(src)
            word.apply(src, {r["id"]: r["text"].replace("Page ", "页码 ") for r in rows}, out)
            target = Document(out).sections[0].header.paragraphs[1]
            self.assertEqual([" PAGE "], target._p.xpath("w:fldSimple/@w:instr"))
            self.assertEqual(["1"], target._p.xpath("w:fldSimple/w:r/w:t/text()"))
