import numpy as np
import holoviews as hv
import geoviews as gv
import panel as pn
import param
from datetime import datetime
from stratus import get_data_files
import os
import time

from dask.distributed import Client
import dask
from holoviews.operation.datashader import rasterize
from holoviews import opts, streams
from panel.viewable import Viewer
import geoviews.feature as gf
from cartopy import crs
from bokeh.models.formatters import PrintfTickFormatter

import xarray as xr
import hvplot.xarray
from pathlib import Path

# ============================================================================
# BOKEH/HOLOVIEWS CONFIGURATION (runs once at import)
# ============================================================================
gv.extension('bokeh')
hv.extension('bokeh')

# Set HDF5 locking before any file operations
os.environ['HDF5_USE_FILE_LOCKING'] = 'FALSE'

# plot default style
opts.defaults(
    opts.Image(
        global_extent=False, projection=crs.PlateCarree(),
        aspect='equal', responsive='width'
    )
)

# ============================================================================
# GLOBAL INITIALIZATION FUNCTIONS (run once per app instance)
# ============================================================================

def get_or_create_dask_client():
    """
    Get or create a singleton Dask client.
    This ensures only one connection is made regardless of how many users connect.
    """
    # Check if client already exists in Panel's cache
    if 'dask_client' in pn.state.cache:
        print("Using existing Dask client from cache")
        return pn.state.cache['dask_client']
    
    CLUSTER_TYPE = os.environ.get("DASK_CLUSTER_TYPE")
    PERSIST_DATA = os.environ.get("PERSIST_DATA", "true").lower() == "true"
    
    print(f"Initializing new Dask client: CLUSTER_TYPE = {CLUSTER_TYPE}")
    
    if CLUSTER_TYPE == 'PBSCluster':
        from dask_jobqueue import PBSCluster
        cluster = PBSCluster(
            job_name='climate-viewer',
            cores=1,
            memory='4GiB',
            processes=4,
            local_directory='/glade/work/pdas47/scratch/pbs.$PBS_JOBID/dask/spill',
            resource_spec='select=1:ncpus=1:mem=4GB',
            queue='casper',
            walltime='01:00:00',
            interface='ib0',
            worker_extra_args=["--lifetime", "25m", "--lifetime-stagger", "4m"]
        )
        cluster.scale(32)
        client = Client(cluster)
        client.wait_for_workers(32)
    
    elif CLUSTER_TYPE == 'LocalCluster':
        from dask.distributed import LocalCluster
        cluster = LocalCluster(
            n_workers=2,
            threads_per_worker=4,
            memory_limit='8GB'
        )
        client = Client(cluster)
    
    elif CLUSTER_TYPE.startswith('scheduler'):
        max_retries = 5
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                print(f"Attempting to connect to Dask scheduler at {CLUSTER_TYPE} (attempt {attempt + 1}/{max_retries})...")
                client = Client(CLUSTER_TYPE, timeout='10s')
                print(f"✓ Connected to Dask scheduler")
                
                # Wait for at least 1 worker to be available
                print("Waiting for Dask workers to be available...")
                workers_available = False
                for wait_attempt in range(30):
                    workers = client.scheduler_info().get('workers', {})
                    if len(workers) > 0:
                        print(f"✓ {len(workers)} Dask worker(s) available")
                        workers_available = True
                        break
                    print(f"  No workers yet, waiting... ({wait_attempt + 1}/30)")
                    time.sleep(2)
                
                if not workers_available:
                    print("WARNING: No Dask workers available yet. Proceeding without workers.")
                    pn.state.cache['persist_data'] = False
                else:
                    pn.state.cache['persist_data'] = PERSIST_DATA
                
                break  # Successfully connected
            
            except Exception as e:
                print(f"✗ Failed to connect to Dask scheduler: {e}")
                if attempt < max_retries - 1:
                    print(f"  Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    print("✗ Could not connect to Dask scheduler after all retries")
                    raise RuntimeError(f"Failed to connect to Dask scheduler at {CLUSTER_TYPE}")
    else:
        raise RuntimeError(f"Unknown cluster type: {CLUSTER_TYPE}")
    
    # Cache the client
    pn.state.cache['dask_client'] = client
    print("✓ Dask client cached for reuse")
    
    return client


def load_datasets_once():
    """
    Load datasets once and cache them for all sessions.
    This is the expensive operation we want to do only once.
    """
    # Check if already loaded
    if 'datasets_loaded' in pn.state.cache:
        print("✓ Using cached datasets (already loaded)")
        return pn.state.cache['ds'], pn.state.cache['std_ds']
    
    print("=" * 70)
    print("LOADING DATASETS FOR THE FIRST TIME (this happens once per app)")
    print("=" * 70)
    
    # Get Dask client
    client = get_or_create_dask_client()
    PERSIST_DATA = pn.state.cache.get('persist_data', True)
    
    # Download files if needed
    data_path = '/home/mambauser/app/LENS2-ncote-dashboard/data_files'
    if not os.path.exists(data_path):
        print("Downloading data files...")
        get_data_files()
    
    # ========== Load mean dataset ==========
    print("\nLoading MEAN dataset...")
    parent_dir = Path('/home/mambauser/app/LENS2-ncote-dashboard/data_files/mean/')
    files = list(parent_dir.glob('*.nc'))
    print(f"  Found {len(files)} files: {', '.join(f.name for f in files)}")
    
    ds = xr.open_mfdataset(
        files,
        parallel=False,  # Avoid HDF5 locking issues on shared storage
        chunks={'time': 1},
        engine='netcdf4',
        combine='by_coords'
    )
    
    ds = ds.convert_calendar('standard')
    ds = ds.assign_coords(lon=(((ds.lon + 180) % 360) - 180))
    ds = ds.roll(lon=int(len(ds['lon']) / 2), roll_coords=True)
    ds = ds.rename({
        k: f"{ds[k].attrs['long_name']} ({ds[k].attrs.get('units', 'unitless')})"
        for k in sorted(list(ds.keys()), reverse=True)
    })
    print("  ✓ Mean dataset opened and transformed")
    
    # ========== Load std_dev dataset ==========
    print("\nLoading STD_DEV dataset...")
    std_parent_dir = Path('/home/mambauser/app/LENS2-ncote-dashboard/data_files/std_dev/')
    std_files = list(std_parent_dir.glob("*.nc"))
    print(f"  Found {len(std_files)} files")
    
    std_ds = xr.open_mfdataset(
        std_files,
        parallel=False,
        chunks={'time': 1},
        engine='netcdf4',
        combine='by_coords'
    )
    
    std_ds = std_ds.convert_calendar('standard')
    std_ds = std_ds.assign_coords(lon=(((std_ds.lon + 180) % 360) - 180))
    std_ds = std_ds.roll(lon=int(len(std_ds['lon']) / 2), roll_coords=True)
    std_ds = std_ds.rename({
        k: f"{std_ds[k].attrs['long_name']} ({std_ds[k].attrs.get('units', 'unitless')})"
        for k in sorted(list(std_ds.keys()), reverse=True)
    })
    print("  ✓ Std_dev dataset opened and transformed")
    
    # ========== Persist if enabled ==========
    if PERSIST_DATA:
        print("\nPersisting datasets to Dask workers...")
        try:
            workers_info = client.scheduler_info()['workers']
            if workers_info:
                mem_before = sum(w['memory'] for w in workers_info.values()) / 1e9
                print(f"  Memory before persist: {mem_before:.2f} GB")
            
            # Persist both datasets
            ds = ds.persist()
            std_ds = std_ds.persist()
            
            # Wait for persistence to complete
            print("  Waiting for persistence to complete...")
            dask.distributed.wait([ds, std_ds], timeout=120)
            
            workers_info = client.scheduler_info()['workers']
            if workers_info:
                mem_after = sum(w['memory'] for w in workers_info.values()) / 1e9
                print(f"  Memory after persist: {mem_after:.2f} GB")
                print(f"  ✓ Datasets persisted ({mem_after - mem_before:.2f} GB used)")
            else:
                print("  ✓ Datasets persisted")
        
        except Exception as e:
            print(f"  ⚠ Warning: Failed to persist datasets: {e}")
            print("  Continuing with lazy loading (data will be computed on-demand)")
    else:
        print("\nPersistence disabled - using lazy loading")
    
    # Cache the datasets
    pn.state.cache['ds'] = ds
    pn.state.cache['std_ds'] = std_ds
    pn.state.cache['datasets_loaded'] = True
    
    print("\n" + "=" * 70)
    print("✓ DATASETS LOADED AND CACHED - subsequent sessions will be instant")
    print("=" * 70 + "\n")
    
    return ds, std_ds


# ============================================================================
# LOAD DATASETS AT MODULE LEVEL (happens once when app starts)
# ============================================================================

print("Initializing CESM2 LENS2 Dashboard...")
try:
    ds, std_ds = load_datasets_once()
except Exception as e:
    print(f"✗ FATAL ERROR during initial data load: {e}")
    raise

# Extract metadata from loaded datasets
min_year = ds.time.min().dt.year.item()
max_year = ds.time.max().dt.year.item()
variables = list(sorted(ds.keys(), reverse=True))
forcing_types = list(ds.coords['forcing_type'].values)

print(f"Dataset metadata extracted:")
print(f"  Years: {min_year} - {max_year}")
print(f"  Variables: {len(variables)}")
print(f"  Forcing types: {len(forcing_types)}")
print("✓ Dashboard ready to accept connections\n")

# ============================================================================
# STATIC HTML DESCRIPTION
# ============================================================================

DESCRIPTION = pn.pane.HTML("""
<h1>User Guide</h1>
<h2>Toolbar options:</h2>
<p class="indent">
    <img src="https://docs.bokeh.org/en/latest/_images/Pan.png" alt=" ">
            PAN: Select to hold and drag the map. <br>
    <img src="https://docs.bokeh.org/en/latest/_images/BoxZoom.png" alt="">
            BOX ZOOM: Select to draw a box and zoom to the box contents. <br> 
    <img src="https://docs.bokeh.org/en/2.4.2/_images/BoxSelect.png" alt="">
            BOX SELECT: Select to draw a box and view regional mean of box contents in the time-series plot. <br> 
    <img src="https://docs.bokeh.org/en/latest/_images/WheelZoom.png" alt="">
            WHEEL ZOOM: Select to use the wheel on your mouse to zoom in and out of the image. <br> 
    <img src="https://docs.bokeh.org/en/latest/_images/Tap.png" alt="">
            TAP: Click on any point on the map to plot the selected variable's time-series. <br>
    <img src="https://docs.bokeh.org/en/latest/_images/Save.png" alt="">
            SAVE: Save a PNG image of the figure to your computer. <br>
    <img src="https://docs.bokeh.org/en/latest/_images/Reset.png" alt="">
            RESET: Click to reset to the original values.<br> 
</p>
                           
<h2>About Data</h2>
<p class="indent">
<p>This interactive dashboard lets users interact with the CESM2 (Community Earth System Model 2) Large Ensemble Community Project (LENS2) climate data developed by a partnership between National Center for Atmospheric Research (NCAR), United States, and the IBS Center for Climate Physics, South Korea. The LENS2 dataset is the result of a computer simulation of Earth system processes based on the past, present and future (1850-2100) climate scenarios. A detailed discussion about the model can be found at <a href="https://www.cesm.ucar.edu/community-projects/lens2">the NCAR's project website.</a> </p>
<h3>Modeling and Uncertainty: </h3>
<p>All models include uncertainty. This uncertainty is represented by the shaded area in the time-series chart above, which shows the ±1 standard deviation region, meaning approximately 68% of the data will be within this region. The darker line within the shaded areas shows an average, or most, likely expected outcome within the range of possibilities.  </p>
<h3>Spatial Scale and Inputs: </h3>

<h2>Monitor App Performance:</h2>
<p>
    <a id="dask-dashboard-link" href="/dask-dashboard/status" target="_blank">Dask Diagnostic UI</a>
</p>

<h2>More Information on Earth System Modeling:</h2>
<p>
    <li><a href="https://www.youtube.com/watch?v=HWjW51i6s2s">Introduction to Earth System Modeling</a></li>
    <li><a href="https://www.youtube.com/watch?v=Yd85l5rj0OE">Introduction to the Community Earth System Model (CESM)</a></li>
    <li><a href="https://www.cesm.ucar.edu/community-projects/lens2">CESM2 Large Ensemble Community Project (LENS2)</a></li>
</p>

<script>
// Auto-detect hostname and update dashboard link
(function() {
    var link = document.getElementById('dask-dashboard-link');
    if (link) {
        var currentUrl = window.location.protocol + '//' + window.location.host + '/dask-dashboard/status';
        link.href = currentUrl;
    }
})();
</script>
""")

# ============================================================================
# PER-SESSION CLASSES (created once per user)
# ============================================================================

class ColorbarControls(Viewer):
    clim = param.Range(default=(0, 100), label="Colorbar Range")
    width = param.Number(default=300)
    clim_locked = param.Boolean(default=False)
    clim_connected_to_ts = param.Boolean(default=False)

    _scientific_format_low_threshold = 0.01
    _scientific_format_high_threshold = 9999

    def __init__(self, **params):
        self._start_input = pn.widgets.FloatInput()
        self._end_input = pn.widgets.FloatInput(align='end')
        self._clim_lock = pn.widgets.Checkbox(name='Lock controls')
        self._clim_connected_to_ts_chkbx = pn.widgets.Checkbox(name='Set CLim range = Y-axis of time-series')
        
        super().__init__(**params)
        self._layout = pn.Column(
            pn.Row(self._start_input, self._end_input),
            self._clim_lock,
            self._clim_connected_to_ts_chkbx
        )
        self._sync_widgets()

    def __panel__(self):
        return self._layout

    @param.depends('clim', '_clim_lock.value', '_clim_connected_to_ts_chkbx.value', watch=True)
    def _sync_widgets(self):
        self.clim_locked = self._clim_lock.value
        self._start_input.disabled = self._clim_lock.value
        self._end_input.disabled = self._clim_lock.value

        self._start_input.name = self.name
        self._start_input.value, self._end_input.value = self.clim
        for i in [self._start_input, self._end_input]:
            i.width = int(self.width * 0.9) // 2
            i.margin = (5, 5)
            if i.value < self._scientific_format_low_threshold or i.value > self._scientific_format_high_threshold:
                i.format = PrintfTickFormatter(format="%.2e")
            else:
                i.format = '0.2f'

        self.clim_connected_to_ts = self._clim_connected_to_ts_chkbx.value
        

    @param.depends('_start_input.value', '_end_input.value', watch=True)
    def _sync_params(self):
        self.clim = (self._start_input.value, self._end_input.value)


class ClimateViewer(param.Parameterized):
    # Dataset parameters
    variable = param.ObjectSelector(default=variables[0], objects=variables)
    forcing_type = param.ObjectSelector(default=forcing_types[0], objects=forcing_types)
    year = param.Integer(default=2015, bounds=(min_year, max_year))
    
    # time-series parameters
    pointer = param.XYCoordinates((0, 0), precedence=-1)
    
    # Plotting parameters
    cmap = param.ObjectSelector(label='Colormap', default='inferno', objects=['inferno', 'viridis', 'inferno_r', 'kb', 'coolwarm', 'coolwarm_r', 'Blues', 'Blues_r'])
    cbar_controls = ColorbarControls(name='Colorbar Controls')
    show_ts_legend = param.Boolean(default=True, label='Toggle time-series legend')

    # Data parameters
    data_subset = param.Parameter(default=hv.Dataset([]), precedence=-1)
    selected = param.Parameter(default=hv.Dataset([]), precedence=-1)
    x_range = param.Range(default=(0, 0), softbounds=(-180, 180))
    y_range = param.Range(default=(0, 0), softbounds=(-90, 90))
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # plot handles
        self.map_hv = None
        self.selection_map_hv = None
        self.selection_ts_hv = None
        self._pointer_marker = None
        self.ts_hv = None
        self._year_marker = None
        self._cbar = None
        
        # setup stream <-> pointer connection
        self._stream = streams.Tap(x=0, y=0)
        self._stream.add_subscriber(self._update_click)

        # selection stream
        self._selection = streams.BoundsXY(bounds=(0, 0, 0, 0))
        self._selection.add_subscriber(self._get_selection_data)

        # zoom stream
        self._zoom = streams.RangeXY(x_range=(None, None), y_range=(None, None))
        self._zoom.add_subscriber(self._update_ranges)
        
        # Initialize map
        self._get_map_data()
        self._plot_map()
        self._plot_pointer_marker()
        self._style_map()
        
        # Initialize Time-series
        self._get_ts_data()
        self._plot_ts()
        self._plot_year_marker()
        self._style_ts()
    
    ## DATA
    @param.depends('variable', 'forcing_type', 'year', watch=True)
    def _get_map_data(self):
        subset = ds[self.variable]\
                    .sel(time=f'{self.year}-01-01', method='nearest') \
                    .sel(forcing_type=self.forcing_type) \
                    .rename({'lat': 'Latitude', 'lon': 'Longitude'})
        subset_hv = hv.Dataset(subset)

        self.data_subset = subset_hv
    
    @param.depends('variable', 'forcing_type', 'pointer', watch=True)
    def _get_ts_data(self):
        ts_mean_subset = ds[self.variable].sel(lat=self.pointer[1], lon=self.pointer[0], method='nearest').sel(forcing_type=self.forcing_type).rename({'lat': 'Latitude', 'lon': 'Longitude'})
        self.ts_mean_subset = hv.Dataset(ts_mean_subset)
        ts_stddev_subset = std_ds[self.variable].sel(lat=self.pointer[1], lon=self.pointer[0], method='nearest').sel(forcing_type=self.forcing_type).rename({'lat': 'Latitude', 'lon': 'Longitude'})
        self.ts_upper_bound = hv.Dataset(ts_mean_subset + ts_stddev_subset)
        self.ts_lower_bound = hv.Dataset(ts_mean_subset - ts_stddev_subset)

    def _get_selection_data(self, bounds):
        minx, miny, maxx, maxy = bounds
        self.selected = self.data_subset.select(Longitude=(minx, maxx), Latitude=(miny, maxy))

    def _update_ranges(self, x_range, y_range):
        self.x_range = x_range
        self.y_range = y_range
    
    ## PLOT
    @param.depends('data_subset', 'selected', watch=True)
    def _plot_map(self):
        plot = gv.Image(
            data = self.data_subset,
            kdims = ['Longitude', 'Latitude'],
            vdims = [self.variable],
            group = 'Map',
            label = self.variable
        )
        self.map_hv = plot
        clim_range = plot.range(self.variable)
        if not self.cbar_controls.clim_locked:
            self.cbar_controls.clim = clim_range

        if not self._selection.bounds == (0, 0, 0, 0):
            plot_selection = gv.Image(
                data = self.selected,
                kdims = ['Longitude', 'Latitude'],
                vdims = [self.variable],
                group = 'Map',
                label = 'Selection',
                show_legend=True
            )
            self.selection_map_hv = plot_selection

    @param.depends('pointer', watch=True)
    def _plot_pointer_marker(self):
        plot = hv.Scatter(
            (self.pointer[0], self.pointer[1])
        ).opts(color='#52a1d5', marker='x', size=13, line_width=3) * hv.Scatter(
            (self.pointer[0], self.pointer[1])
        ).opts(color='#c6e2f2', marker='x', size=10, line_width=1)

        self._pointer_marker = plot
    
    @param.depends('pointer', 'variable', '_get_ts_data', watch=True)
    def _plot_ts(self):
        ts_mean = hv.Curve(
            data = self.ts_mean_subset,
            kdims = ['time'],
            vdims = [self.variable],
            label=f'Mean {self.variable}'
        )
        ts_bounds = hv.Area(
            data = (
                self.ts_lower_bound['time'], 
                self.ts_upper_bound[self.variable],
                self.ts_lower_bound[self.variable], 
            ),
            kdims = ['time'],
            vdims = ['upper_bound', 'lower_bound'],
            label=f'± 1 std. dev.'
        )
        self.ts_hv = ts_mean * ts_bounds

    @param.depends('year', watch=True)
    def _plot_year_marker(self):
        self._year_marker = hv.VLine(
            datetime(self.year, 1, 1)
        ).opts(
            line_dash = 'dashed',
            line_width = 2,
            line_color = 'grey'
        )
    
    @param.depends('selected', watch=True)
    def _plot_region_ts(self):
        region_mean = ds[self.variable].sel(
            lon=slice(self._selection.bounds[0], self._selection.bounds[2]),
            lat=slice(self._selection.bounds[1], self._selection.bounds[3])
        ).mean(dim=['lat', 'lon', 'forcing_type'])
        region_ts_mean = hv.Curve(
            data = region_mean,
            kdims = ['time'],
            vdims = [self.variable],
            label=f'Region mean {self.variable}'
        )
        self.selection_ts_hv = region_ts_mean

    ## STYLE
    @param.depends('_plot_map', 'cmap', 'cbar_controls.clim', watch=True)
    def _style_map(self):
        if not self.x_range == (0, 0):
            x_range = self.x_range
        else:
            x_range = (None, None)
        if not self.y_range == (0, 0):
            y_range = self.y_range
        else:
            y_range = (None, None)

        if not self._selection.bounds == (0, 0, 0, 0):
            alpha = 0.2

            self.selection_map_hv.opts(
                cmap=self.cmap,
                clim=(self.cbar_controls.clim[0], self.cbar_controls.clim[1]),
                xlim=x_range, ylim=y_range,
                clone=False
            )

        else:
            alpha = 1

        self.map_hv.opts(
            cmap=self.cmap,
            title=f"Average {self.variable} in {self.year}",
            tools=['box_select', 'tap'],
            alpha=alpha,
            colorbar=True, clabel=f'{self.variable}',
            xlim=x_range, ylim=y_range,
            clone=False
        )

        self._update_source()
    
    @param.depends('_plot_ts', 'cbar_controls.clim_connected_to_ts', 'cbar_controls.clim', '_plot_region_ts', 'show_ts_legend', watch=True)
    def _style_ts(self):
        pointer_x = f'{self.pointer[0]:.2f}°E' if self.pointer[0] >= 0 else f'{self.pointer[0]*-1:.2f}°W'
        pointer_y = f'{self.pointer[1]:.2f}°N' if self.pointer[1] >= 0 else f'{self.pointer[1]*-1:.2f}°S'

        if self.ts_hv is None:
            return

        self.ts_hv = self.ts_hv.opts(
            opts.Curve(
                show_legend=self.show_ts_legend, 
                show_grid=True, 
                responsive='width', 
                height=300, 
                title=f'{self.variable} at {pointer_x}, {pointer_y}', 
                xlabel='Year'
            ),
            opts.Area(
                show_legend=self.show_ts_legend,
                alpha=0.3, 
            ),
        )

        if self.cbar_controls.clim_connected_to_ts:
            self.ts_hv = self.ts_hv.opts(
                opts.Curve(
                    ylim=(self.cbar_controls.clim[0], self.cbar_controls.clim[1]),
                )
            )
        else:
            self.ts_hv = self.ts_hv.opts(
                opts.Curve(
                    ylim=(None, None),
                )
            )
        
        if not self._selection.bounds == (0, 0, 0, 0):
            self.selection_ts_hv.opts(
                show_legend=self.show_ts_legend,
                clone=False
            )
    
    ## UTILITIES
    def _update_source(self):
        self._stream.source = self.map_hv
        self._selection.source = self.map_hv
        self._zoom.source = self.map_hv
    
    def _update_click(self, x, y):
        self.pointer = (x, y)
    
    ## DASHBOARD PLOT ELEMENTS
    @param.depends('_plot_map', '_style_map', '_plot_pointer_marker')
    def view_map(self):      
        if not self._selection.bounds == (0, 0, 0, 0):
            return self.map_hv * self.selection_map_hv * gf.coastline * self._pointer_marker
        else:
            return self.map_hv * gf.coastline * self._pointer_marker

    @param.depends('_plot_ts', '_style_ts', '_plot_year_marker', '_plot_region_ts')
    def view_ts(self):
        if not self._selection.bounds == (0, 0, 0, 0):
            return self.ts_hv * self.selection_ts_hv * self._year_marker
        else:
            return self.ts_hv * self._year_marker
        
    def _debug(self):
        return pn.pane.HTML(self.ts_lower_bound.data)
    
    # LAYOUT
    @property
    def template(self):
        variable_select = pn.Param(
            self.param.variable,
            widgets={'variable': {'width_policy': 'max', 'width': 100}}, 
            width_policy='fit', min_width=100, max_width=600,
            width=300, margin=(5, 5)
        )
        forcing_type_select = pn.Param(
            self.param.forcing_type, 
            widgets={'forcing_type': {'width_policy': 'max', 'width': 100}},
            width_policy='fit', min_width=100, max_width=600,
            width=300, margin=(5, 5)
        )
        year_slide = pn.Param(
            self.param.year, 
            widgets={'year': {'type': pn.widgets.IntSlider, 'width_policy': 'max', 'width': 100, 'height': 30, 'throttled': True}},
            width_policy='fit', min_width=100, max_width=600,
            width=300, margin=(5, 5)
        )

        cmap_select = pn.Param(
            self.param.cmap, 
            widgets={'cmap': {'width_policy': 'max', 'width': 100}},
            width_policy='fit', min_width=100, max_width=600,
            width=300, margin=(5, 5)
        )

        cbar_range = self.cbar_controls

        toggle_ts_legend = pn.Param(
            self.param.show_ts_legend,
            margin=(5,5)
        )

        dataset_controls = pn.Card(
            variable_select,
            year_slide,
            forcing_type_select,
            title='Dataset controls',
            width_policy='fit'
        )

        plot_controls = pn.Card(
            cmap_select,
            cbar_range,
            toggle_ts_legend,
            title='Plot controls',
            width_policy='fit'
        )

        # Instantiate the template with widgets displayed in the sidebar
        template = pn.template.BootstrapTemplate(
            title='CESM2 Large Ensemble Community Project (LENS2) Dashboard',
            sidebar=[dataset_controls, plot_controls],
            main_max_width='1000px',
            sidebar_width=340
        )

        content = pn.Column(
            self.view_map,
            self.view_ts,
            DESCRIPTION,
            align='center'
        )

        template.header_background = '#1A658F'

        # Append a layout to the main area, to demonstrate the list-like API
        template.main.append(
            content
        )

        return template


# ============================================================================
# PER-SESSION INITIALIZATION (runs once per user connection)
# ============================================================================

def create_session():
    """
    Create a new viewer instance for each user session.
    This is fast because data is already loaded and cached.
    """
    # Track session creation
    if 'session_count' not in pn.state.cache:
        pn.state.cache['session_count'] = 0
    pn.state.cache['session_count'] += 1
    
    session_num = pn.state.cache['session_count']
    print(f">>> New user session #{session_num} created (Session ID: {pn.state.session_id})")
    
    # Create a new ClimateViewer instance for this user
    climate_viewer = ClimateViewer()
    
    return climate_viewer.template


# Create the template (this runs once per user)
template = create_session()
template.servable()