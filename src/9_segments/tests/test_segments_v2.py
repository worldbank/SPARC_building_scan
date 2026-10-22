"""Tests for the City Segments Layer v2 re-implementation on synthetic data with known answers."""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import shapely
from shapely.geometry import LineString, Polygon, box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from segments_v2 import (V2Settings, absorb_islands, apportion_population, build_segments,  # noqa: E402
                               merge_iteratively, polygon_centreline, polygonize_within, road_class)

CRS = "EPSG:32636"


def write_raster(path, value_per_cell, x0=600000, y0=1000000, n=20, cell=100):
    """n×n raster of `cell` m cells, upper-left (x0, y0 + n*cell), constant value."""
    import rasterio
    from rasterio.transform import from_origin
    arr = np.full((n, n), value_per_cell, dtype="float32")
    with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32", crs=CRS,
                       transform=from_origin(x0, y0 + n * cell, cell, cell), nodata=-200) as dst:
        dst.write(arr, 1)
    return str(path)


def grid_roads(x0, y0, size, step, fclass):
    lines = [LineString([(x0 + i, y0), (x0 + i, y0 + size)]) for i in range(step, size, step)]
    lines += [LineString([(x0, y0 + i), (x0 + size, y0 + i)]) for i in range(step, size, step)]
    return gpd.GeoDataFrame({"fclass": [fclass] * len(lines)}, geometry=lines, crs=CRS)


def test_road_classes():
    s = V2Settings()
    got = road_class(pd.Series(["primary", "primary_link", "residential", "footway", "trunk"]), s)
    assert got[[0, 1, 2, 4]].tolist() == ["major", "major", "minor", "major"] and pd.isna(got[3])


def test_overture_roads_map_to_osm_classes():
    from segments_v2 import roads_from_overture
    seg = gpd.GeoDataFrame({"subtype": ["road", "road", "rail", "road"], "class": ["primary", "residential", "standard_gauge", "track"]},
                           geometry=[LineString([(0, 0), (1, 0)])] * 4, crs=CRS)
    out = roads_from_overture(seg)
    assert out["fclass"].tolist() == ["primary", "residential", "track"]
    assert road_class(out["fclass"], V2Settings()).tolist()[:2] == ["major", "minor"]


def test_population_is_conserved_and_area_weighted(tmp_path):
    r = write_raster(tmp_path / "p.tif", 10.0)                 # 400 cells × 10 = 4,000 people on 2 × 2 km
    aoi = box(600000, 1000000, 602000, 1002000)
    zones = gpd.GeoDataFrame(geometry=[box(600000, 1000000, 600050, 1002000),  # half a cell column wide
                                       box(600050, 1000000, 602000, 1002000)], crs=CRS)
    pop = apportion_population(zones, r)
    assert pop.sum() == pytest.approx(4000, rel=1e-6)
    assert pop[0] == pytest.approx(20 * 10 * 0.5, rel=1e-6)    # 20 cells, half of each


def test_polygonize_grid_gives_expected_blocks():
    aoi = box(0, 0, 1000, 1000)
    lines = shapely.union_all(grid_roads(0, 0, 1000, 250, "residential").geometry.to_numpy())
    assert len(polygonize_within(lines, aoi)) == 16


def test_merge_only_pairs_and_respects_groups():
    cells = [box(i * 10, 0, (i + 1) * 10, 10) for i in range(4)]
    g = gpd.GeoDataFrame({"pop": [100.0, 100, 100, 100], "n_blocks": 1, "grp": [1, 1, 2, 2]}, geometry=cells, crs=CRS)
    out, rounds = merge_iteratively(g, 200, "grp", 0.05)
    assert len(out) == 2 and set(out["grp"]) == {1, 2} and (out["pop"] == 200).all() and rounds == 1


