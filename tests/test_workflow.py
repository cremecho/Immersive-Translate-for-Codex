import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest
import uuid

from translate import DB_NAME, add, build, pending, prepare


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("translate-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.source = self.root / "报告.txt"
        self.source.write_bytes(b"One paragraph.\r\n\r\nAnother paragraph.\r\n")
        self.job = self.root / "job"
        prepare(self.source, self.job, "zh-CN")
        self.conn = sqlite3.connect(self.job / DB_NAME)
        self.addCleanup(self.conn.close)

    def save(self, values, replace=False):
        answers = self.root / "answers.json"
        answers.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        return add(self.conn, answers, replace)

    def test_incomplete_rollback_resume_and_collision(self):
        units = pending(self.conn, 20, 2000)["segments"]
        first, last = (u["id"] for u in units)
        with self.assertRaises(ValueError):
            self.save({first: "第一段。", "wrong-id": "无效"})
        self.assertEqual(pending(self.conn, 1, 1)["remaining"], 2)
        self.save({first: "第一段。"})
        self.save({first: "第一段。"})
        with self.assertRaises(ValueError):
            self.save({first: "第一自然段。"})
        self.save({first: "第一自然段。"}, replace=True)
        out = self.root / "结果.txt"
        with self.assertRaises(ValueError):
            build(self.conn, self.job, out)
        self.assertFalse(out.exists())
        self.assertEqual(pending(self.conn, 1, 50)["segments"][0]["id"], last)
        self.save({last: "第二段。"})
        result = build(self.conn, self.job, out)
        self.assertEqual(result["segments"], 2)
        text = out.read_text(encoding="utf-8")
        self.assertLess(text.index("One paragraph"), text.index("第一自然段。"))
        self.assertLess(text.index("第一自然段。"), text.index("Another paragraph"))
        self.assertLess(text.index("Another paragraph"), text.index("第二段。"))
        with self.assertRaises(FileExistsError):
            build(self.conn, self.job, out)

    def test_source_change_and_invalid_targets(self):
        with self.assertRaises(FileExistsError):
            prepare(self.source, self.job, "zh-CN")
        self.save({u["id"]: "译文" for u in pending(self.conn, 20, 2000)["segments"]})
        with self.assertRaises(ValueError):
            build(self.conn, self.job, self.root / "result.pdf")
        self.source.write_text("A new revision.", encoding="utf-8")
        with self.assertRaises(ValueError):
            build(self.conn, self.job, None)
        self.assertFalse((self.root / "报告-zh-CN-dual.txt").exists())
