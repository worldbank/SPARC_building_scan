#!/usr/bin/env python3
"""Flag segments whose building footprints are likely out of date, from the
per-segment baseline vs. current WSF-settled area computed in stage 2.

A segment is flagged when ALL of:
  - change_pct   > --change-pct-threshold   (share of today's settled area that postdates baseline)
  - change_abs_m2 > --change-abs-m2-threshold (absolute growth, guards against tiny segments)
  - building_count >= --min-building-count   ("non-trivial existing building footprint coverage")

The %-change and coverage thresholds are temporary placeholders (per spec:
TBC) -- the absolute-area threshold (10,000 m^2) is the one concrete value
given in the spec.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from city_workspace import add_data_root_arg, enable_logging, load_city, resolve_data_root, skip_existing  # noqa: E402


CHANGE_PCT_THRESHOLD = 0.15  # placeholder; spec says 10-25%, TBC
CHANGE_ABS_M2_THRESHOLD = 10_000.0  # spec's stated value
MIN_BUILDING_COUNT = 10  # placeholder for "non-trivial existing building footprint coverage"


def run(city, args) -> None:
    input_gpkg = city.sources_dir / "segments" / f"{city.slug}_segments_settled_area.gpkg"
    if not input_gpkg.exists():
        raise FileNotFoundError(f"{input_gpkg} not found. Run 2_compute_settled_change.py first.")
    output_gpkg = city.outputs_dir / "outdated_segments.gpkg"
    if skip_existing(output_gpkg, args.force):
        return

    segments = gpd.read_file(input_gpkg, layer="segments_settled_area")
    print(f"Segments loaded: {len(segments):,}")

    has_baseline = segments["baseline_settled_m2"].notna() & (segments["current_settled_m2"] > 0)
    segments["change_pct"] = np.nan
    segments["change_abs_m2"] = np.nan
    segments.loc[has_baseline, "change_abs_m2"] = (
        segments.loc[has_baseline, "current_settled_m2"] - segments.loc[has_baseline, "baseline_settled_m2"]
    )
    segments.loc[has_baseline, "change_pct"] = (
        segments.loc[has_baseline, "change_abs_m2"] / segments.loc[has_baseline, "current_settled_m2"]
    )

    non_trivial_coverage = segments["building_count"] >= args.min_building_count
    exceeds_pct = has_baseline & (segments["change_pct"] > args.change_pct_threshold)
    exceeds_abs = has_baseline & (segments["change_abs_m2"] > args.change_abs_m2_threshold)

    segments["outdated"] = exceeds_pct & exceeds_abs & non_trivial_coverage

    print(f"Segments with a computable change: {has_baseline.sum():,} / {len(segments):,}")
    print(f"Segments meeting non-trivial coverage (>= {args.min_building_count} buildings): {non_trivial_coverage.sum():,}")
    print(f"Segments exceeding %-change threshold (> {args.change_pct_threshold:.0%}): {exceeds_pct.sum():,}")
    print(f"Segments exceeding absolute-area threshold (> {args.change_abs_m2_threshold:,.0f} m^2): {exceeds_abs.sum():,}")
    print(f"Segments flagged outdated (all three): {segments['outdated'].sum():,}")

    segments.to_file(output_gpkg, layer="outdated_segments", driver="GPKG")
    print(f"Saved: {output_gpkg}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--change-pct-threshold", type=float, default=CHANGE_PCT_THRESHOLD)
    parser.add_argument("--change-abs-m2-threshold", type=float, default=CHANGE_ABS_M2_THRESHOLD)
    parser.add_argument("--min-building-count", type=int, default=MIN_BUILDING_COUNT)
    add_data_root_arg(parser)
    args = parser.parse_args()

    data_root = resolve_data_root(args.data_root)
    city = load_city(args.city, data_root)
    enable_logging(city.log_path)
    run(city, args)


if __name__ == "__main__":
    main()
