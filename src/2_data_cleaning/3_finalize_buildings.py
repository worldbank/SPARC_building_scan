#!/usr/bin/env python3
"""Compute 50m neighbor statistics for one city's screened building footprints.
Writes the pipeline's final output.
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import numpy as np
import shapely
from shapely.strtree import STRtree

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root, skip_existing


NEIGHBOR_DISTANCE_M = 50.0


def add_neighbor_stats(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs.is_geographic:
        gdf = gdf.to_crs(gdf.estimate_utm_crs())
    if "area_m2" not in gdf.columns:
        gdf["area_m2"] = gdf.geometry.area

    gdf = gdf.reset_index(drop=True)
    geometries = gdf.geometry.to_numpy()
    areas = gdf["area_m2"].to_numpy()
    tree = STRtree(geometries)

    size_mean = np.full(len(gdf), np.nan)
    size_min = np.full(len(gdf), np.nan)
    size_max = np.full(len(gdf), np.nan)
    dist_mean = np.full(len(gdf), np.nan)
    dist_min = np.full(len(gdf), np.nan)
    dist_max = np.full(len(gdf), np.nan)
    count = np.zeros(len(gdf), dtype="int32")

    for i, geom in enumerate(geometries):
        candidates = tree.query(geom.buffer(NEIGHBOR_DISTANCE_M), predicate="intersects")
        candidates = candidates[candidates != i]
        if not len(candidates):
            continue
        distances = shapely.distance(geom, geometries[candidates])
        within = distances <= NEIGHBOR_DISTANCE_M
        if not within.any():
            continue
        neighbor_sizes = areas[candidates[within]]
        neighbor_distances = distances[within]
        size_mean[i], size_min[i], size_max[i] = neighbor_sizes.mean(), neighbor_sizes.min(), neighbor_sizes.max()
        dist_mean[i], dist_min[i], dist_max[i] = neighbor_distances.mean(), neighbor_distances.min(), neighbor_distances.max()
        count[i] = within.sum()

    gdf["nn_size_mean"] = size_mean
    gdf["nn_size_min"] = size_min
    gdf["nn_size_max"] = size_max
    gdf["nn_edge_dist_mean_m"] = dist_mean
    gdf["nn_edge_dist_min_m"] = dist_min
    gdf["nn_edge_dist_max_m"] = dist_max
    gdf["nn_count_50m"] = count

    print(f"Buildings with >=1 neighbor within 50m: {(count > 0).sum():,}")
    print(f"Buildings with no neighbor within 50m:  {(count == 0).sum():,}")
    return gdf


def run(city, args) -> None:
    input_gpkg = city.sources_dir / "cleaning" / f"{city.slug}_screened.gpkg"
    if not input_gpkg.exists():
        raise FileNotFoundError(f"{input_gpkg} not found. Run 2_drop_false_positives.py first.")
    output_gpkg = city.outputs_dir / "best_available_footprints_cleaned.gpkg"
    if skip_existing(output_gpkg, args.force):
        return

    gdf = gpd.read_file(input_gpkg, layer=f"{city.slug}_screened")
    print(f"Buildings loaded: {len(gdf):,}")

    gdf = add_neighbor_stats(gdf)

    gdf.to_file(output_gpkg, layer="buildings", driver="GPKG")
    print(f"GeoPackage saved: {output_gpkg}")


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
