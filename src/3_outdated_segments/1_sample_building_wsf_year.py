#!/usr/bin/env python3
"""Sample the WSF-detected built-up epoch at each building, then compute a
per-segment baseline: the lower-quartile WSF epoch among that segment's own
buildings. This baseline is the per-segment T used by later stages to measure
how much of today's settled area postdates it.

Segments are a user-supplied external file (not necessarily tied to this
city's own ANALYSIS_ID) -- e.g. a CIESIN City Segments Layer v2 export with a
native ID_SEG field, or any polygon layer. If no obvious ID column is found,
one is assigned.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from dateutil.relativedelta import relativedelta

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root, skip_existing


EPOCH_START = date(2016, 1, 1)  # epoch 1 lands on 2016-07-01 via +6 months
EPOCH_MONTHS = 6
SEGMENT_ID_CANDIDATES = ["ID_SEG", "segment_id", "SEGMENT_UID", "ANALYSIS_ID"]


def epoch_to_year(epoch) -> float:
    if epoch is None or (isinstance(epoch, float) and np.isnan(epoch)) or epoch <= 0:
        return np.nan
    d = EPOCH_START + relativedelta(months=int(epoch) * EPOCH_MONTHS)
    return d.year + (d.month - 1) / 12


def load_segments(path: Path, layer: str | None, id_field: str | None, crs) -> tuple[gpd.GeoDataFrame, str]:
    segments = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    # GeoPackage reserves "fid" for its own feature-id column; an explicit data
    # column with that name (e.g. from a KML-derived merge) fails on write.
    fid_columns = [c for c in segments.columns if c.lower() == "fid"]
    if fid_columns:
        segments = segments.drop(columns=fid_columns)
    segments = segments.loc[segments.geometry.notna() & ~segments.geometry.is_empty].copy()
    invalid = ~shapely.is_valid(segments.geometry.to_numpy())
    if invalid.any():
        segments.loc[invalid, "geometry"] = shapely.make_valid(segments.loc[invalid, "geometry"].to_numpy())
    segments = segments.to_crs(crs).reset_index(drop=True)

    if id_field is None:
        id_field = next((c for c in SEGMENT_ID_CANDIDATES if c in segments.columns), None)
    if id_field is None:
        id_field = "segment_id"
        segments[id_field] = np.arange(1, len(segments) + 1)
        print(f"No ID column found; assigned sequential {id_field}.")
    else:
        print(f"Using segment ID field: {id_field}")
    segments[id_field] = pd.to_numeric(segments[id_field], errors="coerce").astype("Int64")

    # Features with no ID (e.g. outer coverage intentionally added to extend
    # to the full AOI) are kept, not dropped -- each gets a new, unique ID.
    missing_id = segments[id_field].isna()
    if missing_id.any():
        next_id = int(segments[id_field].max(skipna=True) or 0) + 1
        segments.loc[missing_id, id_field] = range(next_id, next_id + int(missing_id.sum()))
        print(f"Assigned new IDs to {missing_id.sum()} feature(s) with no {id_field} (e.g. outer AOI coverage).")

    # Features sharing the same ID (a segment split into pieces by an
    # upstream overlay/union) are dissolved back into one multi-part segment,
    # so each ID is represented by exactly one row.
    duplicated = segments[id_field].duplicated(keep=False)
    if duplicated.any():
        n_ids = segments.loc[duplicated, id_field].nunique()
        print(f"Dissolving {duplicated.sum()} feature(s) sharing an ID back into {n_ids} segment(s).")
        segments = segments.dissolve(by=id_field, aggfunc="first").reset_index()

    return segments, id_field


def run(city, args) -> None:
    output_dir = city.sources_dir / "segments"
    output_gpkg = output_dir / f"{city.slug}_segments_baseline.gpkg"
    if skip_existing(output_gpkg, args.force):
        return
    output_dir.mkdir(parents=True, exist_ok=True)

    buildings_path = city.outputs_dir / "best_available_footprints_cleaned.gpkg"
    if not buildings_path.exists():
        raise FileNotFoundError(f"{buildings_path} not found. Run improving_overture's pipeline first.")
    buildings = gpd.read_file(buildings_path, layer="buildings")
    print(f"Buildings loaded: {len(buildings):,}")

    segments, id_field = load_segments(Path(args.segments), args.segments_layer, args.segments_id_field, buildings.crs)
    print(f"Segments loaded: {len(segments):,}")

    points = gpd.GeoDataFrame(
        {"row": np.arange(len(buildings))},
        geometry=shapely.point_on_surface(buildings.geometry.to_numpy()),
        crs=buildings.crs,
    )
    joined = gpd.sjoin(points, segments[[id_field, "geometry"]], how="left", predicate="within")
    joined = joined.sort_index().loc[~joined.index.duplicated(keep="first")]
    segment_id = joined.set_index("row")[id_field].reindex(range(len(buildings)))

    wsf_path = city.sources_dir / "wsf" / f"{city.slug}_wsf.tif"
    if not wsf_path.exists():
        raise FileNotFoundError(f"{wsf_path} not found. Run 0_download_wsf.py first.")
    with rasterio.open(wsf_path) as src:
        xs = shapely.get_x(points.geometry.to_numpy())
        ys = shapely.get_y(points.geometry.to_numpy())
        samples = np.array([v[0] for v in src.sample(zip(xs, ys), indexes=1)], dtype="int16")

    buildings_wsf = pd.DataFrame({
        "segment_id": segment_id.to_numpy(),
        "wsf_epoch": samples,
    })
    buildings_wsf["wsf_year"] = buildings_wsf["wsf_epoch"].apply(epoch_to_year)

    assigned = buildings_wsf["segment_id"].notna().sum()
    print(f"Buildings assigned to a segment: {assigned:,} / {len(buildings):,}")

    detected = buildings_wsf[buildings_wsf["wsf_epoch"] > 0]
    stats = detected.groupby("segment_id")["wsf_epoch"].quantile(0.25).rename("baseline_epoch")
    counts = buildings_wsf.groupby("segment_id").size().rename("building_count")

    segments = segments.merge(stats, left_on=id_field, right_index=True, how="left")
    segments = segments.merge(counts, left_on=id_field, right_index=True, how="left")
    segments["building_count"] = segments["building_count"].fillna(0).astype("int64")
    segments["baseline_epoch"] = segments["baseline_epoch"].round(2)
    segments["baseline_year"] = segments["baseline_epoch"].apply(epoch_to_year)

    with_baseline = segments["baseline_epoch"].notna().sum()
    print(f"Segments with a usable baseline: {with_baseline:,} / {len(segments):,}")

    segments = segments.rename(columns={id_field: "segment_id"})
    segments.to_file(output_gpkg, layer="segments_baseline", driver="GPKG")
    print(f"Saved: {output_gpkg}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument("--segments", required=True, help="Path to an external segments polygon file")
    parser.add_argument("--segments-layer", help="Layer name, if the segments file has multiple layers")
    parser.add_argument("--segments-id-field", help="Segment ID column (default: auto-detect, else assign)")
    parser.add_argument("--force", action="store_true")
    add_data_root_arg(parser)
    args = parser.parse_args()

    data_root = resolve_data_root(args.data_root)
    city = load_city(args.city, data_root)
    enable_logging(city.log_path)
    run(city, args)


if __name__ == "__main__":
    main()
