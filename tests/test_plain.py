import codecs
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

from immersive import plain


class PlainDocuments(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.gettempdir()) / ("bilingual-text-" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)

    def test_text_encoding_wraps_and_mixed_endings(self):
        src = self.folder / "draft.txt"
        out = self.folder / "result.txt"
        original = "  First line\r\nwraps.\r\n\r\nSecond.\n\nLast."
        expected = "  First line\r\nwraps.\r\n  首段。\r\n\r\nSecond.\n第二段。\n\nLast.\r\n末段。"
        src.write_bytes(codecs.BOM_UTF16_BE + original.encode("utf-16-be"))
        entries = plain.extract(src)
        self.assertEqual([e["text"] for e in entries], ["First line\nwraps.", "Second.", "Last."])
        self.assertEqual([], plain.apply(src, dict(zip([e["id"] for e in entries],
                                                     ["首段。", "第二段。", "末段。"])), out))
        self.assertEqual(codecs.BOM_UTF16_BE + expected.encode("utf-16-be"), out.read_bytes())
        self.assertEqual(codecs.BOM_UTF16_BE + original.encode("utf-16-be"), src.read_bytes())

    def test_single_newline_chinese_paragraphs_and_wrapped_lines(self):
        src = self.folder / "简报.txt"
        out = self.folder / "简报-dual.txt"
        src.write_text("\n当环境压力\n趋于稳定时开始校准。\n读数未见漂移。\n　　复测完成\n1. 导出记录\n",
                       encoding="utf-8")
        units = plain.extract(src)
        self.assertEqual([u["text"] for u in units], [
            "当环境压力\n趋于稳定时开始校准。", "读数未见漂移。", "复测完成", "1. 导出记录",
        ])
        english = ["Start calibration once ambient pressure stabilizes.", "No drift was observed.",
                   "Retest complete", "Export the log"]
        plain.apply(src, dict(zip((u["id"] for u in units), english)), out)
        self.assertIn("　　复测完成\n　　Retest complete\n1. 导出记录\nExport the log",
                      out.read_text(encoding="utf-8"))

    def test_no_blank_english_preserves_wrapped_abbreviations(self):
        src = self.folder / "report.txt"
        src.write_text("The offset in Fig.\n2 stayed below the\nthreshold.\nRecord the result.",
                       encoding="utf-8")
        self.assertEqual([u["text"] for u in plain.extract(src)],
                         ["The offset in Fig.\n2 stayed below the\nthreshold.", "Record the result."])

    def test_rendered_markdown_tables_lists_and_links(self):
        from markdown_it import MarkdownIt

        src = self.folder / "draft.md"
        out = self.folder / "result.md"
        original = (
            "---\ntitle: Draft\n---\n\n# Title\n\n"
            '> 1. [x] **Read** [Guide](docs/a_(b) "Manual") and `x+y`.\n'
            "> 2. Use $a_i$ here.\n\n"
            "| Name | Contents |\n| :--- | ---: |\n"
            "| alpha \\| beta | ![diagram](pic.png) |\n\n"
            "Read [Home][].\n\n[Home]: https://example.net/\n\n"
            "```python\nprint('KEEP')\n```\n"
        )
        src.write_bytes(original.replace("\n", "\r\n").encode())
        entries = plain.extract(src)
        words = {
            "Title": "标题", "Name": "名称", "Contents": "内容", "alpha | beta": "甲 | 乙",
            "⟦m1⟧Read⟦m2⟧ ⟦m3⟧Guide⟦m4⟧ and ⟦m5⟧.": "⟦m1⟧阅读⟦m2⟧⟦m3⟧指南⟦m4⟧和⟦m5⟧。",
            "Use ⟦m1⟧ here.": "此处使用⟦m1⟧。", "Read ⟦m1⟧Home⟦m2⟧.": "阅读⟦m1⟧主页⟦m2⟧。",
        }
        answers = {e["id"]: words[e["text"]] for e in entries}
        self.assertEqual(len(entries), 7)
        self.assertEqual(plain.apply(src, answers, out), [])
        bilingual = out.read_bytes().decode("utf-8")
        self.assertNotIn("\n", bilingual.replace("\r\n", ""))
        html = MarkdownIt("commonmark").enable("table").render(bilingual)
        for fragment in (
            "<h1>标题</h1>", "<strong>阅读</strong>", ">指南</a>和<code>x+y</code>",
            "此处使用$a_i$。", "alpha | beta<br>甲 | 乙", 'href="https://example.net/">主页</a>',
            '<th style="text-align:right">Contents<br>内容</th>',
        ):
            self.assertIn(fragment, html)
        for fragment, count in (("<li>", 2), ("<img ", 1), ("print('KEEP')", 1), ("[x]", 1)):
            self.assertEqual(html.count(fragment), count)
        self.assertEqual(bilingual.count("title: Draft"), 1)
        bad = dict(answers)
        bad["md-7"] = "漏掉全部标记"
        with self.assertRaisesRegex(ValueError, "占位符"):
            plain.apply(src, bad, out)
        self.assertEqual(out.read_bytes().decode("utf-8"), bilingual)

    def test_nontext_markdown_and_scan_of_html_blocks(self):
        src = self.folder / "assets.md"
        out = self.folder / "result.md"
        original = "![a](x.png)\n\n    indented_code()\n\n<figure>raw caption</figure>\n"
        src.write_text(original, encoding="utf-8")
        self.assertEqual(plain.extract(src), [])
        self.assertTrue(any("HTML" in w for w in plain.apply(src, {}, out)))
        self.assertEqual(src.read_bytes(), out.read_bytes())

    def test_multiline_math_between_paragraphs_and_setext_heading(self):
        src = self.folder / "math.md"
        out = self.folder / "dual.md"
        equation = "$$\nE = m c^2\n\n{} + K\n$$\n"
        align = "\\begin{align*}\na &= b \\\\\n  x &= y\n\\end{align*}\n"
        original = "Energy\n======\nAccordingly,\n" + equation + align + "the law holds.\n"
        src.write_text(original, encoding="utf-8")
        units = plain.extract(src)
        self.assertEqual([u["text"] for u in units], ["Energy", "Accordingly,", "the law holds."])
        plain.apply(src, dict(zip((u["id"] for u in units), ("能量", "因此，", "定律成立。"))), out)
        bilingual = out.read_text(encoding="utf-8")
        self.assertIn("Energy\n======\n\n# 能量\n", bilingual)
        self.assertEqual(bilingual.count(equation + align), 1)
