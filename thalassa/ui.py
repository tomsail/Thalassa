from __future__ import annotations

import gc
import glob
import logging
import operator
import os.path
import pathlib
from functools import reduce

import geoviews as gv
import holoviews as hv
import panel as pn
import param
import xarray as xr

from . import api
from . import utils
from . import normalization


logger = logging.getLogger(__name__)
logger.error(logger.handlers)

DATA_DIR = "./data/"
DATA_GLOB = DATA_DIR + os.path.sep + "*"

MAIN_WIDTH = 1450


MISSING_DATA_DIR = pn.pane.Alert(
    f"## Directory <{DATA_DIR}> is missing. Please create it and add some suitable netcdf files.",
    alert_type="danger",
)
EMPTY_DATA_DIR = pn.pane.Alert(
    f"## Directory <{DATA_DIR}> exists but it is empty. Please add some suitable netcdf files.",
    alert_type="danger",
)
CHOOSE_FILE = pn.pane.Alert(
    "## Please select a *Dataset* and click on the **Render** button.",
    alert_type="info",
)
UNKNOWN_FORMAT = pn.pane.Alert(
    f"## The selected dataset is in an unknown format. Please choose a different file.",
    alert_type="danger",
)
PLEASE_RENDER = pn.pane.Alert(
    f"## Please click on the **Render** button to visualize the selected *Variable*",
    alert_type="info",
)


def choose_initial_message() -> pn.pane.Alert:
    if not pathlib.Path(DATA_DIR).is_dir():
        message = MISSING_DATA_DIR
    elif not sorted(filter(utils.can_be_opened_by_xarray, glob.glob(DATA_GLOB))):
        message = EMPTY_DATA_DIR
    else:
        message = CHOOSE_FILE
    return pn.Row(message, width=MAIN_WIDTH, sizing_mode="scale_width")


# Create a custom FloatInput without a spinner
class FloatInputNoSpinner(pn.widgets.input._FloatInputBase):
    pass


