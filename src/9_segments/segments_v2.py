"""City segments for any AOI — open re-implementation of CIESIN's City Segments Layer v2 method.

Source of the method: "Documentation for the City Segments Layer v2", CIESIN, June 2026
(Harvard Dataverse doi:10.7910/DVN/FFS06X, CC0; pages cited below). CIESIN's code is not
published; this follows the documented steps with open tools (shapely, geopandas, rasterio).

Steps (doc pp. 22–26):
  1. Roads: major = motorway/trunk/primary; minor = secondary/tertiary/residential/unclassified.
  2. Water: rivers (waterway fclass = river) are major lines; water polygons ≥ 150 ha are reduced
     to a centreline (major); other waterways (streams, canals, drains) are minor lines.
  3. Major zones = polygonize(major roads + major water + AOI boundary); population from a grid
     by areal weighting; zones < 1,000 people merged with their longest-edge neighbour, two at a
     time, iteratively.
  4. Segments = polygonize(all lines) inside major zones; islands (< 3 ha, one neighbour) absorbed;
     population; segments < min_pop merged with their longest-edge neighbour within the same major
     zone, two at a time, iteratively.

Differences from CIESIN (documented, see README): the AOI replaces the GHS-UCDB 2019 boundary;
ArcGIS road generalisation (dual carriageways, roundabouts, 40 m parallel merge, extend/snap) is
not reproduced yet; the water centreline uses a Voronoi medial axis instead of ArcGIS "Polygon to
Centerline".
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely


@dataclass
class V2Settings:
    major_roads: list[str] = field(default_factory=lambda: ["motorway", "trunk", "primary"])                    # doc p.22
    minor_roads: list[str] = field(default_factory=lambda: ["secondary", "tertiary", "residential", "unclassified"])  # doc p.25
    links_follow_parent: bool = True        # *_link takes its parent's class (doc silent; GUSN drive network includes links)
    major_waterways: list[str] = field(default_factory=lambda: ["river"])                                      # doc p.23
    water_polygon_min_ha: float = 150.0     # doc p.23
    water_clip_buffer_m: float = 1500.0     # doc p.9 (water clipped to boundary + 1.5 km)
    major_zone_min_pop: float = 1000.0      # doc p.24
    min_pop: float = 500.0                  # segment threshold — v2 used 400 (doc p.26); SPARC default 500
    island_max_ha: float = 3.0              # doc p.25
    centreline_spacing_m: float = 25.0      # implementation choice (densification for the Voronoi medial axis)
    centreline_prune_m: float = 300.0       # implementation choice (drop medial-axis side branches shorter than this)
    min_shared_edge_m: float = 0.05         # implementation choice (CIESIN repo block_merging uses 0.05 m)
    extend_dangles_m: float = 0.0           # extend dead-end roads along their direction up to this distance
                                            # if they hit another line (ArcGIS Extend Line; doc p.22/25 gives no value)
    dangle_direction_m: float = 15.0        # length of the end piece used to estimate a dead end's direction
    # Road generalisation (doc p.22, p.25; ArcGIS cartography tools in CIESIN — open replacements below)
    generalize: bool = False                # master switch
    major_merge_m: float = 40.0             # merge parallel major roads (dual carriageways) closer than this (doc: no value)
    minor_merge_m: float = 40.0             # merge parallel secondary/tertiary roads within 40 m (doc p.25)
    minor_merge_classes: list[str] = field(default_factory=lambda: ["secondary", "tertiary"])  # doc p.25
    merge_rule: str = "longest_edge"        # neighbour choice when merging low-population units:
                                            #   longest_edge = documented v2 rule (doc p.24); prefer_low = longest edge
                                            #   among neighbours still below threshold, else longest edge;
                                            #   smallest_pop = adjacent neighbour with the smallest population
    enclosure_max_m2: float = 0.0           # collapse road enclosures (roundabouts, small loops) smaller than this
                                            # to a junction at their centre (doc p.22/25: no value; 0 = off)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- helpers

def utm_crs(aoi: gpd.GeoDataFrame) -> str:
    """Local UTM zone from the AOI centroid (as CIESIN: 'automatically assigned using the centroid', p.21)."""
    c = aoi.to_crs(4326).union_all().centroid
    zone = int(math.floor((c.x + 180) / 6) + 1)
    return f"EPSG:{(32600 if c.y >= 0 else 32700) + zone}"


def roads_from_overture(segments: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Overture Transportation segments → road lines with an OSM-style 'fclass'.

    Keeps subtype 'road'; Overture's road `class` uses the OSM highway vocabulary (motorway, trunk,
    primary, secondary, tertiary, residential, unclassified, service, track, …), so it maps 1:1.
    Links (Overture subclass 'link') keep their parent class.
    """
    roads = segments[segments["subtype"] == "road"].copy() if "subtype" in segments else segments.copy()
    roads["fclass"] = roads["class"].astype(str)
    return roads[["fclass", "geometry"]]


