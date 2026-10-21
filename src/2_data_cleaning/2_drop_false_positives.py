#!/usr/bin/env python3
"""Drop conservative false positives from one city's GEE-enriched, outlier-scored
building footprints.

A feature is dropped when it matches Rule A (AlphaEarth outlier with weak or
non-built Dynamic World support and non-high geometry confidence) or Rule B (low
Google 2.5D presence on a pre-2024 footprint), unless it is recent Overture or
has strong Dynamic World "built" support.
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import pandas as pd

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root, skip_existing


GLO_PROVIDER_VALUE = "3D-GloBFP"
RECENT_YEAR = 2023
STRONG_BUILT_PROBABILITY = 0.60
GOOGLE25D_LOW_CONFIDENCE = 0.10
GOOGLE25D_OLDER_THAN_YEAR = 2024


def apply_screening(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    year = pd.to_datetime(gdf["footprint_date"], errors="coerce", utc=True).dt.year
    provider = gdf["geometry_provider"].astype(str).str.lower()
    dw_class = gdf["dw_top_class_name"].astype(str).str.lower()
    dw_probability = pd.to_numeric(gdf["dw_top_probability"], errors="coerce")
    alpha_outlier = gdf["isolation_forest_outlier"].fillna(False).astype(bool)
    geometry_confidence = gdf["geometry_confidence"].astype(str).str.lower()
    google25d_confidence = pd.to_numeric(gdf["google25d_max_confidence"], errors="coerce")

    is_overture = provider != GLO_PROVIDER_VALUE.lower()

    # Protective guards (kept from the arcpy duplicate): never drop a recent
    # Overture feature, and never drop a feature with strong "built" support.
    is_recent_overture = is_overture & (year >= RECENT_YEAR)
    strong_built = (dw_class == "built") & (dw_probability.fillna(0) >= STRONG_BUILT_PROBABILITY)
    protected = is_recent_overture | strong_built

    # Rule A: AlphaEarth outlier + weak/non-built Dynamic World + low geometry confidence.
    weak_or_non_built = (dw_class != "built") | dw_probability.isna() | (dw_probability < STRONG_BUILT_PROBABILITY)
    rule_a = alpha_outlier & weak_or_non_built & (geometry_confidence != "high")

    # Rule B: low Google 2.5D confidence on an older footprint.
    rule_b = (google25d_confidence <= GOOGLE25D_LOW_CONFIDENCE) & (year < GOOGLE25D_OLDER_THAN_YEAR)

    drop_mask = (~protected) & (rule_a | rule_b)

    original_len = len(gdf)
    screened = gdf.loc[~drop_mask].copy()
    print(f"False positives dropped: {drop_mask.sum():,} / {original_len:,}")
    print(f"Buildings retained: {len(screened):,}")
    return screened


def run(city, args) -> None:
    input_gpkg = city.sources_dir / "gee" / "outlier_results" / f"{city.slug}_buildings_if_tsne.gpkg"
    if not input_gpkg.exists():
        raise FileNotFoundError(f"{input_gpkg} not found. Run 1_detect_outliers.py first.")
    output_dir = city.sources_dir / "cleaning"
    output_gpkg = output_dir / f"{city.slug}_screened.gpkg"
    if skip_existing(output_gpkg, args.force):
        return
    output_dir.mkdir(parents=True, exist_ok=True)

    gdf = gpd.read_file(input_gpkg, layer=f"{city.slug}_buildings_if_tsne")
    print(f"Buildings loaded: {len(gdf):,}")
    screened = apply_screening(gdf)
    screened.to_file(output_gpkg, layer=f"{city.slug}_screened", driver="GPKG")
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
