#!/usr/bin/env python3
"""Build city segments for an AOI with the City Segments Layer v2 method (open re-implementation).

Example (threshold 500 people; OSM layers read straight from a Geofabrik *-latest-free.shp.zip):
    python src/9_segments/run_segments.py \\
        --aoi <aoi.gpkg> \\
        --roads /vsizip/<country>-latest-free.shp.zip/gis_osm_roads_free_1.shp \\
        --waterways /vsizip/<country>-latest-free.shp.zip/gis_osm_waterways_free_1.shp \\
        --water-polygons /vsizip/<country>-latest-free.shp.zip/gis_osm_water_a_free_1.shp \\
        --population <GHS_POP_E2025_GLOBE_R2023A_54009_100_V1_0_Rx_Cy>.tif \\
        --min-pop 500 --out <output folder>
Roads can also come from Overture Transportation: --roads <segments>.parquet --roads-format overture.
The output segments_v2.gpkg (layer "segments") can be passed to step 1 as reporting units:
    python src/1_data_integration/run_workflow.py ... --segments <out>/segments_v2.gpkg --segments-layer segments
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import geopandas as gpd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from segments_v2 import V2Settings, build_segments, roads_from_overture  # noqa: E402


def read_clipped(path: str | None, aoi: gpd.GeoDataFrame, buffer_m: float):
    """Read only features near the AOI (bbox filter in the file's CRS). Accepts GDAL paths, e.g. /vsizip/…zip/layer.shp."""
    if path is None:
        return None
    import pyogrio
    file_crs = pyogrio.read_info(path)["crs"]
    bbox = tuple(aoi.to_crs(aoi.estimate_utm_crs()).buffer(buffer_m).to_crs(file_crs).total_bounds)
    return gpd.read_file(path, bbox=bbox)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--aoi", required=True, type=Path)
    p.add_argument("--aoi-layer")
    p.add_argument("--roads", required=True, type=str,
                   help="Road lines: OSM with 'fclass' (Geofabrik gis_osm_roads_free_1), or Overture Transportation "
                        "segments GeoParquet (use --roads-format overture)")
    p.add_argument("--roads-format", choices=["osm", "overture"], default="osm")
    p.add_argument("--waterways", type=str, help="OSM waterway lines with 'fclass' (gis_osm_waterways_free_1)")
    p.add_argument("--water-polygons", type=str, help="OSM water polygons (gis_osm_water_a_free_1)")
    p.add_argument("--population", required=True, type=Path, help="Population raster (e.g. GHS-POP 2025 R2023A 100 m)")
    p.add_argument("--min-pop", type=float, default=500.0, help="Minimum estimated population per segment (default 500; CIESIN v2 used 400)")
    p.add_argument("--major-min-pop", type=float, default=1000.0, help="Minimum population per major zone (default 1000, as CIESIN v2)")
    p.add_argument("--crs", help="Analysis CRS (default: local UTM from the AOI centroid)")
    p.add_argument("--out", required=True, type=Path)
    a = p.parse_args()

    t0 = time.time()
    a.out.mkdir(parents=True, exist_ok=True)
    aoi = gpd.read_file(a.aoi, layer=a.aoi_layer)
    aoi["geometry"] = aoi.geometry.force_2d()
    s = V2Settings(min_pop=a.min_pop, major_zone_min_pop=a.major_min_pop)
    if s.min_pop > s.major_zone_min_pop:
        print(f"WARNING: --min-pop ({s.min_pop}) > --major-min-pop ({s.major_zone_min_pop}); some segments may stay below min-pop")
    res = build_segments(
        aoi,
        roads_from_overture(gpd.read_parquet(a.roads)) if a.roads_format == "overture" else read_clipped(a.roads, aoi, 500),
        str(a.population),
        read_clipped(a.waterways, aoi, s.water_clip_buffer_m),
        read_clipped(a.water_polygons, aoi, s.water_clip_buffer_m),
        s, a.crs)

    gpkg = a.out / "segments_v2.gpkg"
    for layer in ("segments", "major_zones", "blocks"):
        res[layer].to_file(gpkg, layer=layer, driver="GPKG")
    manifest = {
        "method": "City Segments Layer v2 (CIESIN 2026, doi:10.7910/DVN/FFS06X) — open re-implementation, src/9_segments/segments_v2.py",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_s": round(time.time() - t0, 1),
        "inputs": {k: str(v) if v else None for k, v in
                   {"aoi": a.aoi, "roads": a.roads, "roads_format": a.roads_format, "waterways": a.waterways, "water_polygons": a.water_polygons,
                    "population": a.population}.items()},
        "stats": res["stats"],
        "outputs": {"gpkg": str(gpkg), "layers": ["segments", "major_zones", "blocks"]},
        "environment": {"python": platform.python_version(),
                        **{pkg: version(pkg) for pkg in ("geopandas", "shapely", "rasterio", "numpy", "pyogrio")}},
    }
    (a.out / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in res["stats"].items() if k != "settings"}, indent=2))


if __name__ == "__main__":
    main()
