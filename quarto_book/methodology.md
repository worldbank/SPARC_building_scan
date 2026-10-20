---
title: "Methodology"
subtitle: "Open-Source Layers"
---

This page documents the workflow for option 1.

## Data Sources

The Building Scan workflow leverages various open-source geospatial layers to generate building footprints. These layers include:

#### Overture Maps Buildings

Overture Maps Buildings is a global vector building layer published by the Overture Maps Foundation. Overture conflates several major open and openly licensed footprint sources — OpenStreetMap (OSM), Google Open Buildings, Microsoft Global ML Building Footprints, Esri, and Meta — into a single layer of roughly 2.3 billion footprints, prioritizing community-contributed OSM geometry over machine-learning-derived sources during conflation.

    Overture Maps Foundation. (2024). https://docs.overturemaps.org/

#### 3D-GloBFP

3D-GloBFP is the first global building-footprint dataset to include a modeled height for every individual building. Its footprint geometries are drawn primarily from Microsoft's Global ML Building Footprints (~1.3 billion structures, ca. 2020) and augmented in East Asia with ~280 million building footprints of Shi et al. (2024), produced with their Comprehensive Large-Scale Mapping (CLSM) framework, to address known coverage gaps in the region.

    Che, Y., Li, X., Liu, X., Wang, Y., Liao, W., Zheng, X., et al. (2024). 3D-GloBFP: The first global three-dimensional building footprint dataset. Earth System Science Data, 16, 5357–5374. https://doi.org/10.5194/essd-16-5357-2024

The full workflow can be summarized as four distinct steps: data integration, data cleaning, flagging urban areas for review, and height attribution.

## Data Integration

The integration step combines Overture Maps Buildings and 3D-GloBFP building footprint datasets to create a comprehensive and accurate representation a city's built environment. 

1. Download building footprint, heights, and supplementary datasets for the city’s AOI. 
2. Augment Overture with 3D-GloBFP where it contributes non-duplicate buildings. In cases where duplicates come from the same dataset, keep the most recent footprint.
3. Assign temporary height values, geometry confidence, and review flag.

### Key thresholds

- Start with all footprints of at least 4m². 
- Two footprints overlap if their intersection covers at least 20% of the smaller footprint; a 0.01m² tolerance prevents boundary-touching and slivers from being counted as overlap. 
- Where footprints overlap, priority is given to Overture (OpenStreetMap), then Overture (non-OSM), and finally 3D-GloBFP.
- Temporary height attribution is assigned from three potential sources (in priority order: Overture, estimate from OSM, or 3D-GloBFP). 
- Geometry confidence is set to high for OSM footprints that are either corroborated by an overlapping 3D-GloBFP footprint or carry multiple OSM edit versions; medium for other OSM footprints, non-OSM Overture footprints corroborated by 3D-GloBFP, and 3D-GloBFP gap-fill footprints that carry a height; and low otherwise. 
- Corroboration is recorded where a footprint intersects a 3D-GloBFP polygon by at least 1 m² and at least 35% of the smaller footprint. 
- A review flag is raised where geometry confidence is low, or where two or more height sources disagree by more than 5 m. 