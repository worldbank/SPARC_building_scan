"""Resolve footprint conflicts with a source-priority-then-recency greedy policy."""

from __future__ import annotations

import numpy as np
import pandas as pd
import shapely
import geopandas as gpd


def resolve_overlaps_by_recency(
    footprints,
    *,
    min_intersection_m2: float = 0.01,
    min_smaller_overlap: float = 0.20,
    query_chunk: int = 100_000,
):
    """Return overlap-filtered footprints and a suppression-lineage table."""
    required = {
        "footprint_date", "recency_tiebreak_priority", "integrated_id",
        "area_m2", "geometry",
    }
    missing = required.difference(footprints.columns)
    if missing:
        raise ValueError(f"Missing overlap-resolution columns: {sorted(missing)}")
    if footprints.empty:
        return footprints.copy(), pd.DataFrame(columns=[
            "suppressed_index", "retained_index", "suppressed_integrated_id",
            "retained_integrated_id", "intersection_m2", "suppressed_coverage",
            "retained_coverage", "smaller_overlap", "relation",
        ])

    data = footprints.reset_index(drop=True).copy()
    dates = pd.to_datetime(data.footprint_date, errors="coerce", utc=True)
    priority = pd.to_numeric(
        data.recency_tiebreak_priority, errors="coerce"
    ).fillna(0).to_numpy(dtype="int64")
    ids = data.integrated_id.astype(str).to_numpy()
    order = pd.DataFrame({
        "date": dates, "priority": priority, "integrated_id": ids,
    }).sort_values(
        ["priority", "date", "integrated_id"],
        ascending=[False, False, True], na_position="last", kind="stable",
    ).index.to_numpy(dtype="int64")
    position = np.empty(len(data), dtype="int64")
    position[order] = np.arange(len(data), dtype="int64")

    adjacency: list[list[tuple[int, float]]] = [[] for _ in range(len(data))]
    geometries = data.geometry.to_numpy()
    areas = data.area_m2.to_numpy(dtype="float64")
    spatial_index = data.sindex
    for start in range(0, len(data), query_chunk):
        stop = min(start + query_chunk, len(data))
        pairs = spatial_index.query(geometries[start:stop], predicate="intersects")
        if pairs.size == 0:
            continue
        left = pairs[0].astype("int64") + start
        right = pairs[1].astype("int64")
        unique_pair = left < right
        left, right = left[unique_pair], right[unique_pair]
        if not len(left):
            continue
        intersection = shapely.area(shapely.intersection(
            geometries[left], geometries[right]
        ))
        smaller_area = np.minimum(areas[left], areas[right])
        smaller_overlap = intersection / np.maximum(smaller_area, 1e-9)
        conflict = (
            np.isfinite(intersection)
            & (intersection >= min_intersection_m2)
            & (smaller_overlap >= min_smaller_overlap)
        )
        for left_i, right_i, area in zip(
            left[conflict], right[conflict], intersection[conflict]
        ):
            adjacency[int(left_i)].append((int(right_i), float(area)))
            adjacency[int(right_i)].append((int(left_i), float(area)))

    selected = np.zeros(len(data), dtype=bool)
    suppressed_records = []
    for row_index in order:
        blockers = [
            (other, area) for other, area in adjacency[int(row_index)]
            if selected[other]
        ]
        if not blockers:
            selected[row_index] = True
            continue
        blocker, intersection = min(
            blockers, key=lambda item: (position[item[0]], -item[1])
        )
        suppressed_records.append({
            "suppressed_index": int(row_index),
            "retained_index": int(blocker),
            "suppressed_integrated_id": ids[row_index],
            "retained_integrated_id": ids[blocker],
            "intersection_m2": intersection,
            "suppressed_coverage": min(1.0, intersection / areas[row_index]),
            "retained_coverage": min(1.0, intersection / areas[blocker]),
            "smaller_overlap": min(
                1.0, intersection / min(areas[row_index], areas[blocker])
            ),
            "relation": "suppressed_older_overlap_threshold",
        })

    return (
        data.loc[selected].copy().reset_index(drop=True),
        pd.DataFrame(suppressed_records),
    )