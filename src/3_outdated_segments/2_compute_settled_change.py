#!/usr/bin/env python3
"""Compute baseline vs. current WSF-settled area per segment.

"Current settled" = every pixel ever detected as built-up (epoch 1-20).
"Baseline settled" = pixels detected as built-up at or before each segment's
own baseline epoch (from stage 1). Both are computed from the same raster in
a single vectorized pass: segments are rasterized onto the WSF grid, then
pixel counts are grouped by segment, comparing each pixel's epoch against
*that pixel's own segment's* baseline -- not a single city-wide cutoff.
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root, skip_existing


NO_SEGMENT = -1  # sentinel for "outside any segment" -- segment IDs are always >= 0


def run(city, args) -> None:
    segments_dir = city.sources_dir / "segments"
    input_gpkg = segments_dir / f"{city.slug}_segments_baseline.gpkg"
    output_gpkg = segments_dir / f"{city.slug}_segments_settled_area.gpkg"
    if not input_gpkg.exists():
        raise FileNotFoundError(f"{input_gpkg} not found. Run 1_sample_building_wsf_year.py first.")
    if skip_existing(output_gpkg, args.force):
        return

    wsf_path = city.sources_dir / "wsf" / f"{city.slug}_wsf.tif"
    if not wsf_path.exists():
        raise FileNotFoundError(f"{wsf_path} not found. Run 0_download_wsf.py first.")

    segments = gpd.read_file(input_gpkg, layer="segments_baseline")
    print(f"Segments loaded: {len(segments):,}")

    with rasterio.open(wsf_path) as src:
        wsf = src.read(1)
        transform = src.transform
        cell_area_m2 = abs(transform.a * transform.e)
        shape = src.shape
        raster_crs = src.crs

    if segments.crs != raster_crs:
        segments = segments.to_crs(raster_crs)

    segment_raster = rasterize(
        ((geom, int(seg_id)) for geom, seg_id in zip(segments.geometry, segments["segment_id"])),
        out_shape=shape,
        transform=transform,
        fill=NO_SEGMENT,
        dtype="int32",
    )

    valid = segment_raster != NO_SEGMENT
    pixels = pd.DataFrame({
        "segment_id": segment_raster[valid],
        "wsf_epoch": wsf[valid],
    })
    print(f"Pixels inside a segment: {len(pixels):,} / {wsf.size:,} total raster pixels")

    baseline_lookup = segments.set_index("segment_id")["baseline_epoch"]
    pixels["baseline_epoch"] = pixels["segment_id"].map(baseline_lookup)

    current_mask = pixels["wsf_epoch"] > 0
    baseline_mask = current_mask & pixels["baseline_epoch"].notna() & (
        pixels["wsf_epoch"] <= pixels["baseline_epoch"]
    )

    current_counts = pixels.loc[current_mask].groupby("segment_id").size()
    baseline_counts = pixels.loc[baseline_mask].groupby("segment_id").size()

    segments = segments.set_index("segment_id")
    segments["current_settled_m2"] = (current_counts * cell_area_m2).reindex(segments.index).fillna(0.0)
    segments["baseline_settled_m2"] = (baseline_counts * cell_area_m2).reindex(segments.index).fillna(0.0)
    # No baseline (e.g. zero buildings in the segment) -> no baseline area either.
    segments.loc[segments["baseline_epoch"].isna(), "baseline_settled_m2"] = np.nan
    segments = segments.reset_index()

    have_both = segments["baseline_settled_m2"].notna().sum()
    print(f"Segments with both current and baseline settled area: {have_both:,} / {len(segments):,}")
    print(f"Cell area: {cell_area_m2:.2f} m^2")

    segments.to_file(output_gpkg, layer="segments_settled_area", driver="GPKG")
    print(f"Saved: {output_gpkg}")


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
