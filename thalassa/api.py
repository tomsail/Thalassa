from __future__ import annotations

import logging
import operator
import os
import pathlib
import typing
from functools import reduce

import geopandas as gpd
import geoviews as gv
import holoviews as hv
import numpy as np
import pandas as pd
import shapely.wkt
import xarray as xr
from bokeh.models import HoverTool
from bokeh.models.formatters import DatetimeTickFormatter
from holoviews import opts as hvopts
from holoviews.operation.datashader import dynspread
from holoviews.operation.datashader import rasterize
from holoviews.streams import PointerXY
from holoviews.streams import Selection1D
from holoviews.streams import Stream
from holoviews.streams import Tap

from . import normalization
from . import utils

logger = logging.getLogger(__name__)


def open_dataset(
    path: str | os.PathLike[str],
    load: bool = False,
    normalize: bool = True,
    **kwargs: dict[str, typing.Any],
) -> xr.Dataset:
    """
    Open the file specified in ``path`` using ``xarray`` and return an `xr.Dataset``

    If ``normalize`` is ``True``, then the file is converted/normalized to the ``Thalassa`` schema.
    Normalization only works for ``SCHISM`` and ``ADCIRC`` netcdf files.
    """
    path = pathlib.Path(path)
    default_kwargs: dict[str, typing.Any] = dict(mask_and_scale=True, cache=False)
    if path.suffix in (".nc", ".netcdf"):
        # ADCIRC datasets are not compatible with xarray:
        # They fail with the error:
        #   "dimension 'neta' already exists as a scalar variable",
        #   "dimension 'nvel' already exists as a scalar variable",
        # Source: https://github.com/pydata/xarray/issues/1709#issuecomment-343714896
        #
        # The workaround we have for this is to drop the problematic variables
        # As an implementation detail we make use of an xarray "feature" which ignores
        # non-existing names in `drop_variables`. So we can use `drop_variables=[...]` even
        # if we are opening a dataset from a different solver.
        # This may cause  issues if different solvers use `neta/nvel` as dimension/variable
        # names, but at least for now it seems to be good enough.
        default_kwargs["engine"] = "netcdf4"
        default_kwargs["drop_variables"] = ["neta", "nvel", "max_nvdll", "max_nvell"]
    elif path.suffix in (".zarr", ".zip") or path.is_dir():
        default_kwargs["engine"] = "zarr"
    # TODO: extend with GeoTiff, Grib etc
    else:
        raise ValueError(f"Don't know how to handle this: {path}")
    ds = xr.open_dataset(
        path, **{**default_kwargs, **kwargs}  #  When we drop python3.8 we will be able to use PEP584 syntax
    )
    if normalize:
        ds = normalization.normalize_dataset(ds)
    if load:
        # load dataset to memory
        ds.load()
    return ds


def get_dtf() -> DatetimeTickFormatter:
    dtf = DatetimeTickFormatter(
        hours="%m/%d %H:%M",
        days="%m/%d %H",
        months="%Y/%m/%d",
        years="%Y/%m",
    )
    return dtf


def create_trimesh(
    ds: xr.Dataset,
    variable: str,
    timestamp: str | pd.Timestamp | None = None,
    layer: int | None = None,
) -> gv.TriMesh:
    columns = ["lon", "lat", variable]
    if layer is not None:
        ds = ds.isel(layer=layer)
    if timestamp == "max":
        points_df = ds[columns].max("time").to_dataframe()
    elif timestamp == "min":
        points_df = ds[columns].min("time").to_dataframe()
    elif timestamp:
        points_df = ds.sel({"time": timestamp})[columns].to_dataframe().drop(columns="time")
    else:
        points_df = ds[columns].to_dataframe()
    points_df = points_df.reset_index(drop=True)
    points_gv = gv.Points(points_df, kdims=["lon", "lat"], vdims=[variable])
    trimesh = gv.TriMesh((ds.triface_nodes.values, points_gv))
    return trimesh


def get_tiles() -> gv.Tiles:
    tiles = gv.WMTS("http://c.tile.openstreetmap.org/{Z}/{X}/{Y}.png")
    return tiles


def get_wireframe(
    trimesh: gv.TriMesh,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
) -> gv.DynamicMap:
    kwargs = dict(element=trimesh.edgepaths, precompute=True)
    if x_range:
        kwargs["x_range"] = x_range
    if y_range:
        kwargs["y_range"] = y_range
    wireframe = dynspread(rasterize(**kwargs)).opts(tools=[], cmap=["black"])
    return wireframe


def get_raster(
    trimesh: gv.TriMesh,
    title: str = "",
    clabel: str = "",
    clim_min: float | None = None,
    clim_max: float | None = None,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
) -> gv.DynamicMap:
    kwargs = dict(element=trimesh, precompute=True)
    if x_range:
        kwargs["x_range"] = x_range
    if y_range:
        kwargs["y_range"] = y_range
    logger.debug("rasterize kwargs: %s", kwargs)
    raster = rasterize(**kwargs).opts(
        cmap="viridis",
        clabel=clabel,
        colorbar=True,
        clim=(clim_min, clim_max),
        title=title,
        tools=["hover"],
    )
    return raster