class ThalassaUI:  # pylint: disable=too-many-instance-attributes
    """
    This UI is supposed to be used with a Bootstrap-like template supporting
    a "main" and a "sidebar":
    - `sidebar` will contain the widgets that control what will be rendered in the main area.
      E.g. things like which `source_file` to use, which timestamp to render etc.
    - `main` will contain the rendered graphs.
    In a nutshell, an instance of the `UserInteface` class will have two private attributes:
    - `_main`
    - `_sidebar`
    These objects should be of `pn.Column` type. You can append
    """

    def __init__(self) -> None:
        self._dataset: xr.Dataset
        self._tiles: gv.Tiles = api.get_tiles()
        self._mesh: gv.DynamicMap | None = None
        self._raster: gv.DynamicMap | None = None

        # UI components
        self._main = pn.Column(CHOOSE_FILE, width=MAIN_WIDTH, sizing_mode="fixed")
        self._sidebar = pn.Column(sizing_mode="stretch_width")

        # Define widgets
        self.dataset_file = pn.widgets.Select(
            name="Dataset file",
            options=[""] + sorted(filter(utils.can_be_opened_by_xarray, glob.glob(DATA_GLOB))),
        )
        self.variable = pn.widgets.Select(name="Variable")
        self.layer = pn.widgets.Select(name="Layer")
        self.time = pn.widgets.Select(name="Time")
        self.keep_zoom = pn.widgets.Checkbox(name="Keep Zoom", value=True)
        self.show_mesh = pn.widgets.Checkbox(name="Show Mesh")
        self.show_timeseries = pn.widgets.Checkbox(name="Show Timeseries")
        self.show_stations = pn.widgets.Checkbox(name="Show Stations")
        self.render_button = pn.widgets.Button(name="Render", button_type="primary")

        # Setup UI
        self._sidebar.append(
            pn.WidgetBox(
                self.dataset_file,
                self.variable,
                self.layer,
                self.time,
                pn.Row(self.keep_zoom, self.show_mesh, sizing_mode="stretch_width"),
                pn.Row(self.show_timeseries, self.show_stations, sizing_mode="stretch_width"),
                sizing_mode="stretch_width",
            )
        )
        self._sidebar.append(self.render_button)
        logger.debug("UI setup: done")

        # Define callback
        self.dataset_file.param.watch(fn=self._update_dataset_file, parameter_names="value")
        self.variable.param.watch(fn=self._on_variable_change, parameter_names="value")
        self.render_button.on_click(callback=self._update_main)
        logger.debug("Callback definitions: done")

        self._reset_ui(message=choose_initial_message())

    def _reset_ui(self, message: pn.pane.Alert) -> None:
        self.variable.param.set_param(options=[], disabled=True)
        self.time.param.set_param(options=[], disabled=True)
        self.layer.param.set_param(options=[], disabled=True)
        self.keep_zoom.param.set_param(disabled=True)
        self.show_mesh.param.set_param(disabled=True)
        self.show_timeseries.param.set_param(disabled=True)
        self.show_stations.param.set_param(disabled=True)
        self.render_button.param.set_param(disabled=True)
        self._main.objects = [message]
        self._mesh = None
        self._raster = None

    def _update_dataset_file(self, event: param.Event) -> None:
        logger.debug(event)
        # local variables
        dataset_file = self.dataset_file.value

        if not dataset_file:
            logger.debug("No dataset has been selected. Resetting the UI.")
            self._reset_ui(message=CHOOSE_FILE)
        else:
            try:
                logger.debug("Trying to normalize the selected dataset: %s", dataset_file)
                self._dataset = normalization.normalize_dataset(utils.open_dataset(dataset_file, load=False))
            except ValueError as exc:
                logger.exception("Normalization failed. Resetting the UI")
                self._reset_ui(message=UNKNOWN_FORMAT)
            else:
                logger.exception("Normalization succeeded. Setting widgets")
                variables = utils.filter_visualizable_data_vars(
                    self._dataset, self._dataset.data_vars.keys()
                )
                self.variable.param.set_param(options=variables, value=variables[0], disabled=False)
                self.keep_zoom.param.set_param(disabled=False)
                self.show_mesh.param.set_param(disabled=False)
                self.show_stations.param.set_param(disabled=False)
                # self.show_timeseries.param.set_param(disabled=False)
                self._main.objects = [PLEASE_RENDER]

    def _on_variable_change(self, event: param.Event) -> None:
        logger.warning(event)
        try:
            ds = self._dataset
            variable = self.variable.value
            # handle layer
            if variable and "layer" in ds[variable].dims:
                layers = ds.layer.values.tolist()
                self.layer.disabled = False
                self.layer.param.set_param(options=layers)  # , value=layers[0])
            else:
                self.layer.param.set_param(options=[])
                self.layer.disabled = True
            # handle time
            if variable and "time" in ds[variable].dims:
                self.show_timeseries.disabled = False
                self.time.disabled = False
                self.time.param.set_param(options=["max"] + list(ds.time.values))
            else:
                self.show_timeseries.disabled = True
                self.time.disabled = True
                self.time.param.set_param(options=[])
            self.render_button.param.set_param(disabled=False)
        except:
            logger.exception("error layer")

    def _debug_ui(self) -> None:
        logger.info("Widget values:")
        widgets = [obj for (name, obj) in self.__dict__.items() if isinstance(obj, pn.widgets.Widget)]
        for widget in widgets:
            logger.error("%s: %s", widget.name, widget.value)

    def _get_spinner(self) -> pn.Column:
        """Return a `pn.Column` with an horizontally/vertically aligned spinner."""
        column = pn.Column(
            pn.layout.Spacer(height=100),
            pn.Row(
                pn.layout.HSpacer(),
                pn.Row(pn.indicators.LoadingSpinner(value=True, width=150, height=150)),
            ),
        )
        return column

    def _get_colorbar_row(self, raster: gv.DynamicMap) -> pn.Row:
        clim_min = FloatInputNoSpinner(name="min")
        clim_max = FloatInputNoSpinner(name="max")
        clim_apply = pn.widgets.Button(name="Apply", button_type="primary", align="end")
        clim_reset = pn.widgets.Button(name="reset", button_type="primary", align="end")
        # Set Input widgets JS callbacks
        clim_min.jslink(raster, value="color_mapper.low")
        clim_max.jslink(raster, value="color_mapper.high")
        # Set button JS callbacks
        clim_apply.jscallback(
            clicks="""
                console.log(clim_min.value)
                console.log(clim_max.value)
                console.log(raster.right[0].color_mapper.low)
                console.log(raster.right[0].color_mapper.high)
                raster.right[0].color_mapper.low = clim_min.value
                raster.right[0].color_mapper.high = clim_max.value
                raster.right[0].color_mapper.change.emit()
            """,
            args={"raster": raster, "clim_min": clim_min, "clim_max": clim_max},
        )
        clim_reset.jscallback(
            clicks="""
                //clim_min.value = null
                //clim_max.value = null
                raster.right[0].color_mapper.low = null
                raster.right[0].color_mapper.high = null
                raster.right[0].color_mapper.change.emit()
            """,
            args={"raster": raster, "clim_min": clim_min, "clim_max": clim_max},
        )
        spacer =pn.layout.HSpacer()
        row = pn.Row(
            clim_min, clim_max, *[spacer] * 2, clim_apply, clim_reset,
            width=MAIN_WIDTH,
        )
        return row

    def _update_main(self, event: param.Event) -> None:
        try:
            # XXX For some reason, which I can't understand
            # Inside this specific callback, the logger requires to be WARN and above...
            logger.warning("Updating main")
            self._debug_ui()

            # First of all, retrieve the lon and lat ranges of the previous plot (if there is one)
            # This will allow us to restore the zoom level after re-clicking on the Render button.
            if self.keep_zoom.value and self._raster:
                print(self._raster.streams)
                lon_range = self._raster.range("lon")
                lat_range = self._raster.range("lat")
            else:
                lon_range = None
                lat_range = None
            logger.error("lon_range: %s", lon_range)
            logger.error("lat_range: %s", lat_range)

            # Since each graph takes up to a few GBs of RAM, before we create the new graph we should
            # remove the old one. In order to do so we need to remove *all* the references to the old
            # raster. This includes: - the `_main` column
            # For the record, we render a Spinner in order to show to the users that computations are
            # happening behind the scenes
            self._main_plot = None
            self._raster = None
            self._main.objects = [*self._get_spinner().objects]

            # Now let's make an explicit call to `gc.collect()`. This will make sure
            # that the references to the old raster are really removed before the creation
            # of the new one, thus RAM usage should remain low(-ish).
            gc.collect()

            # Each time a graph is rendered, data are loaded from the dataset
            # This increases the RAM usage over time. E.g. when loading the second variable,
            # the first one remains in RAM.
            # In order to avoid this, we re-open the dataset in order to get a clean Dataset
            # instance without anything loaded into memory
            ds = normalization.normalize_dataset(utils.open_dataset(self.dataset_file.value, load=False))

            # local variables
            variable = self.variable.value
            timestamp = self.time.value
            layer = int(self.layer.value) if self.layer.value is not None else None

            # create plots
            # What we do here needs some explaining.
            # A prerequisite for generating the DynamicMaps is to create the trimesh.
            # The trimesh is needed for the wireframe and the raster.
            # No matter what widget we change (variable, timestamp, layer), we need to generate
            # a new trimesh object. This is why the trimesh is a local variable.
            trimesh = api.create_trimesh(ds=ds, variable=variable, timestamp=timestamp, layer=layer)

            # The wireframe is not always needed + it is always the same regardless of the variable.
            # So, we will generate it on the fly the first time we need it.
            # Therefore, we store it as an instance attribute in order to reuse it in subsequent renderings
            if self.show_mesh.value and self._mesh is None:
                self._mesh = api.get_wireframe(trimesh=trimesh, x_range=lon_range, y_range=lat_range)

            # The raster needs to be stored as an instance variable, too, because we want to
            # be able to restore the zoom level when we change the variable
            self._raster = api.get_raster(trimesh=trimesh, x_range=lon_range, y_range=lat_range)
            self._raster = self._raster.opts(width=MAIN_WIDTH, height=600)

            # In order to control dynamically the ColorBar of the raster we create
            # a `panel.Row` with extra widgets
            cbar_row = self._get_colorbar_row(raster=self._raster)

            # Construct the list of objects that will be part of the main overlay
            main_overlay_components = [self._tiles, self._raster]

            if self.show_mesh.value:
                main_overlay_components.append(self._mesh)

            # The stations row and the ts_plot are only plotted if the relevant checkboxes
            # have been checked. Nevertheless, we can add `None` to a `pn.Row/Column` and
            # that value will be ignored, which allows us to simplify the way we define
            # the rendable objects
            stations_row = None
            ts_plot = None

            if self.show_stations.value:
                stations = xr.open_dataset(DATA_DIR + 'fskill.nc')
                station_pins = api.get_station_pins(stations=stations)
                station_ts = api.get_station_timeseries(
                    stations=stations,
                    pins=station_pins
                ).opts(width=MAIN_WIDTH // 2)
                station_info = api.get_station_table(
                    stations=stations,
                    pins=station_pins,
                ).opts(width=MAIN_WIDTH // 2)
                stations_row = pn.Row(station_ts, station_info, sizing_mode="scale_width", width=MAIN_WIDTH)
                main_overlay_components.append(station_pins)

            if self.show_timeseries.value:
                ts_plot = api.get_tap_timeseries(ds=ds, variable=variable, source_raster=self._raster, layer=layer)
                ts_plot = ts_plot.opts(width=MAIN_WIDTH)

            main_overlay = reduce(operator.mul, main_overlay_components)
            main_row = pn.Row(main_overlay, width=MAIN_WIDTH, sizing_mode="stretch_width")

            # For the record, (and this is probably a panel bug), if we use
            #     self._main.append(ts_plot)
            # then the timeseries plot does not get updated each time we click on the
            # DynamicMap. By replacing the `objects` though, then the updates work fine.
            self._main.clear()
            self._main.objects = [cbar_row, main_row, stations_row, ts_plot]

        except:
            logger.exception("Something went wrong")

    @property
    def sidebar(self) -> pn.Column:
        return self._sidebar

    @property
    def main(self) -> pn.Column:
        return self._main
