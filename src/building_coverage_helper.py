import os
from datetime import date
from pathlib import Path

import geopandas as gpd
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import obstore
import pandas as pd
import rasterio
import xarray as xr
import xyzservices.providers as xyz
from dateutil.relativedelta import relativedelta
from overturemaps import geodataframe as overture_gdf
from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from rasterio.features import MergeAlg, rasterize
from rasterio.plot import show
from shapely.geometry import mapping
from zarr.storage import ObjectStore

# Create a custom StacApiIO handler
stac_api_io = StacApiIO()
stac_api_io.session.verify = False

import GOSTrocks.dataMisc as dMisc
import GOSTrocks.rasterMisc as rMisc

# FROM DRE Atlas - https://dre.energydata.info/static/DRE-Atlas-Technical-Documentation.pdf
building_definitions = {
    "large": {"color": "#118020", "min_area": 200},
    "medium": {"color": "#0000ff", "min_area": 120, "max_area": 200},
    "small": {"color": "#ff69b4", "min_area": 15, "max_area": 120},
    "very_small": {"color": "#ffb6c1", "max_area": 15},
}
building_colours = {
    "large": "#118020",
    "medium": "#0000ff",
    "small": "#ff69b4",
    "very_small": "#ffb6c1",
}

ghs_smod_defs = {
    30: ["urban centre", "#FF0000"],
    23: ["dense urban cluster", "#732600"],
    22: ["semi-dense urban cluster", "#A87000"],
    21: ["suburban grid-cell", "#FFFF00"],
    13: ["rural cluster", "#375623"],
    12: ["low density rural grid cell", "#ABCD66"],
    11: ["very low density rural grid cell", "#CDF57A"],
}

impact_landcover_def = {
    1: ["Water", "#419bdf", "Blue"],
    2: ["Trees", "#397d49", "Green"],
    4: ["Flooded Vegetation", "#7a87c6", "Purple / Indigo"],
    5: ["Crops", "#e49635", "Yellow / Orange"],
    7: ["Built Area", "#c4281b", "Red"],
    8: ["Bare Ground", "#a59b8f", "Gray / Brown"],
    9: ["Snow / Ice", "#a8ebff", "Light Blue"],
    10: ["Clouds", "#616161", "Dark Gray"],
    11: ["Rangeland", "#e3e2c3", "Light Beige"],
}


def hex_to_rgb(hex_str):
    # Remove the '#' prefix if it exists
    hex_str = hex_str.lstrip("#")
    # Convert segments to integers by specifying base 16
    return tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4))


building_colours_rgb = {
    key: hex_to_rgb(color) for key, color in building_colours.items()
}


