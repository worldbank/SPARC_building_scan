from pathlib import Path
import json
import shutil
import subprocess
import sys
from turtle import width

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import xarray as xr
import rioxarray  # noqa: F401
import obstore
import folium

from zarr.storage import ObjectStore
from overturemaps import geodataframe as overture_gdf
from rasterio.features import rasterize, MergeAlg
from rasterio.mask import mask
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

sys.path.insert(0, "C:\\WBG\\Work\\Code\\GOSTrocks\\src")
import GOSTrocks.rasterMisc as rMisc

# FROM DRE Atlas - https://dre.energydata.info/static/DRE-Atlas-Technical-Documentation.pdf
 #green (large size > 200 m²), 
 # blue (medium size 120-200 m²), 
 # bright pink (small size with 15-120 m²) and 
 # light pink (very small structures < 15 m²).

building_definitions = {
     "large": {"color": "#118020", "min_area": 200},
     "medium": {"color": "#0000ff", "min_area": 120, "max_area": 200},
     "small": {"color": "#ff69b4", "min_area": 15, "max_area": 120},
     "very_small": {"color": "#ffb6c1", "max_area": 15}
 }

building_colours = {
     "large": "#118020",
     "medium": "#0000ff",
     "small": "#ff69b4",
     "very_small": "#ffb6c1"
 }

def hex_to_rgb(hex_str):
    # Remove the '#' prefix if it exists
    hex_str = hex_str.lstrip('#')
    
    # Convert segments to integers by specifying base 16
    return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))

building_colours_rgb = {key: hex_to_rgb(color) for key, color in building_colours.items()}  

class SPARC_city:
    def __init__(self, name, boundary_series, output_dir, crs):
        self.name = name
        self.boundary_series = boundary_series
        self.boundary_gdf = gpd.GeoDataFrame(boundary_series.to_frame().T, geometry="geometry", crs=crs)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.overture_file = output_dir / "buildings.gpkg"
        self.wsf_file = output_dir / "wsf.tif"
        self.overture_digitized = output_dir / "overture_digitized.tif"
        self.overture_wsf_comparison_file = output_dir / "overture_wsf_comparison.tif"

    def download_overture(self):
        """
        Download Overture data for the city.

        Parameters:
        - None: Uses the output_dir provided during initialization.
        """        
        output_file = self.output_dir / "buildings.gpkg"
        if not output_file.exists():
            bbox = self.boundary_gdf.total_bounds
            buildings_gdf = overture_gdf("building", bbox=bbox)            
            buildings_gdf.crs = self.boundary_gdf.crs
            buildings_gdf = buildings_gdf[buildings_gdf.intersects(self.boundary_gdf.union_all())]
            # calculate building area in metres
            buildings_gdf["area_m2"] = buildings_gdf.geometry.to_crs(epsg=3857).area
            # classify buildings based on area
            buildings_gdf['bldg_size'] = buildings_gdf['area_m2'].apply(
                lambda area: (
                    "large" if area > building_definitions["large"]["min_area"] else
                    "medium" if building_definitions["medium"]["min_area"] <= area <= building_definitions["medium"]["max_area"] else
                    "small" if building_definitions["small"]["min_area"] <= area <= building_definitions["small"]["max_area"] else
                    "very_small"
                )
            )
            buildings_gdf.to_file(output_file, driver="GPKG")
        else:
            print(f"Overture data already exists at {output_file}")
        return None

    def download_wsf(self):
        """
        Download World Settlement Footprint (WSF) data for the city.

        Parameters:
        - None: Uses the output_dir provided during initialization.
        """
        if not self.wsf_file.exists():
            bbox = self.boundary_gdf.total_bounds
            store = obstore.store.S3Store(
                bucket="us-west-2.opendata.source.coop",
                prefix="mindearth/wsf/World_WSF_20160701-20260101.zarr",
                config={
                    "aws_skip_signature": "true",
                    "aws_region": "us-west-2",
                },
            )
            zarr_store = ObjectStore(store, read_only=True)

            ds = xr.open_zarr(zarr_store, group="0", decode_coords="all")
            subset = ds["wsf_tracker"].sel(
                x=slice(bbox[0], bbox[2]),
                y=slice(bbox[3], bbox[1])  # y is descending
            )
            with subset.rio.to_rasterio_dataset() as subset_rio:
                out_image, out_transform = rasterio.mask.mask(subset_rio, [self.boundary_gdf.union_all().buffer(0)], crop=True)
                out_meta = subset_rio.meta.copy()
                out_meta.update({
                    "driver": "GTiff",
                    "height": out_image.shape[1],
                    "width": out_image.shape[2],
                    "transform": out_transform
                })
                with rasterio.open(self.wsf_file, "w", **out_meta) as dest:
                    dest.write(out_image)


        else:
            print(f"WSF data already exists at {self.wsf_file}")
        return None

    def compare_overture_wsf(self, write_output=True):
        """
        Compare Overture building data with World Settlement Footprint (WSF) data for the city.

        Returns:
        - comparison_result (dict): Dictionary containing comparison metrics.
        """
        buildings = gpd.read_file(self.overture_file)
        with rasterio.open(self.wsf_file) as template:
            meta = template.meta.copy()
            out_shape = template.shape
            transform = template.transform

            if buildings.crs != template.crs:
                buildings = buildings.to_crs(template.crs)

            shapes = ((geom, 1) for geom in buildings.geometry)

            rasterized_buildings = rasterize(
                shapes,
                out_shape=out_shape,
                transform=transform,
                fill=0,
                all_touched=True,
                dtype=rasterio.uint8,
                merge_alg=MergeAlg.add
            )

            meta.update({"dtype": rasterio.uint8, "height": out_shape[0], "width": out_shape[1], "transform": transform})

            if write_output:
                with rasterio.open(self.overture_digitized, 'w', **meta) as dest:
                    dest.write(rasterized_buildings, 1)

            # Calculate comparison metrics between overture and WSF data
            wsf_data = template.read(1)
            wsf_binary = (wsf_data > 0).astype(rasterio.uint8)
            overture_binary = (rasterized_buildings > 0).astype(rasterio.uint8)

            wsf_overture = (overture_binary * 10) + (wsf_binary * 1)

            meta.update({"dtype": rasterio.uint8, "height": out_shape[0], "width": out_shape[1], "transform": transform})
            with rasterio.open(self.overture_wsf_comparison_file, 'w', **meta) as dest:
                dest.write(wsf_overture, 1)

    def map_building_scan(self, map_name):
        """
        Generate a map visualizing the building scan results.

        Parameters:
        - map_name (str): Type of map to produce; options include 'building_footprints', 'overture_digitized', 'wsf', 'wsf_comparison'.
        """
        if map_name == 'building_footprints':
            def get_buildling_color(val):
                return building_colours_rgb.get(val, "gray")
            gdf = gpd.read_file(self.overture_file)
            gdf.sort_values(by='area_m2', ascending=False, inplace=True)
            m = gdf.explore(column='bldg_size', 
                            categorical=True,
                            cmap='viridis',
                            #This colour mapping isn't working
                            #color=lambda val: get_buildling_color(val),
                            legend=True, tooltip=['area_m2'])
            m.save("../docs/building_footprints.html")
