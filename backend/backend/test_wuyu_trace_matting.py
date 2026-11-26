"""op203: small traced graphics lose the page-coloured square around them; nothing else changes."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from services.wuyu_trace_matting import clear_edge_background, clear_small_graphic, underlying_colours
from services.wuyu_trace_export import crop_trace_images

PAGE = (248, 250, 253)
BAR = (8, 98, 198)


def icon_on(background, size=40, colour=(30, 91, 216), ring=False):
    image = Image.new('RGB', (size, size), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, size - 7, size - 7), fill=colour)
    if ring:
        draw.ellipse((14, 14, size - 15, size - 15), fill=background)   # enclosed hole of background colour
    return image


class EdgeBackgroundTests(unittest.TestCase):
    def test_surroundings_clear_and_the_graphic_stays(self):
        out, info = clear_edge_background(icon_on(PAGE))
        self.assertEqual(out.mode, 'RGBA')
        self.assertEqual(out.getpixel((0, 0))[3], 0)
        self.assertEqual(out.getpixel((20, 20)), (30, 91, 216, 255))
        self.assertEqual(info['background'], '#F8FAFD')

    def test_enclosed_background_colour_is_kept(self):
        out, _ = clear_edge_background(icon_on(PAGE, ring=True))
        self.assertEqual(out.getpixel((20, 20))[3], 255)    # the hole inside the ring is part of the graphic

    def test_pixels_are_never_moved_or_recoloured_where_opaque(self):
        source = icon_on(PAGE)
        out, _ = clear_edge_background(source)
        self.assertEqual(out.size, source.size)
        for xy in ((20, 20), (12, 20), (20, 28)):
            self.assertEqual(out.getpixel(xy)[:3], source.getpixel(xy))

    def test_photo_like_border_is_left_alone(self):
        image = Image.new('RGB', (40, 40))
        image.putdata([((x * 37 + y * 11) % 256, (x * 7) % 256, (y * 13) % 256) for y in range(40) for x in range(40)])
        self.assertEqual(clear_edge_background(image), (None, 'border_not_uniform'))

    def test_plain_or_full_crops_are_left_alone(self):
        self.assertEqual(clear_edge_background(Image.new('RGB', (30, 30), PAGE))[1], 'would_clear_everything')
        self.assertEqual(clear_edge_background(Image.new('RGB', (2, 2), PAGE))[1], 'too_small')

    def test_pale_design_element_is_not_eaten(self):
        # A light grey pill (like the "..." bubble) on white differs from the page by more than the tolerance.
        image = Image.new('RGB', (78, 32), (253, 253, 253))
        ImageDraw.Draw(image).rounded_rectangle((0, 0, 77, 31), radius=14, fill=(236, 238, 242))
        out, info = clear_edge_background(image)
        self.assertIsNone(out)


class TraceIntegrationTests(unittest.TestCase):
    def trace(self):
        return {'background': {'color': '#F8FAFD'}, 'elements': [
            {'type': 'shape', 'shape': 'round_rect', 'bounds': [0, 0, 400, 40], 'fill': '#0862C6'},     # title bar
            {'type': 'image', 'bounds': [10, 5, 30, 30]},                                                 # 1 icon on the bar
            {'type': 'image', 'bounds': [100, 100, 40, 40]},                                              # 2 icon on the page
            {'type': 'image', 'bounds': [200, 100, 40, 40]},                                              # 3 icon on its own orange tile
            {'type': 'shape', 'shape': 'rect', 'bounds': [300, 100, 60, 60],
             'fill': {'type': 'linear_gradient', 'angle': 0, 'stops': [{'offset': 0, 'color': '#2E85EB'}, {'offset': 1, 'color': '#086CDF'}]}},
            {'type': 'image', 'bounds': [310, 110, 40, 40]},                                              # 5 icon on a gradient button
        ]}

    def page(self):
        image = Image.new('RGB', (800, 450), PAGE)
        image.paste(Image.new('RGB', (400, 40), BAR), (0, 0))
        image.paste(icon_on(BAR, 30, (255, 255, 255)), (10, 5))
        image.paste(icon_on(PAGE), (100, 100))
        image.paste(icon_on((240, 120, 40), colour=(255, 255, 255)), (200, 100))
        image.paste(icon_on((42, 131, 233), colour=(255, 255, 255)), (310, 110))
        return image

    def run_crop(self, env=None):
        with tempfile.TemporaryDirectory() as root:
            page = Path(root) / 'page.png'
            self.page().save(page)
            with patch.dict(os.environ, env or {}):
                trace = crop_trace_images(self.trace(), page, Path(root) / 'out', [], [root])
            images = {i: Image.open(e['image_path']).copy() for i, e in enumerate(trace['elements']) if e['type'] == 'image'}
            return trace, images

    def test_cleared_where_something_shows_through(self):
        trace, images = self.run_crop()
        for index in (1, 2, 5):
            self.assertEqual(images[index].getpixel((0, 0))[3], 0, index)
            self.assertIn('background_cleared', trace['elements'][index])
        self.assertEqual(images[3].mode, 'RGB')                       # orange tile is part of the icon
        self.assertNotIn('background_cleared', trace['elements'][3])
        self.assertEqual(trace['elements'][2]['asset_origin'], 'exact_raster_crop; no invented_vector')
        self.assertFalse(trace['elements'][2]['asset_quality']['pixels_resampled'])

    def test_switch_off_keeps_todays_crops(self):
        trace, images = self.run_crop({'WUYU_TRACE_CLEAR_SMALL_BACKGROUND': '0'})
        self.assertTrue(all(image.mode == 'RGB' for image in images.values()))
        self.assertFalse(any('background_cleared' in e for e in trace['elements']))

    def test_large_crops_are_not_touched(self):
        trace = {'background': {'color': '#F8FAFD'}, 'elements': [{'type': 'image', 'bounds': [0, 0, 400, 300]}]}
        big = icon_on(PAGE, 300)
        self.assertEqual(clear_small_graphic(trace, 0, big, (800, 450))[1], 'not_small')

    def test_underlying_colours(self):
        colours = underlying_colours(self.trace(), 5)
        self.assertIn((248, 250, 253), colours)
        self.assertIn((46, 133, 235), colours)


if __name__ == '__main__':
    unittest.main()