def get_bbox_from_raster(raster: gv.DynamicMap) -> hv.core.boundingregion.BoundingBox:
    # XXX Even though they seem the same,
    #       raster[()]
    # and
    #       raster.values[0]
    # are not exactly the same. The latter one throws IndexErrors if you run
    # it too soon after the creation of the raster!
    image = raster[()]
    bbox = image.bounds
    return bbox


def get_x_range_from_bbox(bbox: hv.core.boundingregion.BoundingBox) -> tuple[float, float]:
    aarect = bbox.aarect()
    x_range = (aarect.left(), aarect.right())
    return x_range


def get_y_range_from_bbox(bbox: hv.core.boundingregion.BoundingBox) -> tuple[float, float]:
    aarect = bbox.aarect()
    y_range = (aarect.bottom(), aarect.top())
    return y_range


def is_point_in_the_mesh(raster: gv.DynamicMap, lon: float, lat: float) -> bool:
    """Return `True` if the point is inside the mesh of the `raster`, `False` otherwise"""
    raster_dataset = raster.values()[0].data
    data_var_name = raster.ddims[-1].name
    interpolated = raster_dataset[data_var_name].interp(dict(lon=lon, lat=lat)).values
    return typing.cast(bool, ~np.isnan(interpolated))


def _get_stream_timeseries(
    ds: xr.Dataset,
    variable: str,
    source_raster: gv.DynamicMap,
    stream_class: Stream,
    layer: int | None = None,
) -> gv.DynamicMap:
    if stream_class not in {Tap, PointerXY}:
        raise ValueError("Unsupported Stream class. Please choose either Tap or PointerXY")

    if layer is not None:
        ds = ds.isel(layer=layer)
    ds = ds[["lon", "lat", variable]]

    def callback(x: float, y: float) -> hv.Curve:
        if not is_point_in_the_mesh(raster=source_raster, lon=x, lat=y):
            # if the point is not inside the mesh, then omit the timeseries
            title = f"Lon={x:.3f} Lat={y:.3f}"
            plot = hv.Curve([])
        else:
            node_index = utils.get_index_of_nearest_node(ds=ds, lon=x, lat=y)
            ts = ds.isel(node=node_index)
            title = f"Lon={ts.lon.values:.3f} Lat={ts.lat.values:.3f}"
            plot = hv.Curve(ts[variable])
            plot = plot.redim(
                variable,
                range=(ts[variable].min(), ts[variable].max()),
            )
        # setup hover
        hover = HoverTool(
            tooltips=[("time", "@time{%F %T}"), (f"{variable}", f"@{variable}")],
            formatters={
                "@time": "datetime",
            },
        )
        # apply opts
        plot = plot.opts(
            title=title,
            framewise=True,
            padding=0.05,
            show_grid=True,
            tools=[hover],
            xformatter=get_dtf(),
        )
        return plot

    stream = stream_class(x=0, y=0, source=source_raster)
    dmap = gv.DynamicMap(callback, streams=[stream])
    return dmap


def get_station_timeseries(
    stations: xr.Dataset,
    pins: gv.DynamicMap,
) -> hv.DynamicMap:
    def callback(index: list[int]) -> hv.Curve:
        # sometimes there are multiple pins with the same lon/lat
        # When one of these pins gets selected index contains the indices of both pins
        # This causes an exception to be raised.
        # TODO: Until we decide how to resolve this, we just pick the first pin, no matter what.
        if len(index) >= 2:
            logger.warning("TS: multiple pins selected: %r", index)
            index = [index[0]]
        logger.warning("TS: Choosing the first one: %r", index)
        columns = ["stime", "elev_sim", "time", "elev_obs"]
        if not index:
            title = "No stations selected"
            ds = pd.DataFrame(columns=columns)
        else:
            df = pins.data
            title = df.iloc[index[0]].location
            ds = stations.isel(node=df.index[index])[columns]
        dataset = hv.Dataset(ds)
        curve1 = hv.Curve(dataset, kdims=["stime"], vdims=["elev_sim"], label="Simulation")
        curve2 = hv.Curve(dataset, kdims=["time"], vdims=["elev_obs"], label="Observation")
        components = [curve1, curve2]
        overlay = reduce(operator.mul, components).opts(
            hvopts.Curve(
                padding=0.05,
                title=title,
                framewise=True,
                xlabel="Time",
                ylabel="Elevation",
                tools=["hover"],
                xformatter=get_dtf(),
            ),
        )
        return overlay

    stream = Selection1D(source=pins, index=[])
    dmap = hv.DynamicMap(callback, streams=[stream])
    return dmap


_STATION_VARIABLES = [
    "ioc_code",
    "lat",
    "lon",
    "location",
    "Mean Absolute Error",
    "RMSE",
    "Scatter Index",
    "percentage RMSE",
    "BIAS or mean error",
    "Standard deviation of residuals",
    "Correlation Coefficient",
    "R^2",
    "Nash-Sutcliffe Coefficient",
    "lamda index",
]


