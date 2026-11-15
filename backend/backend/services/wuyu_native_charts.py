"""Evidence-bound native category charts; unsupported encodings stay raster crops.

Only chart families sharing the verified category/series source-row schema are
supported here. Scatter, histogram, box, waterfall, heatmap and geographic
charts need different verified data contracts and are deliberately absent.
"""
import math
import re
from copy import deepcopy
from contextlib import contextmanager
from functools import cached_property

from pptx.enum.chart import XL_CHART_TYPE, XL_MARKER_STYLE
from pptx.dml.color import RGBColor
from pptx.util import Pt
from pptx.chart.data import CategoryChartData
from pptx.chart.xlsx import CategoryWorkbookWriter
from xlsxwriter import Workbook

from .wuyu_errors import PipelineError
from .wuyu_trace_styles import validate_font
from .wuyu_visual_policy import visual_expression_policy


NATIVE_CHART_KINDS = {
    # Existing cached "bar" traces are vertical columns, retained intentionally.
    'bar': XL_CHART_TYPE.COLUMN_CLUSTERED,
    'column': XL_CHART_TYPE.COLUMN_CLUSTERED,
    'horizontal_bar': XL_CHART_TYPE.BAR_CLUSTERED,
    'stacked_column': XL_CHART_TYPE.COLUMN_STACKED,
    'stacked_bar': XL_CHART_TYPE.BAR_STACKED,
    'percent_stacked_column': XL_CHART_TYPE.COLUMN_STACKED_100,
    'percent_stacked_bar': XL_CHART_TYPE.BAR_STACKED_100,
    'line': XL_CHART_TYPE.LINE,
    'line_markers': XL_CHART_TYPE.LINE_MARKERS,
    'area': XL_CHART_TYPE.AREA,
    'stacked_area': XL_CHART_TYPE.AREA_STACKED,
    'percent_stacked_area': XL_CHART_TYPE.AREA_STACKED_100,
    'pie': XL_CHART_TYPE.PIE,
    'doughnut': XL_CHART_TYPE.DOUGHNUT,
}
NATIVE_CHART_TYPES = tuple(NATIVE_CHART_KINDS)
PART_TO_WHOLE = frozenset(('pie', 'doughnut'))
PERCENT_STACKED = frozenset(('percent_stacked_column', 'percent_stacked_bar', 'percent_stacked_area'))
LINE_TYPES = frozenset(('line', 'line_markers'))
_COLOR = re.compile(r'^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')


class _LiteralCategoryWorkbookWriter(CategoryWorkbookWriter):
    """Keep source labels literal rather than Excel formulas or hyperlinks.

    python-pptx's default writer enables XlsxWriter string interpretation. A
    category such as '=1+1' must remain that exact source label in both cache and
    embedded workbook, rather than becoming a formula with cached value zero.
    """
    @contextmanager
    def _open_worksheet(self, xlsx_file):
        workbook = Workbook(xlsx_file, {'in_memory': True, 'strings_to_formulas': False,
                                       'strings_to_urls': False})
        try:
            yield workbook, workbook.add_worksheet()
        finally:
            workbook.close()


class _LiteralCategoryChartData(CategoryChartData):
    @cached_property
    def _workbook_writer(self):
        return _LiteralCategoryWorkbookWriter(self)


def native_category_data(element):
    validate_native_chart(element, require_data=True)
    data = _LiteralCategoryChartData()
    data.categories = element['categories']
    for series in element['series']:
        data.add_series(series['name'], series['values'])
    return data


def preserve_unsupported_chart_crops(trace):
    """Adapt a fresh trace copy to exact approved-image regions, never guessed data.

    Call before validation/crop_trace_images in trace preparation. The existing
    image crop guard still rejects out-of-bounds regions and native-text overlap;
    this adapter never deletes native labels to make a risky crop pass.
    """
    result = deepcopy(trace)
    if not isinstance(result, dict) or not isinstance(result.get('elements'), list):
        return result
    records = []
    for index, element in enumerate(result['elements']):
        if not isinstance(element, dict) or element.get('type') != 'chart':
            continue
        kind = element.get('chart_type')
        if not isinstance(kind, str) or kind in NATIVE_CHART_KINDS:
            continue
        replacement = {'type': 'image', 'bounds': deepcopy(element.get('bounds')),
                       'source_crop': deepcopy(element.get('source_crop', element.get('bounds')))}
        if 'animation_step' in element:
            replacement['animation_step'] = element['animation_step']
        result['elements'][index] = replacement
        records.append({'element_index': index,
                        'original_chart_type': kind if re.fullmatch(r'[A-Za-z0-9_ -]{1,80}', kind) else 'unsupported',
                        'fallback': 'approved_final_slide_lossless_crop', 'data_reconstructed': False})
    if records:
        result['native_chart_fallbacks'] = records
    return result


def chart_colors(element):
    style = element.get('chart_style', {})
    if not isinstance(style, dict):
        raise PipelineError('invalid_trace', '原生图表chart_style须为对象。')
    palette = style.get('colors', visual_expression_policy()['color']['categorical'])
    if not isinstance(palette, list) or not 1 <= len(palette) <= 200:
        raise PipelineError('invalid_trace', '原生图表配色须为1至200个三位或六位十六进制颜色。')
    colors = []
    for value in palette:
        match = _COLOR.fullmatch(value) if isinstance(value, str) else None
        if not match:
            raise PipelineError('invalid_trace', '原生图表颜色须为三位或六位十六进制颜色。')
        value = match.group(1).upper()
        colors.append(''.join(char * 2 for char in value) if len(value) == 3 else value)
    if 'font' in style:
        validate_font(style['font'])
    return colors