def road_class(fclass: pd.Series, s: V2Settings) -> pd.Series:
    base = fclass.astype(str).str.removesuffix("_link") if s.links_follow_parent else fclass.astype(str)
    out = pd.Series(None, index=fclass.index, dtype=object)
    out[base.isin(s.minor_roads)] = "minor"
    out[base.isin(s.major_roads)] = "major"
    return out


def _prune_spurs(lines, outline, prune_m: float, edge_tol_m: float, max_rounds: int = 50):
    """Remove medial-axis spurs: dangling branches shorter than prune_m whose free end touches the
    polygon outline (within edge_tol_m). The true ends of a centreline lie inside the polygon
    (half its width from the outline), so they are never pruned — this keeps roads from eroding.
    """
    geom = shapely.line_merge(shapely.set_precision(lines, 0.001))   # 1 mm grid: shared nodes compare equal
    for _ in range(max_rounds):
        parts = shapely.get_parts(geom)
        if len(parts) <= 1:
            return geom
        starts, ends_ = shapely.get_point(parts, 0), shapely.get_point(parts, -1)
        pts = np.vstack([shapely.get_coordinates(starts), shapely.get_coordinates(ends_)]).round(3)
        _, inv, counts = np.unique(pts, axis=0, return_inverse=True, return_counts=True)
        deg = counts[inv.ravel()]
        n = len(parts)
        free_start, free_end = deg[:n] == 1, deg[n:] == 1
        near_start = shapely.distance(starts, outline) < edge_tol_m
        near_end = shapely.distance(ends_, outline) < edge_tol_m
        spur = ((free_start & near_start) | (free_end & near_end)) & (shapely.length(parts) < prune_m)
        if not spur.any():
            return geom
        geom = shapely.line_merge(shapely.union_all(parts[~spur]))
    return geom


def polygon_centreline(poly, spacing_m: float, prune_m: float, edge_tol_m: float | None = None):
    """Approximate medial axis of a polygon: Voronoi edges of the densified boundary that lie inside it.

    edge_tol_m: a dangling branch is a spur if its free end is closer than this to the outline
    (default 2 × spacing; for road corridors of known half-width w use ~0.75 w).
    """
    pts = shapely.get_coordinates(shapely.segmentize(poly.boundary, spacing_m))
    edges = shapely.get_parts(shapely.voronoi_polygons(shapely.multipoints(pts), only_edges=True))
    inner = edges[shapely.contains_properly(poly, edges)]
    inner = inner[shapely.length(inner) > 1e-3]       # round ends create zero-length Voronoi edges
    if len(inner) == 0:
        return shapely.LineString()
    return _prune_spurs(shapely.union_all(inner), poly.boundary, prune_m, edge_tol_m=edge_tol_m or 2 * spacing_m)


