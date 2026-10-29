import shutil
import subprocess

import numpy as np
import pytest

from middlemanagementd.audio.engine import AudioEngine, decode_audio_file
from middlemanagementd.audio.synth import hold_music_loop

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_decode_audio_file(tmp_path):
    path = tmp_path / "opus.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-ac", "2", str(path)], check=True)
    samples = decode_audio_file(path)
    assert abs(len(samples) / 48000 - 2.0) < 0.1
    assert np.isclose(np.abs(samples).max(), 0.8, atol=0.01)


def test_engine_uses_configured_file_and_falls_back(cfg, tmp_path):
    path = tmp_path / "hold.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=1", str(path)], check=True)
    cfg.audio.hold_music_file = str(path)
    assert abs(len(AudioEngine(cfg).hold_music_samples()) - 48000) < 2000

    cfg.audio.hold_music_file = str(tmp_path / "missing.mp3")
    assert np.array_equal(AudioEngine(cfg).hold_music_samples(), hold_music_loop())
