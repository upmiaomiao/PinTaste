"""Tell the user, in plain words, what the system could and could not read from an upload.

The failure this prevents: a presentation whose pages are screenshots has almost no
machine-readable text. The system parsed it as "0 text, 31 images", started reading, and
failed with a generic error. The user was never told that the TEXT is inside pictures, that
reading it needs the model to look at every picture, or that it costs points.

This module only DESCRIBES a parsed material. It never reads an image and never calls a model;
reading pictures stays an explicit, user-confirmed step.
"""

# A page counts as "text-poor" when its native text is shorter than this, measured in
# "information units": one CJK character is one unit, a run of Latin/Cyrillic letters is
# one unit per ~4 characters (a word). Fifteen Chinese characters are a full sentence,
# so a flat character count would call ordinary Chinese pages empty.
NATIVE_TEXT_POOR_UNITS = 8
# A deck counts as image-based when at least this share of its pages is text-poor.
IMAGE_BASED_SHARE = 0.6


_CJK = '\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af'   # CJK ideographs, kana, hangul


def _units(text):
    import re
    cjk = len(re.findall('[' + _CJK + ']', text))
    other = len(re.sub('[' + _CJK + r'\s\W_]', '', text))
    return cjk + other // 4


def _page_of(chunk):
    source = chunk.get('source') if isinstance(chunk, dict) else None
    page = source.get('page') if isinstance(source, dict) else None
    return page if isinstance(page, int) and not isinstance(page, bool) else None


def describe_presentation(material):
    """Return a small, public description of one parsed PPT/PPTX material, or None.

    Keys: pages, text_poor_pages, images, image_based (bool), needs_visual_reading (bool).
    """
    name = str(material.get('filename') or '').lower()
    if not name.endswith(('.pptx', '.ppt')):
        return None
    text_by_page = {}
    for chunk in material.get('chunks') or []:
        page = _page_of(chunk)
        if page is not None and not (chunk.get('source') or {}).get('notes'):
            text_by_page[page] = text_by_page.get(page, 0) + _units(str(chunk.get('text') or ''))
    assets = [a for a in material.get('source_assets') or [] if isinstance(a, dict)]
    pages_with_images = {_page_of(a) for a in assets if _page_of(a) is not None}
    pages = sorted(set(text_by_page) | pages_with_images)
    if not pages:
        return None
    poor = [p for p in pages if text_by_page.get(p, 0) < NATIVE_TEXT_POOR_UNITS and p in pages_with_images]
    image_based = len(poor) / len(pages) >= IMAGE_BASED_SHARE
    return {'pages': len(pages), 'text_poor_pages': len(poor), 'images': len(assets),
            'image_based': image_based, 'needs_visual_reading': image_based}


def visual_reading_notice(description):
    """The sentence shown to the user in the conversation. Plain Chinese, no jargon."""
    if not description or not description.get('needs_visual_reading'):
        return None
    return ('这份 PPT 共 %d 页，其中 %d 页的文字在图片里，系统直接读不到。'
            '需要让模型逐页看图提取文字（共 %d 张图片），会按读图用量消耗积分。'
            '原 PPT 里的流程图、结果图会作为原图参考保留进新页面。是否让系统读图？'
            % (description['pages'], description['text_poor_pages'], description['images']))
