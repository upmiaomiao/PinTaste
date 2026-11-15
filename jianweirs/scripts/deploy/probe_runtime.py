"""Check packaged native libraries without depending on a development PATH."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cv2
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
import omap_api.app
import omap_api.gateway
import omap_api.sar
import omap_api.spectral


def main():
    array = np.arange(256, dtype='float32').reshape(16, 16)
    crs = rasterio.crs.CRS.from_epsg(4326)
    with MemoryFile() as memory:
        with memory.open(driver='GTiff', width=16, height=16, count=1, dtype='float32',
                         crs=crs, transform=from_origin(104, 31, .001, .001)) as target:
            target.write(array, 1)
        with memory.open() as source:
            assert source.crs.to_epsg() == 4326 and np.array_equal(source.read(1), array)
    image = np.zeros((16, 16, 3), dtype='uint8')
    assert cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).shape == (16, 16)
    print(json.dumps({'status': 'ok', 'python': sys.version.split()[0], 'application_imports': True,
                      'native_raster_roundtrip': True, 'opencv': True}))


if __name__ == '__main__':
    main()
