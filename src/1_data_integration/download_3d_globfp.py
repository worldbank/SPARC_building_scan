#!/usr/bin/env python3
"""Download the official 3D-GloBFP tiles intersecting one prepared city AOI."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

import requests

from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root


FIGSHARE_ARTICLE_IDS = [
    28879733, 28881749, 28882700, 28889813, 28890593,
    28891631, 28903454, 28903853, 28904453, 28906499,
]
TILE_NAME = re.compile(
    r"^(?P<grid_id>\d+)_"
    r"(?P<west>-?\d+(?:\.\d+)?)_(?P<south>-?\d+(?:\.\d+)?)_"
    r"(?P<east>-?\d+(?:\.\d+)?)_(?P<north>-?\d+(?:\.\d+)?)_.*\.zip$"
)


def intersects(bounds, aoi_bounds) -> bool:
    west, south, east, north = bounds
    awest, asouth, aeast, anorth = aoi_bounds
    return min(east, aeast) > max(west, awest) and min(north, anorth) > max(south, asouth)


def md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = (destination / member.filename).resolve()
            if destination not in target.parents and target != destination:
                raise ValueError(f"Unsafe path in {archive.name}: {member.filename}")
        source.extractall(destination)


def catalog(session: requests.Session) -> list[dict]:
    records = []
    for article_id in FIGSHARE_ARTICLE_IDS:
        response = session.get(
            f"https://api.figshare.com/v2/articles/{article_id}", timeout=120
        )
        response.raise_for_status()
        article = response.json()
        for item in article.get("files", []):
            match = TILE_NAME.match(item["name"])
            if not match:
                continue
            values = match.groupdict()
            records.append({
                "article_id": article_id,
                "article_doi": article.get("doi"),
                "grid_id": int(values["grid_id"]),
                "filename": item["name"],
                "bounds_wgs84": [
                    float(values["west"]), float(values["south"]),
                    float(values["east"]), float(values["north"]),
                ],
                "download_url": item["download_url"],
                "bytes": int(item["size"]),
                "md5": item.get("supplied_md5") or item.get("computed_md5"),
            })
    by_grid = {}
    for record in records:
        by_grid[record["grid_id"]] = record
    return list(by_grid.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument("--force", action="store_true")
    add_data_root_arg(parser)
    args = parser.parse_args()

    data_root = resolve_data_root(args.data_root)
    city = load_city(args.city, data_root)
    enable_logging(city.log_path)
    source_root = city.sources_dir
    download_root = source_root / "globfp_downloads"
    tile_root = source_root / "globfp_tiles"
    for directory in (download_root, tile_root):
        directory.mkdir(parents=True, exist_ok=True)

    with requests.Session() as session:
        session.headers["User-Agent"] = "best-available-footprints-workflow/1.0"
        selected = [
            record for record in catalog(session)
            if intersects(record["bounds_wgs84"], city.metadata["bbox_wgs84"])
        ]
        if not selected:
            raise RuntimeError("No 3D-GloBFP tiles intersect the AOI")
        for number, record in enumerate(sorted(selected, key=lambda x: x["grid_id"]), 1):
            archive = download_root / record["filename"]
            destination = tile_root / f"tile_{record['grid_id']}"
            valid_existing = (
                archive.exists() and record["md5"] and md5(archive) == record["md5"]
            )
            if args.force or not valid_existing:
                partial = archive.with_suffix(".partial")
                with session.get(record["download_url"], stream=True, timeout=(60, 600)) as response:
                    response.raise_for_status()
                    with partial.open("wb") as output:
                        shutil.copyfileobj(response.raw, output)
                if record["md5"] and md5(partial) != record["md5"]:
                    partial.unlink(missing_ok=True)
                    raise ValueError(f"Checksum failed: {record['filename']}")
                partial.replace(archive)
            if args.force and destination.exists():
                shutil.rmtree(destination)
            if not destination.exists():
                destination.mkdir(parents=True)
                safe_extract(archive, destination)
            record["archive"] = str(archive)
            record["directory"] = str(destination)
            print(f"[{number}/{len(selected)}] {record['filename']}", flush=True)

    manifest = source_root / "globfp_tiles.json"
    manifest.write_text(json.dumps(selected, indent=2) + "\n")
    print(f"Wrote: {manifest}")


if __name__ == "__main__":
    main()