def extend_dangles(lines, targets, max_m: float, direction_m: float = 15.0):
    """Extend dead ends along their direction until they hit `targets` (≤ max_m). Returns (lines, n_extended).

    Open replacement for ArcGIS Extend Line: a dead end is a line end that touches no other line
    after noding. Its direction comes from the last `direction_m` of the line. The extension stops
    at the first target hit; ends with nothing within max_m (true cul-de-sacs) are left as they are.
    """
    if max_m <= 0 or lines is None or lines.is_empty:
        return lines, 0
    noded = shapely.get_parts(shapely.line_merge(shapely.union_all(shapely.get_parts(lines))))
    noded = shapely.get_parts(shapely.union_all(noded))          # split at every crossing
    starts, ends = shapely.get_point(noded, 0), shapely.get_point(noded, -1)
    pts = np.vstack([shapely.get_coordinates(starts), shapely.get_coordinates(ends)]).round(3)
    keys, inv, counts = np.unique(pts, axis=0, return_inverse=True, return_counts=True)
    degree = counts[inv.ravel()]
    target_union = shapely.union_all([lines, targets]) if targets is not None else lines
    tree_geoms = shapely.get_parts(target_union)
    tree = shapely.STRtree(tree_geoms)
    n = len(noded)
    extensions = []
    for k in np.flatnonzero(degree == 1):
        line = noded[k % n]
        at_start = k < n
        L = line.length
        if L == 0:
            continue
        d = min(direction_m, L)
        tip = shapely.line_interpolate_point(line, 0 if at_start else L)
        back = shapely.line_interpolate_point(line, d if at_start else L - d)
        dx, dy = tip.x - back.x, tip.y - back.y
        norm = math.hypot(dx, dy)
        if norm == 0:
            continue
        far = (tip.x + dx / norm * max_m, tip.y + dy / norm * max_m)
        ray = shapely.LineString([(tip.x, tip.y), far])
        best = None
        for idx in tree.query(ray, predicate="intersects"):
            hit = shapely.intersection(ray, tree_geoms[idx])
            for p in shapely.get_parts(shapely.extract_unique_points(hit)):
                dist = shapely.distance(tip, p)
                if dist > 0.01 and (best is None or dist < best[0]):
                    best = (dist, p)
        if best is not None:
            extensions.append(shapely.LineString([(tip.x, tip.y), (best[1].x, best[1].y)]))
    if not extensions:
        return lines, 0
    return shapely.union_all([lines, *extensions]), len(extensions)


def _union(geoms) -> shapely.Geometry:
    geoms = [g for g in geoms if g is not None and not g.is_empty]
    return shapely.union_all(geoms) if geoms else shapely.LineString()


def merge_parallel(lines, dist_m: float):
    """Replace lines lying within dist_m of each other by one centreline (buffer → medial axis).

    Open replacement for ArcGIS Merge Divided Roads / Integrate: parallel carriageways and lines
    closer than dist_m collapse to their middle; enclosures smaller than ~dist_m are filled.
    Free ends retract by about dist_m / 2 — reconnect() restores the recorded contact points.
    """
    if lines is None or lines.is_empty or dist_m <= 0:
        return lines
    corridor = shapely.buffer(lines, dist_m / 2, quad_segs=4)
    spacing = max(dist_m / 8, 2.0)
    # True road ends lie dist_m/2 from the corridor outline; spur ends lie closer. 0.5 × half-width keeps the
    # network connected on real data (0.75 cut connecting pieces in a test city); remaining short spurs are
    # dead ends and do not change the polygons.
    return _union(polygon_centreline(p, spacing, prune_m=dist_m, edge_tol_m=0.5 * dist_m / 2)
                  for p in shapely.get_parts(corridor))


def reconnect(contacts, original_others, new_lines, max_m: float):
    """Connect recorded contact points to the generalised lines (doc p.25: intersection points recorded
    before generalisation, unmodified features snapped to their modified counterparts afterwards).

    A contact needs no connector if the unmodified line through it already reaches the new line
    within max_m (e.g. a cross street that continues over the merged centreline).
    """
    if contacts is None or contacts.is_empty or new_lines is None or new_lines.is_empty:
        return []
    parts = shapely.get_parts(original_others) if original_others is not None and not original_others.is_empty else np.array([])
    tree = shapely.STRtree(parts) if len(parts) else None
    connectors = []
    for p in shapely.get_parts(shapely.extract_unique_points(contacts)):
        d = shapely.distance(p, new_lines)
        if d <= 0.01 or d > max_m:
            continue
        if tree is not None:
            # Only the line(s) passing through this contact count: does that line itself reach the new line?
            through = parts[tree.query(p.buffer(0.01), predicate="intersects")]
            local = shapely.intersection(shapely.union_all(through), p.buffer(max_m)) if len(through) else None
            if local is not None and not local.is_empty and local.intersects(new_lines):
                continue
        connectors.append(shapely.shortest_line(p, new_lines))
    return connectors


