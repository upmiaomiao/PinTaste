"""Honest image quality at actual slide size; never manufacture source pixels."""
import io
import math
from pathlib import Path

from PIL import Image

MINIMUM_PPI = 220
RECOMMENDED_PPI = 300
EMU_PER_INCH = 914400


def raster_quality(pixel_size, bounds, *, coordinate_dpi=96, crop_fraction=None):
    """DPI tags do not change detail: use retained pixels / displayed inches.

    TraceSpec's 96 dpi describes coordinates, not a promise of HD. A 960 pixel
    crop displayed at 960 trace pixels spans 10 inches and therefore has 96 ppi,
    even when a PNG metadata tag claims 300 dpi.
    """
    width, height = pixel_size
    displayed = [float(bounds[2]) / coordinate_dpi, float(bounds[3]) / coordinate_dpi]
    fractions = crop_fraction or (1, 1)
    retained = [max(0, width * fractions[0]), max(0, height * fractions[1])]
    ppi = min(retained[0] / displayed[0], retained[1] / displayed[1])
    status = 'recommended' if ppi >= RECOMMENDED_PPI else 'sufficient' if ppi >= MINIMUM_PPI else 'below_minimum'
    return {'kind': 'raster', 'status': status, 'pixel_dimensions': [width, height],
            'retained_pixel_dimensions': [round(value, 2) for value in retained],
            'displayed_inches': [round(value, 4) for value in displayed],
            'effective_ppi': round(ppi, 2), 'minimum_ppi': MINIMUM_PPI,
            'recommended_ppi': RECOMMENDED_PPI,
            'minimum_pixel_dimensions': [math.ceil(value * MINIMUM_PPI) for value in displayed],
            'recommended_pixel_dimensions': [math.ceil(value * RECOMMENDED_PPI) for value in displayed],
            'detail_reconstructed': False, 'upsampling_is_not_quality_improvement': True}


def image_file_quality(path, bounds, *, coordinate_dpi=96, crop_pixels=None):
    with Image.open(path) as image:
        fraction = ((crop_pixels[2] / image.width, crop_pixels[3] / image.height)
                    if crop_pixels else None)
        return raster_quality(image.size, bounds, coordinate_dpi=coordinate_dpi, crop_fraction=fraction)


def vector_quality():
    return {'kind': 'vector', 'status': 'vector_original', 'minimum_ppi': MINIMUM_PPI,
            'recommended_ppi': RECOMMENDED_PPI, 'effective_ppi': 'not_applicable',
            'original_vector_preserved': True, 'detail_reconstructed': False}


def quality_summary(records):
    low = [record for record in records if record.get('quality', {}).get('status') == 'below_minimum']
    return {'policy': 'retain_original_pixels_and_vectors_no_ai_redraw_or_upsampling',
            'minimum_ppi': MINIMUM_PPI, 'recommended_ppi': RECOMMENDED_PPI,
            'image_count': len(records), 'below_minimum_count': len(low), 'records': records,
            'warnings': [{'code': 'source_image_resolution_below_target',
                          **{key: record[key] for key in ('page', 'element_index') if key in record},
                          'message': ('此图片在当前PPT显示尺寸下约为'
                                      f"{record['quality']['effective_ppi']} ppi，低于220 ppi；"
                                      '已保留现有最高分辨率，建议提供更清晰的原图或缩小显示尺寸。放大像素不会增加真实细节。')}
                         for record in low], 'blocks_export': False}


def protect_presentation_media(presentation):
    """Ask Office to retain originals; python-pptx already embeds source blobs."""
    presentation._element.set('autoCompressPictures', '0')
    for slide in presentation.slides:
        for picture in slide.shapes:
            if picture.shape_type == 13:
                picture._element.blipFill.blip.set('cstate', 'none')


def protect_pptx_media(path):
    """Use after legacy image-only export, before QA and final artifact hashing."""
    from pptx import Presentation
    target = Path(path)
    presentation = Presentation(target)
    protect_presentation_media(presentation)
    temporary = target.with_name(target.name + '.media-quality.tmp')
    try:
        presentation.save(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_presentation_media(presentation):
    records = []
    svg_namespace = '{http://schemas.microsoft.com/office/drawing/2016/SVG/main}svgBlip'
    relationship_namespace = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'
    for page_number, slide in enumerate(presentation.slides, 1):
        for element_index, picture in enumerate(slide.shapes):
            if picture.shape_type != 13:
                continue
            vector_nodes = list(picture._element.iter(svg_namespace))
            vector_preserved = any(slide.part.related_part(node.get(relationship_namespace)).content_type == 'image/svg+xml'
                                   for node in vector_nodes if node.get(relationship_namespace))
            if vector_preserved:
                quality = vector_quality()
            else:
                with Image.open(io.BytesIO(picture.image.blob)) as image:
                    quality = raster_quality(image.size, [0, 0, picture.width, picture.height],
                        coordinate_dpi=EMU_PER_INCH,
                        crop_fraction=(max(0, 1 - picture.crop_left - picture.crop_right),
                                       max(0, 1 - picture.crop_top - picture.crop_bottom)))
            records.append({'page': page_number, 'element_index': element_index, 'quality': quality})
    return {**quality_summary(records),
            'office_image_compression_disabled': presentation._element.get('autoCompressPictures') in ('0', 'false')}
