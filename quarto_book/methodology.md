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

1. Download Overture and 3D-GloBFP for the city’s AOI. 
2. Augment with 3D-GloBFP where it contributes non-duplicate buildings, keeping the most recent footprint (typically Overture).
3. Assign temporary height values, geometry confidence, and review flag.
