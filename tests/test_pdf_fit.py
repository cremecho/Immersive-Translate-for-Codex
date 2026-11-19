from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest

import pymupdf as pdf

from immersive import pdf_layout
from immersive.verify import verify, _normalized


class PdfFitTest(unittest.TestCase):
    def working_files(self, cleanup):
        files = []
        for _ in range(2):
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                path = Path(tmp.name)
            cleanup.callback(path.unlink, missing_ok=True)
            files.append(path)
        return files

    def test_acronym_expansion_and_math_rank_are_fully_visible_and_verified(self):
        with ExitStack() as cleanup:
            source, output = self.working_files(cleanup)
            with pdf.open() as doc:
                page = doc.new_page(width=240, height=115)
                page.insert_text((16, 20), 'RAG', fontsize=6)
                page.insert_text((105, 20), 'Adjacent column', fontsize=6)
                page.insert_text((16, 60), 'The fusion weight and rank', fontsize=10)
                doc.save(source)
            rows = pdf_layout.extract(source)
            lookup = {'RAG': '检索增强生成（RAG）', 'Adjacent column': 'Adjacent column',
                      'The fusion weight and rank': '融合权重，𝑟𝑎𝑛𝑘'}
            translations = {r['id']: lookup[r['text']] for r in rows}
            boxes = {r['id']: r['_rect'] for r in rows}
            report = pdf_layout.apply(source, translations, output)
            self.assertTrue(any('扩排' in warning for warning in report))
            self.assertEqual(verify(source, output, translations, rectangles=boxes)['warnings'], [])
            with pdf.open(output) as doc:
                right = doc[0].get_text(clip=pdf.Rect(240, 0, 480, 115))
                self.assertIn('检索增强生成rag', _normalized(right))
                self.assertIn('融合权重rank', _normalized(right))
                self.assertIn('Adjacent column', right)
            damaged = dict(translations)
            formula = next(r for r in rows if r['text'].endswith('rank'))
            damaged[formula['id']] = '融合权重，𝑧𝑧𝑧𝑧'
            self.assertTrue(any(formula['id'] in w for w in verify(source, output, damaged, boxes)['warnings']))

    def test_cell_wraps_identifier_and_never_widens_across_its_border(self):
        with pdf.open() as doc:
            page = doc.new_page()
            page.insert_text((10, 25), 'ID', fontsize=8)
            units, _ = pdf_layout._fast_units(page, [])
            unit = units[0]
            unit.rect = pdf.Rect(10, 16, 30, 44)
            unit.kind = 'cell'
            space = pdf_layout.LabelSpace(page, units)
            self.assertEqual(space.wider_box(unit, 8, 'ModelVersion'), unit.rect)
            written = doc.new_page()
            warnings = []
            pdf_layout._typeset(written, unit, 'ModelVersion', fit_space=space, fit_warnings=warnings)
            self.assertIn('modelversion', _normalized(written.get_text(clip=unit.rect)))
            self.assertTrue(any('名称边界' in w for w in warnings))

    def test_overflows_on_two_pages_return_both_ids_without_publishing(self):
        with ExitStack() as cleanup:
            source, output = self.working_files(cleanup)
            with pdf.open() as doc:
                for label in ('AAA', 'BBB'):
                    page = doc.new_page(width=120, height=80)
                    page.insert_text((16, 30), label, fontsize=6)
                    page.draw_line((29, 21), (29, 38))
                doc.save(source)
            ids = [r['id'] for r in pdf_layout.extract(source)]
            with self.assertRaises(pdf_layout.PdfFitError) as caught:
                pdf_layout.apply(source, dict.fromkeys(ids, 'Long term ' * 50), output)
            for key in ids:
                self.assertIn(key, str(caught.exception))
            self.assertEqual(output.read_bytes(), b'')
