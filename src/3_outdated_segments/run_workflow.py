#!/usr/bin/env python3
"""Run the full outdated-segments pipeline for one prepared city."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from city_workspace import LOG_FILENAME, add_data_root_arg, enable_logging, resolve_data_root


SCRIPTS = Path(__file__).resolve().parent
WORKFLOW = SCRIPTS.parent


def display_path(value: str) -> str:
    try:
        return f"./{Path(value).resolve().relative_to(WORKFLOW)}"
    except ValueError:
        return value


def format_command(command: list[str]) -> str:
    lines = [" ".join(display_path(token) for token in command[:2])]
    index = 2
    while index < len(command):
        token = command[index]
        has_value = index + 1 < len(command) and not command[index + 1].startswith("--")
        if has_value:
            lines.append(f"{token} {command[index + 1]}")
            index += 2
        else:
            lines.append(token)
            index += 1
    return " \\\n    ".join(lines)


def run(command: list[str]) -> None:
    print(f"\nRunning:\n    {format_command(command)}", flush=True)
    completed = subprocess.run(command, cwd=WORKFLOW)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True, help="Prepared city slug")
    parser.add_argument("--segments", required=True, help="Path to an external segments polygon file")
    parser.add_argument("--segments-layer", help="Layer name, if the segments file has multiple layers")
    parser.add_argument("--segments-id-field", help="Segment ID column (default: auto-detect, else assign)")
    parser.add_argument("--change-pct-threshold", type=float)
    parser.add_argument("--change-abs-m2-threshold", type=float)
    parser.add_argument("--min-building-count", type=int)
    parser.add_argument("--force", action="store_true")
    add_data_root_arg(parser)
    args = parser.parse_args()

    data_root = resolve_data_root(args.data_root)
    enable_logging(data_root / args.city / LOG_FILENAME)

    command = [sys.executable, str(SCRIPTS / "0_download_wsf.py"), "--city", args.city, "--data-root", str(data_root)]
    if args.force:
        command.append("--force")
    run(command)

    command = [
        sys.executable, str(SCRIPTS / "1_sample_building_wsf_year.py"), "--city", args.city,
        "--data-root", str(data_root), "--segments", args.segments,
    ]
    if args.segments_layer:
        command.extend(["--segments-layer", args.segments_layer])
    if args.segments_id_field:
        command.extend(["--segments-id-field", args.segments_id_field])
    if args.force:
        command.append("--force")
    run(command)

    command = [sys.executable, str(SCRIPTS / "2_compute_settled_change.py"), "--city", args.city, "--data-root", str(data_root)]
    if args.force:
        command.append("--force")
    run(command)

    command = [sys.executable, str(SCRIPTS / "3_flag_outdated_segments.py"), "--city", args.city, "--data-root", str(data_root)]
    if args.change_pct_threshold is not None:
        command.extend(["--change-pct-threshold", str(args.change_pct_threshold)])
    if args.change_abs_m2_threshold is not None:
        command.extend(["--change-abs-m2-threshold", str(args.change_abs_m2_threshold)])
    if args.min_building_count is not None:
        command.extend(["--min-building-count", str(args.min_building_count)])
    if args.force:
        command.append("--force")
    run(command)

    print(f"\n--- run_workflow.py: finished ---\nFinished: {data_root / args.city / 'outputs' / 'outdated_segments.gpkg'}")


if __name__ == "__main__":
    main()
