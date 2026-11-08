import os
import numpy as np
from water import load_water

from nav_integration import inspect_raster, load_grid, latlon_to_rc, guidance
from hiker_router import CostConfig, build_edge_costs, cost_to_go

# Path relative to this file, so it works from any working directory.
HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "rasters_USGS1m", "output_USGS1m.tif")

inspect_raster(PATH)

grid, crs = load_grid(PATH, cell_m=10)
print("grid:", grid.dem.shape, "cell:", grid.cell)

start = latlon_to_rc(grid, crs, 43.84579, -110.83303)
print("start cell:", start)


#More sophisticated Goal choice:
# Everything the injured caps can reach from the start
F = cost_to_go(grid, build_edge_costs(grid, CostConfig.injured()), start, forward=True)
reach = np.argwhere(np.isfinite(F))
d = np.hypot(reach[:, 0] - start[0], reach[:, 1] - start[1]) * grid.cell   # metres
print(f"reachable cells: {len(reach):,}, farthest is {d.max():.0f} m from start")

# Pick a reachable cell about 500 m away (or the farthest one if the region is small)
near_500 = np.where(np.abs(d - 500) < 25)[0]
k = near_500[len(near_500) // 2] if len(near_500) else int(np.argmax(d))
goal = tuple(int(x) for x in reach[k])

print("goal cell:", goal)

# Stand-in posterior: a small blob around the start
r, c = np.mgrid[0:grid.dem.shape[0], 0:grid.dem.shape[1]]
post = np.exp(-(((r - start[0]) / 5) ** 2 + ((c - start[1]) / 5) ** 2))
post /= post.sum()

print(guidance(grid, goal, post, CostConfig.injured(), declination_deg=10.0))


from destination import Planner, pick_destination

planner = Planner(grid, start)        # start = the localizer's best estimate

res = planner.plan_latlon(43.86, -110.80, crs)    # or planner.plan((row, col))
print(res["severity"], res["tier"])
for w in res["warnings"]:
    print(" -", w)

load_water(grid, crs.to_epsg(), cache_dir=HERE)   # first run downloads, later runs read the cache
print("Water Rendered.")

pick_destination(planner)             # click anywhere on the map to choose


