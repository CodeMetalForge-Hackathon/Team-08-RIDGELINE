import math
import random
import itertools
import matplotlib.pyplot as plt
import numpy as np
from numpy.random import f
import rasterio
import requests
from pyproj import Transformer
from scipy.ndimage import gaussian_filter, maximum_filter, uniform_filter

##Constants
FILE_PATH = 'rasters_USGS1m/output_USGS1m.tif'
BAROMETERIC_PRECISION = 3
NOISE_STD = 1.0
##true position information
# true_lat = 43.84579
# true_long = -110.83303

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
    ##find true position in grid space
    # transformer = Transformer.from_crs("EPSG:4326", dataset.crs, always_xy=True)
    # (map_x, map_y) = transformer.transform(true_long, true_lat)
    # (true_row, true_col) = dataset.index(map_x, map_y)
    
    peaks_mask, peak_cords = find_true_peaks(elevation_data)
    ##find the 3 closest peaks

    ##Plot basic map
    fig, ax = plt.subplots(figsize=(12, 8))
    ax.imshow(elevation_data, cmap='terrain')
    plt.title('Select Operator Position')
    
    # Block execution and wait for 1 click (timeout=0 means wait forever)
    # show_clicks=True automatically adds a small marker where you click
    print("Waiting for you to click on the plot...")
    clicked_coords = plt.ginput(n=1, timeout=0, show_clicks=True)

    # clicked_coords returns a list of (x, y) tuples. 
    if clicked_coords:
        true_col = int(round(clicked_coords[0][0]))
        true_row = int(round(clicked_coords[0][1]))
        
        print(f"Added marker at Row: {true_row}, Column: {true_col}")
        
        # Optional: permanently plot your own custom marker there
        ax.plot(true_col, true_row, marker='x', color='red', markersize=10, mew=2)
        fig.canvas.draw()

    print("Proceeding to the next part of the script...")
    closest_peaks = closest_landmarks(true_row,true_col,peak_cords)

    # --- Script continues immediately here ---



   # =========================================================
    # 1. SETUP & GET NOISY BEARINGS
    # =========================================================
    # Assuming true_row, true_col and selected_peaks are defined
    # selected_peaks should be a numpy array of shape (3, 2) [row, col]
    selected_peaks = closest_peaks
    selected_peak_rows = closest_peaks[:, 0]
    selected_peak_cols = closest_peaks[:, 1]
    
    # Set your compass error here! 
    # 0.0 = perfect math, 3.0 = realistic human error
    compass_error_std = 3.0 
    
    noisy_bearings = []
    
    # Calculate bearings before triangulation
    for peak_row, peak_col in zip(selected_peak_rows, selected_peak_cols):
        # Calculate bearing. (Ensure order is Operator Row, Col, Peak Row, Col)
        bearing = get_bearing(true_row, true_col, peak_row, peak_col, noise_std=compass_error_std)
        noisy_bearings.append(bearing)

    # =========================================================
    # 2. RUN THE TRIANGULATION ALGORITHM
    # =========================================================
    print(f"Testing Bearings: {noisy_bearings}")
    
    # Pass the clean list of noisy_bearings into the algorithm
    calculated_peaks, calculated_pos = find_selected_landmarks(
        noisy_bearings, 
        selected_peaks, 
        elevation_data.shape
    )
    
    # Extract the estimated position if successful
    if calculated_pos is not None:
        est_row, est_col = calculated_pos
        print(f"Algorithm Estimated Position: Row {est_row:.1f}, Col {est_col:.1f}")
        print(f"True Operator Position: Row {true_row}, Col {true_col}")
    else:
        print("Triangulation failed.")

    # =========================================================
    # 3. PLOT THE RESULTS
    # =========================================================
    plt.figure(figsize=(12, 8))
    plt.imshow(elevation_data, cmap='terrain', alpha=0.6)
    
    # Plot Landmark Peaks
    plt.scatter(selected_peak_cols, selected_peak_rows, color='magenta', marker='^', s=100, 
                edgecolor='black', label="Landmark Peaks")
    
    # Plot True Operator Position
    plt.scatter(true_col, true_row, color="blue", marker="o", s=100, label="True Operator Position")

    # Plot Algorithm's Estimated Position
    if calculated_pos is not None:
        plt.scatter(est_col, est_row, color="red", marker="x", s=150, 
                    linewidths=3, label="Triangulated Estimate")

    # Draw the Bearing Rays
    line_length = 5000 
    for i, bearing in enumerate(noisy_bearings):
        line_label = "Compass Bearings" if i == 0 else None
        
        # Calculate line endpoint based on the angle
        end_col = true_col + (line_length * math.sin(math.radians(bearing)))
        end_row = true_row - (line_length * math.cos(math.radians(bearing)))
        
        plt.plot([true_col, end_col], [true_row, end_row], color='lime', 
                 linestyle='--', linewidth=1.5, alpha=0.8, label=line_label)

    # Lock map bounds
    map_height, map_width = elevation_data.shape
    plt.xlim(0, map_width)
    plt.ylim(map_height, 0)
    
    plt.legend(loc="upper right", fontsize=10)
    plt.title(f"3-Point Resection", fontsize=14)
    plt.tight_layout()
    plt.show()



