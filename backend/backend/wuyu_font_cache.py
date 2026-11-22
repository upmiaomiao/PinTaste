"""Bounded, file-backed font faces shared by export text measurement.

Loading a large CJK face through BytesIO makes a private copy of the entire
font file for every cached pixel size. Keep only a few file-backed faces.
"""
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont


MAX_FONT_FACES = 16


@lru_cache(maxsize=MAX_FONT_FACES)
def load_font(path, index, size, modified_ns, length):
    # Timestamp/length identify a changed font. Active callers keep their face
    # alive during measurement even when the cache evicts its reference.
    return ImageFont.truetype(path, size=size, index=index)


def measurement_font(path, size, index=0):
    path = Path(path).resolve()
    stat = path.stat()
    return load_font(str(path), index, size, stat.st_mtime_ns, stat.st_size)
