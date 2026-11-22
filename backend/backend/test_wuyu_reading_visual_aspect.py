"""Decorative hairlines are not sent to the reader; PPTX figures are read in page order."""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from services.wuyu_asset_extraction import extract_source_assets
from services.wuyu_errors import PipelineError
from services.wuyu_reading_visuals import MAX_VISUAL_ASPECT, extreme_aspect, reader_images


def png(width, height, color=(200, 30, 30)):
    out = io.BytesIO()
    Image.new('RGB', (width, height), color).save(out, 'PNG')
    return out.getvalue()


class ExtremeAspectTests(unittest.TestCase):
    def test_ratio_boundary(self):
        self.assertFalse(extreme_aspect({'width': 640, 'height': 640}))
        self.assertFalse(extreme_aspect({'width': 3200, 'height': 32}))  # exactly 100:1 is kept
        self.assertTrue(extreme_aspect({'width': 3392, 'height': 32}))  # 106:1 is a decorative strip
        self.assertTrue(extreme_aspect({'width': 10352, 'height': 32}))
        self.assertTrue(extreme_aspect({'width': 2, 'height': 647}))
        self.assertEqual(MAX_VISUAL_ASPECT, 100)

    def test_missing_or_bad_size_is_kept(self):
        for asset in ({}, {'width': 0, 'height': 10}, {'width': 'x', 'height': 1}, None):
            self.assertFalse(extreme_aspect(asset))


class ReaderImagesGuardTests(unittest.TestCase):
    def test_last_guard_rejects_strip_instead_of_upscaling(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = png(796, 6)
            path = Path(tmp) / 'a.png'
            path.write_bytes(raw)
            import hashlib
            asset = {'id': 'x', 'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
            with self.assertRaises(PipelineError) as caught:
                reader_images({'visual_inputs': [asset]}, {'WUYU_ALLOWED_MATERIAL_ROOTS': [tmp]})
            self.assertEqual(caught.exception.code, 'source_image_unreadable')

    def test_normal_image_still_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = png(640, 640)
            path = Path(tmp) / 'a.png'
            path.write_bytes(raw)
            import hashlib
            asset = {'id': 'x', 'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
            images = reader_images({'visual_inputs': [asset]}, {'WUYU_ALLOWED_MATERIAL_ROOTS': [tmp]})
            self.assertEqual(len(images), 1)


def deck(path, slides):
    """slides: {page: [(media name, png bytes)]}; media is stored in reverse page order on purpose."""
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/></Types>')
    rels_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    r_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    ids = ''.join(f'<p:sldId id="{255 + p}" r:id="rId{p}"/>' for p in sorted(slides))
    pres = ('<?xml version="1.0" encoding="UTF-8"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            f'xmlns:r="{r_ns}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>')
    pres_rels = ''.join(f'<Relationship Id="rId{p}" Type="{r_ns}/slide" Target="slides/slide{p}.xml"/>' for p in sorted(slides))
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', ct)
        z.writestr('ppt/presentation.xml', pres)
        z.writestr('ppt/_rels/presentation.xml.rels', f'<Relationships xmlns="{rels_ns}">{pres_rels}</Relationships>')
        entries = [(p, n, b) for p in sorted(slides, reverse=True) for n, b in slides[p]]
        for page in sorted(slides):
            pics = ''.join(f'<p:pic><p:blipFill><a:blip r:embed="rImg{i}"/></p:blipFill></p:pic>'
                           for i, (n, _) in enumerate(slides[page]))
            z.writestr(f'ppt/slides/slide{page}.xml',
                       '<?xml version="1.0" encoding="UTF-8"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                       f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="{r_ns}"><p:cSld><p:spTree>'
                       f'{pics}<p:sp><p:txBody><a:p><a:r><a:t>page {page}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
            rel = ''.join(f'<Relationship Id="rImg{i}" Type="{r_ns}/image" Target="../media/{n}"/>'
                          for i, (n, _) in enumerate(slides[page]))
            z.writestr(f'ppt/slides/_rels/slide{page}.xml.rels', f'<Relationships xmlns="{rels_ns}">{rel}</Relationships>')
        for _, n, b in entries:
            z.writestr(f'ppt/media/{n}', b)


class PptxPageOrderTests(unittest.TestCase):
    def test_cap_drops_trailing_pages_not_target_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            colors = iter(range(1, 255))
            slides = {p: [(f'p{p}_{i}.png', png(700 + p, 700 + i, (next(colors), 5, 5))) for i in range(3)]
                      for p in range(1, 8)}
            source = Path(tmp) / 'deck.pptx'
            deck(source, slides)
            config = {'UPLOAD_FOLDER': tmp, 'WUYU_ALLOWED_MATERIAL_ROOTS': [tmp], 'WUYU_MAX_SOURCE_ASSETS': 9}
            assets, warnings = extract_source_assets(source, config, 'a' * 64, 'deck.pptx', lambda: None)
            pages = sorted({a['source'].get('page') for a in assets})
            self.assertEqual(len(assets), 9)
            self.assertEqual(pages, [1, 2, 3])  # earliest pages kept, in page order
            self.assertIn('source_asset_limit_reached', warnings)


if __name__ == '__main__':
    unittest.main()