def collapse_small_enclosures(lines, max_m2: float):
    """Collapse road loops smaller than max_m2 (roundabouts, small enclosed features) to a junction.

    The loop's lines are cut out and every road that met the loop is joined to the loop's centre
    (doc p.22: "generalize ... roundabouts ... into representative lines and junctions").
    Returns (lines, number of loops collapsed).
    """
    if max_m2 <= 0 or lines is None or lines.is_empty:
        return lines, 0
    faces = shapely.get_parts(shapely.polygonize(shapely.get_parts(shapely.union_all(lines))))
    small = faces[shapely.area(faces) < max_m2]
    if len(small) == 0:
        return lines, 0
    loops = shapely.union_all(shapely.boundary(small))
    kept = shapely.difference(lines, shapely.buffer(loops, 0.5))
    centres = shapely.centroid(small)
    rings = shapely.buffer(shapely.boundary(small), 1.0)
    tree = shapely.STRtree(rings)
    connectors = []
    parts = shapely.get_parts(shapely.line_merge(kept)) if not kept.is_empty else []
    for part in parts:
        for end in (shapely.get_point(part, 0), shapely.get_point(part, -1)):
            hits = tree.query(end, predicate="intersects")
            if len(hits):
                connectors.append(shapely.LineString([end, centres[hits[0]]]))
    return _union([kept, *connectors]), len(small)


def polygonize_within(lines, boundary) -> np.ndarray:
    """Polygons formed by lines inside a boundary polygon (the boundary closes the faces)."""
    inside = shapely.intersection(lines, boundary) if lines is not None and not lines.is_empty else shapely.LineString()
    noded = shapely.union_all([inside, boundary.boundary])
    faces = shapely.get_parts(shapely.polygonize(shapely.get_parts(noded)))
    keep = shapely.contains(boundary, shapely.point_on_surface(faces))
    return faces[keep]


def apportion_population(zones: gpd.GeoDataFrame, raster_path: str) -> np.ndarray:
    """Sum grid population per zone; cells crossing zones are split by area (areal weighting, doc p.24)."""
    import rasterio
    from rasterio.windows import Window, from_bounds

    with rasterio.open(raster_path) as src:
        xmin, ymin, xmax, ymax = zones.to_crs(src.crs).total_bounds
        w = from_bounds(xmin, ymin, xmax, ymax, src.transform)
        c0, r0 = math.floor(w.col_off), math.floor(w.row_off)
        win = Window(c0, r0, math.ceil(w.col_off + w.width) - c0, math.ceil(w.row_off + w.height) - r0)
        arr = src.read(1, window=win, boundless=True, fill_value=0).astype("float64")
        if src.nodata is not None:
            arr[arr == src.nodata] = 0
        arr[~np.isfinite(arr) | (arr < 0)] = 0
        t = src.window_transform(win)
        crs = src.crs
    rows, cols = np.nonzero(arr > 0)
    if len(rows) == 0:
        return np.zeros(len(zones))
    x0, y0 = t.c + cols * t.a, t.f + rows * t.e          # north-up grid (no rotation), as GHS-POP
    x1, y1 = x0 + t.a, y0 + t.e
    cells = gpd.GeoDataFrame({"v": arr[rows, cols]}, geometry=shapely.box(x0, y1, x1, y0), crs=crs).to_crs(zones.crs)
    cells["carea"] = cells.area
    z = gpd.GeoDataFrame({"zid": np.arange(len(zones))}, geometry=zones.geometry.to_numpy(), crs=zones.crs)
    inter = gpd.overlay(z, cells, how="intersection", keep_geom_type=True)
    inter["p"] = inter["v"] * inter.area / inter["carea"]
    return inter.groupby("zid")["p"].sum().reindex(range(len(zones)), fill_value=0).to_numpy()