def test_merge_uses_longest_edge_neighbour():
    a = box(0, 0, 10, 10)                       # low pop; shares 10 m with b, 2 m with c
    b = box(10, 0, 20, 10)
    c = box(-2, 10, 0, 20)
    c2 = box(0, 10, 2, 20)                      # c2 touches a along 2 m
    g = gpd.GeoDataFrame({"pop": [10.0, 900, 900, 900], "n_blocks": 1}, geometry=[a, b, c, c2], crs=CRS)
    out, _ = merge_iteratively(g, 500, None, 0.05)
    merged = out[out["n_blocks"] == 2].geometry.iloc[0]
    assert merged.equals(shapely.union(a, b).normalize()) or merged.normalize().equals(shapely.union(a, b).normalize())


def test_merge_rules_choose_different_neighbours():
    # a (low, 100) touches b (big, 900) along 10 m and c (low, 150) along 5 m.
    a = box(0, 0, 10, 10)
    b = box(10, 0, 20, 10)
    c = box(0, 10, 5, 15)
    g = gpd.GeoDataFrame({"pop": [100.0, 900, 150], "n_blocks": 1}, geometry=[a, b, c], crs=CRS)
    pick = {}
    for rule in ("longest_edge", "prefer_low", "smallest_pop"):
        out, _ = merge_iteratively(g, 400, None, 0.05, max_rounds=1, rule=rule)
        pick[rule] = sorted(out.loc[out["n_blocks"] == 2, "pop"].tolist())
    assert pick["longest_edge"] == [1000.0]      # a joins big b (longest edge)
    assert pick["prefer_low"] == [250.0]         # a joins low c
    assert pick["smallest_pop"] == [250.0]       # c has the smallest population


def test_default_merge_is_independent_of_row_order():
    # 6 × 6 grid of 100 m blocks with uneven population; the default (smallest-first) result must not depend on row order.
    rng = np.random.default_rng(3)
    cells = [box(x * 100, y * 100, (x + 1) * 100, (y + 1) * 100) for x in range(6) for y in range(6)]
    g = gpd.GeoDataFrame({"pop": rng.integers(20, 300, len(cells)).astype(float), "n_blocks": 1}, geometry=cells, crs=CRS)
    norm = lambda d: sorted(d.geometry.normalize().to_wkb())
    a, _ = merge_iteratively(g, 500, None, 0.05)
    b, _ = merge_iteratively(g.sample(frac=1, random_state=9).reset_index(drop=True), 500, None, 0.05)
    assert norm(a) == norm(b)


def test_islands_absorbed():
    big = Polygon([(0, 0), (500, 0), (500, 500), (0, 500)], holes=[[(200, 200), (300, 200), (300, 300), (200, 300)]])
    island = box(200, 200, 300, 300)            # 1 ha, one neighbour
    g = gpd.GeoDataFrame({"pop": [800.0, 50], "n_blocks": 1, "ID_MAJOR": 1}, geometry=[big, island], crs=CRS)
    out, n = absorb_islands(g, 3, "ID_MAJOR", 0.05)
    assert n == 1 and len(out) == 1 and out["pop"].iloc[0] == 850


def test_centreline_of_long_rectangle_is_near_midline():
    river = box(0, 0, 3000, 400)                # 3 km × 400 m "river" polygon
    cl = polygon_centreline(river, 25, 300)
    assert cl.length > 2000
    ys = shapely.get_coordinates(cl)[:, 1]
    assert np.median(np.abs(ys - 200)) < 30


def test_extend_dangles_closes_short_gap_only():
    from segments_v2 import extend_dangles
    base = LineString([(0, 0), (100, 0)])
    near = LineString([(50, 100), (50, 10)])     # stops 10 m short of `base`, heading towards it
    far = LineString([(80, 100), (80, 40)])      # stops 40 m short
    cul = LineString([(20, 100), (20, 60)])      # heads at `base` but extension limited below
    lines = shapely.union_all([base, near, far, cul])
    out, n = extend_dangles(lines, None, 30)
    assert n == 1                                # only the 10 m gap closes (40 m > 30 m limit)
    assert out.intersects(shapely.Point(50, 0)) and out.distance(shapely.Point(50, 5)) < 1e-6
    assert out.distance(shapely.Point(80, 20)) > 1                 # 40 m gap stays open
    # Dead ends pointing away from everything are never extended.
    out2, n2 = extend_dangles(shapely.union_all([base, LineString([(50, 0), (50, 100)])]), None, 200)
    assert n2 == 0


