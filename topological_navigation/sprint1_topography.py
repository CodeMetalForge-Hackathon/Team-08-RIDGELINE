##Goal: Demonstrate the ability to load and interept topographical data for basic navigation and localization
import math
import random

import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from pyproj import Transformer
from rasterio.warp import transform_bounds

# File path to your GeoTIFF
file_path = 'rasters_USGS1m/output_USGS1m.tif'
##helper function to find barometric altitude
def get_barometric_altitude(lat, lon):
    url = f"https://api.open-meteo.com/v1/elevation?latitude={lat}&longitude={lon}"
    response = requests.get(url).json()
    
    if "elevation" in response:
        # Returns a list, so we grab the first item
        elevation_meters = response["elevation"][0]
        print(f"Elevation at {lat}, {lon} is {elevation_meters} meters.")
        return (elevation_meters,elevation_meters + 3*random.random())
    return None
def filter_grid_by_altitude(elevation_grid, min_alt, max_alt, nodata_val=-9999):
    """
    Filters a 2D numpy array of elevations, returning the coordinates 
    of all pixels within the target altitude range.
    """
    # 1. Create a boolean mask (True if within range, False otherwise)
    # We also explicitly exclude the NoData value
    mask = (elevation_grid >= min_alt) & (elevation_grid <= max_alt) & (elevation_grid != nodata_val)
    
    # 2. Extract the row and column indices where the mask is True
    # This returns an array of [row, col] pairs representing the valid pixels
    valid_pixels = np.argwhere(mask)
    
    # 3. (Optional) Extract the actual elevation values for those pixels
    valid_elevations = elevation_grid[mask]
    
    print(f"Found {len(valid_pixels)} points between {min_alt}m and {max_alt}m.")
    
    return valid_pixels, mask
def get_highest_point(filepath, elevation_grid):
    """
    Finds the highest point in the current mesh and gies its lat long and row/column position
    """
    # 1. Find the 2D index (row, column) of the maximum elevation value.
    # We use .argmax() which safely handles masked arrays if you masked out NoData values.
    # unravel_index converts the flattened 1D index back into a 2D [row, col] format.
    highest_row, highest_col = np.unravel_index(elevation_grid.argmax(), elevation_grid.shape)
    
    # 2. Get the peak's elevation value just to print/verify
    peak_elevation = elevation_grid[highest_row, highest_col]
    print(f"Highest point found at Row {highest_row}, Col {highest_col} with altitude {peak_elevation:.2f}m")
    
    # 3. Convert that exact pixel into GPS coordinates
    with rasterio.open(filepath) as dataset:
        # Get map coordinates (e.g., meters)
        map_x, map_y = dataset.xy(highest_row, highest_col)
        
        # Transform to GPS degrees (EPSG:4326)
        transformer = Transformer.from_crs(dataset.crs, "EPSG:4326", always_xy=True)
        highest_long, highest_lat = transformer.transform(map_x, map_y)
    return(highest_lat,highest_long,highest_col,highest_row)
def get_bearing (row1,col1,row2,col2):
    """
    Calculate the bearing in degrees between the two points given
    """
    # Calculate difference in x (columns). Positive dx means moving East.
    dx = col2 - col1
    
    # Calculate difference in y (rows). 
    # Because Row 0 is at the top of the map, row2 > row1 means moving South.
    # We subtract row2 from row1 so that a positive dy means moving North.
    dy = row1 - row2
    
    # math.atan2(y, x) normally calculates the angle from the X-axis.
    # By swapping the arguments to math.atan2(dx, dy), we calculate the angle 
    # from the Y-axis (North), which perfectly matches a compass bearing.
    bearing_radians = math.atan2(dx, dy)
    
    # Convert radians to degrees
    bearing_degrees = math.degrees(bearing_radians)
    
    # Normalize the result to a standard 0-360 compass range
    # (atan2 can return negative numbers for Western bearings)
    bearing_degrees = (bearing_degrees + 360) % 360
    return bearing_degrees
    