def shared_edges(geoms: np.ndarray, min_len: float) -> dict[int, list[tuple[int, float]]]:
    """Neighbours sharing an edge (rook), with shared boundary length. Point contacts are ignored."""
    tree = shapely.STRtree(geoms)
    left, right = tree.query(geoms, predicate="intersects")
    m = left < right
    left, right = left[m], right[m]
    lengths = shapely.length(shapely.intersection(shapely.boundary(geoms[left]), shapely.boundary(geoms[right])))
    nb: dict[int, list[tuple[int, float]]] = {}
    for i, j, L in zip(left, right, lengths):
        if L > min_len:
            nb.setdefault(int(i), []).append((int(j), float(L)))
            nb.setdefault(int(j), []).append((int(i), float(L)))
    return nb


def merge_iteratively(gdf: gpd.GeoDataFrame, threshold: float, group: str | None, min_edge: float,
                      max_rounds: int = 10_000, rule: str = "longest_edge",
                      order: str = "pop") -> tuple[gpd.GeoDataFrame, int]:
    """Merge units with pop < threshold into their longest-edge neighbour (same group), two at a time.

    Each round, every low-population unit (smallest first) pairs with its longest-edge eligible
    neighbour; a unit takes part in at most one merge per round ("only two zones could be dissolved
    into a single zone at a given time step", doc p.24). Repeats until no low unit can merge.
    """
    g = gdf.reset_index(drop=True).copy()
    rounds = 0
    while rounds < max_rounds:
        low = np.flatnonzero(g["pop"].to_numpy() < threshold)
        if len(low) == 0:
            break
        geoms = g.geometry.to_numpy()
        nb = shared_edges(geoms, min_edge)
        grp = g[group].to_numpy() if group else None
        # Ties (equal population, equal edge length) are broken by location, not row number, so the
        # result depends only on the geometry and population — never on input order.
        cxy = shapely.get_coordinates(shapely.centroid(geoms)).round(3)
        loc = [tuple(c) for c in cxy]
        used, pairs = set(), []
        # order: "pop" = smallest population first (default, input-order independent);
        # "index" = stored feature order (as ArcGIS cursors process OBJECTIDs) — for sensitivity tests only.
        for i in (sorted(low, key=lambda k: (g["pop"].iat[k], loc[k])) if order == "pop" else low):
            if i in used:
                continue
            cands = [(round(L, 6), loc[j], j) for j, L in nb.get(int(i), []) if j not in used and (grp is None or grp[i] == grp[j])]
            if not cands:
                continue
            if rule == "prefer_low":
                low_cands = [c for c in cands if g["pop"].iat[c[2]] < threshold]
                j = max(low_cands or cands)[2]
            elif rule == "smallest_pop":
                j = min((g["pop"].iat[j], -L, xy, j) for L, xy, j in cands)[3]
            else:
                j = max(cands)[2]
            pairs.append((int(i), j))
            used.update((int(i), j))
        if not pairs:
            break
        rounds += 1
        drop = []
        for i, j in pairs:
            g.at[j, "geometry"] = shapely.union(geoms[i], geoms[j])
            g.at[j, "pop"] = g.at[j, "pop"] + g.at[i, "pop"]
            g.at[j, "n_blocks"] = g.at[j, "n_blocks"] + g.at[i, "n_blocks"]
            drop.append(i)
        g = g.drop(index=drop).reset_index(drop=True)
    return g, rounds


def absorb_islands(gdf: gpd.GeoDataFrame, max_ha: float, group: str, min_edge: float) -> tuple[gpd.GeoDataFrame, int]:
    """Absorb units < max_ha with exactly one neighbour into that neighbour (same group), doc p.25."""
    g = gdf.reset_index(drop=True).copy()
    absorbed = 0
    while True:
        geoms = g.geometry.to_numpy()
        nb = shared_edges(geoms, min_edge)
        small = np.flatnonzero(shapely.area(geoms) < max_ha * 10_000)
        cxy = shapely.get_coordinates(shapely.centroid(geoms)).round(3)
        small = sorted(small, key=lambda k: tuple(cxy[k]))          # location order, not row order
        grp = g[group].to_numpy()
        targets, used = {}, set()
        for i in small:
            n = nb.get(int(i), [])
            if len(n) == 1 and grp[n[0][0]] == grp[i] and int(i) not in used and n[0][0] not in used:
                targets[int(i)] = n[0][0]
                used.update((int(i), n[0][0]))
        if not targets:
            return g, absorbed
        for i, j in targets.items():
            g.at[j, "geometry"] = shapely.union(g.geometry.iat[j], geoms[i])
            g.at[j, "pop"] = g.at[j, "pop"] + g.at[i, "pop"]
            g.at[j, "n_blocks"] = g.at[j, "n_blocks"] + g.at[i, "n_blocks"]
        g = g.drop(index=list(targets)).reset_index(drop=True)
        absorbed += len(targets)


