#!/usr/bin/env python3
"""Run the full improving_overture pipeline for one prepared city."""

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
        return f"./{Path(value).relative_to(WORKFLOW)}"
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
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ee-project", help="Earth Engine Cloud project id")
    parser.add_argument("--skip-gee", action="store_true", help="Skip stage 0 (reuse existing GEE extraction)")
    add_data_root_arg(parser)
    args = parser.parse_args()

    data_root = resolve_data_root(args.data_root)
    enable_logging(data_root / args.city / LOG_FILENAME)

    if not args.skip_gee:
        command = [
            sys.executable, str(SCRIPTS / "0_get_gee_data.py"), "--city", args.city,
            "--data-root", str(data_root),
        ]
        if args.ee_project:
            command.extend(["--ee-project", args.ee_project])
        if args.force:
            command.append("--force")
        run(command)

    command = [
        sys.executable, str(SCRIPTS / "1_detect_outliers.py"), "--city", args.city,
        "--data-root", str(data_root),
    ]
    if args.force:
        command.append("--force")
    run(command)

    command = [
        sys.executable, str(SCRIPTS / "2_drop_false_positives.py"), "--city", args.city,
        "--data-root", str(data_root),
    ]
    if args.force:
        command.append("--force")
    run(command)

    command = [
        sys.executable, str(SCRIPTS / "3_finalize_buildings.py"), "--city", args.city,
        "--data-root", str(data_root),
    ]
    if args.force:
        command.append("--force")
    run(command)

    print(f"\n--- run_workflow.py: finished ---\nFinished: {data_root / args.city / 'outputs' / 'best_available_footprints_cleaned.gpkg'}")


if __name__ == "__main__":
    main()
