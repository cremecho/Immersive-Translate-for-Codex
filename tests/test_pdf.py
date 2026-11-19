import hashlib
import os
import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

import pymupdf as pdf

from immersive import pdf_layout


def temporary_pdf(cleanup):
    fd, name = tempfile.mkstemp(suffix='.pdf')
    os.close(fd)
    path = Path(name)
    cleanup.callback(path.unlink, missing_ok=True)
    return path


class PdfLayoutTest(unittest.TestCase):
    def test_math_geometry_and_readability(self):
        with ExitStack() as cleanup:
            source, result = temporary_pdf(cleanup), temporary_pdf(cleanup)
            with pdf.open() as doc:
                p = doc.new_page(width=240, height=130)
                p.insert_text((20, 30), 'Loss function', fontsize=12)
                p.insert_text((45, 59), 'E = mc²', fontname='tiit', fontsize=13)
                # Three adjacent spans sharing a single logical line.
                x = 20
                for text, face in [('Estimate ', 'helv'), ('x = y²', 'tiit'), (' during fitting.', 'helv')]:
                    p.insert_text((x, 85), text, fontname=face, fontsize=11)
                    x += pdf.get_text_length(text, fontname=face, fontsize=11)
                p.insert_text((20, 107), 'The score is x = 2.', fontsize=10)
                doc.save(source)
            records = pdf_layout.extract(source)
            expected = {'Loss function': '损失函数', 'Estimate': '估计', 'during fitting.': '在拟合时。',
                        'The score is x = 2.': '分数为 x = 2。'}
            self.assertCountEqual([r['text'] for r in records], expected)
            translated = {r['id']: expected[r['text']] for r in records}
            warnings = pdf_layout.apply(source, translated, result)
            mixed = next(r for r in records if 'score' in r['text'])
            self.assertTrue(any(mixed['id'] in w and '数学' in w for w in warnings))
            with pdf.open(result) as output, pdf.open(source) as orig:
                for formula in ('E = mc²', 'x = y²'):
                    before = orig[0].search_for(formula)[0]
                    after = output[0].search_for(formula)[-1]
                    self.assertLess(abs(after.x0 - 240 - before.x0), .01)
                    self.assertLess(abs(after.y0 - before.y0), .01)
                    # Compare the actual equation ink inside the same source cell.
                    crop = before + (.7, 0, -.7, 0)
                    self.assertEqual(orig[0].get_pixmap(clip=crop).samples,
                                     output[0].get_pixmap(clip=crop + (240, 0, 240, 0)).samples)
            translated[records[0]['id']] = '无法容纳的超长文本' * 80
            with self.assertRaisesRegex(ValueError, records[0]['id']):
                pdf_layout.apply(source, translated, result)

    def test_spreads_keep_source_pixels_and_right_side_graphics(self):
        with ExitStack() as cleanup:
            source, result = temporary_pdf(cleanup), temporary_pdf(cleanup)
            with pdf.open() as doc:
                p = doc.new_page(width=250, height=190)
                p.draw_rect(pdf.Rect(0, 0, 250, 48), fill=(.85, .91, 1), color=None)
                p.insert_text((16, 31), 'Inspection report', fontsize=16, fontname='hebo')
                p.insert_text((16, 68), 'Temperature', fontsize=10)
                p.insert_text((145, 68), 'Pressure', fontsize=10)
                p.insert_text((16, 92), '32.5', fontsize=10)
                pic = pdf.Pixmap(pdf.csRGB, pdf.IRect(0, 0, 16, 12), 0)
                pic.clear_with(120)
                p.insert_image(pdf.Rect(15, 110, 75, 155), pixmap=pic)
                p.insert_text((100, 140), 'Within range', fontsize=10)
                p.draw_line((100, 145), (170, 145), width=1, color=(.8, .3, .1))
                rotated = doc.new_page(width=230, height=120)
                rotated.insert_text((18, 50), 'Orientation', fontsize=12)
                rotated.set_rotation(90)
                cropped = doc.new_page(width=300, height=250)
                cropped.insert_text((100, 75), 'Visible label', fontsize=12)
                cropped.set_cropbox(pdf.Rect(65, 25, 285, 235))
                scan = doc.new_page(width=130, height=85)
                scan.insert_image(scan.rect, pixmap=pic)
                scan.insert_text((5, 22), 'Hidden OCR', render_mode=3)
                doc.new_page(width=150, height=200)
                doc[0].insert_link({'kind': pdf.LINK_URI, 'from': pdf.Rect(15, 16, 125, 35), 'uri': 'https://example.org'})
                doc[0].insert_link({'kind': pdf.LINK_GOTO, 'from': pdf.Rect(15, 54, 88, 73), 'page': 1, 'to': pdf.Point(18, 50)})
                doc.save(source)
            digest = hashlib.sha256(source.read_bytes()).digest()
            records = pdf_layout.extract(source)
            phrases = {'Inspection report': '检查报告', 'Temperature': '温度', 'Pressure': '压力',
                       'Within range': '范围正常', 'Orientation': '方向', 'Visible label': '可见标签'}
            self.assertCountEqual([r['text'] for r in records], phrases)
            warnings = pdf_layout.apply(source, {r['id']: phrases[r['text']] for r in records}, result)
            self.assertTrue(any('第 4 页' in w for w in warnings))
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).digest())
            with pdf.open(result) as written, pdf.open(source) as orig:
                self.assertEqual(len(written), len(orig))
                for p, old in zip(written, orig):
                    self.assertEqual(p.rect, pdf.Rect(0, 0, 2 * old.rect.width, old.rect.height))
                    self.assertEqual(old.get_pixmap().samples, p.get_pixmap(clip=old.rect).samples)
                right = pdf.Rect(250, 0, 500, 190)
                content = written[0].get_text(clip=right)
                self.assertNotIn('Inspection', content)
                self.assertIn('32.5', content)
                for text in ('检查报告', '温度', '压力', '范围正常'):
                    self.assertIn(text, ''.join(content.split()))
                for area in (pdf.Rect(15, 111, 73, 153), pdf.Rect(100, 143, 171, 147),
                             pdf.Rect(150, 4, 225, 40)):
                    self.assertEqual(orig[0].get_pixmap(clip=area).samples,
                                     written[0].get_pixmap(clip=area + (250, 0, 250, 0)).samples)
                for i, word in ((1, '方向'), (2, '可见标签')):
                    rect = orig[i].rect
                    translated = written[i].get_text(clip=rect + (rect.width, 0, rect.width, 0))
                    self.assertIn(word, ''.join(translated.split()))
                links = written[0].get_links()
                self.assertEqual(len(links), 4)
                self.assertEqual([link['page'] for link in links if link['kind'] == pdf.LINK_GOTO], [1, 1])
                self.assertEqual({round(link['from'].x0) for link in links}, {15, 265})

    def test_publisher_reference_first_page(self):
        reference = Path(__file__).resolve().parents[1] / 'a9c484c88c154695a102011e66b0a884-translated_dual.pdf'
        if not reference.is_file():
            self.skipTest('The supplied visual reference is not in this checkout.')
        with ExitStack() as cleanup:
            source, result = temporary_pdf(cleanup), temporary_pdf(cleanup)
            with pdf.open(reference) as original:
                p = original[0]
                width, height = p.rect.width / 2, p.rect.height
                # The supplied BabelDOC spread references the source article as
                # its own unmodified PDF Form. Isolate that instead of translating
                # a pre-translated page or rasterizing the original.
                name = next(name for _, name, owner, bounds in p.get_xobjects()
                            if owner == 0 and abs(bounds[0]) < .01 and abs(bounds[2] - width) < .01)
                xref = original.get_new_xref()
                original.update_object(xref, '<<>>')
                original.update_stream(xref, f'q /{name} Do Q'.encode('ascii'))
                p.set_contents(xref)
                p.set_cropbox(pdf.Rect(0, 0, width, height))
                original.select([0])
                original.save(source, garbage=4, deflate=True)
            records = pdf_layout.extract(source)
            title = next(r for r in records if r['text'].startswith('A trustworthy agentic'))
            translated = {r['id']: r['text'] for r in records}
            translated[title['id']] = '面向安全关键工程系统的可信智能体人工智能框架' + ''.join(re.findall(r'⟦f\d+⟧', title['text']))
            self.assertEqual(pdf_layout.apply(source, translated, result), [])
            with pdf.open(source) as original, pdf.open(result) as output:
                p = output[0]
                self.assertEqual(original[0].get_pixmap().samples, p.get_pixmap(clip=original[0].rect).samples)
                right = p.get_text(clip=pdf.Rect(width, 0, width * 2, height))
                self.assertIn('可信智能体人工智能框架', ''.join(right.split()))
                self.assertNotIn('A trustworthy agentic', right)

    def test_legacy_ids_resume_without_shifting_new_paragraphs(self):
        with ExitStack() as cleanup:
            source, result = temporary_pdf(cleanup), temporary_pdf(cleanup)
            with pdf.open() as doc:
                page = doc.new_page(width=280, height=90)
                page.insert_text((10, 24), 'Calibration notes', fontsize=12)
                doc.save(source)
            fresh = pdf_layout.extract(source)
            self.assertEqual(fresh[0]['id'], 'p1-b1')
            with self.assertRaisesRegex(ValueError, 'p1-b1'):
                pdf_layout.apply(source, {'p1-b1': '校准记录'}, result, source_texts={'p1-b1': 'Different section'})
            pdf_layout.apply(source, {'p1-t1': '校准记录'}, result)
            with pdf.open(result) as output:
                self.assertIn('校准记录', ''.join(output[0].get_text().split()))

    def test_literal_formula_token_survives_inline_superscript(self):
        with ExitStack() as cleanup:
            source, result = temporary_pdf(cleanup), temporary_pdf(cleanup)
            with pdf.open() as doc:
                page = doc.new_page(width=350, height=80)
                page.insert_htmlbox(pdf.Rect(10, 10, 320, 70),
                                    '<div>Read the literal ⟦f1⟧ marker and calculate x<sup>2</sup>.<br>'
                                    'Repeat the measurement and record the calibration result.</div>',
                                    css='*{margin:0;font-size:10pt;}sup{font-size:7pt;}')
                doc.save(source)
            unit, = pdf_layout.extract(source)
            value = unit['text'].replace('Read the literal', '查看原标记').replace('Repeat the measurement', '重复测量')
            pdf_layout.apply(source, {unit['id']: value}, result)
            with pdf.open(result) as doc:
                right = ''.join(doc[0].get_text(clip=pdf.Rect(350,0,700,80)).split())
                self.assertIn('⟦f1⟧', right)
                self.assertIn('重复测量', right)

    def test_publisher_glossary_and_prose_abutting_typeset_equation(self):
        reference = Path(__file__).resolve().parents[1] / 'a9c484c88c154695a102011e66b0a884-translated_dual.pdf'
        if not reference.exists():
            self.skipTest('Publisher reference unavailable')
        with ExitStack() as cleanup:
            source, result = temporary_pdf(cleanup), temporary_pdf(cleanup)
            with pdf.open(reference) as doc:
                doc.select([1, 4])
                for page in doc:
                    width = page.rect.width / 2
                    name = next(name for _,name,parent,box in page.get_xobjects()
                                if parent == 0 and abs(box[0]) < .01 and abs(box[2] - width) < .01)
                    stream = doc.get_new_xref()
                    doc.update_object(stream, '<<>>')
                    doc.update_stream(stream, f'q /{name} Do Q'.encode())
                    page.set_contents(stream)
                    page.set_cropbox(pdf.Rect(0, 0, width, page.rect.height))
                doc.save(source, garbage=4)
            units = pdf_layout.extract(source)
            signal = next(u for u in units if 'Noise Ratio' in u['text'])
            self.assertIn('Signal-to-Noise Ratio', signal['text'])
            prefix = next(u for u in units if 'the system requires only' in u['text'])
            self.assertTrue(prefix['text'].endswith('dataset'))
            values = {u['id']: u['text'] for u in units}
            values[signal['id']] = '信噪比'
            values[prefix['id']] = '系统只需正常工况数据集'
            pdf_layout.apply(source, values, result)
            with pdf.open(result) as out, pdf.open(source) as orig:
                equation_start = prefix['_rect'][2] + .1
                top = prefix['_rect'][1]
                rect = pdf.Rect(equation_start, top - 1, equation_start + 51, top + 16)
                offset = orig[1].rect.width
                scale = round(offset * 2) / offset  # Align the fractional-width right page to the pixel grid.
                matrix = pdf.Matrix(scale, scale)
                self.assertEqual(orig[1].get_pixmap(clip=rect, matrix=matrix).samples,
                                 out[1].get_pixmap(clip=rect+(offset, 0, offset, 0), matrix=matrix).samples)

    def test_tagged_cells_across_interleaved_content_streams(self):
        with ExitStack() as cleanup:
            source, result = temporary_pdf(cleanup), temporary_pdf(cleanup)
            with pdf.open() as doc:
                page = doc.new_page(width=280, height=100)
                tree, table, row, col1, col2 = [doc.get_new_xref() for _ in range(5)]
                page_ref = page.xref
                for x, y, label, mark in [(16, 32, 'Normal', 0), (122, 32, 'Sensor type', 1),
                                          (16, 44, 'pressure', 2), (122, 44, 'and model', 3)]:
                    page.insert_text((x, y), label, fontsize=10)
                    stream = page.get_contents()[-1]
                    native = doc.xref_stream(stream)
                    doc.update_stream(stream, f'/TD <</MCID {mark}>> BDC\n'.encode() + native + b'\nEMC')
                doc.update_object(tree, f'<</Type/StructTreeRoot/K {table} 0 R>>')
                doc.update_object(table, f'<</Type/StructElem/S/Table/P {tree} 0 R/K[{row} 0 R]>>')
                doc.update_object(row, f'<</Type/StructElem/S/TR/P {table} 0 R/K[{col1} 0 R {col2} 0 R]>>')
                for col, marks in [(col1, '0 2'), (col2, '1 3')]:
                    doc.update_object(col, f'<</Type/StructElem/S/TD/P {row} 0 R/Pg {page_ref} 0 R/K[{marks}]>>')
                doc.xref_set_key(doc.pdf_catalog(), 'StructTreeRoot', f'{tree} 0 R')
                doc.save(source)
            units = pdf_layout.extract(source)
            self.assertEqual([u['text'] for u in units], ['Normal pressure', 'Sensor type and model'])
            pdf_layout.apply(source, {units[0]['id']: '正常压力', units[1]['id']: '传感器类型与型号'}, result)
            with pdf.open(result) as doc:
                self.assertIn('正常压力', ''.join(doc[0].get_text(clip=pdf.Rect(280,0,402,100)).split()))
                self.assertIn('传感器类型与型号', ''.join(doc[0].get_text(clip=pdf.Rect(402,0,560,100)).split()))


if __name__ == '__main__':
    unittest.main()