# ---------------------------------------------------------------- main steps

def prepare_lines(aoi, roads, waterways, water_polygons, s: V2Settings):
    """Major and minor line sets in the AOI CRS (doc steps 2–3)."""
    crs = aoi.crs
    boundary = aoi.union_all()
    clip_area = boundary.buffer(s.water_clip_buffer_m)
    cls = road_class(roads["fclass"], s)
    base = roads["fclass"].astype(str).str.removesuffix("_link")
    major_roads = _union(roads.geometry[cls == "major"])
    to_merge = (cls == "minor") & base.isin(s.minor_merge_classes)
    merge_roads = _union(roads.geometry[to_merge])                 # secondary / tertiary
    other_roads = _union(roads.geometry[(cls == "minor") & ~to_merge])
    gen = {"major_connectors": 0, "minor_connectors": 0, "enclosures_collapsed": 0}
    if s.generalize:
        edge = boundary.boundary
        # Major roads: record contacts (other roads + AOI edge), merge, reconnect.
        others = _union([merge_roads, other_roads])
        contacts = _union([shapely.intersection(others, major_roads), shapely.intersection(edge, major_roads)])
        major_g = merge_parallel(major_roads, s.major_merge_m)
        conn = reconnect(contacts, _union([others, edge]), major_g, s.major_merge_m)
        major_roads, gen["major_connectors"] = _union([major_g, *conn]), len(conn)
        # Secondary/tertiary: same with 40 m.
        others = _union([major_roads, other_roads])
        contacts = _union([shapely.intersection(others, merge_roads), shapely.intersection(edge, merge_roads)])
        merge_g = merge_parallel(merge_roads, s.minor_merge_m)
        conn = reconnect(contacts, _union([others, edge]), merge_g, s.minor_merge_m)
        merge_roads, gen["minor_connectors"] = _union([merge_g, *conn]), len(conn)
        # Roundabouts and other small enclosures, on major roads alone and on all roads.
        major_roads, _ = collapse_small_enclosures(major_roads, s.enclosure_max_m2)
        all_roads, gen["enclosures_collapsed"] = collapse_small_enclosures(
            _union([major_roads, merge_roads, other_roads]), s.enclosure_max_m2)
        # Keep the major lines in the 'all roads' set: subtracting them (even with a 1 cm buffer) cut the last
        # centimetre off every street meeting a major road, so those streets no longer closed any block.
        minor_roads = all_roads
    else:
        minor_roads = _union([merge_roads, other_roads])
    major = [major_roads]
    minor = [minor_roads]
    big_water, centrelines = None, []
    if water_polygons is not None and len(water_polygons):
        wp = gpd.clip(water_polygons.to_crs(crs), clip_area)
        wp = wp[wp.area >= s.water_polygon_min_ha * 10_000]
        if len(wp):
            big_water = wp.union_all()
            for poly in shapely.get_parts(big_water):
                centrelines.append(polygon_centreline(poly, s.centreline_spacing_m, s.centreline_prune_m))
    if waterways is not None and len(waterways):
        ww = gpd.clip(waterways.to_crs(crs), clip_area)
        is_major = ww["fclass"].isin(s.major_waterways)
        rivers = ww.geometry[is_major]
        if big_water is not None:                       # erase river lines overlapping big water polygons (p.23)
            rivers = rivers.difference(big_water)
        major += [*rivers]
        minor += [*ww.geometry[~is_major]]
    major += centrelines
    return _union(major), _union(minor), len(centrelines), gen


