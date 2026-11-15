"""Describe delivered grids without claiming cross-sensor registration."""
from __future__ import annotations

from rasterio.transform import array_bounds
from rasterio.warp import transform_bounds


def scientific_spatial_metadata(crs, transform, width: int, height: int, *, kind: str,
                                resampling: str) -> dict:
    bounds = list(array_bounds(height, width, transform))
    return {
        'input_crs': 'EPSG:4326', 'axis_order': 'longitude_latitude',
        'raster_crs': str(crs), 'raster_bounds': bounds,
        'actual_bbox_wgs84': list(transform_bounds(crs, 'EPSG:4326', *bounds, densify_pts=21)),
        'affine_gdal': list(transform.to_gdal()),
        'affine_order': 'existing transform field uses a,b,c,d,e,f; affine_gdal uses c,a,b,f,d,e',
        'coordinate_contract': {
            'input_crs': 'EPSG:4326', 'raster_crs': str(crs),
            'gcj02_shift_applied': False, 'fixed_provider_pixel_shift_applied': False,
            'georeferencing_basis': 'source_product_CRS_and_affine',
            'within_output_band_grid_consistent': True,
            'resampling': resampling,
            'common_grid_with_other_products': False,
        },
        'spatial_registration': {
            'status': 'unknown', 'strict_same_point_eligible': False,
            'reason': 'no_cross_sensor_control_point_audit',
            'absolute_ground_accuracy_verified': False,
            'cross_sensor_registration_verified': False,
            'scope': 'this_science_asset_only',
            'caveats': (['SAR side-looking geometry, layover, shadow and DEM/orbit errors can affect spatial interpretation; RTC does not prove alignment to optical imagery.']
                        if kind == 'sar' else
                        ['Orthorectified product metadata does not prove subpixel alignment to another sensor or date.']),
        },
    }
