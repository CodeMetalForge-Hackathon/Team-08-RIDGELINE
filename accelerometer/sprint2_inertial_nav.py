# interpret CSV data --> add noise --> calulate velocity --> calculate acceleration --> data to reconstruct path of travel
# integrate acceleration data
# internal IMU
# characterize noise - function that simulates noisy behavior of iPhone accel
# cumulative integration for pos and vel

"""
Dead-reckoning from iPhone accelerometer data.

Pipeline
  1. Characterize sensor noise from a stationary recording (Gaussian model:
     per-axis bias = mean, sigma = std) and sanity-check the Gaussian assumption.
  2. Remove bias, rotate device-frame acceleration into a fixed world frame.
  3. Detect stationary periods (zero-velocity updates) using the noise model.
  4. Integrate with the trapezoid rule on real timestamps:
        acceleration -> velocity -> position.

Inputs (all from CoreMotion on iOS):
  acc : (N, 3) userAcceleration (gravity already removed), device frame, in g
  t   : (N,)   timestamps in seconds (CMDeviceMotion.timestamp)
  R   : (N, 3, 3) optional device->world rotation matrices (from attitude)

Outputs: acceleration, velocity, position, each an (N, 3) array, columns X, Y, Z.
"""
from dataclasses import dataclass
import numpy as np

G = 9.80665  # m/s^2 per g


# --------------------------------------------------------------------------
# 1. Noise characterization
# --------------------------------------------------------------------------
@dataclass
class NoiseModel:
    bias: np.ndarray    # (3,) mean offset per axis   [m/s^2]
    sigma: np.ndarray   # (3,) std-dev per axis       [m/s^2]
    n: int
    jb: np.ndarray      # (3,) Jarque-Bera statistic (Gaussianity check)

    @property
    def looks_gaussian(self) -> bool:
        """JB ~ chi2(2); 5.99 is the 5% critical value."""
        return bool(np.all(self.jb < 5.99))

    @classmethod
    def from_stationary(cls, acc_still, in_g=True) -> "NoiseModel":
        """Fit a Gaussian N(bias, sigma^2) per axis to a motionless recording."""
        a = np.asarray(acc_still, float) * (G if in_g else 1.0)
        if a.ndim != 2 or a.shape[1] != 3 or len(a) < 30:
            raise ValueError("need an (N>=30, 3) stationary recording")
        n = len(a)
        bias = a.mean(axis=0)
        sigma = a.std(axis=0, ddof=1)
        z = (a - bias) / sigma
        skew = (z ** 3).mean(axis=0)
        exkurt = (z ** 4).mean(axis=0) - 3.0
        jb = n / 6.0 * (skew ** 2 + exkurt ** 2 / 4.0)
        return cls(bias, sigma, n, jb)


# --------------------------------------------------------------------------
# 2-3. Helpers
# --------------------------------------------------------------------------
def _stationary_mask(a_dev, sigma, k=3.0, window=10):
    """True where *every* axis stays inside k*sigma for `window` samples
    (centered). Under the Gaussian model, noise exceeds 3 sigma only ~0.3%
    of the time per sample, so a sustained quiet window means 'not moving'."""
    quiet = np.all(np.abs(a_dev) < k * sigma, axis=1).astype(int)
    run = np.convolve(quiet, np.ones(window, int), mode="same")
    return run >= window


def _cumtrapz0(y, t):
    """Cumulative trapezoid integral with initial value 0, same length as y."""
    out = np.zeros_like(y)
    dt = np.diff(t)[:, None]
    out[1:] = np.cumsum(0.5 * (y[1:] + y[:-1]) * dt, axis=0)
    return out


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
def track(acc, t, noise: NoiseModel, R=None, in_g=True, zupt=False,
          k=3.0, window=10, v0=None, p0=None):
    """Return (acceleration, velocity, position), each (N, 3) in SI units
    (m/s^2, m/s, m) expressed in the world frame (or device frame if R=None)."""
    a = np.asarray(acc, float) * (G if in_g else 1.0)
    t = np.asarray(t, float)
    if a.shape[0] != t.shape[0] or a.shape[1] != 3:
        raise ValueError("acc must be (N,3) and t must be (N,)")
    if np.any(np.diff(t) <= 0):
        raise ValueError("timestamps must be strictly increasing")

    # Bias is a device-frame property -> remove it BEFORE rotating.
    a = a - noise.bias
    still = _stationary_mask(a, noise.sigma, k, window) if zupt else np.zeros(len(a), bool)

    if R is not None:
        a = np.einsum("nij,nj->ni", np.asarray(R, float), a)

    # Deadband: acceleration inside the noise floor carries no information.
    a = np.where(still[:, None], 0.0, a)

    # Velocity with zero-velocity resets, then position.
    v = np.zeros_like(a)
    v[0] = np.zeros(3) if v0 is None else v0
    dt = np.diff(t)
    for i in range(1, len(a)):
        v[i] = 0.0 if still[i] else v[i - 1] + 0.5 * (a[i] + a[i - 1]) * dt[i - 1]
    p = _cumtrapz0(v, t) + (np.zeros(3) if p0 is None else p0)
    return a, v, p


# --------------------------------------------------------------------------
# Demo with synthetic data: still -> push +X -> brake -> still
# --------------------------------------------------------------------------
if __name__ == "__main__":
    rng = np.random.default_rng(0)
    fs = 100.0
    t = np.arange(0, 12, 1 / fs)
    true_a = np.zeros((len(t), 3))
    true_a[(t >= 3) & (t < 4)] = [1.0, 0, 0]       # m/s^2
    true_a[(t >= 4) & (t < 5)] = [-1.0, 0, 0]   # brake immediately: net 1 m
    bias_g = np.array([0.004, -0.002, 0.003])
    meas_g = true_a / G + bias_g + rng.normal(0, 0.002, true_a.shape)

    still_g = bias_g + rng.normal(0, 0.002, (500, 3))      # calibration capture
    nm = NoiseModel.from_stationary(still_g)
    print("bias  [m/s^2]:", nm.bias.round(4))
    print("sigma [m/s^2]:", nm.sigma.round(4), "| Gaussian?", nm.looks_gaussian)

    for z in (False, True):
        a, v, p = track(meas_g, t, nm, zupt=z)
        print(f"ZUPT={z!s:5}  final v={v[-1].round(3)}  final p={p[-1].round(3)}  (truth: v=0, p=[1. 0. 0.])")
