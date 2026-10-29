import numpy as np

from middlemanagementd.audio.echo import BargeInDetector, EchoModel, calibrate, envelope

RNG = np.random.default_rng(3)


def speechy_envelope(n: int) -> np.ndarray:
    """Syllable-like bursts, like Piper output."""
    t = np.arange(n)
    return (0.15 * (0.6 + 0.4 * np.sin(t / 2.3)) * (RNG.random(n) > 0.15)).astype(np.float32)


def echo_of(ref: np.ndarray, gain: float, delay: int, noise: float, n: int) -> np.ndarray:
    mic = np.full(n, noise, dtype=np.float32)
    mic[delay : delay + len(ref)] += gain * ref[: n - delay]
    return mic + RNG.normal(0, noise * 0.1, n).astype(np.float32)


def test_envelope_shape():
    env = envelope(np.ones(48000, dtype=np.float32) * 0.5, 48000)
    assert len(env) == 33 and np.allclose(env, 0.5)


def test_calibration_recovers_gain_and_delay():
    ref = speechy_envelope(80)
    mic = echo_of(ref, gain=1.8, delay=5, noise=0.02, n=110)
    m = calibrate(ref, mic, noise_floor=0.02)
    assert m is not None and m.calibrated
    assert m.delay_frames == 5
    assert 1.8 <= m.gain <= 1.8 * 1.4


def test_no_echo_means_near_zero_gain():
    ref = speechy_envelope(80)
    m = calibrate(ref, np.full(110, 0.01, dtype=np.float32), noise_floor=0.01)
    assert m is not None and m.gain < 0.05


def run_detector(mic: np.ndarray, ref: np.ndarray, model: EchoModel, noise: float) -> int | None:
    d = BargeInDetector(ref, model, noise, ratio=2.0, min_seconds=0.35)
    for i, rms in enumerate(mic):
        if d.push(i, float(rms)):
            return i
    return None


def test_own_echo_does_not_trigger():
    ref = speechy_envelope(200)
    mic = echo_of(ref, 1.8, 5, 0.02, 220)
    model = calibrate(ref[:80], mic[:110], 0.02)
    assert run_detector(mic, ref, model, 0.02) is None


def test_user_talking_over_triggers():
    ref = speechy_envelope(200)
    mic = echo_of(ref, 1.8, 5, 0.02, 220)
    model = calibrate(ref[:80], mic[:110], 0.02)
    mic[120:180] += 1.2  # user speaking much louder than the echo, close to the mic
    hit = run_detector(mic, ref, model, 0.02)
    assert hit is not None and 120 <= hit < 140


def test_headphones_reduce_to_plain_vad():
    ref = speechy_envelope(200)
    model = EchoModel(gain=0.0, delay_frames=0, calibrated=True)
    mic = np.full(220, 0.01, dtype=np.float32)
    assert run_detector(mic, ref, model, 0.01) is None
    mic[50:70] = 0.08
    assert run_detector(mic, ref, model, 0.01) is not None