def get_station_table(
    stations: xr.Dataset,
    pins: gv.DynamicMap,
) -> hv.DynamicMap:
    def callback(index: list[int]) -> hv.Table:
        # sometimes there are multiple pins with the same lon/lat
        # When one of these pins gets selected index contains the indices of both pins
        # This causes an exception to be raised.
        # TODO: Until we decide how to resolve this, we just pick the first pin, no matter what.
        if len(index) >= 2:
            logger.warning("ST: multiple pins selected: %r", index)
            index = [index[0]]
        logger.warning("ST: Choosing the first one: %r", index)
        if not index:
            df = pd.DataFrame(columns=["attribute", "value"]).set_index("attribute")
        else:
            ds = stations.isel(node=pins.data.index[index])
            df = ds[_STATION_VARIABLES].to_dataframe().T
            df.index.name = "attribute"
            df.columns = ["value"]
        table = hv.Table(df, kdims=["attribute"])
        return table

    stream = Selection1D(source=pins, index=[])
    dmap = hv.DynamicMap(callback, streams=[stream])
    return dmap


def get_station_pins(stations: xr.Dataset) -> gv.Points:
    df = stations[["lon", "lat", "location"]].to_dataframe()
    pins = gv.Points(df, kdims=["lon", "lat"], vdims=["location"])
    pins = pins.opts(color="red", marker="circle_dot", size=10, tools=["tap", "hover"])
    return pins


def get_tap_timeseries(
    ds: xr.Dataset,
    variable: str,
    source_raster: gv.DynamicMap,
    layer: int | None = None,
) -> gv.DynamicMap:
    dmap = _get_stream_timeseries(
        ds=ds,
        variable=variable,
        source_raster=source_raster,
        stream_class=Tap,
        layer=layer,
    )
    return dmap


def get_pointer_timeseries(
    ds: xr.Dataset,
    variable: str,
    source_raster: gv.DynamicMap,
    layer: int | None = None,
) -> gv.DynamicMap:
    dmap = _get_stream_timeseries(
        ds=ds,
        variable=variable,
        source_raster=source_raster,
        stream_class=PointerXY,
        layer=layer,
    )
    return dmap


def extract_timeseries(ds: xr.Dataset, variable: str, lon: float, lat: float) -> xr.DataArray:
    index = utils.get_index_of_nearest_node(ds=ds, lon=lon, lat=lat)
    # extracted = ds[[variable, "lon", "lat"]].isel(node=index)
    return ds[variable].isel(node=index)


# def plot_timeseries(ds: xr.DataArray, lon: float, lat: float) -> gv.DynamicMap:
#     node_index = utils.get_index_of_nearest_node(ds=ds, lon=lon, lat=lat)
#     node_lon = ds.lon.isel(node_index)
#     node_lat = ds.lat.isel(node_index)
#     title = f"Lon={x:.3f} Lat={y:.3f} - {node_lon}, {node_lat}"
#     plot = (
#         hv.Curve(ds)
#         .redim(variable, range=(ts.min(), ts.max()))
#         .opts(title=title, framewise=True, padding=0.05, show_grid=True)
#     )
#     return plot


def generate_mesh_polygon(ds: xr.Dataset) -> gpd.GeoDataFrame:
    logger.debug("Starting polygon generation")
    # Get the indexes of the nodes
    first_nodes = ds.node.values[ds.triface_nodes.values[:, 0]]
    second_nodes = ds.node.values[ds.triface_nodes.values[:, 1]]
    third_nodes = ds.node.values[ds.triface_nodes.values[:, 2]]

    # Get the lons/lats of the nodes
    first_lons = ds.lon.values[first_nodes]
    first_lats = ds.lat.values[first_nodes]
    second_lons = ds.lon.values[second_nodes]
    second_lats = ds.lat.values[second_nodes]
    third_lons = ds.lon.values[third_nodes]
    third_lats = ds.lat.values[third_nodes]

    # Stack the coords, one polygon per line
    polygons_per_line = np.vstack(
        (
            first_lons,
            first_lats,
            second_lons,
            second_lats,
            third_lons,
            third_lats,
            first_lons,
            first_lats,
        ),
    ).T

    # Re-stack the polygon coords. This time we should have 4 points per
    polygons_coords = np.stack(
        (
            polygons_per_line[:, :2],
            polygons_per_line[:, 2:4],
            polygons_per_line[:, 4:6],
            polygons_per_line[:, 6:8],
        ),
        axis=1,
    )
    # sanity check
    if polygons_coords.shape[1:] != (4, 2):
        raise ValueError("Something went wrong")

    # generate Polygon instance
    polygons = shapely.polygons(polygons_coords)
    polygon = shapely.coverage_union_all(polygons)

    # convert to GeoDataFrame
    gdf = gpd.GeoDataFrame(geometry=[polygon])

    logger.info("Polygon: generated")

    return gdf
