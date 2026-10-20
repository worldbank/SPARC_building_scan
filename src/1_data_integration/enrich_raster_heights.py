#!/usr/bin/env python3
"""Attach Google 2.5D, TEMPO, and GBA.Height values to integrated footprints."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from affine import Affine
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.windows import Window, from_bounds

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root


VALID_MIN_HEIGHT_M = 0.5
VALID_MAX_HEIGHT_M = 100.0


def cloud_url(uri: str) -> str:
    if uri.startswith("gs://"):
        bucket, key = uri[5:].split("/", 1)
        return f"https://storage.googleapis.com/{bucket}/{key}"
    return uri


def google_cogs(records: list[dict], aoi_bounds: tuple[float, ...]) -> list[str]:
    xmin, ymin, xmax, ymax = aoi_bounds
    urls = []
    for record in records:
        path_value = record.get("manifest_path")
        if not path_value or not Path(path_value).exists():
            continue
        manifest = json.loads(Path(path_value).read_text())
        for tileset in manifest.get("tilesets", []):
            for source in tileset.get("sources", []):
                transform = source["affineTransform"]
                dimensions = source["dimensions"]
                left = float(transform["translateX"])
                top = float(transform["translateY"])
                right = left + float(transform["scaleX"]) * int(dimensions["width"])
                bottom = top + float(transform["scaleY"]) * int(dimensions["height"])
                tx0, tx1 = sorted((left, right))
                ty0, ty1 = sorted((bottom, top))
                if tx1 <= xmin or tx0 >= xmax or ty1 <= ymin or ty0 >= ymax:
                    continue
                prefix = manifest.get("uriPrefix", "")
                for uri in source.get("uris", []):
                    urls.append(cloud_url(prefix + uri))
    return sorted(set(urls))


def point_coordinates(data: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
    points = shapely.point_on_surface(data.geometry.to_numpy())
    return shapely.get_x(points), shapely.get_y(points)


def sample_sources(
    paths: list[str], data: gpd.GeoDataFrame, bands: tuple[int, ...], downsample: int = 1
) -> tuple[list[np.ndarray], np.ndarray]:
    """Average valid samples when multiple source rasters cover a footprint point."""
    source_x, source_y = point_coordinates(data)
    totals = [np.zeros(len(data), dtype="float64") for _ in bands]
    counts = np.zeros(len(data), dtype="uint16")
    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF",
        GDAL_HTTP_MULTIPLEX="YES",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        GDAL_CACHEMAX=512,
    ):
        for path in paths:
            with rasterio.open(path) as src:
                transformer = Transformer.from_crs(data.crs, src.crs, always_xy=True)
                x, y = transformer.transform(source_x, source_y)
                inside = (
                    (x >= src.bounds.left) & (x <= src.bounds.right)
                    & (y >= src.bounds.bottom) & (y <= src.bounds.top)
                )
                indexes = np.flatnonzero(inside)
                if not len(indexes):
                    continue
                if downsample > 1:
                    raw = from_bounds(
                        float(x[indexes].min()), float(y[indexes].min()),
                        float(x[indexes].max()), float(y[indexes].max()), src.transform,
                    )
                    col0 = max(0, math.floor(raw.col_off) - 1)
                    row0 = max(0, math.floor(raw.row_off) - 1)
                    col1 = min(src.width, math.ceil(raw.col_off + raw.width) + 2)
                    row1 = min(src.height, math.ceil(raw.row_off + raw.height) + 2)
                    window = Window(col0, row0, max(1, col1 - col0), max(1, row1 - row0))
                    out_width = max(1, math.ceil(window.width / downsample))
                    out_height = max(1, math.ceil(window.height / downsample))
                    raster = src.read(
                        bands, window=window,
                        out_shape=(len(bands), out_height, out_width),
                        masked=True, resampling=Resampling.bilinear,
                    )
                    reduced_transform = src.window_transform(window) * Affine.scale(
                        window.width / out_width, window.height / out_height
                    )
                    inverse = ~reduced_transform
                    columns, rows = inverse * (x[indexes], y[indexes])
                    rows = np.clip(np.floor(rows).astype(int), 0, out_height - 1)
                    columns = np.clip(np.floor(columns).astype(int), 0, out_width - 1)
                    sampled = np.ma.asarray(raster[:, rows, columns].T)
                else:
                    coordinates = zip(x[indexes], y[indexes])
                    sampled = np.ma.asarray(
                        list(src.sample(coordinates, indexes=bands, masked=True))
                    )
                if sampled.ndim == 1:
                    sampled = sampled[:, None]
                values = np.asarray(sampled.filled(np.nan), dtype="float64")
                valid = ~np.ma.getmaskarray(sampled).any(axis=1) & np.isfinite(values).all(axis=1)
                good_indexes = indexes[valid]
                if not len(good_indexes):
                    continue
                for number in range(len(bands)):
                    totals[number][good_indexes] += values[valid, number]
                counts[good_indexes] += 1
    outputs = [np.full(len(data), np.nan, dtype="float64") for _ in bands]
    valid = counts > 0
    for output, total in zip(outputs, totals):
        output[valid] = total[valid] / counts[valid]
    return outputs, counts


def valid_height(values: np.ndarray) -> np.ndarray:
    return np.isfinite(values) & (values >= VALID_MIN_HEIGHT_M) & (values <= VALID_MAX_HEIGHT_M)


def recompute_height_attributes(data: gpd.GeoDataFrame) -> None:
    candidates = [
        ("native_geometry", "native_height_m", "high"),
        ("OSM_levels_x_3m", "height_floors_estimate_m", "medium"),
        ("3D-GloBFP_vector", "height_globfp_vector_m", "medium"),
        ("Google_2.5D_2023", "height_google_2_5d_m", "low"),
        ("GBA.Height", "height_gba_m", "low"),
        ("TEMPO_2023Q4", "height_tempo_m", "low"),
    ]
    arrays = []
    best = np.full(len(data), np.nan)
    source = np.full(len(data), None, dtype=object)
    confidence = np.full(len(data), None, dtype=object)
    for source_name, column, source_confidence in candidates:
        values = pd.to_numeric(data.get(column), errors="coerce").to_numpy(dtype="float64")
        valid = valid_height(values)
        arrays.append(np.where(valid, values, np.nan))
        take = np.isnan(best) & valid
        best[take] = values[take]
        source[take] = source_name
        confidence[take] = source_confidence
    stack = np.vstack(arrays)
    count = np.isfinite(stack).sum(axis=0)
    minimum = np.min(np.where(np.isfinite(stack), stack, np.inf), axis=0)
    maximum = np.max(np.where(np.isfinite(stack), stack, -np.inf), axis=0)
    value_range = maximum - minimum
    value_range[count < 2] = np.nan
    data["height_best_m"] = best
    data["height_source"] = source
    data["height_source_count"] = count.astype("int16")
    data["height_range_m"] = value_range
    data["height_confidence"] = confidence
    data["review_required"] = (
        data.geometry_confidence.eq("low") | ((count >= 2) & (value_range > 5))
    )


def enrich(city: str, data_root: Path, google_presence: float, tempo_density: float) -> Path:
    paths = load_city(city, data_root)
    enable_logging(paths.log_path)
    output = paths.outputs_dir
    parquet_path = output / "best_available_footprints.parquet"
    manifest_path = paths.sources_dir / "height_sources.json"
    if not parquet_path.exists():
        raise FileNotFoundError(f"Build integrated footprints first: {parquet_path}")
    if not manifest_path.exists():
        raise FileNotFoundError(f"Acquire raster height sources first: {manifest_path}")
    data = gpd.read_parquet(parquet_path)
    manifest = json.loads(manifest_path.read_text())

    if "height_best_vector_m" not in data:
        data["height_best_vector_m"] = data.height_best_m
        data["height_source_vector"] = data.height_source

    google_urls = google_cogs(manifest.get("google_2_5d", []), tuple(data.total_bounds))
    if google_urls:
        (google_height, google_presence_values), google_samples = sample_sources(
            google_urls, data, (2, 3), downsample=8
        )
        google_valid = valid_height(google_height) & (google_presence_values >= google_presence)
        google_height[~google_valid] = np.nan
    else:
        google_height = np.full(len(data), np.nan)
        google_presence_values = np.full(len(data), np.nan)
        google_samples = np.zeros(len(data), dtype="uint16")

    tempo_records = manifest.get("tempo", [])
    tempo_paths = [record.get("local_path") or record.get("cog_url") for record in tempo_records]
    tempo_paths = [value for value in tempo_paths if value]
    if tempo_paths:
        (tempo_density_values, tempo_height_normalized), tempo_samples = sample_sources(
            tempo_paths, data, (1, 2)
        )
        tempo_height = tempo_height_normalized * 100.0
        tempo_valid = valid_height(tempo_height) & (tempo_density_values >= tempo_density)
        tempo_height[~tempo_valid] = np.nan
    else:
        tempo_density_values = np.full(len(data), np.nan)
        tempo_height = np.full(len(data), np.nan)
        tempo_samples = np.zeros(len(data), dtype="uint16")

    gba_paths = [record.get("local_path") for record in manifest.get("gba_height", [])]
    gba_paths = [value for value in gba_paths if value and Path(value).exists()]
    if gba_paths:
        (gba_height,), gba_samples = sample_sources(gba_paths, data, (1,))
        gba_height[~valid_height(gba_height)] = np.nan
    else:
        gba_height = np.full(len(data), np.nan)
        gba_samples = np.zeros(len(data), dtype="uint16")

    data["height_google_2_5d_m"] = google_height
    data["google_building_presence"] = google_presence_values
    data["height_google_sample_count"] = google_samples.astype("int16")
    data["height_tempo_m"] = tempo_height
    data["tempo_building_density"] = tempo_density_values
    data["height_tempo_sample_count"] = tempo_samples.astype("int16")
    data["height_gba_m"] = gba_height
    data["height_gba_sample_count"] = gba_samples.astype("int16")
    recompute_height_attributes(data)

    data.to_parquet(parquet_path, index=False)
    gpkg = data.copy()
    for column in gpkg.select_dtypes(include=["datetimetz"]).columns:
        gpkg[column] = gpkg[column].astype("string")
    gpkg.to_file(output / "best_available_footprints.gpkg", layer="buildings", driver="GPKG")

    summary_path = output / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    summary.update({
        "height_available": int(data.height_best_m.notna().sum()),
        "review_required": int(data.review_required.sum()),
        "height_raster_enrichment": {
            "assignment": "raster value at footprint point-on-surface",
            "google_2_5d_2023_buildings": int(np.isfinite(google_height).sum()),
            "tempo_2023q4_buildings": int(np.isfinite(tempo_height).sum()),
            "gba_height_buildings": int(np.isfinite(gba_height).sum()),
            "google_presence_threshold": google_presence,
            "tempo_density_threshold": tempo_density,
        },
    })
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "assignment_method": (
            "Point-on-surface sampling; Google storage rasters read at their 4 m "
            "effective scale; overlapping raster samples averaged"
        ),
        "valid_height_range_m": [VALID_MIN_HEIGHT_M, VALID_MAX_HEIGHT_M],
        "google_2_5d": {
            "vintage": "2023-06-30", "height_band": 2, "presence_band": 3,
            "presence_threshold": google_presence, "cogs_opened": len(google_urls),
        },
        "tempo": {
            "vintage": "2023 Q4", "density_band": 1, "height_band": 2,
            "height_scale_m": 100, "density_threshold": tempo_density,
            "cogs_opened": len(tempo_paths),
        },
        "gba_height": {"height_band": 1, "local_tiles": len(gba_paths)},
        "best_height_priority": [
            "native_geometry", "OSM_levels_x_3m", "3D-GloBFP_vector",
            "Google_2.5D_2023", "GBA.Height", "TEMPO_2023Q4",
        ],
        "interpretation": (
            "Raster values describe the cell containing the footprint interior point; "
            "they are not independent per-building measurements."
        ),
    }
    (output / "height_enrichment_metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    print(json.dumps(summary.get("height_raster_enrichment", {}), indent=2))
    return parquet_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument(
        "--google-presence-threshold", type=float, default=0.5,
        help="Minimum Google building-presence value required for its height",
    )
    parser.add_argument(
        "--tempo-density-threshold", type=float, default=0.001,
        help="Minimum TEMPO building-density value required for its height",
    )
    add_data_root_arg(parser)
    args = parser.parse_args()
    if not 0 <= args.google_presence_threshold <= 1:
        raise ValueError("Google presence threshold must be between 0 and 1")
    if not 0 <= args.tempo_density_threshold <= 1:
        raise ValueError("TEMPO density threshold must be between 0 and 1")
    data_root = resolve_data_root(args.data_root)
    path = enrich(
        args.city, data_root, args.google_presence_threshold, args.tempo_density_threshold
    )
    print(f"Updated: {path}")


if __name__ == "__main__":
    main()
