"""Known missing-tile and provider-notice checks, without model inference.

The notice check matches measured repeated texture signatures from a rejected
provider response. It does not classify arbitrary QR codes as invalid imagery.
Passing these checks is not semantic proof of cloud-free or usable imagery.
"""
from functools import lru_cache
import io
import json
from pathlib import Path

from PIL import Image, ImageChops

from .protocol import detect_ovital_placeholder


@lru_cache(maxsize=1)
def _notice_patterns() -> list:
    path=Path(__file__).with_name("resources")/"provider-notice-patterns.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["patterns"]
    except (OSError,ValueError,KeyError):
        return []


def detect_provider_notice_tile(payload: bytes) -> dict:
    result={"algorithm":"known_repeated_provider_notice_v1","detected":False,"matching_regions":0,
            "basis":"known rejected provider texture; not a generic QR-code detector"}
    with Image.open(io.BytesIO(payload)) as opened:
        image=opened.convert("RGB")
    width,height=image.size
    if min(width,height)<96:
        return result
    red,green,blue=image.split()
    rg=ImageChops.difference(red,green).histogram();gb=ImageChops.difference(green,blue).histogram()
    if sum(rg[:2])<width*height*.98 or sum(gb[:2])<width*height*.98:
        return result
    gray=image.convert("L");hist=gray.histogram()
    # This measured raster includes antialiased text/QR edges; roughly 65% of
    # pixels are near black/white. Exact known texture matches remain mandatory.
    if sum(hist[:33])+sum(hist[223:])<width*height*.60:
        return result
    binary=gray.point([0 if value<128 else 255 for value in range(256)]).tobytes()
    lines=[binary[y*width:(y+1)*width] for y in range(height)]
    positions=set()
    for pattern in _notice_patterns():
        rows=[bytes.fromhex(value) for value in pattern["rows"]]
        pw,ph=pattern["width"],pattern["height"]
        anchor=pattern["anchor_row"];needle=rows[anchor]
        for y,line in enumerate(lines):
            top=y-anchor
            if not 0<=top<=height-ph:
                continue
            left=line.find(needle)
            while left>=0:
                if left+pw<=width and all(lines[top+dy][left:left+pw]==expected for dy,expected in enumerate(rows)):
                    positions.add((left,top))
                    xs={round(x/max(1,pw)) for x,_ in positions};ys={round(y/max(1,ph)) for _,y in positions}
                    if len(positions)>=3 and len(xs)>=2 and len(ys)>=2:
                        result.update(detected=True,matching_regions=len(positions),matched_signature=pattern["id"])
                        return result
                left=line.find(needle,left+1)
    result["matching_regions"]=len(positions)
    return result


def validate_optical_preview(payload: bytes) -> dict:
    placeholder=detect_ovital_placeholder(payload)
    notice=detect_provider_notice_tile(payload) if not placeholder["detected"] else {"detected":False,"skipped":"grid_already_detected"}
    reason="omap_placeholder_grid" if placeholder["detected"] else "provider_notice_tile" if notice["detected"] else None
    return {"valid":reason is None,"reason":reason,"placeholder_check":placeholder,"provider_notice_check":notice,
            "semantic_content_verified":False,"boundary":"Passing known error-tile checks is not proof of cloud-free ROI coverage or correct georegistration."}
