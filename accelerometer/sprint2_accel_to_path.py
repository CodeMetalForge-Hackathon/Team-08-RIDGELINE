import numpy as np

ACC_IN_G = False        # set True if the CSV is in g (CoreMotion default)
STILL_SECONDS = 0.8     # phone assumed motionless at the start for this long (used to measure noise)
N_TRIALS = 500          # Gaussian-noise trials averaged to build the path

def integrate(a, t):
    """Trapezoid-rule integral over the real (uneven) timestamps, starting from 0."""
    dt = np.diff(t)[..., None]
    out = np.zeros_like(a)
    out[..., 1:, :] = np.cumsum(0.5 * (a[..., 1:, :] + a[..., :-1, :]) * dt, axis=-2)
    return out

def reconstruct_path(csv_file="accel.csv", n_trials=N_TRIALS, seed=0):
    """Import the iPhone CSV (timestamp,x,y,z) and reconstruct the path of travel.

    Returns:
        t         (N,)   timestamps (seconds)
        path      (N, 3) averaged position, columns [X, Y, Z] in meters
        path_std  (N, 3) uncertainty (std across the noise trials) at each point
    """
    # Import the CSV
    data = np.genfromtxt(csv_file, delimiter=",", names=True)
    t = data["timestamp"]
    acc = np.column_stack([data["x"], data["y"], data["z"]])
    if ACC_IN_G:
        acc = acc * 9.80665                            # g -> m/s^2

    # 1. Characterize the noise from the stationary start: Gaussian with mean = bias, std = sigma
    still = (t - t[0]) < STILL_SECONDS
    bias = acc[still].mean(axis=0)
    sigma = acc[still].std(axis=0, ddof=1)
    acc = acc - bias                                   # remove the constant offset

    # 2. Add Gaussian noise (n_trials versions), integrate each: acc -> vel -> pos
    rng = np.random.default_rng(seed)
    noisy = acc + rng.normal(0.0, sigma, (n_trials,) + acc.shape)
    pos = integrate(integrate(noisy, t), t)

    # 3. Average the trials: mean = reconstructed path, std = uncertainty at each point
    return t, pos.mean(axis=0), pos.std(axis=0)

if __name__ == "__main__":
    t, path, path_std = reconstruct_path()
    print(path)