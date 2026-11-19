from contextlib import chdir, closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from immersive import packets
from immersive.verify import verify
from translate import DB_NAME, finish, start

import pymupdf as pdf


class PacketWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("translation-packets-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.job = self.root / "job"
        self.source = self.root / "报告.txt"
        self.repeated = "Inspect the instrument again after the indicator remains stable for one minute."
        self.source.write_text(self.repeated + "\n\nDone.\n\n" + self.repeated, encoding="utf-8")
        self.output = self.root / "双语.txt"

    def begin(self):
        return start(self.source, self.job, "zh-CN", chars=30)

    def done(self):
        conn = sqlite3.connect(self.job / DB_NAME)
        try:
            return finish(conn, self.job, self.output)
        finally:
            conn.close()

    def make_pdf(self):
        self.source = self.root / "report.pdf"
        self.output = self.root / "dual.pdf"
        with pdf.open() as doc:
            page = doc.new_page(width=240, height=145)
            page.insert_text((18, 30), "Axial marker", fontsize=12, fontname="tibi", color=(0, .2, .65))
            page.insert_text((18, 73), "The score is x = 2.", fontsize=12)
            page.insert_text((18, 115), "Rate", fontsize=12)
            # Unresolved named destinations occur in publisher PDFs.
            ref = doc.get_new_xref()
            doc.update_object(ref, "<< /Type/Annot /Subtype/Link /Rect[18 28 42 43] /Dest/absent >>")
            doc.xref_set_key(page.xref, "Annots", f"[{ref} 0 R]")
            doc.save(self.source)

    def answer_pdf(self, packets):
        expected = {"Axial marker": None, "The score is x = 2.": "Score: x = 2.", "Rate": "Frequency"}
        for packet in packets:
            rows = json.loads(Path(packet["source"]).read_text(encoding="utf-8"))["items"]
            answers = {key: expected[source] for key, source, *_ in rows}
            Path(packet["answer"]).write_text(json.dumps(answers), encoding="utf-8")

    def test_pdf_null_keeps_exact_glyphs_and_advisories_allow_delivery(self):
        self.make_pdf()
        groups = self.begin()["packets"]
        self.answer_pdf(groups)
        # Null is positional too, so a preserved graph label never shifts the
        # following translations when moving from keyed to ordered responses.
        for packet in groups:
            answer = Path(packet["answer"])
            old = json.loads(answer.read_text("utf-8"))
            prompt = json.loads(Path(packet["prompt"]).read_text("utf-8"))
            rows = json.loads(Path(packet["source"]).read_text("utf-8"))["items"]
            answer.write_text(json.dumps([prompt["key"], *(old[row[0]] for row in rows)]), "utf-8")
        result = self.done()
        self.assertEqual(result["delivery_status"], "ready")
        self.assertEqual(result["warning_groups"], {"math": 1, "link": 1, "fit": 1})
        with pdf.open(self.source) as original, pdf.open(self.output) as dual:
            label = original[0].search_for("Axial marker")[0]
            shift = (240, 0, 240, 0)
            self.assertEqual(original[0].get_pixmap(clip=label).samples,
                             dual[0].get_pixmap(clip=label + shift).samples)
            right = dual[0].get_text(clip=original[0].rect + shift)
            self.assertIn("Axial marker", right)
            self.assertIn("Frequency", "".join(right.split()))

    def test_pdf_displaced_translation_requires_review(self):
        self.make_pdf()
        self.answer_pdf(self.begin()["packets"])
        # Simulate a saved box for a label on the wrong side of the source page.
        with closing(sqlite3.connect(self.job / DB_NAME)) as conn, conn:
            rows = conn.execute("SELECT id, unit FROM segments").fetchall()
            key, unit = next((key, json.loads(raw)) for key, raw in rows if json.loads(raw)["text"] == "Rate")
            unit["_rect"] = [185, 90, 228, 127]
            conn.execute("UPDATE segments SET unit=? WHERE id=?", (json.dumps(unit), key))
        result = self.done()
        self.assertEqual(result["delivery_status"], "review")
        self.assertEqual(result["warning_groups"]["check"], 1)
        self.assertTrue(any(key in warning for warning in result["review_findings"]))

    def test_resume_v2_pdf_answers_and_reuse_verified_v2_delivery(self):
        self.make_pdf()
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        version2 = "2" + str(self.source.resolve()) + original_hash + "zh-CN"
        key = hashlib.sha256(version2.encode()).hexdigest()[:16]
        self.job = self.root / ".translate-work" / ("document-" + key)
        groups = self.begin()["packets"]
        self.assertGreater(len(groups), 1)
        self.answer_pdf(groups[:1])
        with chdir(self.root):
            resumed = start(self.source, None, "zh-CN", chars=30)
        self.assertEqual(Path(resumed["job"]), self.job)
        self.assertEqual([item["answer"] for item in resumed["packets"]], [item["answer"] for item in groups[1:]])
        self.answer_pdf(resumed["packets"])
        self.done()
        # Match the pre-upgrade delivery schema and snapshot, including the old
        # report that predated delivery_status. The recorded output must survive.
        with closing(sqlite3.connect(self.job / DB_NAME)) as conn, conn:
            values = dict(conn.execute("SELECT id,translation FROM segments ORDER BY rowid"))
            snapshot = json.dumps(["2", original_hash, "zh-CN", values], ensure_ascii=False)
            state = hashlib.sha256(snapshot.encode()).hexdigest()
            legacy = {"output": str(self.output), "warning_groups": {"check": 1, "math": 1},
                      "warnings": ["第 1 页的斜排或直排文字保留原貌。"]}
            conn.execute("UPDATE deliveries SET state=?,report=?", (state, json.dumps(legacy)))
        before = self.output.read_bytes()
        with patch("immersive.verify.verify", wraps=verify) as recheck:
            for _ in range(2):
                resumed = self.done()
                self.assertTrue(resumed["reused"])
                self.assertEqual(resumed["delivery_status"], "ready")
            recheck.assert_called_once()
            self.assertEqual(recheck.call_args.args[1], self.output)
        self.assertEqual(before, self.output.read_bytes())

    def test_parallel_answers_retries_and_safe_reuse(self):
        groups = self.begin()["packets"]
        self.assertGreaterEqual(len(groups), 2)
        expected = {self.repeated: "示值稳定一分钟后，再次检查仪器。", "Done.": "完成。"}
        answers = []
        for packet in groups:
            rows = json.loads(Path(packet["source"]).read_text(encoding="utf-8"))["items"]
            values = {row[0]: expected[row[1]] for row in rows}
            answers.append((Path(packet["answer"]), values))
        for answer, values in reversed(answers[1:]):
            answer.write_text(json.dumps(values), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"001\.answer\.json"):
            self.done()
        self.assertFalse(self.output.exists())
        self.assertEqual(len(self.begin()["packets"]), 1)
        path, values = answers[0]
        path.write_text('{"wrong":"错误"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "段号"):
            self.done()
        path.write_text(json.dumps(values), encoding="utf-8")
        finished = self.done()
        self.assertEqual(finished["segments"], 3)
        self.assertEqual(self.output.read_text(encoding="utf-8").count(expected[self.repeated]), 2)
        self.assertTrue(self.done()["reused"])
        self.output.write_text("我的手工校订", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.done()

    def test_compact_answers_keep_order_fan_out_and_guard_wrong_or_short_packet(self):
        first, second = self.begin()["packets"]
        prompts = [json.loads(Path(p["prompt"]).read_text("utf-8")) for p in (first, second)]
        self.assertEqual([p["items"] for p in prompts], [[self.repeated], ["Done."]])
        self.assertNotIn("contexts", prompts[0])
        first_file, second_file = (Path(p["answer"]) for p in (first, second))
        # Both packets have one entry. A swapped response must still fail.
        first_file.write_text(json.dumps([prompts[1]["key"], "完成。"]), "utf-8")
        with self.assertRaisesRegex(ValueError, "原始 key"):
            self.done()
        first_file.write_text(json.dumps([prompts[0]["key"]]), "utf-8")
        with self.assertRaisesRegex(ValueError, "需要 1 项"):
            self.done()
        first_file.write_text(json.dumps([prompts[0]["key"], "一分钟后复检。"]), "utf-8")
        # A worker using the original {id: translation} contract can finish another packet.
        old = json.loads(Path(second["source"]).read_text("utf-8"))
        second_file.write_text(json.dumps({old["items"][0][0]: "完成。"}), "utf-8")
        self.assertEqual(self.done()["delivery_status"], "ready")
        result = self.output.read_text("utf-8")
        self.assertEqual(result.count("一分钟后复检。"), 2)
        self.assertEqual(result.count("完成。"), 1)

    def test_compact_formula_context_is_shared_and_natural_language_always_reaches_worker(self):
        original = "𝑇𝑖= {type: 𝜏, equipment: 𝜀}"
        notes = "独立的公式已保留原位，只翻译 text 中的片段。原行：" + original
        math = "。正文与行内数学共用文字层，请保留完整算式、变量和上下标。"
        rows = [["p1-g1", "type:", notes + math], ["p1-g2", "equipment:", notes],
                ["p1-g3", "Held ⟦f1⟧", "保留公式标记：⟦f1⟧=𝑇"]]
        prompt = packets._prompt("en", rows, True)
        self.assertEqual(prompt["contexts"], {1: original, 2: "⟦f1⟧=𝑇"})
        self.assertEqual(prompt["items"], [["type:", 1], ["equipment:", 1], ["Held ⟦f1⟧", 2]])
        for text in ("where", "and", "sys", "equipment:", "AI", "ISO", "Tool-based", "Qwen3-8B",
                     "https://example.org。请阅读", "sim", "YES"):
            self.assertFalse(packets._native_text({"id":"p1-g1", "text":text, "context":notes}))
        for text in ("https://example.org/k?a=4#ref", "10.1234/journal.1", "a@b.example"):
            self.assertTrue(packets._native_text({"id":"p1-g1", "text":text}))
            self.assertFalse(packets._native_text({"id":"txt-1", "text":text}))
        formula = "PDF 第 6 页。独立的公式已保留原位，只翻译 text 中的片段。原行：Sim(𝑇𝑖, 𝑇𝑗)"
        self.assertTrue(packets._native_text({"id":"p6-g2", "text":"Sim(", "context":formula}))
        self.assertFalse(packets._native_text({"id":"p6-g2", "text":"Sim(", "context":"PDF 第 6 页"}))

    def test_all_identifiers_finish_without_model_input(self):
        self.source = self.root / "ids.pdf"
        self.output = self.root / "dual.pdf"
        with pdf.open() as doc:
            page = doc.new_page(width=240, height=180)
            page.insert_text((15, 35), "https://example.org/calibration")
            page.insert_text((15, 72), "10.1234/journal.1")
            doc.save(self.source)
        self.assertEqual(self.begin()["packets"], [])
        self.assertEqual(self.done()["delivery_status"], "ready")
        with pdf.open(self.output) as dual, pdf.open(self.source) as original:
            expected = original[0].get_pixmap()
            right = dual[0].get_pixmap(clip=original[0].rect + (240, 0, 240, 0))
            self.assertEqual(expected.samples, right.samples)

    def test_distinct_translation_ids_and_placeholder_repair(self):
        self.source = self.root / "notes.md"
        self.source.write_text("Read the **guide**.", encoding="utf-8")
        group, = self.begin()["packets"]
        unit, = json.loads(Path(group["source"]).read_text(encoding="utf-8"))["items"]
        key, source = unit[:2]
        answers = Path(group["answer"])
        answers.write_text(json.dumps({key: None}), encoding="utf-8")
        self.output = self.root / "双语.md"
        with self.assertRaisesRegex(ValueError, "译文为空"):
            self.done()
        answers.write_text(json.dumps({key: "请阅读指南。"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "格式标记"):
            self.done()
        tag = json.loads(Path(group["prompt"]).read_text("utf-8"))["key"]
        answers.write_text(json.dumps([tag, "请阅读指南。"]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "第 1 项译文.*格式标记"):
            self.done()
        values = {key: source.replace("Read the ", "请阅读").replace("guide", "指南").replace(".", "。")}
        answers.write_text(json.dumps(values), encoding="utf-8")
        self.done()
        self.assertIn("**指南**", self.output.read_text(encoding="utf-8"))