class SPARC_city:
    """Manage building coverage datasets and analysis for one city.

    The class stores a city's boundary and output location, and provides
    methods to download and attribute Overture buildings, World Settlement
    Footprint, land-cover, and related geospatial datasets.
    """

    def __init__(self, name, boundary_series, output_dir, crs, verbose=False):
        """Initialize a city-level building coverage workflow.

        Parameters:
        - name (str): Name used to identify the city.
        - boundary_series (geopandas.Series): A row containing the city's
            boundary geometry.
        - output_dir (str or pathlib.Path): Directory for downloaded and
            derived datasets. It is created if it does not exist.
        - crs: Coordinate reference system for ``boundary_series``.
        """
        self.name = name
        self.boundary_series = boundary_series
        self.boundary_gdf = gpd.GeoDataFrame(
            boundary_series.to_frame().T, geometry="geometry", crs=crs
        ).to_crs(4326)
        self.output_dir = output_dir
        self.map_dir = os.path.join(output_dir, "maps")
        os.makedirs(self.map_dir, exist_ok=True)
        self.verbose = verbose

        self.overture_file = os.path.join(output_dir, "buildings.gpkg")
        self.smod_file = os.path.join(output_dir, "ghs_smod.tif")
        self.wsf_file = os.path.join(output_dir, "wsf.tif")
        self.landcover_file = os.path.join(
            output_dir, "landcover_impact_observatory.tif"
        )
        self.overture_digitized = os.path.join(output_dir, "overture_digitized.tif")
        self.overture_wsf_comparison_file = os.path.join(
            output_dir, "overture_wsf_comparison.tif"
        )
        self.flood_folder = os.path.join(output_dir, "flood")
        self.google_25d_file = os.path.join(
            output_dir, "google_tiles", "google_2_5d.vrt"
        )
        os.makedirs(self.flood_folder, exist_ok=True)
        self.flood_file = os.path.join(output_dir, "FATHOM_COMBINED_FLOOD.tif")

    def download_overture(self, release="2026-09-23.0"):
        """
        Download Overture data for the city.

        Parameters:
        - release (str): The release version of the Overture data to download. Defaults to "2026-09-23.0".
                        This is necessary because not defining the release causes the library to search a STAC
                        catalog to find the appropriate release, which causes SSL errors

        """
        output_file = os.path.join(self.output_dir, "buildings.gpkg")
        if not os.path.exists(output_file):
            bbox = self.boundary_gdf.total_bounds
            if self.boundary_gdf.crs != 4326:
                bbox = self.boundary_gdf.to_crs(epsg=4326).total_bounds
            if release:
                buildings_gdf = overture_gdf("building", bbox=bbox, release=release)
            else:
                buildings_gdf = overture_gdf("building", bbox=bbox)
            buildings_gdf.crs = 4326
            buildings_gdf = buildings_gdf[
                buildings_gdf.intersects(self.boundary_gdf.union_all())
            ]
            # calculate building area in metres
            buildings_gdf["area_m2"] = buildings_gdf.geometry.to_crs(epsg=6933).area
            # classify buildings based on area
            buildings_gdf["bldg_size"] = buildings_gdf["area_m2"].apply(
                lambda area: (
                    "large"
                    if area > building_definitions["large"]["min_area"]
                    else "medium"
                    if building_definitions["medium"]["min_area"]
                    <= area
                    <= building_definitions["medium"]["max_area"]
                    else "small"
                    if building_definitions["small"]["min_area"]
                    <= area
                    <= building_definitions["small"]["max_area"]
                    else "very_small"
                )
            )
            buildings_gdf.to_file(output_file, driver="GPKG")
        else:
            if self.verbose:
                print(f"Overture data already exists at {output_file}")
        return None

    def download_landcover(
        self, stac_url="https://api.impactobservatory.com/stac-aws/"
    ):
        """
        Download landcover data for the city. Right now we are leveraging the
        Impact Observatory - https://registry.opendata.aws/io-lulc/.


        Parameters:
        - stac_url (str): The URL of the STAC catalog to use for downloading landcover data. Defaults to "https://api.impactobservatory.com/stac-aws/".
        """
        if not os.path.exists(self.landcover_file):
            # Set rasterio ENV to ignore SSL
            with rasterio.Env(GDAL_HTTP_UNSAFESSL="YES"):
                landcover_folder = os.path.join(self.output_dir, "landcover")
                os.makedirs(landcover_folder, exist_ok=True)
                stac_url = stac_url
                collection_id = "io-10m-annual-lulc"

                catalog = Client.open(stac_url)

                search = catalog.search(
                    collections=[collection_id],
                    intersects=mapping(self.boundary_gdf.to_crs(4326).union_all()),
                    datetime="2024-01-01/2024-12-31",
                )

                items = list(search.items())
                all_hrefs = []
                for item in items:
                    cur_lc_r = rasterio.open(item.get_assets()["supercell"].href)
                    rMisc.clipRaster(
                        cur_lc_r,
                        self.boundary_gdf,
                        os.path.join(landcover_folder, f"landcover_{item.id}.tif"),
                    )

                # merge all tiffs in landcover folder
                landcover_tiffs = [
                    os.path.join(landcover_folder, f)
                    for f in os.listdir(landcover_folder)
                    if f.endswith(".tif")
                ]
                if landcover_tiffs:
                    with rasterio.open(landcover_tiffs[0]) as src0:
                        meta = src0.meta
                    meta.update(count=len(landcover_tiffs))
                    with rasterio.open(self.landcover_file, "w", **meta) as dst:
                        for id, layer in enumerate(landcover_tiffs, start=1):
                            with rasterio.open(layer) as src1:
                                dst.write(src1.read(1), id)
        else:
            if self.verbose:
                print(f"Landcover data already exists at {self.landcover_file}")

    def download_smod(self, path=None):
        """
        Download GHS-SMOD data for the city.

        Parameters:
        - path (str): The path to the GHS-SMOD raster file. Defaults to None.
        """
        if not os.path.exists(self.smod_file):
            if path is None:
                raise ValueError("Path to GHS-SMOD raster file must be provided.")
            os.makedirs(os.path.dirname(self.smod_file), exist_ok=True)
            smodR = rasterio.open(path)
            rMisc.clipRaster(smodR, self.boundary_gdf, self.smod_file)
        else:
            if self.verbose:
                print(f"GHS-SMOD data already exists at {self.smod_file}")

    def download_google_25d(self):
        """
        Download Google 2.5D data for the city.

        Parameters:
        - None: Uses the output_dir provided during initialization.
        """
        if not os.path.exists(self.google_25d_file):
            os.makedirs(os.path.dirname(self.google_25d_file), exist_ok=True)
            dMisc.download_google_2_5d(
                self.boundary_gdf.union_all(),
                2023,
                os.path.dirname(self.google_25d_file),
                overwrite=False,
                create_vrt=True,
            )
        else:
            if self.verbose:
                print(f"Google 2.5D data already exists at {self.google_25d_file}")

    def download_wsf(self):
        """
        Download World Settlement Footprint (WSF) data for the city.

        Parameters:
        - None: Uses the output_dir provided during initialization.
        """
        if not os.path.exists(self.wsf_file):
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
                y=slice(bbox[3], bbox[1]),  # y is descending
            )
            with subset.rio.to_rasterio_dataset() as subset_rio:
                out_image, out_transform = rasterio.mask.mask(
                    subset_rio, [self.boundary_gdf.union_all().buffer(0)], crop=True
                )
                out_meta = subset_rio.meta.copy()
                out_meta.update(
                    {
                        "driver": "GTiff",
                        "height": out_image.shape[1],
                        "width": out_image.shape[2],
                        "transform": out_transform,
                    }
                )
                with rasterio.open(self.wsf_file, "w", **out_meta) as dest:
                    dest.write(out_image)

        else:
            if self.verbose:
                print(f"WSF data already exists at {self.wsf_file}")
        return None

    def download_flood(
        self,
        flood_type=["COASTAL", "FLUVIAL", "PLUVIAL"],
        defence=["DEFENDED"],
        return_period=["1in5", "1in10", "1in50"],
        climate_model=["PERCENTILE50"],
        year=["2020"],
    ):
        """Download flood data based on the specified parameters.

        Parameters:
        - flood_type (list): List of flood types to download. Defaults to ["COASTAL", "FLUVIAL", "PLUVIAL"].
        - defence (list): List of defence types to consider. Defaults to ["DEFENDED"].
        - return_period (list): List of return periods to download. Defaults to ["1in5", "1in10", "1in50"].
        - climate_model (list): List of climate models to use. Defaults to ["PERCENTILE50"].
        - year (list): List of years to download. Defaults to ["2020"].

        """
        flood_vrts = dMisc.get_fathom_vrts(True)
        sel_vrts = flood_vrts.loc[
            (flood_vrts["FLOOD_TYPE"].isin(flood_type))
            & (flood_vrts["DEFENCE"].isin(defence))
            & (flood_vrts["RETURN"].isin(return_period))
            & (flood_vrts["CLIMATE_MODEL"].isin(climate_model))
        ]
        # For each image in the selected images dataframe, we clip out the area of interest
        #     which is defined by the ioso3 code, but could be any GeoDataFrame
        clip_aoi = self.boundary_gdf.copy()
        for idx, row in sel_vrts.iterrows():
            out_file = os.path.join(
                self.flood_folder, os.path.basename(row["PATH"]).replace(".vrt", ".tif")
            )
            if not os.path.exists(out_file):
                with rasterio.Env(GDAL_HTTP_UNSAFESSL="YES"):
                    cur_r = rasterio.open(row["PATH"])
                    if clip_aoi.crs != cur_r.crs:
                        clip_aoi = clip_aoi.to_crs(cur_r.crs)
                    rMisc.clipRaster(cur_r, clip_aoi, out_file)

    def attribute_buildings(
        self, wsf=True, landcover=True, ghs_smod=True, google_25d=True
    ):
        """Attach WSF results to buildings in the overture dataset.

        Parameters:
        - wsf (bool): Whether to attach WSF results. Default is True.
        - landcover (bool): Whether to attach landcover results. Default is True.
        - ghs_smod (path or None): Whether to attach GHS-SMOD results. Default is None, set to file path to attribute
        - google_25d (bool): Whether to attach Google 25D results. Default is True.

        """
        buildings = gpd.read_file(self.overture_file)
        if google_25d:  # and not "google_25d" in buildings.columns:
            google_25dR = rasterio.open(self.google_25d_file)
            if buildings.crs != google_25dR.crs:
                buildings = buildings.to_crs(google_25dR.crs)
            google_25d_values = list(
                google_25dR.sample(
                    [(geom.centroid.x, geom.centroid.y) for geom in buildings.geometry]
                )
            )
            buildings["google_25d"] = [
                val[1] for val in google_25d_values
            ]  # the second band of the VRT is height
            # Calculate estimated volume based on Google 25D height
            buildings["google_25d_volume"] = (
                buildings["google_25d"] * buildings.geometry.area
            )
            # estimate number of floors
            buildings["google_25d_floors"] = (buildings["google_25d"] / 3).apply(
                np.ceil
            )
        if wsf and "wsf" not in buildings.columns:
            wsfR = rasterio.open(self.wsf_file)
            if buildings.crs != wsfR.crs:
                buildings = buildings.to_crs(wsfR.crs)
            wsf_values = list(
                wsfR.sample(
                    [(geom.centroid.x, geom.centroid.y) for geom in buildings.geometry]
                )
            )
            buildings["wsf"] = [val[0] for val in wsf_values]
            # convert the wsf number to the date it was built. A value of 1 indicates the building was built before
            #  2016-07-01, and every epoch represents an increasing 6-month interval.
            buildings["wsf_date_built"] = buildings["wsf"].apply(
                lambda x: (
                    date(2016, 1, 1) + relativedelta(months=x * 6) if x > 0 else pd.NaT
                )
            )
            # if wsf = 0, set the date built to NaT (Not a Time)
            buildings.loc[buildings["wsf"] == 0, "wsf_date_built"] = pd.NaT

        if landcover and "landcover" not in buildings.columns:
            landcoverR = rasterio.open(self.landcover_file)
            if buildings.crs != landcoverR.crs:
                buildings = buildings.to_crs(landcoverR.crs)
            landcover_values = list(
                landcoverR.sample(
                    [(geom.centroid.x, geom.centroid.y) for geom in buildings.geometry]
                )
            )
            buildings["landcover"] = [val[0] for val in landcover_values]
            buildings["landcover_label"] = buildings["landcover"].map(
                lambda x: impact_landcover_def.get(x, ["Unknown", "#000000"])[0]
            )

        if ghs_smod and "ghs_smod_label" not in buildings.columns:
            ghs_smodR = rasterio.open(self.smod_file if ghs_smod is True else ghs_smod)
            if buildings.crs != ghs_smodR.crs:
                buildings = buildings.to_crs(ghs_smodR.crs)
            ghs_smod_values = list(
                ghs_smodR.sample(
                    [(geom.centroid.x, geom.centroid.y) for geom in buildings.geometry]
                )
            )
            buildings["ghs_smod"] = [val[0] for val in ghs_smod_values]
            buildings["ghs_smod_label"] = buildings["ghs_smod"].map(
                lambda x: ghs_smod_defs.get(x, ["Unknown", "#000000"])[0]
            )
        buildings.to_file(self.overture_file)
        return buildings

    def summarize_geospatial_data(self):        
        """
        Summarize geospatial data for the city, including buildings, landcover, and GHS-SMOD.

        Parameters:
        None

        Returns:
        - summary (dict): Dictionary containing summary statistics for each geospatial dataset.
        """
        buildings = gpd.read_file(self.overture_file)
        summary = {}
        summary["num_buildings"] = buildings["bldg_size"].value_counts().to_dict()
        if "wsf" in buildings.columns:
            summary["wsf_date_built"] = (
                buildings["wsf_date_built"].value_counts().to_dict()
            )
        if "landcover" in buildings.columns:
            summary["landcover"] = buildings["landcover"].value_counts().to_dict()
        if "ghs_smod" in buildings.columns:
            summary["ghs_smod"] = buildings["ghs_smod"].value_counts().to_dict()

        # go through the raw raster data extracted for the AOI and summarize those
        for raster_name, raster_file in [
            ("wsf", self.wsf_file),
            ("landcover", self.landcover_file),
            ("ghs_smod", self.smod_file),
        ]:
            if os.path.exists(raster_file):
                curR = rasterio.open(raster_file)
                curD = curR.read(1)
                # Get counts of unique values in the raster
                unique, counts = np.unique(curD, return_counts=True)
                summary[raster_name + "_raster"] = dict(
                    zip(unique.tolist(), counts.tolist())
                )

        return summary

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
                merge_alg=MergeAlg.add,
            )

            meta.update(
                {
                    "dtype": rasterio.uint8,
                    "height": out_shape[0],
                    "width": out_shape[1],
                    "transform": transform,
                }
            )

            if write_output:
                with rasterio.open(self.overture_digitized, "w", **meta) as dest:
                    dest.write(rasterized_buildings, 1)

            # Calculate comparison metrics between overture and WSF data
            wsf_data = template.read(1)
            wsf_binary = (wsf_data > 0).astype(rasterio.uint8)
            overture_binary = (rasterized_buildings > 0).astype(rasterio.uint8)

            wsf_overture = (overture_binary * 10) + (wsf_binary * 1)

            meta.update(
                {
                    "dtype": rasterio.uint8,
                    "height": out_shape[0],
                    "width": out_shape[1],
                    "transform": transform,
                }
            )
            with rasterio.open(self.overture_wsf_comparison_file, "w", **meta) as dest:
                dest.write(wsf_overture, 1)

    def map_building_scan(self, map_name, output_file):
        """
        Generate a map visualizing the building scan results.

        Parameters:
        - map_name (str): Type of map to produce; options include 'building_footprints', 'overture_digitized', 'wsf', 'wsf_comparison'.
        """
        if map_name == "wsf":
            curR = rasterio.open(self.wsf_file)
            curD = curR.read(1)

            wsf_values, wsf_counts = np.unique(curD, return_counts=True)

            cmap = plt.get_cmap("magma")
            # BoundaryNorm ensures each integer class gets assigned to its exact color index
            bounds = np.append(wsf_values, wsf_values[-1] + 1)
            norm = mcolors.BoundaryNorm(bounds, cmap.N)

            resampled_cmap = plt.get_cmap("magma", wsf_values.max())
            wsf_colours = [resampled_cmap(i) for i in wsf_values]
            wsf_labels = pd.to_datetime("2016-07-01") + pd.to_timedelta(
                (wsf_values[1:].astype(int)) * 6 * 30, unit="days"
            )
            wsf_labels = wsf_labels.strftime("%Y-%m-%d")

            # Add colour and label for value 0 as gray and no data
            wsf_labels = ["No Data"] + list(wsf_labels)
            fig, ax = plt.subplots(figsize=(10, 10))
            ep = show(curD, transform=curR.transform, ax=ax, cmap=cmap, norm=norm)

            # custom legend
            legend_patches = [
                plt.Line2D(
                    [0],
                    [0],
                    marker="s",
                    color="w",
                    markerfacecolor=wsf_colours[i],
                    markersize=15,
                    label=wsf_labels[i],
                )
                for i in range(len(wsf_values))
            ]
            ax.legend(
                handles=legend_patches, loc="upper right", bbox_to_anchor=(1.2, 1)
            )

            ax.set_title("WSF Map", fontsize=16)
            plt.savefig(output_file, bbox_inches="tight")
            plt.close()

        if map_name == "landcover":
            curR = rasterio.open(self.landcover_file)
            curD = curR.read(1)
            lc_values, lc_counts = np.unique(curD, return_counts=True)
            lc_colours = [
                impact_landcover_def.get(val, ["Unknown", "#D3D3D3", "Unknown"])[1]
                for val in lc_values
            ]
            labels = [
                impact_landcover_def.get(val, ["Unknown", "#D3D3D3", "Unknown"])[0]
                for val in lc_values
            ]

            cmap = mcolors.ListedColormap(lc_colours)
            # BoundaryNorm ensures each integer class gets assigned to its exact color index
            bounds = np.append(lc_values, lc_values[-1] + 1)
            norm = mcolors.BoundaryNorm(bounds, cmap.N)

            fig, ax = plt.subplots(figsize=(10, 10))
            ep = show(curD, transform=curR.transform, ax=ax, cmap=cmap, norm=norm)

            # custom legend
            legend_patches = [
                plt.Line2D(
                    [0],
                    [0],
                    marker="s",
                    color="w",
                    markerfacecolor=lc_colours[i],
                    markersize=15,
                    label=labels[i],
                )
                for i in range(len(lc_values))
            ]
            ax.legend(
                handles=legend_patches, loc="upper right", bbox_to_anchor=(1.2, 1)
            )
            ax.set_title("Landcover Map", fontsize=16)

            plt.savefig(output_file, bbox_inches="tight")
            plt.close()

        if map_name == "building_footprints":
            # folium maps are interactive HTML, not images
            output_file = str(Path(output_file).with_suffix(".html"))

            def get_building_color(val):
                return building_colours_rgb.get(val, "gray")

            gdf = gpd.read_file(self.overture_file)
            gdf.sort_values(by="area_m2", ascending=False, inplace=True)
            m = gdf.explore(
                column="bldg_size",
                categorical=True,
                cmap="viridis",
                # This colour mapping isn't working
                # color=lambda val: get_building_color(val),
                legend=True,
                tooltip=["area_m2"],
                tiles=xyz.Esri.WorldImagery,
            )
            # Add the boundary outline to the map as a hollow polygon with thick black edge
            self.boundary_gdf.boundary.explore(m=m, color="black", weight=3)
            m.save(output_file)

        return output_file

    def write_markdown_summary(self, summary=None, map_files=None, output_file=None):
        """
        Combine the geospatial summary and generated maps into a single markdown report.

        Parameters:
        - summary (dict): Output of summarize_geospatial_data. Computed if None.
        - map_files (list of str): Paths to map files (png/jpg/html). Defaults to all files in self.map_dir.
        - output_file (str): Path of the markdown file. Defaults to <output_dir>/<name>_summary.md.

        Returns:
        - output_file (str): Path to the written markdown file.
        """
        if summary is None:
            summary = self.summarize_geospatial_data()
        if output_file is None:
            output_file = os.path.join(self.output_dir, f"{self.name}_summary.md")
        if map_files is None:
            map_files = sorted(
                os.path.join(self.map_dir, f)
                for f in os.listdir(self.map_dir)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".html"))
            )

        def wsf_label(val):
            val = int(val)
            return (
                "No Data"
                if val <= 0
                else (date(2016, 1, 1) + relativedelta(months=val * 6)).isoformat()
            )

        def label_value(key, val):
            if key.startswith("landcover"):
                return impact_landcover_def.get(int(val), ["Unknown"])[0]
            if key.startswith("ghs_smod"):
                return ghs_smod_defs.get(int(val), ["Unknown"])[0]
            if key == "wsf_raster":
                return wsf_label(val)
            return str(val)

        def make_table(key, counts, value_header, count_header):
            total = sum(counts.values())
            rows = [
                f"| {value_header} | {count_header} | Percent |",
                "| --- | ---: | ---: |",
            ]
            for val, cnt in sorted(counts.items(), key=lambda kv: str(kv[0])):
                pct = (cnt / total * 100) if total else 0
                rows.append(f"| {label_value(key, val)} | {cnt:,} | {pct:.1f}% |")
            rows.append(f"| **Total** | **{total:,}** | 100.0% |")
            return "\n".join(rows)

        section_titles = {
            "num_buildings": ("Buildings by size class", "Size class", "Buildings"),
            "wsf_date_built": (
                "Buildings by WSF construction date",
                "Date built",
                "Buildings",
            ),
            "landcover": ("Buildings by landcover", "Landcover", "Buildings"),
            "ghs_smod": (
                "Buildings by GHS-SMOD class",
                "Settlement class",
                "Buildings",
            ),
            "wsf_raster": ("WSF raster pixels", "Date built", "Pixels"),
            "landcover_raster": ("Landcover raster pixels", "Landcover", "Pixels"),
            "ghs_smod_raster": ("GHS-SMOD raster pixels", "Settlement class", "Pixels"),
        }

        lines = [f"# Building scan summary: {self.name}", ""]
        if "num_buildings" in summary:
            lines += [
                f"Total Overture buildings: **{sum(summary['num_buildings'].values()):,}**",
                "",
            ]

        lines += ["## Summary statistics", ""]
        for key, counts in summary.items():
            title, value_header, count_header = section_titles.get(
                key, (key, "Value", "Count")
            )
            lines += [
                f"### {title}",
                "",
                make_table(key, counts, value_header, count_header),
                "",
            ]

        if map_files:
            lines += ["## Maps", ""]
            md_dir = os.path.dirname(os.path.abspath(output_file))
            for map_file in map_files:
                rel_path = Path(
                    os.path.relpath(os.path.abspath(map_file), md_dir)
                ).as_posix()
                title = Path(map_file).stem.replace("_", " ").title()
                lines += [f"### {title}", ""]
                if map_file.lower().endswith(".html"):
                    # The html needs to be an embeded iframe for proper display
                    lines.append(
                        f'<iframe src="{rel_path}" width="100%" height="600px"></iframe>'
                    )
                else:
                    lines.append(f"![{title}]({rel_path})")
                lines.append("")

        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return output_file
