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

The integration step (`src/1_data_integration`) combines Overture Maps Buildings and 3D-GloBFP into a single "best-available" footprint layer for a city, with one geometry per building, a best-available height, and attributes describing each footprint's source, confidence, and need for review. `run_workflow.py` runs the scripts below in order; each step can also be run on its own.

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

1. Remove footprints smaller than 16m². 
2. Identify potential false positives by attributing three 10m raster datasets: 
    - Alpha Earth Embeddings (mean values from 64 bands). 
    - Dynamic World Land Cover (mean value from top class). 
    - Google 2.5D building presence confidence (max. value). 

### Key thresholds

- Start with all footprints of at least 4m². 
- An outlier label is assigned with embeddings using an Isolation Forest algorithm (contamination rate of 0.05 and 300 estimators). 
- Building presence confidence is converted to categories: low < 0.1, medium < 0.5, high ≥ 0.5, and no data. 
- Two conditions flag a footprint for removal: 
  - Embedding evidence: Footprint is an embedding outlier, dynamic world is non-built (or built with probability below 0.6), and geometry confidence is not high. 
  - Presence evidence: Maximum building presence confidence is low (< 0.1). Only applies to footprints predating 2024, since the dataset ends in 2023. 

A footprint with a recent Overture source year (2023 or later) is safeguarded from the removal flags. 