##Helper Functions
def find_selected_landmarks(nearest_bearings, all_peaks, map_shape):
    """
    Finds the operator's position using 3-point resection.
    Includes reality-check constraints to prevent off-map geometric coincidences.
    
    map_shape: A tuple of (height, width) from elevation_data.shape
    """
    all_peaks = np.array(all_peaks)
    map_height, map_width = map_shape
    
    try:
        just_bearings = [item[2] for item in nearest_bearings]
    except (TypeError, IndexError):
        just_bearings = nearest_bearings
        
    bearings_rad = np.radians(just_bearings)
    u_x = np.sin(bearings_rad)
    u_y = -np.cos(bearings_rad)
    n_x = np.cos(bearings_rad)
    n_y = np.sin(bearings_rad)
    
    best_error = float('inf')
    best_operator_pos = None
    best_peaks = None

    for peak_combo in itertools.combinations(all_peaks, 3):
        for p in itertools.permutations(peak_combo):
            d0 = n_x[0] * p[0][1] + n_y[0] * p[0][0]
            d1 = n_x[1] * p[1][1] + n_y[1] * p[1][0]
            d2 = n_x[2] * p[2][1] + n_y[2] * p[2][0]
            
            det = n_x[0] * n_y[1] - n_y[0] * n_x[1]
            
            # CONSTRAINT 1: Reject lines that are too parallel (Determinant too small)
            # A small determinant means the peaks are practically lined up with each other.
            if abs(det) < 0.1: 
                continue 
                
            est_col = (d0 * n_y[1] - d1 * n_y[0]) / det
            est_row = (n_x[0] * d1 - n_x[1] * d0) / det
            
            # CONSTRAINT 2: The Bounding Box
            # If the estimated position is off the physical map, discard it immediately!
            if not (0 <= est_row < map_height and 0 <= est_col < map_width):
                continue
            
            # CONSTRAINT 3: Forward Direction Check
            valid_direction = True
            for i in range(3):
                vec_x = p[i][1] - est_col
                vec_y = p[i][0] - est_row
                if (vec_x * u_x[i] + vec_y * u_y[i]) <= 0:
                    valid_direction = False
                    break
                    
            if not valid_direction:
                continue
                
            # Calculate the Error
            error = abs(n_x[2] * est_col + n_y[2] * est_row - d2)
            
            if error < best_error:
                best_error = error
                best_operator_pos = (est_row, est_col)
                best_peaks = p

    if best_operator_pos is None:
        print("Could not find a valid on-map triangulation matching these bearings.")
        return None, None
        
    print(f"Triangulation success! Intersection Error: {best_error:.2f} pixels.")
    return np.array(best_peaks), best_operator_pos
def closest_landmarks(true_row,true_col,peak_cords):
    """
    Find the 3 closest peaks to the true location and inject random noise
    to simulate manual compass bearing error.
    
    NOISE_STD: Standard deviation of the error in pixels (meters). 
               A value of 15 means 68% of the time, the error is within +/- 15 meters.
    """
    peak_cords = np.array(peak_cords)

    rows = peak_cords[:, 0]
    cols = peak_cords[:, 1]

    # 1. Calculate distances and find the 3 closest
    distances = np.sqrt((rows - true_row)**2 + (cols - true_col)**2)
    closest_indices = np.argsort(distances)[:3]
    closest_peaks = peak_cords[closest_indices].astype(float) # Convert to float for math
    
    # 2. Generate random Gaussian noise for both rows and columns
    # size=3 generates a distinct random number for each of the 3 peaks
    row_noise = np.random.normal(loc=0.0, scale=NOISE_STD, size=3)
    col_noise = np.random.normal(loc=0.0, scale=NOISE_STD, size=3)
    
    # 3. Add the noise to the exact peak coordinates
    closest_peaks[:, 0] += row_noise
    closest_peaks[:, 1] += col_noise
    
    # 4. Round back to whole numbers so they remain valid pixel/grid coordinates
    noisy_peaks = np.round(closest_peaks).astype(int)
    return noisy_peaks
def get_bearing(row1, col1, row2, col2, noise_std=0.0):
    """
    Calculate the bearing in degrees between the two points given.
    
    noise_std: The standard deviation of the compass error in degrees.
               Set to 0.0 to disable noise and get perfect bearings.
    """
    # Calculate difference in x (columns). Positive dx means moving East.
    dx = col2 - col1
    
    # Calculate difference in y (rows). 
    dy = row1 - row2
    
    # math.atan2(y, x) normally calculates the angle from the X-axis.
    bearing_radians = math.atan2(dx, dy)
    
    # Convert radians to degrees
    bearing_degrees = math.degrees(bearing_radians)
    
    # --- NOISE INJECTION ---
    # If a noise value is provided, add random Gaussian error to the angle
    if noise_std > 0.0:
        bearing_degrees += random.gauss(0.0, noise_std)
    
    # Normalize the result to a standard 0-360 compass range
    bearing_degrees = (bearing_degrees + 360) % 360
    
    return bearing_degrees
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