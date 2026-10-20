#!/usr/bin/env python3
"""Prepare an arbitrary city AOI and optional reporting segments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely

from city_workspace import LOG_FILENAME, add_data_root_arg, enable_logging, resolve_data_root, slugify


def read_vector(path: Path, layer: str | None = None) -> gpd.GeoDataFrame:
    if path.suffix.lower() in {".parquet", ".geoparquet"}:
        return gpd.read_parquet(path)
    kwargs = {"layer": layer} if layer else {}
    return gpd.read_file(path, **kwargs)


def polygonal(data: gpd.GeoDataFrame, label: str) -> gpd.GeoDataFrame:
    if data.crs is None:
        raise ValueError(f"{label} has no coordinate reference system")
    data = data.loc[data.geometry.notna() & ~data.geometry.is_empty].copy()
    if data.empty:
        raise ValueError(f"{label} contains no usable geometry")
    invalid = ~shapely.is_valid(data.geometry.to_numpy())
    if invalid.any():
        data.loc[invalid, "geometry"] = shapely.make_valid(
            data.loc[invalid, "geometry"].to_numpy()
        )
    data = data.loc[np.isin(shapely.get_type_id(data.geometry.to_numpy()), [3, 6])]
    if data.empty:
        raise ValueError(f"{label} contains no polygon geometry")
    return data.reset_index(drop=True)


def local_utm_epsg(aoi_wgs84) -> int:
    point = aoi_wgs84.representative_point()
    zone = max(1, min(60, int((point.x + 180) // 6) + 1))
    return (32600 if point.y >= 0 else 32700) + zone


def prepare(args) -> Path:
    slug = args.city_slug or slugify(args.city_name)
    if not slug:
        raise ValueError("City slug is empty")
    data_root = resolve_data_root(args.data_root)
    city_root = data_root / slug
    inputs = city_root / "inputs"
    sources = city_root / "sources"
    output = city_root / "outputs"
    for directory in (inputs, sources, output):
        directory.mkdir(parents=True, exist_ok=True)
    enable_logging(city_root / LOG_FILENAME)

    aoi_source = polygonal(read_vector(Path(args.aoi), args.aoi_layer), "AOI")
    aoi_wgs84 = aoi_source.to_crs(4326).geometry.union_all()
    analysis_crs = args.analysis_crs or f"EPSG:{local_utm_epsg(aoi_wgs84)}"
    aoi_projected = gpd.GeoDataFrame(
        {"city_slug": [slug], "city_name": [args.city_name], "country": [args.country]},
        geometry=[aoi_wgs84], crs=4326,
    ).to_crs(analysis_crs)
    aoi_geometry = aoi_projected.geometry.iloc[0]

    if args.segments:
        segments = polygonal(
            read_vector(Path(args.segments), args.segments_layer), "Segments"
        ).to_crs(analysis_crs)
        segments = segments.loc[segments.intersects(aoi_geometry)].copy()
        segments.geometry = segments.geometry.intersection(aoi_geometry)
        segments = segments.loc[
            ~segments.geometry.is_empty
            & np.isin(shapely.get_type_id(segments.geometry.to_numpy()), [3, 6])
            & (shapely.area(segments.geometry.to_numpy()) > 0)
        ].reset_index(drop=True)
        if segments.empty:
            raise ValueError("No segment polygon has positive-area overlap with the AOI")
    else:
        segments = gpd.GeoDataFrame(
            {"segment_name": [args.city_name]}, geometry=[aoi_geometry], crs=analysis_crs
        )
    segments = segments.drop(columns=["ANALYSIS_ID", "SEGMENT_UID"], errors="ignore")
    segments.insert(0, "ANALYSIS_ID", np.arange(1, len(segments) + 1, dtype="int32"))
    segments["SEGMENT_UID"] = slug + "-" + segments.ANALYSIS_ID.astype(str)
    segments["analysis_area_ha"] = shapely.area(segments.geometry.to_numpy()) / 10_000

    aoi_projected.to_parquet(inputs / "aoi.parquet", index=False)
    aoi_projected.to_crs(4326).to_file(inputs / "aoi.geojson", driver="GeoJSON")
    segments.to_parquet(inputs / "segments.parquet", index=False)
    metadata = {
        "city_slug": slug,
        "city_name": args.city_name,
        "country": args.country,
        "data_root": str(data_root),
        "analysis_crs": str(aoi_projected.crs),
        "aoi_area_km2": float(aoi_geometry.area / 1e6),
        "bbox_wgs84": list(map(float, aoi_projected.to_crs(4326).total_bounds)),
        "segments": int(len(segments)),
        "aoi_source": str(Path(args.aoi).resolve()),
        "segments_source": str(Path(args.segments).resolve()) if args.segments else None,
    }
    path = inputs / "metadata.json"
    path.write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city-name", required=True)
    parser.add_argument("--country", default="")
    parser.add_argument("--city-slug")
    parser.add_argument("--aoi", required=True)
    parser.add_argument("--aoi-layer")
    parser.add_argument("--segments")
    parser.add_argument("--segments-layer")
    parser.add_argument("--analysis-crs", help="Projected CRS, for example EPSG:32636")
    add_data_root_arg(parser)
    prepare(parser.parse_args())


if __name__ == "__main__":
    main()
