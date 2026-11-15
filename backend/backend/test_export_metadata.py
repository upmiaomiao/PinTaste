"""Real image-PPTX export regression; temporary self-made files only.

Run with --expect-baseline on the unchanged image to prove its metadata warning,
or without that flag on the candidate. No app, DB, model or storage integration.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import logging
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image, UnidentifiedImageError
from pptx import Presentation
from pptx.util import Inches
from services import export_service as service


BASELINE = '--expect-baseline' in sys.argv
sys.argv = [arg for arg in sys.argv if arg != '--expect-baseline']
CP = 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties'
DC = 'http://purl.org/dc/elements/1.1/'


@contextmanager
def warning_records():
    records = []
    class Recorder(logging.Handler):
        def emit(self, record):
            records.append(record)
    handler = Recorder(logging.WARNING)
    logger = service.logger
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


class ExportMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wuyu-export-metadata109-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = []
        for index, color in enumerate([(20, 70, 180), (70, 170, 80), (220, 80, 30)]):
            path = self.root / ('self-made-' + str(index) + '.png')
            Image.new('RGB', (320, 180), color).save(path)
            self.images.append(str(path))
        self.before = datetime.now(timezone.utc) - timedelta(seconds=2)

    def check_metadata_warning(self, records):
        metadata = [r.getMessage() for r in records if r.getMessage().startswith('Failed to set core properties:')]
        self.assertEqual(len(metadata), 1 if BASELINE else 0)
        if BASELINE:
            self.assertIn('NoneType', metadata[0])

    def check_metadata(self, raw):
        self.assertIsInstance(raw, bytes)
        prs = Presentation(BytesIO(raw))
        core = prs.core_properties
        self.assertEqual(core.author, 'banana-slides')
        self.assertEqual(core.last_modified_by, 'banana-slides')
        self.assertEqual(core.title, Presentation().core_properties.title)
        self.assertIsNone(core.last_printed)
        for value in [core.created, core.modified]:
            self.assertIsInstance(value, datetime)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            self.assertGreaterEqual(value, self.before)
            self.assertLessEqual(value, datetime.now(timezone.utc) + timedelta(seconds=2))
        self.assertEqual(core.created, core.modified)
        with zipfile.ZipFile(BytesIO(raw)) as package:
            self.assertIsNone(package.testzip())
            xml = ET.fromstring(package.read('docProps/core.xml'))
            self.assertIsNone(xml.find('{' + CP + '}lastPrinted'))
            self.assertEqual(xml.find('{' + DC + '}creator').text, 'banana-slides')
        return prs

    def test_bytes_export_has_real_dates_and_unset_printed_without_losing_title(self):
        with warning_records() as logs:
            raw = service.ExportService.create_pptx_from_images(self.images[:1])
        self.check_metadata_warning(logs)
        prs = self.check_metadata(raw)
        self.assertEqual(len(prs.slides), 1)

    def test_three_pages_keep_image_bytes_order_and_full_slide_placement(self):
        with warning_records() as logs:
            raw = service.ExportService.create_pptx_from_images(self.images)
        self.check_metadata_warning(logs)
        prs = self.check_metadata(raw)
        self.assertEqual(len(prs.slides), 3)
        self.assertEqual(prs.slide_width, Inches(10))
        self.assertEqual(prs.slide_height, Inches(5.625))
        for slide, path in zip(prs.slides, self.images):
            self.assertEqual(len(slide.shapes), 1)
            picture = slide.shapes[0]
            self.assertEqual(picture.image.blob, Path(path).read_bytes())
            self.assertEqual((picture.left, picture.top), (0, 0))
            self.assertEqual((picture.width, picture.height), (prs.slide_width, prs.slide_height))

    def test_file_export_return_contract_and_portrait_geometry(self):
        output = self.root / 'self-made-output.pptx'
        with warning_records() as logs:
            returned = service.ExportService.create_pptx_from_images(self.images[:1], str(output), '9:16')
        self.check_metadata_warning(logs)
        self.assertIsNone(returned)
        prs = self.check_metadata(output.read_bytes())
        self.assertEqual((prs.slide_width, prs.slide_height), (Inches(5.625), Inches(10)))
        self.assertEqual(len(prs.slides), 1)

    def test_missing_image_warning_is_preserved_and_valid_image_still_exports(self):
        missing = str(self.root / 'self-made-missing.png')
        with warning_records() as logs:
            raw = service.ExportService.create_pptx_from_images([missing, self.images[0]])
        self.check_metadata_warning(logs)
        self.assertTrue(any(r.getMessage().startswith('Image not found:') for r in logs))
        self.assertEqual(len(self.check_metadata(raw).slides), 1)

    def test_corrupt_image_error_still_propagates(self):
        corrupt = self.root / 'self-made-invalid.png'
        corrupt.write_bytes(b'this-is-not-an-image')
        with warning_records() as logs:
            with self.assertRaises(UnidentifiedImageError):
                service.ExportService.create_pptx_from_images([str(corrupt)])
        self.check_metadata_warning(logs)

    def test_output_write_error_still_propagates(self):
        with warning_records() as logs:
            with self.assertRaises(FileNotFoundError):
                service.ExportService.create_pptx_from_images(self.images[:1], str(self.root / 'absent' / 'output.pptx'))
        self.check_metadata_warning(logs)

    def test_unrelated_metadata_failure_is_still_warned_not_silenced(self):
        factory = service.Presentation
        class FailingCore:
            def __init__(self, core):
                object.__setattr__(self, '_core', core)
            def __getattr__(self, name):
                return getattr(self._core, name)
            def __setattr__(self, name, value):
                if name == 'created':
                    raise ValueError('synthetic metadata setter failure')
                setattr(self._core, name, value)
        class PresentationProxy:
            def __init__(self):
                object.__setattr__(self, '_prs', factory())
            @property
            def core_properties(self):
                return FailingCore(self._prs.core_properties)
            def __getattr__(self, name):
                return getattr(self._prs, name)
            def __setattr__(self, name, value):
                setattr(self._prs, name, value)
        with patch.object(service, 'Presentation', PresentationProxy), warning_records() as logs:
            raw = service.ExportService.create_pptx_from_images(self.images[:1])
        metadata = [r.getMessage() for r in logs if r.getMessage().startswith('Failed to set core properties:')]
        self.assertEqual(len(metadata), 1)
        self.assertIn('synthetic metadata setter failure', metadata[0])
        self.assertEqual(len(Presentation(BytesIO(raw)).slides), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
