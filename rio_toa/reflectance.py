import json
import numpy as np
import rasterio as rio
import collections
from rasterio.coords import BoundingBox
import riomucho
from rasterio import warp

from rio_toa import toa_utils
from rio_toa import sun_utils


def reflectance(img, MR, AR, E, src_nodata=0):
    """Calculate surface reflectance of Landsat 8
    as outlined here: http://landsat.usgs.gov/Landsat8_Using_Product.php

    R_raw = MR * Q + AR

    R =    R_raw / cos(Z) =   R_raw / sin(E)

    Z = 90 - np.radians(E)


    where:

        R_raw = TOA planetary reflectance, without correction for solar angle.
                Note that P does not contain a correction for the sun angle.
        R = TOA reflectance with a correction for the sun angle.
        MR = Band-specific multiplicative rescaling factor from the metadata
            (REFLECTANCE_MULT_BAND_x, where x is the band number)
        AR = Band-specific additive rescaling factor from the metadata
            (REFLECTANCE_ADD_BAND_x, where x is the band number)
        Q = Quantized and calibrated standard product pixel values (DN)
        E = Local sun elevation angle. The scene center sun elevation angle
            in degrees is provided in the metadata (SUN_ELEVATION).
        Z = Local solar zenith angle

    Parameters
    -----------
    img: ndarray
        array of input pixels

    MR: float
        multiplicative rescaling factor from scene metadata
    AR: float
        additive rescaling factor from scene metadata
    E: float
        local sun elevation angle in degrees

    Returns
    --------
    ndarray:
        float32 ndarray with shape == input shape

    """
    rf = ((MR * img[:, :].astype(np.float32)) + AR) / np.sin(np.deg2rad(E))
    rf[img == src_nodata] = 0.0

    return rf


def _reflectance_worker(open_files, window, ij, g_args):
    """rio mucho worker for reflectance
    TODO
    ----
    integrate rescaling functionality for
    different output datatypes
    """
    data = np.swapaxes(riomucho.utils.array_stack([src.read(window=window).astype(np.float32)
                                    for src in open_files]), 0, -1)

    rows, cols, depth = data.shape

    if g_args['pixel_sunangle']:
        bbox = BoundingBox(
                    *warp.transform_bounds(
                        g_args['src_crs'],
                        {'init': u'epsg:4326'},
                        *open_files[0].window_bounds(window)))

        E = sun_utils.sun_elevation(
                        bbox,
                        (rows, cols),
                        g_args['date_collected'],
                        g_args['time_collected_utc'])

    else:
        E = [g_args['E'] for i in range(depth)]

    output = toa_utils.rescale(reflectance(
                    data,
                    g_args['M'],
                    g_args['A'],
                    E,
                    g_args['src_nodata']).reshape(depth, rows, cols),
                g_args['rescale_factor'], g_args['dst_dtype'])

    return output


def calculate_landsat_reflectance(src_paths, src_mtl, dst_path, rescale_factor,
                                  creation_options, bands, dst_dtype,
                                  processes, pixel_sunangle):
    """
    Parameters
    ------------
    src_path: string
    src_mtl: string

    Returns
    ---------
    out: None
        Output is written to dst_path
    """
    mtl = toa_utils._load_mtl(src_mtl)

    M = [mtl['L1_METADATA_FILE']['RADIOMETRIC_RESCALING']['REFLECTANCE_MULT_BAND_{}'.format(b)]
            for b in bands]
    A = [mtl['L1_METADATA_FILE']['RADIOMETRIC_RESCALING']['REFLECTANCE_ADD_BAND_{}'.format(b)]
            for b in bands]

    E = mtl['L1_METADATA_FILE']['IMAGE_ATTRIBUTES']['SUN_ELEVATION']
    date_collected = mtl['L1_METADATA_FILE']['PRODUCT_METADATA']['DATE_ACQUIRED']
    time_collected_utc = mtl['L1_METADATA_FILE']['PRODUCT_METADATA']['SCENE_CENTER_TIME']

    dst_dtype = np.__dict__[dst_dtype]

    for src_path in src_paths:
        with rio.open(src_path) as src:
            dst_profile = src.profile.copy()
            src_nodata = src.nodata

            for co in creation_options:
                dst_profile[co] = creation_options[co]

            dst_profile['dtype'] = dst_dtype

    global_args = {
        'A': A,
        'M': M,
        'E': E,
        'src_nodata': 0,
        'src_crs': dst_profile['crs'],
        'dst_dtype': dst_dtype,
        'rescale_factor': rescale_factor,
        'pixel_sunangle': pixel_sunangle,
        'date_collected': date_collected,
        'time_collected_utc': time_collected_utc,
        'bands': len(bands)
    }

    dst_profile.update(count=len(bands))
    dst_profile.update(photometric='rgb')

    with riomucho.RioMucho(list(src_paths),
                           dst_path,
                           _reflectance_worker,
                           options=dst_profile,
                           global_args=global_args,
                           mode='manual_read') as rm:

        rm.run(processes)
