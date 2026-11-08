"""
plotting.py - readable maps for routes.

    from plotting import plot_routes
    plot_routes(grid, {"my route": path}, start)                  # hillshade + contours
    plot_routes(grid, paths, start, reach_mask=np.isfinite(F))    # dim unreachable terrain
    plot_routes(grid, paths, start, basemap=True)                 # real topo tiles (internet)

Axes are UTM metres (same CRS as the raster), so tile basemaps line up.
"""
import numpy as np


def elevation_cmap():
    """Low = deep green -> light green -> tan -> brown -> dark gray (high). No blue: blue is for water."""
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list("land", [
        (0.00, "#3f7d3a"),   # deepest: green
        (0.25, "#8fba62"),
        (0.50, "#cfba78"),   # mid: tan
        (0.75, "#9a7550"),   # high: brown
        (1.00, "#5b5550"),   # highest: dark gray
    ])


def shaded_relief(dem, cell):
    """Hillshaded RGB using the land colormap, stretched over the 2nd-98th elevation percentiles."""
    from matplotlib.colors import LightSource
    vmin, vmax = np.percentile(dem, [2, 98])
    return LightSource(azdeg=315, altdeg=40).shade(
        dem, cmap=elevation_cmap(), vmin=vmin, vmax=vmax,
        blend_mode="soft", vert_exag=2.0, dx=cell, dy=cell)


def draw_water(ax, water, extent, zorder=1.5):
    """
    Paint known water. `water` is a bool mask (all open water) or the uint8 class raster from
    water.py: 1 open water (solid blue), 2 stream/river line (darker blue), 3 drainageway
    that may be dry (pale blue). These are the only blue things on the map.
    """
    if water is None:
        return
    w = np.asarray(water)
    w = w.astype(np.uint8) if w.dtype == bool else w
    if not w.any():
        return
    from matplotlib.colors import ListedColormap
    cmap = ListedColormap([(0, 0, 0, 0), "#3b8ed0", "#1f6fb2", "#8cc0e6"])
    ax.imshow(w, extent=extent, origin="upper", cmap=cmap, vmin=0, vmax=3, zorder=zorder)


def _xy(grid, rc):
    rc = np.asarray(rc, float)
    x0, y0 = grid.origin_xy
    return x0 + (rc[:, 1] + 0.5) * grid.cell, y0 - (rc[:, 0] + 0.5) * grid.cell


def plot_routes(grid, paths: dict, start, reach_mask=None, basemap=False, water=None,
                crs="EPSG:26912", pad_m=300.0, contour_m=20.0,
                title="routes", save=None):
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource

    dem = grid.dem
    H, W = dem.shape
    x0, y0 = grid.origin_xy
    extent = (x0, x0 + W * grid.cell, y0 - H * grid.cell, y0)   # left, right, bottom, top
    if water is None:
        water = grid.water                                      # set by water.load_water()

    fig, ax = plt.subplots(figsize=(11, 9))

    if basemap:
        try:
            import contextily as ctx
            # tiles need the axes limits set first
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
            ctx.add_basemap(ax, crs=crs, source=ctx.providers.OpenTopoMap, zorder=0)
        except Exception as e:                                  # no internet / not installed
            print(f"(basemap unavailable: {e}); using hillshade instead)")
            basemap = False

    if not basemap:
        ax.imshow(shaded_relief(dem, grid.cell), extent=extent, origin="upper", zorder=1)
    draw_water(ax, water, extent)

    # contour lines
    lo, hi = np.floor(dem.min() / contour_m) * contour_m, np.ceil(dem.max() / contour_m) * contour_m
    levels = np.arange(lo, hi + contour_m, contour_m)
    cs = ax.contour(dem, levels=levels, extent=extent, origin="upper",
                    colors="k", linewidths=0.4, alpha=0.5, zorder=2)
    ax.clabel(cs, levels=levels[::5], fontsize=7, fmt="%d m")

    # dim everything the router says is unreachable
    if reach_mask is not None:
        shade = np.ma.masked_where(reach_mask, np.ones_like(dem))
        ax.imshow(shade, extent=extent, origin="upper", cmap="gray_r",
                  alpha=0.35, vmin=0, vmax=1.5, zorder=3)

    # routes
    cmap = plt.cm.tab10
    for i, (name, p) in enumerate(paths.items()):
        x, y = _xy(grid, p)
        ax.plot(x, y, lw=2.5, color=cmap(i % 10), label=name, zorder=5)
        ax.scatter(x[-1], y[-1], marker="*", s=220, color=cmap(i % 10),
                   edgecolor="k", zorder=6)
    sx, sy = _xy(grid, [start])
    ax.scatter(sx, sy, c="lime", s=160, edgecolor="k", zorder=7, label="start")

    # zoom to the routes
    allx = np.concatenate([_xy(grid, p)[0] for p in paths.values()] + [sx])
    ally = np.concatenate([_xy(grid, p)[1] for p in paths.values()] + [sy])
    ax.set_xlim(allx.min() - pad_m, allx.max() + pad_m)
    ax.set_ylim(ally.min() - pad_m, ally.max() + pad_m)

    ax.set_aspect("equal")
    ax.set_xlabel("UTM easting (m)")
    ax.set_ylabel("UTM northing (m)")
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=8)
    plt.tight_layout()
    if save:
        plt.savefig(save, dpi=110)
        plt.close(fig)
    else:
        plt.show()
