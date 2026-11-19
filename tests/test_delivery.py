import json
from contextlib import chdir, closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from uuid import uuid4

import pymupdf as pdf

from immersive.pdf_figures import _paints_in_prefix
from translate import DB_NAME, finish, start


class AutomaticDeliveryTest(unittest.TestCase):
    def test_shorten_all_failures_and_retry_only_those_answers(self):
        root = Path(tempfile.gettempdir()) / ("translation-deliver-" + uuid4().hex)
        root.mkdir()
        self.addCleanup(shutil.rmtree, root)
        with chdir(root):
            src, job = root / "test.pdf", root / "work"
            with pdf.open() as doc:
                for word in ("First", "Second"):
                    page = doc.new_page(width=120, height=70)
                    page.insert_text((20, 30), word)
                doc.save(src)
            task = start(src, job, "zh-CN", 10000)
            packet, = task["packets"]
            rows = json.loads(Path(packet["source"]).read_text("utf-8"))["items"]
            Path(packet["answer"]).write_text(json.dumps({r[0]: "W" * 400 for r in rows}), "utf-8")
            with closing(sqlite3.connect(job / DB_NAME)) as conn:
                result = finish(conn, job, None, auto_output=True)
                self.assertEqual(result["delivery_status"], "shorten")
                self.assertEqual({r["source"] for r in result["items"]}, {"First", "Second"})
                self.assertTrue(all(r["current"] == "W" * 400 for r in result["items"]))
                self.assertEqual(list((root / "output/pdf").glob("*.pdf")), [])
                changes = root / "changes.json"
                changes.write_text(json.dumps({r[0]: "短" for r in rows}), "utf-8")
                ready = finish(conn, job, None, auto_output=True, corrections=changes)
                self.assertEqual(ready["delivery_status"], "ready")
                original = Path(ready["output"])
                first_bytes = original.read_bytes()
                self.assertTrue(finish(conn, job, None, auto_output=True)["reused"])
                changes.write_text(json.dumps({rows[0][0]: "新"}), "utf-8")
                revised = finish(conn, job, None, auto_output=True, corrections=changes)
                self.assertEqual(revised["page_cache"]["pages_reused"], 1)
                self.assertNotEqual(revised["output"], ready["output"])
                self.assertEqual(original.read_bytes(), first_bytes)

    def test_figure_operator_probe_handles_pdf_delimiters(self):
        self.assertTrue(_paints_in_prefix(b'/Obj /Tag BDC [2 .5] 0 d 10 10 m 20 20 l S\n'))
        for content in (b'(S) Tj', b'% S\n(F) Tj', b'/S BMC EMC', b'<< /Text ( S ) >> BDC',
                        b'[(text with B*)] TJ', b'<42> Tj', b' '*8191 + b'Stuff'):
            self.assertFalse(_paints_in_prefix(content))
