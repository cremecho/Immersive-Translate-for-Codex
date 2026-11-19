import shutil
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pymupdf as pdf

from immersive import pdf_layout


class IncrementalPdfTest(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.gettempdir()) / ('pdf-retry-' + uuid.uuid4().hex)
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder)
        self.source, self.out, self.cache = folder / 'source.pdf', folder / 'spread.pdf', folder / 'cache'
        with pdf.open() as doc:
            for text in ('Alpha element', 'Beta element', 'Gamma element'):
                page = doc.new_page(width=260, height=145)
                page.insert_text((28, 50), text, fontsize=11)
            doc[1].set_cropbox(pdf.Rect(15, 8, 240, 130))
            doc[1].set_rotation(90)
            scan = doc.new_page(width=150, height=95)
            pix = pdf.Pixmap(pdf.csRGB, pdf.IRect(0, 0, 12, 16), 0)
            pix.clear_with(85)
            scan.insert_image(scan.rect, pixmap=pix)
            doc[0].insert_link(dict(kind=pdf.LINK_GOTO, page=1, to=pdf.Point(15, 20),
                                    **{'from': pdf.Rect(28, 38, 94, 51)}))
            doc.set_toc([[1, 'Reading order', 1]])
            doc.set_metadata({'title': 'Inspection'})
            doc.save(self.source)
        self.sources = {r['id']: r['text'] for r in pdf_layout.extract(self.source)}
        self.values = dict(zip(self.sources, ['Item one', 'Item two', 'Item three'], strict=True))
        self.stats = {}

    def build(self):
        return pdf_layout.apply(self.source, self.values, self.out, source_texts=self.sources,
                                cache_dir=self.cache, stats=self.stats)

    def test_cache_replays_warnings_and_only_touches_the_changed_page(self):
        warnings = self.build()
        self.assertEqual(self.stats, dict(pages_rendered=3, pages_reused=0, pages_original=1))
        with pdf.open(self.out) as cold:
            first_pixels = [p.get_pixmap().samples for p in cold]
            first_links = cold[0].get_links()
            self.assertEqual([link['page'] for link in first_links], [1, 1])
        with patch.object(pdf_layout, '_fast_units', side_effect=AssertionError('cache miss')):
            self.assertEqual(self.build(), warnings)
        with pdf.open(self.out) as warm:
            self.assertEqual(first_pixels, [p.get_pixmap().samples for p in warm])
            self.assertEqual(warm.get_toc(), [[1, 'Reading order', 1]])
            self.assertEqual(warm.metadata['title'], 'Inspection')
        second = list(self.values)[1]
        self.values[second] = 'Part two'
        with patch.object(pdf_layout, '_typeset', wraps=pdf_layout._typeset) as typeset:
            self.build()
            self.assertEqual([call.args[1].id for call in typeset.call_args_list], [second])
        self.assertEqual(self.stats, dict(pages_rendered=1, pages_reused=2, pages_original=1))
        with pdf.open(self.source) as original, pdf.open(self.out) as changed:
            self.assertEqual(first_pixels[0], changed[0].get_pixmap().samples)
            self.assertEqual(first_pixels[2:], [p.get_pixmap().samples for p in list(changed)[2:]])
            for link, expected in zip(changed[0].get_links(), first_links, strict=True):
                self.assertEqual({k: v for k, v in link.items() if k != 'xref'},
                                 {k: v for k, v in expected.items() if k != 'xref'})
            page = changed[1]
            width = original[1].rect.width
            self.assertEqual(original[1].get_pixmap().samples, page.get_pixmap(clip=original[1].rect).samples)
            self.assertIn('Part two', page.get_text(clip=pdf.Rect(width, 0, 2*width, page.rect.height)))

    def test_failed_middle_page_checkpoints_both_earlier_and_later_pages(self):
        second = list(self.values)[1]
        self.values[second] = 'Overflow ' * 120
        self.out.write_bytes(b'previously published file')
        with self.assertRaisesRegex(pdf_layout.PdfFitError, second):
            self.build()
        self.assertEqual(self.out.read_bytes(), b'previously published file')
        self.values[second] = 'Done'
        with patch.object(pdf_layout, '_typeset', wraps=pdf_layout._typeset) as typeset:
            self.build()
            self.assertEqual([call.args[1].id for call in typeset.call_args_list], [second])
        self.assertEqual(self.stats['pages_reused'], 2)

    def test_corrupt_snapshot_and_changed_source_record_never_reused(self):
        self.build()
        with closing(sqlite3.connect(self.cache/'pages.sqlite3')) as conn, conn:
            conn.execute("UPDATE pages SET snapshot=x'0000' WHERE number=1")
        self.build()
        self.assertEqual(self.stats['pages_rendered'], 1)
        second = list(self.sources)[1]
        self.sources[second] = 'Wrong source'
        with self.assertRaisesRegex(ValueError, second):
            self.build()

    def test_legacy_job_uses_checked_source_and_discards_cache_when_source_changes(self):
        self.sources = {k.replace('-b', '-t'): value for k, value in self.sources.items()}
        self.values = {k.replace('-b', '-t'): value for k, value in self.values.items()}
        self.build()
        self.build()
        self.assertEqual(self.stats['pages_reused'], 3)
        replacement = self.source.with_name('updated.pdf')
        with pdf.open(self.source) as source:
            source[0].draw_rect(pdf.Rect(24, 80, 100, 115), fill=(.4, .5, .6))
            source.save(replacement)
        replacement.replace(self.source)
        self.build()
        self.assertEqual(self.stats['pages_reused'], 0)
        with pdf.open(self.source) as source, pdf.open(self.out) as result:
            ink = pdf.Rect(30, 85, 90, 105)
            self.assertEqual(source[0].get_pixmap(clip=ink).samples,
                             result[0].get_pixmap(clip=ink + (260, 0, 260, 0)).samples)
