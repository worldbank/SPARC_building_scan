# SPARC Building Scan

Welcome to the [SPARC](https://www.worldbank.org/en/topic/urbandevelopment/brief/sparc) Building Scan page. This website provides an introduction to the Building Scan data product, serves as a guide for technical users, and documents the detailed methodology behind the analysis.

Tier 2 of the SPARC program focuses on tailored analytical products for individual cities. The Building Scan product is designed to provide detailed insights into the building stock of a city, including information on building types, heights, footprints, and other relevant attributes. This enables city planners, policymakers, and investors to make informed decisions regarding urban development, infrastructure planning, and risk assessment.

## Building Scan Workflows
The Building Scan product generates the best analysis-ready building footprints for a given city. Building footprints are detailed, digitized, polygons of the building stock over an area of interest. They are a foundational data layer critical to inform urban planning, climate risk and resilience analytics, infrastructure/service gaps and cadastral systems.

Project teams often default to public data sources such as Open Street Map or Google Buildings. While these are excellent options, the accuracy of these public datasets varies greatly by location. Buildings from these sources are often outdated, missing in informal and recently developed areas, contain faulty polygons, or lack critical attribute information. The Building Scan product provides a consolidated workflow to help teams acquire the best building layer via three possible options.

### Option 1 — Open-Source Layers
Data integration of public datasets (Overture, OSM, 3D GlobFP), with data cleaning to remove duplicates and false positives.

- **Output**: Building footprints (with estimated height and year), plus completeness assessment.
- **Typical Use Case**: Investment scoping.
- **Accuracy**: Moderate.

### Option 2 — AI Extraction
Option 1 supplemented with AI extraction in areas flagged as being poorly mapped (with missing or outdated buildings). AI extraction relies on high resolution satellite imagery and the open-source segment anything model (SAM3).

- **Output**: High-coverage building footprints (with estimated height and year).
- **Typical Use Case**: Analytics informing infrastructure operations.
- **Accuracy**: High (~0.8).

### Option 3 — Manual Digitization
Digitization carried out by a firm with procred imagery.

- **Output**: Full-coverage building footprints (with estimated height and year).
- **Typical Use Case**: Taxation, need for complete building registry or cadastral system.
- **Accuracy**: Very high (~0.9/95).

## Data Attributes

Each building is attributed with the following variables.

- **Building Heights:** Information on the vertical dimension of buildings, which is crucial for urban planning and risk assessment.
- **Building Age:** Information on the age of buildings, which can be important for understanding the historical development of the city and for assessing the need for maintenance or retrofitting.




## SPARC Project Background 

Cities in developing countries often lack current, reliable, and accessible geospatial data. Existing information is frequently fragmented across analogue or disconnected systems, costly to acquire and analyze, and difficult to reuse because of inconsistent licensing and limited technical capacity. The Smart Parcel Atlas for Resilient Cities (SPARC) program addresses this gap by creating a shared, scalable platform for urban geospatial intelligence. There are three tiers of the SPARC program:
1. Tier 1 — Global urban data and analytics
2. Tier 2 — City-specific geospatial intelligence
3. Tier 3 — Operational uptake and client integration

### Tier 1 — Global urban data and analytics
Provides standardized, quality-assured geospatial data and automated statistics for approximately 10,000 cities worldwide. Supports early operational scoping and serves as a “conversation starter” for country and city engagements. Made available through the [SPARC Toolkit on GeoInsights](https://geoinsights.worldbank.org/toolkits/sparc) for internal and external users.

### Tier 2 — City-specific geospatial intelligence
Delivers higher-resolution, on-demand analytics for up to 100 priority cities. Supports investment option assessment and site prioritization through six initial services: buildings and parcels; urban public infrastructure; extreme heat exposure; multi-hazard exposure; green infrastructure; and urban settlement forecasts.

### Tier 3 — Operational uptake and client integration
Connects SPARC analytics directly to World Bank Group operations and client decision-making.
Regional operational teams lead demand identification, client engagement, local validation, and incorporation of findings into projects and local systems.