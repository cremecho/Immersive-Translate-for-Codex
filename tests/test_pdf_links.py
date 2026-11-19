import unittest

import pymupdf as pdf

from immersive.pdf_links import collect_links, copy_links


class BilingualLinksTest(unittest.TestCase):
    def test_moved_emails_and_section_target_on_different_width_page(self):
        with pdf.open() as source, pdf.open() as dual:
            p = source.new_page(width=260, height=160)
            p.insert_text((15, 28), "Contact a@x.org and b@x.org.", fontsize=10)
            p.insert_text((15, 65), "See Section 2.", fontsize=10)
            source.new_page(width=190, height=120)
            p = source[0]
            for email in ("a@x.org", "b@x.org"):
                p.insert_link({"kind": pdf.LINK_URI, "from": p.search_for(email)[0], "uri": "mailto:"+email})
            p.insert_link({"kind": pdf.LINK_GOTO, "from": p.search_for("Section 2")[0],
                           "page": 1, "to": pdf.Point(18, 45)})
            source.reload_page(p)
            links = collect_links(source)
            p = dual.new_page(width=520, height=160)
            p.show_pdf_page(pdf.Rect(0, 0, 260, 160), source, 0)
            p.insert_text((350, 93), "a@x.org", fontsize=10)
            p.insert_text((350, 104), "b@x.org", fontsize=10)
            p.insert_text((380, 128), "第2节", fontsize=12, fontname="china-s")
            dual.new_page(width=380, height=120)
            self.assertEqual(copy_links(dual, links, [260, 190]), [])
            with pdf.open(stream=dual.tobytes()) as out:
                p = out[0]
                left = [x for x in p.get_links() if x["from"].x0 < 260]
                right = [x for x in p.get_links() if x["from"].x0 >= 260]
                self.assertEqual(len(left), 3)
                self.assertEqual(len(right), 3)
                for x, email in zip(right[:2], ("a@x.org", "b@x.org")):
                    self.assertEqual(x["uri"], "mailto:"+email)
                    self.assertLess(abs(x["from"].x0 - 350), .01)
                    inside = p.search_for(email, clip=pdf.Rect(260, 0, 520, 160))[0]
                    self.assertTrue(inside.contains(x["from"]))
                self.assertEqual((right[0]["from"] & right[1]["from"]).get_area(), 0)
                self.assertEqual(right[2]["to"], pdf.Point(208, 45))
                self.assertEqual(left[2]["to"], pdf.Point(18, 45))

    def test_named_fitr_and_missing_target(self):
        with pdf.open() as doc:
            doc.new_page(width=220, height=170)
            doc.new_page(width=180, height=130)
            page = doc[0]
            page.insert_text((15, 45), "Figure 9 is described in Appendix Z.")
            # Use real low-level catalog and link entries; insert_link builds
            # named actions, while most publisher files use a named destination.
            doc.xref_set_key(doc.pdf_catalog(), "Dests",
                             f"<< /known [{doc[1].xref} 0 R /FitR 12 30 90 101] >>")
            refs = []
            for label, name in (("Figure 9", "known"), ("Appendix Z", "absent")):
                r = page.search_for(label)[0] * ~page.transformation_matrix
                x = doc.get_new_xref()
                doc.update_object(x, f"<< /Type/Annot /Subtype/Link /Rect[{r.x0} {r.y0} {r.x1} {r.y1}] /Dest/{name} >>")
                refs.append(f"{x} 0 R")
            doc.xref_set_key(page.xref, "Annots", "["+" ".join(refs)+"]")
            with pdf.open(stream=doc.tobytes()) as src:
                known, absent = collect_links(src)[0]
                self.assertEqual(known["kind"], pdf.LINK_GOTO)
                self.assertEqual(known["page"], 1)
                self.assertEqual(known["to"], pdf.Point(12, 29))
                self.assertEqual(absent["kind"], pdf.LINK_NAMED)
                self.assertEqual(absent["nameddest"], "absent")
