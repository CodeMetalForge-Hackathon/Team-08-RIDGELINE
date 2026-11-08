"""Make a fake iPhone accelerometer CSV (timestamp,x,y,z) for testing.
Phone rests 1 s, moves 1 m along X, then 1 m along Y while rising 0.2 m, then rests.
Sensor has a small constant bias plus Gaussian noise. Returns the true path too."""
import numpy as np

def make_test_csv(path="accel.csv", seed=0, noise_std=0.02, bias=(0.03, -0.02, 0.02)):
    rng = np.random.default_rng(seed)
    t = 10.0 + np.concatenate([[0.0], np.cumsum(rng.uniform(0.008, 0.012, 599))])  # ~100 Hz, jittered
    t0 = t - t[0]

    def leg(start, dur, disp):                    # smooth rest-to-rest move
        tau = np.clip((t0 - start) / dur, 0, 1)
        s   = 10*tau**3 - 15*tau**4 + 6*tau**5
        s_dd = np.where((t0 > start) & (t0 < start + dur),
                        (60*tau - 180*tau**2 + 120*tau**3) / dur**2, 0.0)
        return np.outer(s, disp), np.outer(s_dd, disp)

    p1, a1 = leg(1.0, 1.5, [1.0, 0.0, 0.0])
    p2, a2 = leg(3.0, 1.5, [0.0, 1.0, 0.2])
    true_pos, true_acc = p1 + p2, a1 + a2
    acc = true_acc + np.array(bias) + rng.normal(0, noise_std, true_acc.shape)
    np.savetxt(path, np.column_stack([t, acc]), delimiter=",", header="timestamp,x,y,z", comments="")
    return t, true_pos

if __name__ == "__main__":
    make_test_csv()