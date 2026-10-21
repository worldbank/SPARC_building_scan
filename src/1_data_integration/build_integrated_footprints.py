#!/usr/bin/env python3
"""Build attributed best-available footprints for one prepared city."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl_best_available_footprints")

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd
import shapely
from matplotlib import pyplot as plt

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root, skip_existing
from footprint_overlap_resolution import resolve_overlaps_by_recency


matplotlib.use("Agg")
MIN_AREA_M2 = 4.0  # candidate polygons smaller than this are dropped before selection
OVERLAP_TOLERANCE_M2 = 0.01  # below this, an intersection is a boundary-touch/sliver, not a real conflict
OVERLAP_SMALLER_FRACTION = 0.20  # intersection must cover >= 20% of the smaller footprint to count as a conflict
GLOBFP_DATE = pd.Timestamp("2020-01-01", tz="UTC")  # 3D-GloBFP's documented model vintage; it has no per-feature date
RECENCY_TIEBREAK = {
    # Primary ranking key for overlap resolution: higher priority always wins.
    # footprint_date (most recent first) only decides between candidates of
    # equal priority, e.g. two overlapping OSM footprints.
    "OpenStreetMap": 3,
    "Overture_nonOSM": 2,
    "3D-GloBFP_gapfill": 1,
}


# --- Overture's "sources" column is a list of per-source-record dicts (provider,
# dataset, update_time, record_id, ...); these three helpers pull out what we need. ---


def source_list(value) -> list[dict]:
    if isinstance(value, np.ndarray):
        value = value.tolist()
    return value if isinstance(value, list) else []


def first_source(value, key, default=None):
    records = source_list(value)
    return records[0].get(key, default) if records else default


def latest_update(value):
    # A feature's date is the LATEST update_time across all of its attached source
    # records (e.g. an OSM way edited by multiple changesets), not the first one.
    records = source_list(value)
    times = pd.to_datetime(
        [record.get("update_time") for record in records], errors="coerce", utc=True
    )
    return times.max() if len(times) and times.notna().any() else pd.NaT


def clean(data: gpd.GeoDataFrame, crs) -> gpd.GeoDataFrame:
    # Reproject, drop null/empty/invalid-after-repair geometry, keep only
    # Polygon/MultiPolygon (type ids 3/6), and drop anything under MIN_AREA_M2.
    if data.crs is None:
        raise ValueError("Source data has no CRS")
    data = data.to_crs(crs).copy()
    data = data.loc[data.geometry.notna() & ~data.geometry.is_empty].copy()
    invalid = ~shapely.is_valid(data.geometry.to_numpy())
    if invalid.any():
        data.loc[invalid, "geometry"] = shapely.make_valid(
            data.loc[invalid, "geometry"].to_numpy()
        )
    data = data[data.geom_type.isin(["Polygon", "MultiPolygon"])]
    data["area_m2"] = shapely.area(data.geometry.to_numpy())
    return data.loc[data.area_m2 >= MIN_AREA_M2].reset_index(drop=True)


def within_aoi(data: gpd.GeoDataFrame, aoi) -> gpd.GeoDataFrame:
    # Keep a feature if its point-on-surface falls inside the AOI, not if it merely
    # intersects the boundary. Whole buildings are kept (never clipped), even ones
    # that straddle the AOI edge, as long as their interior point is inside.
    points = shapely.point_on_surface(data.geometry.to_numpy())
    return data.loc[shapely.within(points, aoi)].reset_index(drop=True)


# --- Step 1: load + clean each raw source, independently, into a common shape. ---


def load_overture(city_root: Path, crs, aoi) -> gpd.GeoDataFrame:
    path = city_root / "sources/overture_buildings.parquet"
    columns = ["id", "sources", "height", "num_floors", "geometry"]
    data = within_aoi(clean(gpd.read_parquet(path, columns=columns), crs), aoi)
    data["provider"] = [first_source(value, "provider", "unknown") for value in data.sources]
    data["source_dataset"] = [first_source(value, "dataset", "unknown") for value in data.sources]
    data["source_record_id"] = [first_source(value, "record_id") for value in data.sources]
    data["source_update_time"] = [first_source(value, "update_time") for value in data.sources]
    data["footprint_update_time"] = [latest_update(value) for value in data.sources]
    data["source_version"] = [first_source(value, "version") for value in data.sources]
    data["source_license"] = [first_source(value, "license") for value in data.sources]
    data["native_height_m"] = pd.to_numeric(data.height, errors="coerce")
    data["native_floors"] = pd.to_numeric(data.num_floors, errors="coerce")
    data.to_parquet(city_root / "sources/overture_clipped.parquet", index=False)
    return data


def load_globfp(city_root: Path, crs, aoi, bbox_wgs84) -> gpd.GeoDataFrame:
    # One tile can cover a much larger area than the AOI, so each shapefile read is
    # pre-filtered to the AOI's WGS84 bbox (the tiles' native CRS) before reprojecting.
    manifest = json.loads((city_root / "sources/globfp_tiles.json").read_text())
    pieces = []
    for record in manifest:
        files = list(Path(record["directory"]).rglob("*.shp"))
        if len(files) != 1:
            raise ValueError(
                f"3D-GloBFP tile {record['grid_id']}: expected one shapefile, found {len(files)}"
            )
        part = gpd.read_file(
            files[0], bbox=tuple(bbox_wgs84), columns=["BFID", "Height"], engine="pyogrio"
        )
        if len(part):
            if "BFID" not in part:
                part["BFID"] = np.arange(len(part), dtype="int64").astype(str)
            part["tile_id"] = int(record["grid_id"])
            part["globfp_id"] = str(record["grid_id"]) + ":" + part.BFID.astype(str)
            pieces.append(part)
    if not pieces:
        data = gpd.GeoDataFrame(
            columns=["BFID", "Height", "tile_id", "globfp_id", "area_m2", "geometry"],
            geometry="geometry", crs=crs,
        )
    else:
        data = gpd.GeoDataFrame(
            pd.concat(pieces, ignore_index=True), geometry="geometry", crs=pieces[0].crs
        )
        data = within_aoi(clean(data, crs), aoi)
        data = data.drop_duplicates("globfp_id").reset_index(drop=True)
        data["Height"] = pd.to_numeric(data.Height, errors="coerce")
    data.to_parquet(city_root / "sources/globfp3d_clipped.parquet", index=False)
    return data


# --- Step 2: turn each cleaned source into "candidate" records on a common schema
# (integrated_id, geometry_source, footprint_date, recency_tiebreak_priority, ...)
# so Overture and 3D-GloBFP features can be pooled and compared directly. ---


def overture_candidates(data, slug: str) -> gpd.GeoDataFrame:
    source = np.where(data.provider.astype(str).str.lower().eq("osm"), "OpenStreetMap", "Overture_nonOSM")
    result = gpd.GeoDataFrame({
        "integrated_id": [f"{slug}-OVR-{i:08d}" for i in range(1, len(data) + 1)],
        "geometry_source": source,
        "geometry_source_id": data.id.astype(str).to_numpy(),
        "geometry_provider": data.provider.astype(str).to_numpy(),
        "geometry_dataset": data.source_dataset.astype(str).to_numpy(),
        "geometry_license": data.source_license.astype(str).to_numpy(),
        "source_record_id": data.source_record_id.to_numpy(),
        "source_update_time": data.source_update_time.to_numpy(),
        "footprint_update_time": data.footprint_update_time.to_numpy(),
        "source_version": data.source_version.to_numpy(),
        "native_height_m": data.native_height_m.to_numpy(),
        "native_floors": data.native_floors.to_numpy(),
        "area_m2": data.area_m2.to_numpy(),
        "geometry": data.geometry.to_numpy(),
    }, geometry="geometry", crs=data.crs)
    result["footprint_date"] = pd.to_datetime(
        result.footprint_update_time, errors="coerce", utc=True
    )
    result["footprint_date_basis"] = np.where(
        result.footprint_date.notna(), "latest_source_update_time", "unknown"
    )
    result["recency_tiebreak_priority"] = result.geometry_source.map(RECENCY_TIEBREAK)
    return result


def globfp_candidates(data, slug: str) -> gpd.GeoDataFrame:
    # 3D-GloBFP has no per-feature dates, so every footprint is dated 2020-01-01
    # (its documented model vintage). It has the lowest source priority, so it
    # always loses to an overlapping Overture/OSM footprint and is kept only
    # where no Overture footprint overlaps it by >= 20% of the smaller
    # footprint (pure gap-fill).
    result = gpd.GeoDataFrame({
        "integrated_id": [f"{slug}-GLO-{i:08d}" for i in range(1, len(data) + 1)],
        "geometry_source": "3D-GloBFP_gapfill",
        "geometry_source_id": data.globfp_id.astype(str).to_numpy(),
        "geometry_provider": "3D-GloBFP",
        "geometry_dataset": "3D-GloBFP",
        "geometry_license": "CC-BY-4.0",
        "source_record_id": data.BFID.astype(str).to_numpy(),
        "source_update_time": None,
        "footprint_update_time": None,
        "source_version": "2020 model",
        # 3D-GloBFP's modelled height is not a native attribute of the geometry; it is
        # attached as height_globfp_vector_m in attach_globfp_height (priority 3).
        "native_height_m": np.nan,
        "native_floors": np.nan,
        "area_m2": data.area_m2.to_numpy(),
        "geometry": data.geometry.to_numpy(),
    }, geometry="geometry", crs=data.crs)
    result["footprint_date"] = GLOBFP_DATE
    result["footprint_date_basis"] = "dataset_vintage_2020"
    result["recency_tiebreak_priority"] = RECENCY_TIEBREAK["3D-GloBFP_gapfill"]
    return result


# --- Step 4: does an independent 3D-GloBFP footprint corroborate each surviving
# feature? This is a CONFIDENCE signal only -- it never changes which geometry was
# selected in step 3, and it uses its own (stricter) 35% threshold, not the 20%
# OVERLAP_SMALLER_FRACTION used for conflict resolution. ---


def globfp_support(integrated, globfp) -> tuple[np.ndarray, pd.DataFrame]:
    supported = np.zeros(len(integrated), dtype=bool)
    if integrated.empty or globfp.empty:
        return supported, pd.DataFrame()
    joined = gpd.sjoin(
        integrated[["geometry"]], globfp[["geometry"]], how="inner", predicate="intersects"
    )
    if joined.empty:
        return supported, pd.DataFrame()
    left = joined.index.to_numpy(dtype="int64")
    right = joined.index_right.to_numpy(dtype="int64")
    intersection = shapely.area(shapely.intersection(
        integrated.geometry.to_numpy()[left], globfp.geometry.to_numpy()[right]
    ))
    smaller = np.minimum(
        integrated.area_m2.to_numpy()[left], globfp.area_m2.to_numpy()[right]
    )
    # Support threshold: >=1 m^2 AND >=35% of the smaller footprint (independent of
    # the 20%/OVERLAP_SMALLER_FRACTION conflict threshold used in overlap resolution).
    keep = (intersection >= 1.0) & (intersection / np.maximum(smaller, 1e-9) >= 0.35)
    supported[np.unique(left[keep])] = True
    return supported, pd.DataFrame({
        "integrated_index": left[keep], "globfp_index": right[keep],
        "intersection_m2": intersection[keep],
    })


def attach_globfp_height(integrated, globfp) -> None:
    # Step A: join each feature's point-on-surface to whichever 3D-GloBFP polygon
    # contains it (sorted by area so the smallest/most-specific containing polygon
    # wins ties). This is how OSM/Overture footprints get a 3D-GloBFP height.
    integrated["height_globfp_vector_m"] = np.nan
    integrated["globfp_height_id"] = None
    if integrated.empty or globfp.empty:
        return
    points = gpd.GeoDataFrame(
        {"integrated_index": np.arange(len(integrated), dtype="int64")},
        geometry=shapely.point_on_surface(integrated.geometry.to_numpy()), crs=integrated.crs,
    )
    target = globfp[["globfp_id", "Height", "area_m2", "geometry"]]
    joined = gpd.sjoin(points, target, how="left", predicate="within")
    joined = joined.sort_values(["integrated_index", "area_m2"]).drop_duplicates("integrated_index")
    joined = joined.set_index("integrated_index")
    height = pd.to_numeric(joined.Height, errors="coerce")
    integrated.loc[joined.index, "height_globfp_vector_m"] = height
    integrated.loc[joined.index, "globfp_height_id"] = joined.globfp_id

    # Step B: 3D-GloBFP-selected footprints use their own polygon's height.
    # geometry_source_id holds the globfp_id for these rows, and globfp_id is
    # unique (load_globfp drops duplicates), so this is a direct lookup.
    own = integrated.geometry_source.eq("3D-GloBFP_gapfill")
    if own.any():
        own_ids = integrated.loc[own, "geometry_source_id"]
        own_height = own_ids.map(globfp.set_index("globfp_id").Height)
        integrated.loc[own, "height_globfp_vector_m"] = pd.to_numeric(
            own_height, errors="coerce"
        ).to_numpy()
        integrated.loc[own, "globfp_height_id"] = own_ids.to_numpy()


# --- Step 5: pre-raster height. height_best_m takes the first valid value in this
# priority order: native Overture height, OSM floor count x 3m, 3D-GloBFP vector
# height. enrich_raster_heights.py later extends this same priority list with
# Google 2.5D / GBA.Height / TEMPO and recomputes these fields from scratch. ---


def height_attributes(data) -> None:
    direct = pd.to_numeric(data.native_height_m, errors="coerce").to_numpy(dtype="float64")
    floors = pd.to_numeric(data.native_floors, errors="coerce").to_numpy(dtype="float64")
    floor_height = np.where((floors > 0) & (floors <= 40), floors * 3.0, np.nan)  # cap: implausible floor counts are ignored
    globfp = pd.to_numeric(data.height_globfp_vector_m, errors="coerce").to_numpy(dtype="float64")
    data["height_floors_estimate_m"] = floor_height
    best = np.full(len(data), np.nan)
    source = np.full(len(data), None, dtype=object)
    stack = []
    for name, values in [
        ("native_geometry", direct),       # priority 1: Overture's own height attribute
        ("OSM_levels_x_3m", floor_height),  # priority 2: OSM num_floors * 3m
        ("3D-GloBFP_vector", globfp),       # priority 3: 3D-GloBFP's vector height
    ]:
        valid = np.isfinite(values) & (values >= 0.5) & (values <= 100)  # plausible-building-height bounds
        stack.append(np.where(valid, values, np.nan))
        take = np.isnan(best) & valid
        best[take], source[take] = values[take], name
    values = np.vstack(stack)
    valid_count = np.isfinite(values).sum(axis=0)
    empty = valid_count == 0
    values[:, empty] = 0
    value_range = np.nanmax(values, axis=0) - np.nanmin(values, axis=0)
    value_range[valid_count < 2] = np.nan
    data["height_best_m"] = best
    data["height_source"] = source
    data["height_source_count"] = valid_count.astype("int16")
    data["height_range_m"] = value_range
    data["height_confidence"] = np.where(
        pd.Series(source).eq("native_geometry"), "high",
        np.where(pd.Series(source).notna(), "medium", None),
    )


def assign_segments(data, segments) -> None:
    # Attaches reporting-segment identifiers (ANALYSIS_ID/SEGMENT_UID) by point-on-
    # surface, for attribution/summaries only -- segments never affect which
    # geometry gets selected.
    points = gpd.GeoDataFrame(
        {"row_index": np.arange(len(data), dtype="int64")},
        geometry=shapely.point_on_surface(data.geometry.to_numpy()), crs=data.crs,
    )
    joined = gpd.sjoin(
        points, segments[["ANALYSIS_ID", "SEGMENT_UID", "geometry"]],
        how="left", predicate="within",
    ).sort_index()
    joined = joined.loc[~joined.index.duplicated(keep="first")].set_index("row_index")
    data["ANALYSIS_ID"] = joined.ANALYSIS_ID.reindex(range(len(data))).to_numpy()
    data["SEGMENT_UID"] = joined.SEGMENT_UID.reindex(range(len(data))).to_numpy()


# --- Step 6: geometry_confidence/selection_reason/review_required -- a plain-
# English explanation of why each geometry was trusted, and a flag for which
# features a human should double-check. ---


def confidence_attributes(data) -> None:
    osm = data.geometry_source.eq("OpenStreetMap")
    # OSM record_ids look like "w1484281873@1" -- the trailing "@N" is the edit
    # version; >=2 means the feature has been revised at least once.
    data["osm_version"] = pd.to_numeric(
        data.source_record_id.astype("string").str.extract(r"@(\d+)$")[0], errors="coerce"
    ).astype("Int32")
    data["osm_multiple_versions"] = data.osm_version.fillna(0).ge(2)
    data["geometry_confidence"] = "low"
    data.loc[osm, "geometry_confidence"] = "medium"
    data.loc[osm & (data.globfp_support | data.osm_multiple_versions), "geometry_confidence"] = "high"
    data.loc[~osm & data.globfp_support, "geometry_confidence"] = "medium"
    data.loc[
        data.geometry_source.eq("3D-GloBFP_gapfill") & data.height_best_m.notna(),
        "geometry_confidence",
    ] = "medium"
    data["selection_reason"] = np.select([
        osm & data.globfp_support,
        osm & data.osm_multiple_versions,
        osm,
        data.geometry_source.eq("Overture_nonOSM") & data.globfp_support,
        data.geometry_source.eq("Overture_nonOSM"),
    ], [
        "OSM_selected_3D_supported", "OSM_selected_multiple_versions",
        "OSM_selected", "Overture_selected_3D_supported", "Overture_selected",
    ], default="3D-GloBFP_selected")
    # Flag for manual review: low-confidence geometry, OR two-plus independent
    # height estimates that disagree by more than 5 m.
    data["review_required"] = (
        data.geometry_confidence.eq("low")
        | ((data.height_source_count >= 2) & (data.height_range_m > 5))
    )


# --- Step 7: audit trail. One row per surviving feature (role=selected_geometry)
# plus one row per feature suppressed in step 3, grouped by the surviving
# integrated_id so a feature's full suppression history can be filtered out. ---


def make_lineage(integrated, provisional, suppressed) -> pd.DataFrame:
    selected = pd.DataFrame({
        "integrated_id": integrated.integrated_id,
        "role": "selected_geometry",
        "source_dataset": integrated.geometry_dataset,
        "source_id": integrated.geometry_source_id,
        "footprint_date": integrated.footprint_date,
        "footprint_date_basis": integrated.footprint_date_basis,
        "retained_footprint_date": integrated.footprint_date,
        "relation": "selected",
        "intersection_m2": integrated.area_m2,
        "smaller_overlap": 1.0,
    })
    removed = suppressed.copy()
    if len(removed):
        index = removed.suppressed_index.astype(int)
        removed["integrated_id"] = removed.retained_integrated_id
        removed["role"] = "suppressed_older_overlap"
        removed["source_dataset"] = provisional.geometry_dataset.to_numpy()[index]
        removed["source_id"] = provisional.geometry_source_id.to_numpy()[index]
        removed["footprint_date"] = provisional.footprint_date.to_numpy()[index]
        removed["footprint_date_basis"] = provisional.footprint_date_basis.to_numpy()[index]
        removed["retained_footprint_date"] = provisional.footprint_date.to_numpy()[
            removed.retained_index.astype(int)
        ]
    columns = [
        "integrated_id", "role", "source_dataset", "source_id", "footprint_date",
        "footprint_date_basis", "retained_footprint_date", "relation",
        "intersection_m2", "smaller_overlap",
    ]
    return pd.concat([
        selected.reindex(columns=columns), removed.reindex(columns=columns)
    ], ignore_index=True)


def overview(data, city_name: str, path: Path) -> None:
    # Two-panel summary map: left = which source "won" each footprint (colors
    # below, no legend rendered on the panel itself), right = best vector height.
    colors = {
        "OpenStreetMap": "#17365D", "Overture_nonOSM": "#5B9BD5",
        "3D-GloBFP_gapfill": "#ED7D31",
    }
    fig, axes = plt.subplots(1, 2, figsize=(11, 5), constrained_layout=True)
    for source, group in data.groupby("geometry_source"):
        group.plot(ax=axes[0], color=colors.get(source, "#A5A5A5"), linewidth=0)
    axes[0].set_title("Selected geometry source")
    finite = data.loc[data.height_best_m.notna()]
    if len(finite):
        finite.plot(
            ax=axes[1], column="height_best_m", cmap="viridis", linewidth=0,
            legend=True, legend_kwds={"label": "Best vector height (m)"},
            vmin=0, vmax=max(3, float(finite.height_best_m.quantile(0.98))),
        )
    axes[1].set_title("Best vector-derived height")
    for axis in axes:
        axis.set_axis_off()
    fig.suptitle(f"{city_name}: integrated building footprints", fontsize=15)
    fig.savefig(path, dpi=180, facecolor="white", bbox_inches="tight")
    plt.close(fig)


def build(city: str, data_root: Path, force: bool = False) -> None:
    paths = load_city(city, data_root)
    enable_logging(paths.log_path)
    output = paths.outputs_dir
    output.mkdir(parents=True, exist_ok=True)
    parquet_path = output / "best_available_footprints.parquet"
    if skip_existing(parquet_path, force):
        return
    metadata = paths.metadata
    crs = metadata["analysis_crs"]
    aoi = gpd.read_parquet(paths.inputs_dir / "aoi.parquet").to_crs(crs).geometry.union_all()
    segments = gpd.read_parquet(paths.inputs_dir / "segments.parquet").to_crs(crs)

    # Step 1: load + clean both raw sources independently.
    overture = load_overture(paths.city_dir, crs, aoi)
    globfp = load_globfp(paths.city_dir, crs, aoi, metadata["bbox_wgs84"])

    # Step 2: put both sources on a common candidate schema and pool them.
    overture_selected = overture_candidates(overture, city)
    globfp_selected = globfp_candidates(globfp, city)
    provisional = gpd.GeoDataFrame(
        pd.concat([overture_selected, globfp_selected], ignore_index=True),
        geometry="geometry", crs=crs,
    )
    if provisional.empty:
        raise RuntimeError("Neither Overture nor 3D-GloBFP supplied a footprint in the AOI")

    # Step 3: pick one geometry per real-world building (highest source priority wins, then newest).
    # See footprint_overlap_resolution.resolve_overlaps_by_recency for the algorithm.
    integrated, suppressed = resolve_overlaps_by_recency(
        provisional,
        min_intersection_m2=OVERLAP_TOLERANCE_M2,
        min_smaller_overlap=OVERLAP_SMALLER_FRACTION,
    )

    # Step 4: corroboration signal (does 3D-GloBFP independently agree?).
    supported, support_edges = globfp_support(integrated, globfp)
    integrated["globfp_support"] = supported

    # Step 5: pre-raster height (native > OSM floors > 3D-GloBFP vector).
    attach_globfp_height(integrated, globfp)
    height_attributes(integrated)

    # Step 6: confidence/selection_reason/review_required.
    confidence_attributes(integrated)

    # Reporting-segment assignment (doesn't affect selection or confidence).
    assign_segments(integrated, segments)

    # Step 7: suppression audit trail.
    lineage = make_lineage(integrated, provisional, suppressed)

    integrated.to_parquet(parquet_path, index=False)
    gpkg = integrated.copy()
    for column in gpkg.select_dtypes(include=["datetimetz"]).columns:
        gpkg[column] = gpkg[column].astype("string")
    gpkg.to_file(output / "best_available_footprints.gpkg", layer="buildings", driver="GPKG")
    lineage.to_parquet(output / "footprint_lineage.parquet", index=False)
    lineage.to_csv(output / "footprint_lineage.csv", index=False)
    overview(integrated, metadata["city_name"], output / "overview.png")
    summary = {
        "city_slug": city,
        "city_name": metadata["city_name"],
        "country": metadata["country"],
        "analysis_crs": crs,
        "aoi_area_km2": metadata["aoi_area_km2"],
        "input_overture": int(len(overture)),
        "input_3d_globfp": int(len(globfp)),
        "integrated_buildings": int(len(integrated)),
        "geometry_source_counts": {
            str(key): int(value) for key, value in integrated.geometry_source.value_counts().items()
        },
        "suppressed_by_overlap_and_recency": int(len(suppressed)),
        "overlap_rule": {
            "minimum_intersection_m2": OVERLAP_TOLERANCE_M2,
            "minimum_fraction_of_smaller_footprint": OVERLAP_SMALLER_FRACTION,
            "winner": "highest source priority, then most recent footprint",
            "source_priority": RECENCY_TIEBREAK,
        },
        "globfp_support_edges": int(len(support_edges)),
        "height_available": int(integrated.height_best_m.notna().sum()),
        "review_required": int(integrated.review_required.sum()),
        "unassigned_segments": int(integrated.ANALYSIS_ID.isna().sum()),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument("--force", action="store_true")
    add_data_root_arg(parser)
    args = parser.parse_args()
    data_root = resolve_data_root(args.data_root)
    build(args.city, data_root, args.force)


if __name__ == "__main__":
    main()
