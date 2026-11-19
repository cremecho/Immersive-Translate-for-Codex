import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

import pymupdf as pdf

from immersive import pdf_layout


class PdfFiguresTest(unittest.TestCase):
    def test_native_figure_labels_stay_in_art_and_legacy_ids_resume(self):
        with ExitStack() as cleanup:
            def temporary():
                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
                    path = Path(f.name)
                cleanup.callback(path.unlink, missing_ok=True)
                return path

            source, result = temporary(), temporary()
            diagram = pdf.Rect(20, 30, 120, 90)
            table = pdf.Rect(20, 140, 120, 200)
            with pdf.open() as doc:
                page = doc.new_page(width=220, height=270)
                page.insert_text((20, 20), 'Body sentence.')
                for area, label in ((diagram, 'Chart label'), (table, 'Table cell')):
                    with pdf.open() as artwork:
                        part = artwork.new_page(width=100, height=60)
                        part.draw_rect(pdf.Rect(3, 3, 95, 55), color=(.5, 0, .8))
                        part.insert_text((12, 30), label)
                        page.show_pdf_page(area, artwork, 0)
                page.insert_text((20, 104), 'Figure 1: Caption')
                page.insert_text((20, 215), 'Table 1: Caption')
                doc.save(source)
            records = pdf_layout.extract(source)
            expected = {'Body sentence.', 'Figure 1: Caption', 'Table 1: Caption', 'Table cell'}
            self.assertEqual({r['text'] for r in records}, expected)
            self.assertTrue(all('-g' in r['id'] for r in records))
            pdf_layout.apply(source, {r['id']: 'Done' for r in records}, result)
            with pdf.open(source) as orig, pdf.open(result) as dual:
                orig_pixels = orig[0].get_pixmap(clip=diagram).samples
                for offset in (0, 220):
                    after = dual[0].get_pixmap(clip=diagram + (offset, 0, offset, 0))
                    self.assertEqual(orig_pixels, after.samples)
                self.assertIn('Chart label', dual[0].get_text(clip=diagram + (220, 0, 220, 0)))
                old_units, _ = pdf_layout._fast_units(orig[0], [])
            self.assertIn('Chart label', [u.text for u in old_units])
            old_records = {u.id: u.text for u in old_units}
            pdf_layout.apply(source, old_records, result, source_texts=old_records)

    def test_named_figure_without_caption_preserves_figure_only_page(self):
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            source = Path(f.name)
        self.addCleanup(source.unlink, missing_ok=True)
        with pdf.open() as doc, pdf.open() as art:
            art.new_page().insert_text((10, 30), 'Editable illustration')
            page = doc.new_page()
            page.show_pdf_page(page.rect, art, 0)
            xref = next(x for x, _, parent, _ in page.get_xobjects() if not parent)
            doc.xref_set_key(xref, 'PTEX.FileName', '(./figures/example.pdf)')
            doc.save(source)
        self.assertEqual(pdf_layout.extract(source), [])
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            out = Path(f.name)
        self.addCleanup(out.unlink, missing_ok=True)
        pdf_layout.apply(source, {}, out)
        with pdf.open(out) as dual:
            self.assertEqual(dual[0].get_text().count('Editable illustration'), 2)

    def test_body_redaction_overlapping_figure_does_not_erase_label(self):
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            source = Path(f.name)
        self.addCleanup(source.unlink, missing_ok=True)
        with pdf.open() as doc, pdf.open() as artwork:
            part = artwork.new_page(width=100, height=80)
            part.insert_text((10, 25), 'Diagram label')
            page = doc.new_page(width=220, height=150)
            page.show_pdf_page(pdf.Rect(20, 20, 120, 100), artwork, 0)
            xref = next(x for x, _, parent, _ in page.get_xobjects() if not parent)
            doc.xref_set_key(xref, 'PTEX.FileName', '(./figures/plot.pdf)')
            page.insert_text((30, 45), 'Overlapping body')
            doc.save(source)
        records = pdf_layout.extract(source)
        self.assertEqual([r['text'] for r in records], ['Overlapping body'])
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            out = Path(f.name)
        self.addCleanup(out.unlink, missing_ok=True)
        pdf_layout.apply(source, {records[0]['id']: 'Updated'}, out)
        with pdf.open(out) as dual:
            right = dual[0].get_text(clip=pdf.Rect(220, 0, 440, 150))
            self.assertIn('Diagram label', right)
            self.assertIn('Updated', right)
            self.assertNotIn('Overlapping body', right)
