# Step 9 — City segments (reporting units)

Builds neighbourhood-scale **city segments** for any area of interest (AOI): street blocks bounded by roads, waterways and the AOI boundary, merged until each segment holds a minimum estimated population. The segments can be passed to step 1 as reporting units (`--segments`), so every integrated footprint gets a `SEGMENT_UID`.

This is an open-source re-implementation (geopandas, shapely, rasterio — no ArcGIS) of the method documented for CIESIN's **City Segments Layer v2** (Documentation for the City Segments Layer v2, CIESIN, June 2026, pp. 19–26; data CC0 1.0, Harvard Dataverse [doi:10.7910/DVN/FFS06X](https://doi.org/10.7910/DVN/FFS06X)). CIESIN's own code is not published; the published layer covers only the 2019 GHS-UCDB urban-centre boundaries, so a request AOI usually needs a new run.

## Method (documentation pages in brackets)
1. **Roads** (p. 22, 25): major = `motorway`, `trunk`, `primary`; minor = `secondary`, `tertiary`, `residential`, `unclassified` (`*_link` follows its parent class).
2. **Water** (p. 9, 23): waterways tagged `river` and the centreline of water polygons ≥ 150 ha are major lines; other waterways (streams, canals, drains) are minor lines. Water is clipped to the AOI + 1.5 km.
3. **Major zones** (p. 24): major lines + AOI boundary are polygonised; population is summed from a population grid with areal weighting for cells crossing zones; zones < 1,000 people are merged with their longest-edge neighbour, two at a time, iteratively.
4. **Segments** (p. 25–26): all lines are polygonised inside the major zones; islands < 3 ha with one neighbour are absorbed; blocks below the minimum population are merged with their longest-edge neighbour within the same major zone, two at a time, iteratively.

### Deliberate choices and deviations
| Topic | Here | CIESIN v2 |
|---|---|---|
| Boundary | Any AOI (`--aoi`) | GHS-UCDB 2019 urban-centre boundary |
| Minimum population | `--min-pop`, **default 500** | 400 in the documentation; the published layer behaves like ~500 (see Validation) |
| Merge order | Smallest population first; ties broken by location → **deterministic and independent of input row order** (tested) | Not documented |
| Road generalisation (dual carriageways, roundabouts, 40 m parallel merge, snap/extend) | Implemented but **off by default** (`V2Settings.generalize`, `extend_dangles_m`, `enclosure_max_m2`); no gain in validation | ArcGIS cartography tools, parameter values not documented |
| Water centreline | Voronoi medial axis | ArcGIS *Polygon to Centerline* |
| Roads input | OSM (Geofabrik `fclass`) or Overture Transportation (`--roads-format overture`) | GUSN 2020 (Boeing, OSMnx drivable network) |
| Population input | Any single-band raster, e.g. GHS-POP 2025 R2023A 100 m or LandScan Mosaic | GHS-POP 2025 R2023A 100 m |

## Run
```bash
python src/9_segments/run_segments.py \
  --aoi <aoi.gpkg> \
  --roads /vsizip/<country>-latest-free.shp.zip/gis_osm_roads_free_1.shp \
  --waterways /vsizip/<country>-latest-free.shp.zip/gis_osm_waterways_free_1.shp \
  --water-polygons /vsizip/<country>-latest-free.shp.zip/gis_osm_water_a_free_1.shp \
  --population <GHS_POP_E2025_GLOBE_R2023A_54009_100_V1_0_Rx_Cy>.tif \
  --min-pop 500 --out <output folder>
```
- Analysis CRS: local UTM zone from the AOI centroid (override with `--crs`).
- Outputs in `--out`: `segments_v2.gpkg` with layers `segments` (`ID_SEG`, `ID_MAJOR`, `POP_SEG`, `AREAHA_SEG`, `n_blocks`, `below_min_pop`), `major_zones`, `blocks` (before merging), and `run_manifest.json` (inputs, settings, statistics, package versions).
- Use as reporting units in step 1: `python src/1_data_integration/run_workflow.py ... --segments <out>/segments_v2.gpkg --segments-layer segments`.

Dependencies are those of `src/requirements.txt` (geopandas, shapely, rasterio, numpy, pandas, pyogrio, pyarrow); tests need `pytest`.

## Tests
```bash
python -m pytest src/9_segments/tests -q
```
16 synthetic tests with known answers: road classes, Overture class mapping, population conservation and areal weighting, polygonisation, pair-wise merging within major zones, longest-edge choice, merge rules, row-order independence, islands, river centreline, dead-end extension, parallel-road merge, reconnection, roundabout collapse, end-to-end run.

## Validation against the published layer (South Sudan, GUSN 2020 roads as in v2, GHS-POP 2025)
| City | Published segments / median people | This code, min 400 | This code, min 500 |
|---|---|---|---|
| Juba | 267 / 825 | 369 / 644 | 293 / 828 |
| Malakal | 95 / 828 | 121 / 659 | 100 / 815 |
| Renk | 105 / 791 | 135 / 684 | 111 / 784 |

- Population totals are identical to the published layer in all three cities.
- Published internal boundaries follow GUSN 2020 roads 97–100 %; at matching sizes about half of the boundaries coincide (within 10 m). Processing blocks in stored feature order (as ArcGIS cursors do) moves about half the boundaries when only the file order changes, so ~50 % agreement is what two valid runs of this method give.
- With current OSM roads results differ further where OSM changed since 2020.

## Licence and provenance
Same licence as this repository. Method re-implemented from CIESIN's public (CC0) documentation of the City Segments Layer v2, produced under a World Bank engagement.