def validate_native_chart(element, require_data=False):
    """Validate numeric/schema suitability, not scientific meaning of a total.

    Material-grounded planning must still establish common units and genuinely
    mutually exclusive parts before selecting a composition encoding. A positive
    sum alone cannot prove that unrelated rates are parts of one whole.
    """
    kind = element.get('chart_type')
    if not isinstance(kind, str) or kind not in NATIVE_CHART_KINDS:
        raise PipelineError('unsupported_native_chart', '此图表尚无已核验的原生数据结构，请保留已确认页图中的无损裁切，不要猜造数据。')
    chart_colors(element)
    if not require_data and 'categories' not in element and 'series' not in element:
        return
    categories, series = element.get('categories'), element.get('series')
    if (not isinstance(categories, list) or not 1 <= len(categories) <= 200 or
            not isinstance(series, list) or not 1 <= len(series) <= 20):
        raise PipelineError('invalid_trace', '图表源数据维度无效。')
    if any(isinstance(category, bool) or not isinstance(category, (str, int, float)) or
           len(str(category)) > 500 or isinstance(category, float) and not math.isfinite(category)
           for category in categories):
        raise PipelineError('invalid_trace', '图表类目无效。')
    for item in series:
        if (not isinstance(item, dict) or not isinstance(item.get('name'), str) or
                not 1 <= len(item['name']) <= 500 or not isinstance(item.get('values'), list) or
                len(item['values']) != len(categories)):
            raise PipelineError('invalid_trace', '图表系列长度必须与类目一致。')
        for value in item['values']:
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                      not math.isfinite(value) or abs(value) > 1e15):
                raise PipelineError('invalid_trace', '图表数值必须为有限源数据。')
    if kind in PART_TO_WHOLE:
        if len(series) != 1:
            raise PipelineError('invalid_chart_composition', '饼图或圆环图仅支持一个互斥组成数据系列。')
        values = series[0]['values']
        if any(value is None or value < 0 for value in values) or math.fsum(values) <= 0:
            raise PipelineError('invalid_chart_composition', '饼图或圆环图须为非负源数据且总和大于零，不能把缺失值或负数变成占比。')
    if kind in PERCENT_STACKED:
        if any(value is None or value < 0 for item in series for value in item['values']):
            raise PipelineError('invalid_chart_composition', '百分比堆积图须使用非负、无缺失的组成源数据。')
        if any(math.fsum(item['values'][index] for item in series) <= 0 for index in range(len(categories))):
            raise PipelineError('invalid_chart_composition', '百分比堆积图每个类目的总和须大于零；不能对空总量进行归一化。')


def apply_native_chart_colors(chart, element):
    """Apply explicit source/brand colors to real series and category points."""
    normalize_native_axis_ids(chart)
    colors = chart_colors(element)
    kind = element['chart_type']
    if kind in PERCENT_STACKED:
        # Keep the embedded observations unchanged. Only the native percent
        # axis expresses PowerPoint's validated per-category normalization.
        axis = chart.value_axis
        axis.minimum_scale = 0
        axis.maximum_scale = 1
        axis.tick_labels.number_format = '0%'
        axis.tick_labels.number_format_is_linked = False
    use_category_colors = kind in PART_TO_WHOLE or (len(chart.series) == 1 and kind not in LINE_TYPES and 'area' not in kind)
    for index, series in enumerate(chart.series):
        color = RGBColor.from_string(colors[index % len(colors)])
        series.format.fill.solid()
        series.format.fill.fore_color.rgb = color
        series.format.line.color.rgb = color
        if kind in LINE_TYPES:
            series.format.line.width = Pt(2)
        if kind == 'line_markers':
            series.marker.style = XL_MARKER_STYLE.CIRCLE
            series.marker.size = 6
            series.marker.format.fill.solid()
            series.marker.format.fill.fore_color.rgb = color
            series.marker.format.line.color.rgb = color
        if use_category_colors:
            for point_index, point in enumerate(series.points):
                point_color = RGBColor.from_string(colors[point_index % len(colors)])
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = point_color
                point.format.line.color.rgb = RGBColor(255, 255, 255) if kind in PART_TO_WHOLE else point_color
                if kind in PART_TO_WHOLE:
                    point.format.line.width = Pt(1)


def normalize_native_axis_ids(chart):
    """Remap stock axis IDs into the Office-compatible positive int31 range.

    Definitions and cross/plot references share one collision-free mapping.
    Existing compatible IDs, source values and workbooks remain unchanged;
    undefined references stay undefined instead of fabricating a target axis.
    """
    namespace = '{http://schemas.openxmlformats.org/drawingml/2006/chart}'
    nodes = [node for node in chart._chartSpace.iter()
             if node.tag in (namespace + 'axId', namespace + 'crossAx')]
    values = [int(node.get('val')) for node in nodes if re.fullmatch(r'-?[0-9]+', node.get('val', ''))]
    used = {value for value in values if 0 <= value <= 2 ** 31 - 1}
    mapping, candidate = {}, 1
    for value in values:
        if -(2 ** 31) <= value < 0 or 2 ** 31 <= value <= 2 ** 32 - 1:
            if str(value) not in mapping:
                while candidate in used:
                    candidate += 1
                mapping[str(value)] = str(candidate)
                used.add(candidate)
    for node in nodes:
        value = node.get('val', '')
        if re.fullmatch(r'-?[0-9]+', value) and str(int(value)) in mapping:
            node.set('val', mapping[str(int(value))])
    return mapping
