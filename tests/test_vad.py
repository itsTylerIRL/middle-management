import numpy as np

from middlemanagementd.audio.vad import VADState


def frames(level: float, n: int, rng=np.random.default_rng(0)):
    return [rng.normal(0, level, 480).astype(np.float32) for _ in range(n)]


def test_detects_speech_after_quiet_room():
    v = VADState(frame_seconds=0.03)
    for f in frames(0.003, 15):
        v.push(f)
    assert not v.in_speech
    for f in frames(0.1, 10):
        v.push(f)
    assert v.in_speech and v.speech_seconds > 0.2


def test_spiky_calibration_does_not_deafen():
    v = VADState(frame_seconds=0.03)
    calib = frames(0.02, 10)
    calib[2] = calib[2] * 40  # keyboard clack
    calib[7] = calib[7] * 40
    for f in calib:
        v.push(f)
    assert v.threshold < 0.1
    for f in frames(0.12, 8):
        v.push(f)
    assert v.in_speech


def test_isolated_spike_is_not_speech():
    v = VADState(frame_seconds=0.03)
    for f in frames(0.003, 12):
        v.push(f)
    v.push(frames(0.5, 1)[0])
    for f in frames(0.003, 5):
        v.push(f)
    assert not v.in_speech


def test_end_of_speech_silence_counted():
    v = VADState(frame_seconds=0.03)
    for f in frames(0.003, 10) + frames(0.1, 10) + frames(0.003, 20):
        v.push(f)
    assert v.silence_seconds >= 0.5
