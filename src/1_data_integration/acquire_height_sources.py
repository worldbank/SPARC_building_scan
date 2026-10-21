#!/usr/bin/env python3
"""Resolve the raster height sources needed for one prepared city.

Google Open Buildings 2.5D and TEMPO are retained as cloud COG URLs. GBA.Height
tiles are selectively extracted from the official mediaTUM ZIP distribution.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import shutil
import struct
import subprocess
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

import fsspec
import geopandas as gpd
import requests
import shapely
from s2sphere import LatLng, LatLngRect, RegionCoverer

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root


GOOGLE_MANIFEST_ROOT = (
    "https://storage.googleapis.com/open-buildings-temporal-data/v1/manifests"
)
TEMPO_INDEX_URL = "https://opendata.aiforgood.ai/building-density/tile_index.gpkg"
GBA_FTP_HOST = "dataserv.ub.tum.de"
GBA_FULL_USER = os.environ.get("GBA_FTP_USER", "m1782307")
GBA_FULL_PASSWORD = os.environ.get("GBA_FTP_PASSWORD", "m1782307")
GBA_REP_USER = os.environ.get("GBA_REP_FTP_USER", "m1782307.rep")
GBA_REP_PASSWORD = os.environ.get("GBA_REP_FTP_PASSWORD", "m1782307.rep")
GBA_INDEX_FILES = {
    "height_tif.geojson": "767f26324724e3bee832466273efa51bfc6dd011a8d41cb25d65bb71f90986aaf76363a946fa724723f8400cf415e3239969cf0628edb180f2fa1e77ed3408ae",
    "height_zip.geojson": "9a58cb51e65c6979184cfab7802c9e0d8ecd715a37127bbdb8ecafb50c5b0887087b2f4d970d9cd5b7b7b158328cd596b9015ca40d8ccc8827e9cef5c0b351fc",
}


def digest(path: Path, algorithm: str = "sha512") -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def download_http(session: requests.Session, url: str, path: Path, force: bool = False) -> None:
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    with session.get(url, stream=True, timeout=(60, 600)) as response:
        response.raise_for_status()
        with temporary.open("wb") as target:
            shutil.copyfileobj(response.raw, target)
    temporary.replace(path)


def google_tokens(bounds: list[float]) -> list[str]:
    west, south, east, north = bounds
    if west > east:
        raise ValueError("AOIs crossing the antimeridian are not supported")
    rectangle = LatLngRect.from_point_pair(
        LatLng.from_degrees(south, west), LatLng.from_degrees(north, east)
    )
    coverer = RegionCoverer()
    coverer.min_level = 2
    coverer.max_level = 2
    coverer.max_cells = 64
    return sorted(cell.to_token() for cell in coverer.get_covering(rectangle))


def acquire_google(
    session: requests.Session, city_root: Path, metadata: dict, force: bool
) -> list[dict]:
    epsg = gpd.read_parquet(city_root / "inputs/aoi.parquet").crs.to_epsg()
    if epsg is None:
        raise ValueError("Google manifest access requires an EPSG analysis CRS")
    directory = city_root / "sources/google_2_5d/manifests"
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for token in google_tokens(metadata["bbox_wgs84"]):
        filename = f"{token}_EPSG_{epsg}_2023_06_30.json"
        url = f"{GOOGLE_MANIFEST_ROOT}/{filename}"
        path = directory / filename
        status = "available"
        if force or not path.exists():
            response = session.get(url, timeout=120)
            if response.status_code == 404:
                if force:
                    path.unlink(missing_ok=True)
                status = "not_covered"
            else:
                response.raise_for_status()
                path.write_bytes(response.content)
        records.append({
            "s2cell_token": token,
            "manifest_url": url,
            "manifest_path": str(path) if path.exists() else None,
            "status": status if not path.exists() else "available",
        })
    return records


def acquire_tempo(
    session: requests.Session, city_root: Path, aoi_wgs84, force: bool, download_cogs: bool,
    data_root: Path,
) -> list[dict]:
    index_path = data_root / "shared/tempo/tile_index.gpkg"
    download_http(session, TEMPO_INDEX_URL, index_path, force)
    index = gpd.read_file(index_path)
    aoi = gpd.GeoSeries([aoi_wgs84], crs=4326).to_crs(index.crs).iloc[0]
    candidates = index.iloc[index.sindex.query(aoi, predicate="intersects")].copy()
    candidates = candidates.loc[
        shapely.area(shapely.intersection(candidates.geometry.to_numpy(), aoi)) > 0
    ]
    if "data_2023q4" not in candidates:
        raise ValueError("TEMPO tile index lacks the data_2023q4 field")
    records = []
    local_root = city_root / "sources/tempo_2023q4"
    for row in candidates.itertuples(index=False):
        url = row.data_2023q4
        if not isinstance(url, str) or not url:
            continue
        filename = Path(url).name
        local_path = local_root / filename
        if download_cogs:
            download_http(session, url, local_path, force)
        records.append({
            "filename": filename,
            "cog_url": url,
            "local_path": str(local_path) if local_path.exists() else None,
        })
    return records


def download_gba_indexes(force: bool, data_root: Path) -> tuple[Path, Path]:
    directory = data_root / "shared/gba_height/index"
    directory.mkdir(parents=True, exist_ok=True)
    for filename, expected in GBA_INDEX_FILES.items():
        path = directory / filename
        if force or not path.exists() or digest(path) != expected:
            temporary = path.with_suffix(path.suffix + ".partial")
            subprocess.run([
                "curl", "-L", "--fail", "--silent", "--show-error", "--retry", "6",
                "--user", f"{GBA_REP_USER}:{GBA_REP_PASSWORD}",
                f"ftp://{GBA_FTP_HOST}/{filename}", "-o", str(temporary),
            ], check=True)
            if digest(temporary) != expected:
                temporary.unlink(missing_ok=True)
                raise ValueError(f"Checksum failed for official GBA index {filename}")
            temporary.replace(path)
    return directory / "height_tif.geojson", directory / "height_zip.geojson"


def curl_range(url: str, username: str, password: str, start: int, end: int, path: Path) -> None:
    subprocess.run([
        "curl", "-L", "--fail", "--silent", "--show-error", "--retry", "6",
        "--retry-delay", "3", "--user", f"{username}:{password}",
        "--range", f"{start}-{end}", url, "-o", str(path),
    ], check=True)


def extract_member(url: str, info: zipfile.ZipInfo, output: Path) -> None:
    header_path = output.with_suffix(output.suffix + ".header.partial")
    compressed_path = output.with_suffix(output.suffix + ".compressed.partial")
    temporary = output.with_suffix(output.suffix + ".partial")
    try:
        curl_range(
            url, GBA_FULL_USER, GBA_FULL_PASSWORD,
            info.header_offset, info.header_offset + 65535, header_path,
        )
        header = header_path.read_bytes()
        if len(header) < 30 or header[:4] != b"PK\x03\x04":
            raise RuntimeError(f"Invalid ZIP header for {info.filename}")
        fields = struct.unpack("<4s5H3I2H", header[:30])
        filename_length, extra_length = fields[-2:]
        start = info.header_offset + 30 + filename_length + extra_length
        curl_range(
            url, GBA_FULL_USER, GBA_FULL_PASSWORD,
            start, start + info.compress_size - 1, compressed_path,
        )
        decompressor = (
            zlib.decompressobj(-zlib.MAX_WBITS)
            if info.compress_type == zipfile.ZIP_DEFLATED else None
        )
        if info.compress_type not in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
            raise ValueError(f"Unsupported ZIP compression type {info.compress_type}")
        crc = 0
        written = 0
        with compressed_path.open("rb") as source, temporary.open("wb") as target:
            for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                data = decompressor.decompress(block) if decompressor else block
                target.write(data)
                crc = binascii.crc32(data, crc)
                written += len(data)
            if decompressor:
                data = decompressor.flush()
                target.write(data)
                crc = binascii.crc32(data, crc)
                written += len(data)
        if written != info.file_size or (crc & 0xFFFFFFFF) != info.CRC:
            raise RuntimeError(f"Size or CRC mismatch for {info.filename}")
        temporary.replace(output)
    finally:
        for path in (header_path, compressed_path, temporary):
            path.unlink(missing_ok=True)


def acquire_gba(city_root: Path, aoi_wgs84, force: bool, data_root: Path) -> list[dict]:
    fine_path, archive_path = download_gba_indexes(force=force, data_root=data_root)
    index = gpd.read_file(fine_path)
    aoi = gpd.GeoSeries([aoi_wgs84], crs=4326).to_crs(index.crs).iloc[0]
    selected = index.iloc[index.sindex.query(aoi, predicate="intersects")].copy()
    selected = selected.loc[
        shapely.area(shapely.intersection(selected.geometry.to_numpy(), aoi)) > 0
    ]
    archive_index = gpd.read_file(archive_path).set_index("path")
    tile_root = city_root / "sources/gba_height"
    tile_root.mkdir(parents=True, exist_ok=True)
    records = []
    for parent, group in selected.groupby("zipfile_path", sort=True):
        remote_relative = parent.removeprefix("./")
        remote_path = "/FD_Server_5/m1782307/" + remote_relative
        ftp_url = f"ftp://{GBA_FTP_HOST}/{remote_relative}"
        requested = {Path(value).name: value for value in group.path}
        filesystem = fsspec.filesystem(
            "ftp", host=GBA_FTP_HOST, username=GBA_FULL_USER,
            password=GBA_FULL_PASSWORD, block_size=8 * 1024 * 1024, timeout=300,
        )
        with filesystem.open(remote_path, "rb") as remote:
            with zipfile.ZipFile(remote) as archive:
                missing = set(requested) - set(archive.namelist())
                if missing:
                    raise RuntimeError(f"GBA members missing from {parent}: {sorted(missing)}")
                for filename, official_path in requested.items():
                    info = archive.getinfo(filename)
                    output = tile_root / filename
                    if force or not output.exists() or output.stat().st_size != info.file_size:
                        extract_member(ftp_url, info, output)
                    records.append({
                        "filename": filename,
                        "official_index_path": official_path,
                        "parent_archive": parent,
                        "parent_sha512": archive_index.loc[parent, "SHA512"],
                        "local_path": str(output),
                        "bytes": info.file_size,
                        "zip_crc32": f"{info.CRC:08x}",
                        "sha256": digest(output, "sha256"),
                    })
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--download-tempo", action="store_true",
        help="Download TEMPO COGs instead of retaining official cloud URLs",
    )
    parser.add_argument("--skip-google", action="store_true")
    parser.add_argument("--skip-tempo", action="store_true")
    parser.add_argument("--skip-gba", action="store_true")
    add_data_root_arg(parser)
    args = parser.parse_args()

    data_root = resolve_data_root(args.data_root)
    city = load_city(args.city, data_root)
    enable_logging(city.log_path)
    aoi_wgs84 = gpd.read_parquet(city.inputs_dir / "aoi.parquet").to_crs(4326).geometry.union_all()
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "city_slug": args.city,
        "google_2_5d": [], "tempo": [], "gba_height": [],
        "source_notes": {
            "google_2_5d": "2023-06-30 manifests; cloud COGs; bands 1 count, 2 height m, 3 presence",
            "tempo": "2023 Q4; band 1 density, band 2 normalized height multiplied by 100 m",
            "gba_height": "Official 3 m GeoTIFF members selectively extracted from mediaTUM ZIPs",
        },
    }
    with requests.Session() as session:
        session.headers["User-Agent"] = "best-available-footprints-workflow/2.0"
        if not args.skip_google:
            result["google_2_5d"] = acquire_google(session, city.city_dir, city.metadata, args.force)
        if not args.skip_tempo:
            result["tempo"] = acquire_tempo(
                session, city.city_dir, aoi_wgs84, args.force, args.download_tempo, data_root
            )
    if not args.skip_gba:
        result["gba_height"] = acquire_gba(city.city_dir, aoi_wgs84, args.force, data_root)
    manifest = city.sources_dir / "height_sources.json"
    manifest.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: len(result[key]) for key in ("google_2_5d", "tempo", "gba_height")}, indent=2))
    print(f"Wrote: {manifest}")


if __name__ == "__main__":
    main()
