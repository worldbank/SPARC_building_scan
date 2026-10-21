#!/usr/bin/env python3
"""Extract Dynamic World, AlphaEarth, and Google Open Buildings 2.5D signals
for one prepared city's integrated building footprints, via Earth Engine.

Reads <data_root>/<city>/outputs/best_available_footprints.gpkg (the output of
best_available_footprints_workflow) and joins Earth Engine results back onto it
by integrated_id.
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

import ee
import geopandas as gpd
import pandas as pd

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root, skip_existing


ID_FIELD = "integrated_id"

DW_CLASSES = [
    "water", "trees", "grass", "flooded_vegetation", "crops",
    "shrub_and_scrub", "built", "bare", "snow_and_ice",
]
DW_CLASS_NAMES = {
    0: "Water", 1: "Trees", 2: "Grass", 3: "Flooded vegetation", 4: "Crops",
    5: "Shrub and scrub", 6: "Built", 7: "Bare", 8: "Snow and ice",
}


def load_buildings(city) -> gpd.GeoDataFrame:
    path = city.outputs_dir / "best_available_footprints.gpkg"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run best_available_footprints_workflow for this "
            "city first."
        )
    gdf = gpd.read_file(path, layer="buildings")
    print(f"Buildings loaded: {len(gdf):,}")
    if ID_FIELD not in gdf.columns:
        raise KeyError(f"{ID_FIELD} not found in {path}")
    return gdf


def prepare_for_gee(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    processing = gdf[[ID_FIELD, "geometry"]].copy()
    empty = processing.geometry.isna().sum()
    if empty:
        print(f"Removing {empty} empty geometries from GEE processing...")
        processing = processing[~processing.geometry.isna()].copy()
    invalid = (~processing.geometry.is_valid).sum()
    if invalid:
        print(f"Fixing {invalid} invalid geometries...")
        processing["geometry"] = processing.geometry.make_valid()
    processing = processing.to_crs("EPSG:4326")
    print(f"Buildings ready for GEE: {len(processing):,}")
    return processing


def load_images(year: int, google_25d_year: int):
    dynamic_world = ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1").filterDate(
        f"{year}-01-01", f"{year + 1}-01-01"
    )
    dw_probability_image = dynamic_world.select(DW_CLASSES).mean()
    print("Dynamic World probability bands:", dw_probability_image.bandNames().getInfo())

    alphaearth = ee.ImageCollection("GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL").filterDate(
        f"{year}-01-01", f"{year + 1}-01-01"
    )
    embedding = alphaearth.mosaic()
    alpha_band_names = embedding.bandNames().getInfo()
    print(f"AlphaEarth dimensions: {len(alpha_band_names)}")
    if len(alpha_band_names) != 64:
        print(f"WARNING: expected 64 AlphaEarth bands, found {len(alpha_band_names)}.")

    google_25d = ee.ImageCollection("GOOGLE/Research/open-buildings-temporal/v1").filterDate(
        f"{google_25d_year}-01-01", f"{google_25d_year + 1}-01-01"
    )
    # Temporal maximum: the highest building_presence observed during the year.
    google_25d_image = google_25d.select("building_presence").max()

    return dw_probability_image, embedding, alpha_band_names, google_25d_image


def extract_batches(
    processing: gpd.GeoDataFrame,
    dw_probability_image,
    embedding,
    google_25d_image,
    scale: int,
    google_25d_scale: int,
    batch_size: int,
    pause_seconds: float,
    checkpoint_dir: Path,
) -> pd.DataFrame:
    combined_image = dw_probability_image.addBands(embedding)
    total = len(processing)
    total_batches = (total + batch_size - 1) // batch_size
    print(f"Total buildings: {total:,} | batch size: {batch_size} | batches: {total_batches}")

    all_records = []
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        batch_number = start // batch_size + 1
        # Keyed by building-index range, not batch number: a changed --batch-size
        # naturally invalidates stale checkpoints instead of misreading them.
        checkpoint_path = checkpoint_dir / f"{start:07d}_{end:07d}.parquet"

        if checkpoint_path.exists():
            batch_df = pd.read_parquet(checkpoint_path)
            print(f"Batch {batch_number}/{total_batches} ({start + 1}-{end}): resumed from checkpoint, {len(batch_df)} records")
            all_records.append(batch_df)
            continue

        print(f"Batch {batch_number}/{total_batches} ({start + 1}-{end})")

        batch_gdf = processing.iloc[start:end]
        buildings_ee = ee.FeatureCollection(batch_gdf[[ID_FIELD, "geometry"]].__geo_interface__)

        try:
            results = combined_image.reduceRegions(
                collection=buildings_ee, reducer=ee.Reducer.mean(), scale=scale, tileScale=4
            )
            batch_records = [f["properties"] for f in results.getInfo()["features"]]
            print(f"  DW/AlphaEarth records: {len(batch_records)}")
        except Exception as exc:
            print(f"ERROR processing DW/AlphaEarth in batch {batch_number}: {exc}")
            batch_records = []

        try:
            google_results = google_25d_image.reduceRegions(
                collection=buildings_ee, reducer=ee.Reducer.max(), scale=google_25d_scale, tileScale=4
            )
            google_records = [f["properties"] for f in google_results.getInfo()["features"]]
            print(f"  Google 2.5D records: {len(google_records)}")
        except Exception as exc:
            print(f"ERROR processing Google 2.5D in batch {batch_number}: {exc}")
            google_records = []

        batch_df = pd.DataFrame(batch_records)
        google_df = pd.DataFrame(google_records)
        if len(google_df) and len(batch_df):
            value_columns = [c for c in google_df.columns if c != ID_FIELD]
            if value_columns:
                google_df = google_df[[ID_FIELD, value_columns[0]]].rename(
                    columns={value_columns[0]: "google25d_max_confidence"}
                )
                batch_df = batch_df.merge(google_df, on=ID_FIELD, how="left")
        # Only checkpoint non-empty results, so a resume retries any batch that
        # failed outright (e.g. a transient Earth Engine error) rather than
        # treating a failure as permanently done.
        if len(batch_df):
            batch_df.to_parquet(checkpoint_path, index=False)
        all_records.append(batch_df)
        time.sleep(pause_seconds)

    valid = [frame for frame in all_records if len(frame)]
    if not valid:
        raise RuntimeError("No buildings were returned from Earth Engine.")
    df = pd.concat(valid, ignore_index=True)
    df = df.drop_duplicates(subset=[ID_FIELD], keep="first")
    print(f"GEE records after duplicate removal: {len(df):,}")
    return df


def summarize_dynamic_world(df: pd.DataFrame, alpha_band_names: list[str]) -> pd.DataFrame:
    for class_name in DW_CLASSES:
        if class_name not in df.columns:
            print(f"WARNING: {class_name} not found.")
            df[class_name] = pd.NA
    dw_values = df[DW_CLASSES].apply(pd.to_numeric, errors="coerce")
    # idxmax raises on an all-NaN row (no Dynamic World coverage at all for that
    # building), so compute it only where at least one class has a value.
    has_signal = dw_values.notna().any(axis=1)
    missing = int((~has_signal).sum())
    if missing:
        print(f"WARNING: {missing:,} buildings have no Dynamic World data at all.")
    df["dw_top_class"] = pd.Series(pd.NA, index=df.index, dtype=object)
    df.loc[has_signal, "dw_top_class"] = dw_values.loc[has_signal].idxmax(axis=1)
    df["dw_top_probability"] = dw_values.max(axis=1)
    class_to_id = {name: i for i, name in enumerate(DW_CLASSES)}
    df["dw_top_class_id"] = df["dw_top_class"].map(class_to_id)
    df["dw_top_class_name"] = df["dw_top_class_id"].map(DW_CLASS_NAMES)
    df["dw_built_probability"] = pd.to_numeric(df["built"], errors="coerce")

    rename = {band: f"alphaearth_{band}" for band in alpha_band_names}
    df = df.rename(columns=rename)
    alpha_columns = [f"alphaearth_{band}" for band in alpha_band_names if f"alphaearth_{band}" in df.columns]
    print(f"AlphaEarth columns retrieved: {len(alpha_columns)}")

    if "google25d_max_confidence" not in df.columns:
        print("WARNING: Google 2.5D results not found.")
        df["google25d_max_confidence"] = pd.NA
    else:
        df["google25d_max_confidence"] = pd.to_numeric(df["google25d_max_confidence"], errors="coerce")

    df = df.drop(columns=[c for c in DW_CLASSES if c in df.columns], errors="ignore")
    preferred = [
        ID_FIELD, "dw_top_class_id", "dw_top_class", "dw_top_class_name",
        "dw_top_probability", "dw_built_probability", "google25d_max_confidence",
    ]
    preferred = [c for c in preferred if c in df.columns]
    other = [c for c in df.columns if c not in preferred and c not in alpha_columns]
    return df[[c for c in preferred + alpha_columns + other if c in df.columns]]


def join_to_original(original: gpd.GeoDataFrame, df: pd.DataFrame) -> gpd.GeoDataFrame:
    gee_join = df.drop(columns=["geometry"], errors="ignore")
    conflicts = [c for c in gee_join.columns if c != ID_FIELD and c in original.columns]
    if conflicts:
        print("Existing fields replaced by GEE results:", conflicts)
        original = original.drop(columns=conflicts)
    output = original.merge(gee_join, on=ID_FIELD, how="left", validate="one_to_one")
    matched = output["dw_top_class_name"].notna().sum()
    print(f"Buildings with GEE results: {matched:,} / without: {len(output) - matched:,}")
    return output


def run(city, args) -> None:
    gee_dir = city.sources_dir / "gee"
    output_gpkg = gee_dir / f"{city.slug}_buildings_gee.gpkg"
    if skip_existing(output_gpkg, args.force):
        return
    gee_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = gee_dir / "checkpoints"
    if args.force and checkpoint_dir.exists():
        shutil.rmtree(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    print("Authenticating Earth Engine...")
    ee.Authenticate()
    ee.Initialize(project=args.ee_project) if args.ee_project else ee.Initialize()

    original = load_buildings(city)
    processing = prepare_for_gee(original)
    dw_image, embedding, alpha_band_names, google_image = load_images(args.year, args.google_25d_year)
    df = extract_batches(
        processing, dw_image, embedding, google_image,
        args.scale, args.google_25d_scale, args.batch_size, args.pause_seconds,
        checkpoint_dir,
    )
    df = summarize_dynamic_world(df, alpha_band_names)
    output = join_to_original(original, df)

    output_csv = gee_dir / f"{city.slug}_buildings_gee.csv"
    df.to_csv(output_csv, index=False)
    output.to_file(output_gpkg, layer=f"{city.slug}_buildings_gee", driver="GPKG")
    print(f"CSV saved: {output_csv}")
    print(f"GeoPackage saved: {output_gpkg}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ee-project", help="Earth Engine Cloud project id")
    parser.add_argument("--year", type=int, default=2025, help="Dynamic World / AlphaEarth year")
    parser.add_argument("--google-25d-year", type=int, default=2023)
    parser.add_argument("--scale", type=int, default=10, help="Dynamic World/AlphaEarth resolution (m)")
    parser.add_argument("--google-25d-scale", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--pause-seconds", type=float, default=0.5)
    add_data_root_arg(parser)
    args = parser.parse_args()

    data_root = resolve_data_root(args.data_root)
    city = load_city(args.city, data_root)
    enable_logging(city.log_path)
    run(city, args)


if __name__ == "__main__":
    main()