def test_extension_creates_extra_block():
    from segments_v2 import extend_dangles
    aoi = box(0, 0, 100, 100)
    stub = LineString([(50, 0), (50, 90)])       # ends 10 m short of the top boundary
    assert len(polygonize_within(stub, aoi)) == 1
    ext, _ = extend_dangles(stub, aoi.boundary, 20)
    assert len(polygonize_within(ext, aoi)) == 2


def test_merge_parallel_dual_carriageway():
    from segments_v2 import merge_parallel
    dual = shapely.union_all([LineString([(0, 0), (1000, 0)]), LineString([(0, 20), (1000, 20)])])  # 20 m apart
    out = merge_parallel(dual, 40)
    ys = shapely.get_coordinates(out)[:, 1]
    assert out.length < 1100 and np.median(np.abs(ys - 10)) < 2      # one line in the middle
    far = shapely.union_all([LineString([(0, 0), (1000, 0)]), LineString([(0, 60), (1000, 60)])])  # 60 m apart
    assert len(shapely.get_parts(shapely.line_merge(merge_parallel(far, 40)))) == 2      # not merged


def test_reconnect_t_street_but_not_crossing_street():
    from segments_v2 import merge_parallel, reconnect
    dual = shapely.union_all([LineString([(0, 0), (1000, 0)]), LineString([(0, 20), (1000, 20)])])
    t_street = LineString([(300, 200), (300, 20)])                  # ends on the upper carriageway
    cross = LineString([(700, -200), (700, 200)])                    # crosses both carriageways
    contacts = shapely.union_all([shapely.intersection(t_street, dual), shapely.intersection(cross, dual)])
    merged = merge_parallel(dual, 40)
    conn = reconnect(contacts, shapely.union_all([t_street, cross]), merged, 40)
    assert len(conn) == 1                                            # only the T street needs a connector
    assert shapely.distance(conn[0], shapely.Point(300, 20)) < 1e-6


def test_collapse_roundabout_to_junction():
    from segments_v2 import collapse_small_enclosures
    ring = shapely.Point(0, 0).buffer(15).exterior                   # roundabout, ~700 m²
    arms = [LineString([(15, 0), (300, 0)]), LineString([(-15, 0), (-300, 0)]), LineString([(0, 15), (0, 300)])]
    lines = shapely.union_all([ring, *arms])
    out, n = collapse_small_enclosures(lines, 2000)
    assert n == 1
    assert out.distance(shapely.Point(0, 0)) < 1e-6                  # arms meet at the centre
    assert len(shapely.get_parts(shapely.polygonize(shapely.get_parts(out)))) == 0   # loop gone
    big_loop = shapely.box(0, 0, 200, 200).exterior                  # 4 ha block is kept
    assert collapse_small_enclosures(big_loop, 2000)[1] == 0


def test_build_segments_end_to_end(tmp_path):
    r = write_raster(tmp_path / "p.tif", 12.5)  # 5,000 people on 2 × 2 km
    aoi = gpd.GeoDataFrame(geometry=[box(600000, 1000000, 602000, 1002000)], crs=CRS)
    major = gpd.GeoDataFrame({"fclass": ["primary"]}, geometry=[LineString([(601000, 1000000), (601000, 1002000)])], crs=CRS)
    minor = grid_roads(600000, 1000000, 2000, 200, "residential")
    roads = pd.concat([major, minor], ignore_index=True)
    res = build_segments(aoi, roads, r, settings=V2Settings(min_pop=500), crs=CRS)
    seg, st = res["segments"], res["stats"]
    assert st["major_zones"] == 2                                       # split by the primary road, both ≥ 1,000
    assert st["blocks"] == 100                                          # 10 × 10 blocks of 200 m
    assert st["pop_total_segments"] == pytest.approx(5000, rel=1e-6)    # population conserved
    assert (seg["POP_SEG"] >= 500).all()
    # Segments never cross the major road: every segment lies in one major zone.
    for _, s in seg.iterrows():
        assert (s.geometry.bounds[2] <= 601000 + 1e-6) or (s.geometry.bounds[0] >= 601000 - 1e-6)
