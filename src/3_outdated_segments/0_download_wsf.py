#!/usr/bin/env python3
"""Stream and clip the WSF Tracker raster for one prepared city.

Source: World Settlement Footprint Tracker, streamed from the public Zarr
store on Source Cooperative (mindearth/wsf). Pixel value is an int8 "earliest
built-up epoch" index: 0 = never detected as built-up; 1-20 = the biannual
epoch (1 = 2016-07-01, 2 = 2017-01-01, ... +6 months per step) at which that
pixel was first detected as built-up. Confirmed from the dataset's own
attributes at read time.

The clipped raster is reprojected to the city's analysis CRS so pixel counts
translate directly to m^2 in later stages.
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import obstore
import rasterio
import rasterio.mask
import rioxarray  # noqa: F401 -- registers the .rio accessor on xarray objects
import xarray as xr
from rasterio.warp import Resampling, calculate_default_transform, reproject
from zarr.storage import ObjectStore

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root, skip_existing


WSF_BUCKET = "us-west-2.opendata.source.coop"
WSF_PREFIX = "mindearth/wsf/World_WSF_20160701-20260101.zarr"
WSF_VARIABLE = "wsf_tracker"
EPOCH_START = "2016-07-01"  # epoch 1
EPOCH_MONTHS = 6  # per epoch step


def stream_clip_wsf(boundary_wgs84, bbox_wgs84: tuple[float, float, float, float]):
    """Stream the WSF Tracker raster, clipped to boundary_wgs84 (EPSG:4326)."""
    store = obstore.store.S3Store(
        bucket=WSF_BUCKET,
        prefix=WSF_PREFIX,
        config={"aws_skip_signature": "true", "aws_region": "us-west-2"},
    )
    zarr_store = ObjectStore(store, read_only=True)
    ds = xr.open_zarr(zarr_store, group="0", decode_coords="all")
    print("WSF epoch meaning:", ds[WSF_VARIABLE].attrs.get("description"))

    west, south, east, north = bbox_wgs84
    subset = ds[WSF_VARIABLE].sel(x=slice(west, east), y=slice(north, south))  # y descending
    with subset.rio.to_rasterio_dataset() as subset_rio:
        out_image, out_transform = rasterio.mask.mask(
            subset_rio, [boundary_wgs84.buffer(0)], crop=True
        )
        out_meta = subset_rio.meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "height": out_image.shape[1],
            "width": out_image.shape[2],
            "transform": out_transform,
        })
    return out_image, out_meta


def reproject_to_crs(image, meta: dict, dst_crs: str):
    dst_transform, width, height = calculate_default_transform(
        meta["crs"], dst_crs, meta["width"], meta["height"], *rasterio.transform.array_bounds(
            meta["height"], meta["width"], meta["transform"]
        )
    )
    dst_meta = meta.copy()
    dst_meta.update({"crs": dst_crs, "transform": dst_transform, "width": width, "height": height})
    import numpy as np
    dst_image = np.zeros((meta["count"], height, width), dtype=image.dtype)
    reproject(
        source=image,
        destination=dst_image,
        src_transform=meta["transform"],
        src_crs=meta["crs"],
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        resampling=Resampling.nearest,  # categorical epoch index -- never interpolate
    )
    return dst_image, dst_meta


def run(city, args) -> None:
    output_tif = city.sources_dir / "wsf" / f"{city.slug}_wsf.tif"
    if skip_existing(output_tif, args.force):
        return
    output_tif.parent.mkdir(parents=True, exist_ok=True)

    aoi = gpd.read_parquet(city.inputs_dir / "aoi.parquet")
    aoi_wgs84 = aoi.to_crs(4326)
    boundary_wgs84 = aoi_wgs84.geometry.union_all()
    bbox_wgs84 = tuple(aoi_wgs84.total_bounds)

    print(f"Streaming WSF Tracker for bbox {bbox_wgs84}...")
    image, meta = stream_clip_wsf(boundary_wgs84, bbox_wgs84)
    print(f"Clipped raster: {image.shape}, CRS {meta['crs']}")

    analysis_crs = city.metadata["analysis_crs"]
    image, meta = reproject_to_crs(image, meta, analysis_crs)
    print(f"Reprojected to {analysis_crs}: {image.shape}")

    with rasterio.open(output_tif, "w", **meta) as dest:
        dest.write(image)
    print(f"Saved: {output_tif}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument("--force", action="store_true")
    add_data_root_arg(parser)
    args = parser.parse_args()

    data_root = resolve_data_root(args.data_root)
    city = load_city(args.city, data_root)
    enable_logging(city.log_path)
    run(city, args)


if __name__ == "__main__":
    main()
