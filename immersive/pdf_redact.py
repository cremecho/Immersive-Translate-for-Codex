"""Remove source text in one native redaction pass per translated page."""

import pymupdf as pdf


def redact_units(page, units):
    # PDF QuadPoints describe the individual ink strips. The surrounding /Rect
    # is only their envelope: text in the gaps (notably formulas) must survive.
    # One annotation with multiple quads avoids creating and walking thousands
    # of annotation appearances for normal scientific prose.
    to_pdf = ~page.transformation_matrix
    points = []
    envelope = pdf.Rect()
    for unit in units:
        for line in unit.lines:
            for span in line.spans:
                if not span['text'].strip():
                    continue
                box = pdf.Rect(span['bbox'])
                if line.angle % 180 == 0:
                    y = (box.y0 + box.y1) / 2
                    box.y0, box.y1 = y - .2, y + .2
                else:
                    x = (box.x0 + box.x1) / 2
                    box.x0, box.x1 = x - .2, x + .2
                if box.is_empty:
                    continue
                envelope |= box
                native = box * to_pdf
                points.append(f'{native.x0:.5f} {native.y1:.5f} {native.x1:.5f} {native.y1:.5f} '
                              f'{native.x0:.5f} {native.y0:.5f} {native.x1:.5f} {native.y0:.5f}')
    if points:
        ann = page.add_redact_annot(envelope, fill=False, cross_out=False)
        page.parent.xref_set_key(ann.xref, "QuadPoints", "[" + " ".join(points) + "]")
        page.apply_redactions(images=pdf.PDF_REDACT_IMAGE_NONE, graphics=pdf.PDF_REDACT_LINE_ART_NONE)