def build_segments(aoi: gpd.GeoDataFrame, roads: gpd.GeoDataFrame, population: str,
                   waterways: gpd.GeoDataFrame | None = None, water_polygons: gpd.GeoDataFrame | None = None,
                   settings: V2Settings | None = None, crs: str | None = None) -> dict:
    """Run steps 1–4 for one AOI. Returns dict with 'blocks', 'major_zones', 'segments' and 'stats'."""
    s = settings or V2Settings()
    crs = crs or utm_crs(aoi)
    aoi = aoi.to_crs(crs)
    roads = roads.to_crs(crs)
    repaired = shapely.get_parts(shapely.make_valid(aoi.union_all()))
    boundary = shapely.union_all([p for p in repaired if p.geom_type in ("Polygon", "MultiPolygon")])
    major_lines, minor_lines, n_centre, gen = prepare_lines(aoi, roads, waterways, water_polygons, s)
    major_lines, n_ext_major = extend_dangles(major_lines, boundary.boundary, s.extend_dangles_m, s.dangle_direction_m)

    # Step 3 — major zones
    mz = gpd.GeoDataFrame(geometry=polygonize_within(major_lines, boundary), crs=crs)
    mz["pop"] = apportion_population(mz, population)
    mz["n_blocks"] = 1
    n_major_raw = len(mz)
    mz, major_rounds = merge_iteratively(mz, s.major_zone_min_pop, None, s.min_shared_edge_m, rule=s.merge_rule)
    mz["ID_MAJOR"] = np.arange(1, len(mz) + 1)

    # Step 4 — blocks inside major zones, islands, population, merge to min_pop
    all_lines = shapely.union_all([major_lines, minor_lines])
    all_lines, n_ext_all = extend_dangles(all_lines, boundary.boundary, s.extend_dangles_m, s.dangle_direction_m)
    faces = polygonize_within(all_lines, boundary)
    blocks = gpd.GeoDataFrame(geometry=faces, crs=crs)
    blocks["ID_MAJOR"] = gpd.sjoin(gpd.GeoDataFrame(geometry=blocks.geometry.representative_point(), crs=crs),
                                   mz[["ID_MAJOR", "geometry"]], predicate="within", how="left")["ID_MAJOR"].to_numpy()
    blocks["pop"] = apportion_population(blocks, population)
    blocks["n_blocks"] = 1
    blocks_out = blocks.copy()
    seg, n_islands = absorb_islands(blocks, s.island_max_ha, "ID_MAJOR", s.min_shared_edge_m)
    seg, seg_rounds = merge_iteratively(seg, s.min_pop, "ID_MAJOR", s.min_shared_edge_m, rule=s.merge_rule)
    seg["ID_SEG"] = np.arange(1, len(seg) + 1)
    seg["POP_SEG"] = seg["pop"].round(1)
    seg["AREAHA_SEG"] = (seg.area / 10_000).round(3)
    seg["below_min_pop"] = seg["pop"] < s.min_pop
    seg = seg[["ID_SEG", "ID_MAJOR", "POP_SEG", "AREAHA_SEG", "n_blocks", "below_min_pop", "geometry"]]

    stats = {
        "crs": crs, "aoi_km2": round(boundary.area / 1e6, 3),
        "water_centrelines": n_centre,
        "dangles_extended_major": n_ext_major, "dangles_extended_all": n_ext_all,
        "generalisation": gen,
        "major_zones_raw": n_major_raw, "major_zones": len(mz), "major_merge_rounds": major_rounds,
        "blocks": len(blocks_out), "islands_absorbed": n_islands,
        "segments": len(seg), "segment_merge_rounds": seg_rounds,
        "segments_below_min_pop": int(seg["below_min_pop"].sum()),
        "pop_total_blocks": round(float(blocks_out["pop"].sum()), 1),
        "pop_total_segments": round(float(seg["POP_SEG"].sum()), 1),
        "pop_segment_median": round(float(seg["POP_SEG"].median()), 1) if len(seg) else None,
        "settings": s.as_dict(),
    }
    return {"blocks": blocks_out, "major_zones": mz, "segments": seg, "stats": stats}
