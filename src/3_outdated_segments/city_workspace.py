"""Shared helpers for locating a prepared city's data under a user-controlled root."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DATA_ROOT_ENV_VAR = "BAFW_DATA_ROOT"
LOG_FILENAME = "workflow.log"


def slugify(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()


def resolve_data_root(cli_value: str | None) -> Path:
    value = cli_value or os.environ.get(DATA_ROOT_ENV_VAR)
    if not value:
        raise SystemExit(
            f"No data root configured. Pass --data-root or set {DATA_ROOT_ENV_VAR}."
        )
    return Path(value).expanduser().resolve()


def add_data_root_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-root",
        help=f"Root directory for prepared city data (or set {DATA_ROOT_ENV_VAR})",
    )


@dataclass
class CityPaths:
    slug: str
    data_root: Path
    city_dir: Path
    inputs_dir: Path
    sources_dir: Path
    outputs_dir: Path
    log_path: Path
    metadata: dict


def load_city(slug: str, data_root: Path) -> CityPaths:
    normalized = slugify(slug)
    if normalized != slug:
        print(f"Normalized city slug {slug!r} -> {normalized!r}")
    slug = normalized
    city_dir = data_root / slug
    metadata_path = city_dir / "inputs/metadata.json"
    if not metadata_path.exists():
        raise SystemExit(
            f"No prepared city at {city_dir}. Run prepare_city.py --city-slug {slug} "
            f"--data-root {data_root} first."
        )
    metadata = json.loads(metadata_path.read_text())
    return CityPaths(
        slug=slug,
        data_root=data_root,
        city_dir=city_dir,
        inputs_dir=city_dir / "inputs",
        sources_dir=city_dir / "sources",
        outputs_dir=city_dir / "outputs",
        log_path=city_dir / LOG_FILENAME,
        metadata=metadata,
    )


class _Tee:
    """Writes to multiple streams, e.g. the real terminal and a log file."""

    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> None:
        for stream in self._streams:
            stream.write(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def enable_logging(log_path: Path) -> None:
    """Mirror stdout/stderr to log_path (appended) in addition to the terminal."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", buffering=1)
    label = Path(sys.argv[0]).name
    log_file.write(f"\n=== {label} :: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')} ===\n")
    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)


def skip_existing(path: Path, force: bool) -> bool:
    if path.exists() and not force:
        print(f"Existing: {path}")
        return True
    return False
