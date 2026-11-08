import math
import random

import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from scipy.ndimage import gaussian_filter, maximum_filter, uniform_filter

##Constants
FILE_PATH = 'rasters_USGS1m/output_USGS1m.tif'
BAROMETERIC_PRECISION = 3

def main():
    ##Load Data
    with rasterio.open(FILE_PATH) as dataset:
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

    ##Find Peaks
    peaks_mask, peak_cords = find_true_peaks(elevation_data)

    ##Plot basic map
    plt.figure(figsize=(12,8))
    im1 = plt.imshow(elevation_data, cmap='terrain')
    cbar1 = plt.colorbar(im1, label='Elevation (meters)')
    
    # Add titles and labels
    plt.title('Topographical Elevation Map (USGS 1m DEM)', fontsize=14)
    plt.xlabel('Column (Pixels)')
    plt.ylabel('Row (Pixels)')
    plt.tight_layout()
    
    ##Plot peaks map
    plt.figure(figsize=(12, 8))
    # Base terrain in grayscale so the peaks stand out
    im2 = plt.imshow(elevation_data, cmap='gray', alpha=0.5)
    cbar2 = plt.colorbar(im2, label='Elevation (meters)')

    # Plot peaks using scatter points based on coordinates
    if len(peak_cords) > 0:
        # peak_cords is [row, col]. We extract cols for X, and rows for Y
        peak_rows = peak_cords[:, 0]
        peak_cols = peak_cords[:, 1]
        
        plt.scatter(peak_cols, peak_rows, color='magenta', marker='^', s=100, 
                    edgecolor='black', label="True Peaks")

    plt.title("Geomorphometry: Identified Mountain Peaks", fontsize=14)
    plt.xlabel('Column (Pixels)')
    plt.ylabel('Row (Pixels)')
    plt.legend(loc='upper right')
    
    plt.tight_layout()
    plt.show()



##Helper Functions
def find_true_peaks(elevation_data, min_altitude=2500, isolation_meters=800, min_prominence_proxy=150):
    """
    Finds major mountain peaks by combining isolation (window size), 
    absolute altitude thresholds, and a prominence proxy (Large-scale TPI).
    """
    print("Smoothing terrain...")
    # Heavier smoothing to melt away boulders and micro-terrain
    smoothed = gaussian_filter(elevation_data, sigma=5)
    
    # ---------------------------------------------------------
    # 1. ENFORCE ISOLATION (Distance to next highest point)
    # ---------------------------------------------------------
    print(f"Applying {isolation_meters}m isolation filter...")
    # If a pixel is the highest point in this massive window, it's a candidate
    local_max_grid = maximum_filter(smoothed, size=isolation_meters)
    is_local_max = (smoothed == local_max_grid)
    
    # ---------------------------------------------------------
    # 2. ENFORCE ABSOLUTE ALTITUDE
    # ---------------------------------------------------------
    # The peak must be higher than the valley floor/foothills
    is_high_enough = (smoothed >= min_altitude)
    
    # ---------------------------------------------------------
    # 3. ENFORCE PROMINENCE (Difference from surroundings)
    # ---------------------------------------------------------
    print("Calculating large-scale TPI for prominence...")
    # Calculate the average elevation in a massive 1km radius
    regional_mean = uniform_filter(smoothed, size=1000)
    
    # TPI = How much taller is this pixel than the 1km area around it?
    macro_tpi = smoothed - regional_mean
    
    # The peak must stand out significantly from its surrounding region
    is_prominent = (macro_tpi >= min_prominence_proxy)
    
    # ---------------------------------------------------------
    # COMBINE ALL PROPERTIES
    # ---------------------------------------------------------
    # A true peak must satisfy all three geographical rules
    true_peaks_mask = is_local_max & is_high_enough & is_prominent
    
    # Extract the actual coordinates of the true peaks
    peak_pixels = np.argwhere(true_peaks_mask)
    
    print(f"Algorithm finished. Found {len(peak_pixels)} true peaks.")
    
    return true_peaks_mask, peak_pixels

def get_barometric_altitude(lat, lon):
    """
    Mocks function of real barometric altitude sensor on a ATAK mobile device returning a noisy and true response
    """
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

if __name__ == "__main__":
    main()