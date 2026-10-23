---
title: "Methodology"
subtitle: "Open-Source Layers"
---

This page documents the workflow for option 1.

## Data Sources

The Building Scan workflow leverages various open-source geospatial layers to generate building footprints and heights.

### Footprint sources

#### Overture Maps Buildings

Overture Maps Buildings is a global vector building layer published by the Overture Maps Foundation. Overture conflates several major open and openly licensed footprint sources — OpenStreetMap (OSM), Google Open Buildings, Microsoft Global ML Building Footprints, Esri, and Meta — into a single layer of roughly 2.3 billion footprints, prioritizing community-contributed OSM geometry over machine-learning-derived sources during conflation.

[[Overture Maps Foundation](https://docs.overturemaps.org/). (2024).]{.small .text-muted}

#### 3D-GloBFP

3D-GloBFP is the first global building-footprint dataset to include a modeled height for every individual building. Its footprint geometries are drawn primarily from Microsoft's Global ML Building Footprints (~1.3 billion structures, ca. 2020) and augmented in East Asia with ~280 million building footprints of Shi et al. (2024), produced with their Comprehensive Large-Scale Mapping (CLSM) framework, to address known coverage gaps in the region.

[Che, Y., Li, X., Liu, X., Wang, Y., Liao, W., Zheng, X., et al. [(2024)](https://doi.org/10.5194/essd-16-5357-2024). 3D-GloBFP: The first global three-dimensional building footprint dataset. *Earth System Science Data*, 16, 5357–5374.]{.small .text-muted}

### Raster height sources

We sample three gridded height products to fill heights where no vector height is available. They describe the raster cell containing a building, not the building itself, so they are treated as low-confidence estimates.

| Source | Vintage | Bands used | Access |
|---|---|---|---|
| Google Open Buildings 2.5D Temporal | 2023-06-30 | Band 2 height (m), band 3 building presence | Cloud-optimized GeoTIFFs (COGs), streamed |
| GBA.Height (Global Building Atlas) | — | Band 1 height (m), 3 m resolution | GeoTIFF tiles extracted from the official mediaTUM ZIP archives |
| TEMPO | 2023 Q4 | Band 1 building density, band 2 normalized height (× 100 m) | COGs, streamed by default or optionally downloaded |

The full workflow has four distinct steps: data integration, data cleaning, flagging urban areas for review, and height attribution. Preliminary heights are attached during data integration and refined in the height attribution step.

## 1) Data Integration

The integration step (`src/1_data_integration`) combines Overture Maps Buildings and 3D-GloBFP into a single "best-available" footprint layer for a city, with one geometry per building, a best-available height, and attributes describing each footprint's source, confidence, and need for review. `run_workflow.py` runs the scripts below in order.

| Stage | Script | Purpose |
|---|---|---|
| 1 | `prepare_city.py` | Set up the city workspace |
| 2 | `download_overture.py`, `download_3d_globfp.py` | Download footprint sources for the AOI |
| 3 | `build_integrated_footprints.py` | Clean, merge, deduplicate, and attribute footprints |
| 4 | `acquire_height_sources.py`, `enrich_raster_heights.py` | Attach raster heights and finalize the best height |

### 1.1. Prepare inputs

The user supplies a city name and an area of interest (AOI) polygon. The AOI is reprojected to a local UTM zone, which is used for all area and overlap calculations.

### 1.2. Download footprint sources

- **Overture:** buildings within the AOI's bounding box are downloaded as GeoParquet using the `overturemaps` CLI.
- **3D-GloBFP:** the tile catalog is read from Figshare, every tile intersecting the AOI bounding box is downloaded, verified against its MD5 checksum, and extracted.

### 1.3. Clean and harmonize

Each source is cleaned independently:

1. Reproject to the analysis CRS.
2. Drop null or empty geometries, repair invalid ones, and keep only polygons.
3. Drop footprints smaller than **4 m²**.
4. Keep footprints whose interior point (point-on-surface) falls inside the AOI. Footprints are never clipped, so buildings straddling the AOI boundary stay whole.

#### Resolve overlapping footprints

We pool the two sources and resolve conflicting footprints so each building is represented once.

- **Conflict rule:** two footprints conflict when their intersection is at least **0.01 m²** *and* covers at least **20%** of the smaller footprint. Smaller overlaps are treated as boundary slivers, and both footprints are kept.
- **Ranking:** candidates are ranked by source priority (OpenStreetMap > Overture non-OSM > 3D-GloBFP), then by footprint date (most recent first), then by ID.
  - *Why source before date:* footprint dates are not comparable across sources. The 3D-GloBFP date is the dataset's 2020 vintage, not when each building was observed, and Overture's `update_time` records when a record was last edited or ingested, not when the building was captured. Within a source, dates are more consistent and break ties. OSM ranks above other Overture sources because human-digitized geometry is generally more accurate than machine-learning-derived footprints.
  - *Trade-off:* an outdated footprint from a higher-priority source (e.g., a demolished building still in OSM) is kept over a newer, lower-priority one. We leave such cases for later cleaning and review steps.
- **Selection:** footprints are visited in rank order. A footprint is kept unless it conflicts with one already kept, in which case it is suppressed.

In practice, Overture geometry is kept wherever it exists, and 3D-GloBFP adds buildings that Overture is missing. Every suppressed footprint is recorded in a lineage table alongside the footprint that replaced it.

### 1.4. Attribute the integrated footprints

**3D-GloBFP support.** A footprint is *supported* when a 3D-GloBFP footprint overlaps it by at least 1 m² and 35% of the smaller footprint. This is an independent check that the building exists and does not affect which geometry is selected.

**Preliminary height.** Each footprint takes the first valid value (0.5–100 m.) from the following, in priority order:

1. Native Overture height
2. OSM floor count × 3 m (for 1–40 floors)
3. 3D-GloBFP modeled height, taken from the 3D-GloBFP footprint containing the building's interior point (or from the footprint itself, if it came from 3D-GloBFP)

**Geometry confidence.**

| Confidence | Criteria |
|---|---|
| High | OSM footprint that is supported by 3D-GloBFP or has been edited more than once in OSM |
| Medium | Any other OSM footprint; non-OSM Overture footprint supported by 3D-GloBFP; 3D-GloBFP footprint with a height |
| Low | All other footprints |

#### Raster height enrichment

The raster height sources are located for the AOI. We then sample each footprint at its interior point. Where several rasters cover a point, valid values are averaged. Raster heights are only accepted when they fall within 0.5–100 m. Additionally, building presence must be ≥ 0.5 (for Google), and building density is ≥ 0.001 (for TEMPO).

The best height is then recomputed across all six sources, in order of priority:

| Priority | Source | Height confidence |
|---|---|---|
| 1 | Native Overture height | High |
| 2 | OSM floors × 3 m | Medium |
| 3 | 3D-GloBFP | Medium |
| 4 | Google 2.5D (2023) | Low |
| 5 | GBA.Height | Low |
| 6 | TEMPO (2023 Q4) | Low |

**Review flag.** A footprint is flagged for review (`review_required`) when its geometry confidence is low, or when two or more height sources disagree by more than 5 m.

### Outputs

Outputs are written to `<data-root>/<city>/outputs/`:

- `best_available_footprints.parquet` / `.gpkg`: the integrated footprints with source, height, confidence, and review attributes
- `footprint_lineage.parquet` / `.csv`: each selected footprint and the footprints it replaced
- `summary.json`: input and output counts, source breakdown, and parameters used
- `height_enrichment_metadata.json`: raster sources, bands, thresholds, and height priority order
- `overview.png`: maps of the selected geometry source and best vector height

## 2) Data Cleaning

The cleaning step (`src/2_data_cleaning`) removes likely false positives from the integrated footprints using satellite-derived evidence of whether a building is present. It then adds neighborhood statistics for later review. `run_workflow.py` runs the scripts below in order; each step can also be run on its own.

| Stage | Script | Purpose |
|---|---|---|
| 0 | `0_get_gee_data.py` | Attach Earth Engine land-cover, embedding, and building-presence values to each footprint |
| 1 | `1_detect_outliers.py` | Flag embedding outliers with an Isolation Forest |
| 2 | `2_drop_false_positives.py` | Drop footprints with evidence of being false positives |
| 3 | `3_finalize_buildings.py` | Compute neighbor statistics |

### 2.1. Attach remote-sensing evidence

We start from all integrated footprints (≥ 4 m², from step 1) and use Google Earth Engine to summarize three raster datasets over each footprint:

| Dataset | Year | Resolution | Footprint value |
|---|---|---|---|
| AlphaEarth satellite embeddings | 2025 | 10 m | Mean of each of the 64 embedding bands |
| Dynamic World land cover | 2025 | 10 m | Class with the highest mean probability (annual mean, then footprint mean) and that probability |
| Google Open Buildings 2.5D building presence | 2023 | 4 m | Maximum presence confidence (annual maximum, then footprint maximum) |

### 2.2. Detect embedding outliers

Footprints whose embeddings differ markedly from the rest of the city are likely to be something other than a building (e.g., vegetation, bare ground, or water). We standardize the 64 embedding bands and fit an Isolation Forest (**300** estimators, contamination rate of **0.05**), which labels the most anomalous 5% of footprints in each city as candidate outliers. These candidates are then narrowed by the conditions in 2.3.

### 2.3. Drop false positives

A footprint is removed when either of two conditions holds:

- **Embedding evidence:** the footprint is an embedding outlier, its top Dynamic World class is not built (or is built with a probability below **0.6**), and its geometry confidence is not high.
- **Presence evidence:** its maximum Google 2.5D presence confidence is **≤ 0.1**, and its footprint date is before **2024**. Google 2.5D ends in 2023, so it cannot confirm buildings mapped afterward. Footprints with no date or no Google 2.5D coverage are not tested.

Two safeguards override both conditions. A footprint is always kept if:

- it comes from Overture and its footprint date is **2023 or later**, or
- its top Dynamic World class is built with a probability of at least **0.6**.

### 2.4. Compute neighbor statistics

For each footprint, we find all other footprints within **50 m** (edge-to-edge) and record their number, the mean, minimum, and maximum of their areas, and the mean, minimum, and maximum of their distances. These support the next step: reviewing urban areas.

### Outputs

The cleaned footprints are written to `<data-root>/<city>/outputs/best_available_footprints_cleaned.gpkg`, with all step 1 attributes plus the Earth Engine values, outlier scores, and neighbor statistics. Intermediate results are kept in `<data-root>/<city>/sources/`:

- `gee/`: footprints with Earth Engine values (`.gpkg` and `.csv`)
- `gee/outlier_results/`: footprints with Isolation Forest results and t-SNE diagnostic plots
- `cleaning/`: footprints remaining after false positives are dropped

## 3) Flag Areas for Review

The review step (`src/3_outdated_segments`) identifies neighborhood segments where the building footprint layer is likely out of date — where satellite evidence shows meaningfully more settled area today than the footprints we currently have account for. `run_workflow.py` runs the scripts below in order.

| Stage | Script | Purpose |
|---|---|---|
| 0 | `0_download_wsf.py` | Stream and clip the WSF Tracker raster for the AOI |
| 1 | `1_sample_building_wsf_year.py` | Establish each segment's baseline from its own buildings |
| 2 | `2_compute_settled_change.py` | Measure settled-area growth since each segment's baseline |
| 3 | `3_flag_outdated_segments.py` | Flag segments whose footprints are likely outdated |

### 3.1. Stream the WSF Tracker raster

We use the World Settlement Footprint (WSF) Tracker, a global biannual record of built-up land cover since 2016, streamed directly from its public Zarr archive and clipped to the AOI. Each pixel's value is the earliest epoch at which it was detected as built-up: **0** means never detected; **1–20** index the half-year epochs from 2016-07-01 onward, six months apart. The clipped raster is reprojected to the analysis CRS so pixel counts translate directly to area.

[World Settlement Footprint Tracker. Streamed from `source.coop/mindearth/wsf`.]{.small .text-muted}

### 3.2. Establish each segment's baseline

This step takes reporting segments as an external input (e.g., the CIESIN City Segments Layer) rather than the reporting units from step 1 — segments are not used in steps 1–2.

For every footprint from step 2 (cleaned buildings), we sample the WSF epoch at its interior point. Within each segment, the baseline is the **lower quartile** of WSF epoch among its own buildings — the point below which the oldest quarter of the segment's *currently mapped* building stock sits. This is a per-segment, not city-wide, reference point: a segment's effective footprint-coverage horizon is a property of that segment, not of the city as a whole.

*Why lower quartile, and why epoch 1 dominates in practice:* epoch 1 is left-censored — it covers everything built before monitoring began in mid-2016, regardless of whether that was 1990 or 2015. In the cities tested so far, every segment's buildings include enough pre-2016 development that the lower quartile lands on epoch 1; the baseline would only move later for a segment where more than 75% of its currently-mapped buildings postdate 2016. This is treated as a genuine finding, not an error — a segment where buildings cluster this old is legitimately an old segment, and the growth comparison in 3.3 still varies meaningfully by segment even when the baseline epoch does not.

Segments are also given a `building_count`, used as a coverage gate in 3.4.

### 3.3. Measure settled-area change

For each segment we compare two areas from the same raster:

- **Current settled area:** every pixel ever detected as built-up (epoch 1–20).
- **Baseline settled area:** pixels detected as built-up at or before *that segment's own* baseline epoch from 3.2.

Segments are rasterized onto the WSF grid once; pixel counts are then grouped by segment and compared against each segment's own threshold in a single pass, rather than clipping the raster per segment.

### 3.4. Flag outdated segments

A segment is flagged when all three hold:

| Condition | Threshold |
|---|---|
| Share of today's settled area that postdates baseline | **> 15%** *(placeholder; 10–25% under consideration)* |
| Absolute settled-area growth since baseline | **> 10,000 m²** |
| Non-trivial existing building footprint coverage | **≥ 10 buildings** *(placeholder)* |

The percentage and coverage thresholds are provisional and will be calibrated; the absolute-area threshold reflects the current specification.

### Outputs

Outputs are written to `<data-root>/<city>/outputs/outdated_segments.gpkg`: segment geometries with `baseline_epoch`, `baseline_year`, `building_count`, `current_settled_m2`, `baseline_settled_m2`, `change_pct`, `change_abs_m2`, and the `outdated` flag. Intermediate results are kept in `<data-root>/<city>/sources/`:

- `wsf/`: the clipped, reprojected WSF Tracker raster
- `segments/`: segments with baseline, then with settled-area statistics, at each stage