##Ground truth location
true_lat = 43.84579
true_long = -110.83303
##internal sensors provide 
(true_altitude,barometeric_altitude) = get_barometric_altitude(true_lat,true_long)
print(f"Barometric Altitude: {barometeric_altitude}")
print(f"True Altitude: {true_altitude}")


def load_and_display_dem(filepath):
    # Open the GeoTIFF file
    with rasterio.open(filepath) as dataset:
        # Read the first band (elevation data is typically in band 1)
        elevation_data = dataset.read(1)
        
        # Get the NoData value from the dataset metadata
        nodata_val = dataset.nodata

    # Handle NoData values (often extremely large negative or positive numbers)
    # We mask them out so they don't skew the visualization's color scale
    if nodata_val is not None:
        elevation_data = np.ma.masked_equal(elevation_data, nodata_val)
    else:
        # Fallback: mask out typical placeholder values like -9999
        elevation_data = np.ma.masked_less(elevation_data, -9000)

    # Calculate basic statistics (ignoring masked NoData values)
    min_elev = np.min(elevation_data)
    max_elev = np.max(elevation_data)
    ## Find absoloute cordinates: 
    native_bounds = dataset.bounds
    (highest_long,highest_long,highest_col,highest_row) = get_highest_point(file_path,elevation_data)

    # 2. Transform the bounds to standard GPS Coordinates (EPSG:4326)
    # transform_bounds takes (source_crs, destination_crs, left, bottom, right, top)
    lon_left, lat_bottom, lon_right, lat_top = transform_bounds(
        dataset.crs, 
        'EPSG:4326', 
        native_bounds.left, 
        native_bounds.bottom, 
        native_bounds.right, 
        native_bounds.top
    )
    transformer = Transformer.from_crs("EPSG:4326", dataset.crs, always_xy=True)
    (map_x, map_y) = transformer.transform(true_long, true_lat)
    (true_row, true_col) = dataset.index(map_x, map_y)

    ## Filter elevation data from observed barometric pressure
    (valid_indices, boolean_mask) = filter_grid_by_altitude(elevation_data, barometeric_altitude-10, barometeric_altitude+10)
    plt.figure(figsize=(12,8))
    plt.title("raw topo")

    im = plt.imshow(elevation_data,cmap='terrain')
    cbar = plt.colorbar(im, label='Elevation (meters)')
    
    # Add titles and labels
    plt.title('Topographical Elevation Map (USGS 1m DEM)', fontsize=14)
    plt.xlabel('Column (Pixels)')
    plt.ylabel('Row (Pixels)')
    
    # Show the visualization
    plt.tight_layout()
    
    bearing = get_bearing(highest_row, highest_col, true_row, true_col)
    plt.figure
    plt.figure(figsize=(12, 8))

    # 1. Plot the base map (Original terrain)
    plt.imshow(elevation_data, cmap='gray', alpha=0.6)

    # 2. Prepare the overlay
    filtered_overlay = np.ma.masked_where(~boolean_mask, elevation_data)

    # 3. Plot the highlighted overlay
    im = plt.imshow(filtered_overlay, cmap='autumn')

    # 4. Add styling and labels
    cbar = plt.colorbar(im, fraction=0.046, pad=0.04)
    cbar.set_label('Altitude (meters)')

    plt.title(f'Topographical Map: Barometric Altitude Filtering ({barometeric_altitude-10}m - {barometeric_altitude+10}m)', fontsize=14)
    plt.xlabel('Column (X)')
    plt.ylabel('Row (Y)')

    # 5. Plot the Points
    plt.scatter(true_col, true_row, color='blue', marker='o', s=150, label="True Point")
    plt.scatter(highest_col, highest_row, color='red', marker='*', s=200, edgecolor='black', label="Highest Point")


    # Draw the line from the highest point to the projected end point
    plt.plot([highest_col, true_col], [highest_row, true_row], color='lime', linestyle='--', linewidth=2, label=f"Bearing: {bearing:.1f}°")
    plt.legend()
    plt.show()

if __name__ == "__main__":
    load_and_display_dem(file_path)