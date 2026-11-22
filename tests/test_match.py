"""Contract tests for the scene loudness matching stubs.

Every test in this file is red in the initial state: each public function in
``sceneloud.match`` raises ``NotImplementedError``.  The tests must not be
modified while fixing the implementation.
"""

import copy
import math

import pytest

from sceneloud.match import (
    SceneLoudError,
    apply_scene_gain,
    block_loudness,
    detect_outliers,
    scene_batch,
    scene_loudness,
    smooth_transition,
    target_gain,
    validate,
)


def _tone(lufs, count, sample_rate=16000, frequency=1000.0):
    """Build a sine whose mean-square loudness proxy is ``lufs`` LUFS."""
    mean_square = 10.0 ** ((lufs + 0.691) / 10.0)
    amplitude = math.sqrt(2.0 * mean_square)
    return [
        amplitude * math.sin(2.0 * math.pi * frequency * index / sample_rate)
        for index in range(count)
    ]


def _scene(start, end, samples):
    """Build a scene mapping with the timeline and mono samples."""
    return {"start": start, "end": end, "samples": samples}


def test_block_loudness_uses_overlapping_blocks_for_short_scene():
    """A 120 ms scene with 100 ms blocks must yield 50%-overlapped blocks."""
    samples = [0.1] * 1920
    blocks = block_loudness(samples, block_ms=100, sample_rate=16000)
    assert len(blocks) == 3
    assert all(math.isfinite(value) for value in blocks)


def test_scene_loudness_gates_relative_to_content_not_fixed_minus_20():
    """Quiet dialogue below -20 LUFS must survive gating; silence must not drag it down."""
    rate = 16000
    samples = _tone(-30.0, rate, rate) + [0.0] * rate
    lufs = scene_loudness(samples, block=rate, hop=rate)
    assert lufs == pytest.approx(-30.0, abs=0.1)


def test_target_gain_clamps_to_plus_minus_24_db():
    """Gain requests are limited to the inclusive [-24, +24] dB range."""
    assert target_gain(-60.0, 0.0) == pytest.approx(24.0, abs=1e-9)
    assert target_gain(0.0, -60.0) == pytest.approx(-24.0, abs=1e-9)


def test_apply_scene_gain_falls_back_instead_of_clipping():
    """A gain that would exceed full scale returns the original, unclipped samples."""
    samples = [0.8, -0.9]
    result = apply_scene_gain(samples, 6.0)
    assert result == pytest.approx(samples, abs=1e-12)


def test_smooth_transition_preserves_lengths_and_boundaries():
    """Crossfading must not insert or remove samples or shift scene boundaries."""
    scenes = [
        _scene(0.0, 1.0, [0.2] * 1000),
        _scene(1.0, 2.0, [-0.2] * 1000),
    ]
    result = smooth_transition(scenes, window_ms=100.0)
    assert [(scene["start"], scene["end"]) for scene in result] == [
        (0.0, 1.0),
        (1.0, 2.0),
    ]
    assert [len(scene["samples"]) for scene in result] == [1000, 1000]
    assert sum(len(scene["samples"]) for scene in result) == 2000


def test_detect_outliers_uses_target_relative_deviation():
    """Only the scene more than max_dev LU from target is reported, by index."""
    scenes = [
        _scene(0.0, 1.0, _tone(-23.0, 16000)),
        _scene(1.0, 2.0, _tone(-31.0, 16000)),
        _scene(2.0, 3.0, _tone(-22.0, 16000)),
    ]
    assert detect_outliers(scenes, target=-23.0, max_dev=3.0) == [1]


def test_validate_flags_overlapping_scenes():
    """Overlapping scene ranges must be reported instead of processed twice."""
    scenes = [
        _scene(0.0, 2.0, [0.1] * 2000),
        _scene(1.0, 3.0, [0.1] * 2000),
    ]
    problems = validate(scenes)
    assert any("overlap" in problem.lower() for problem in problems)


def test_validate_flags_empty_samples():
    """A scene with no samples must be reported, never measured as -inf."""
    problems = validate([_scene(0.0, 1.0, [])])
    assert any("empty" in problem.lower() for problem in problems)


def test_scene_batch_rolls_back_on_invalid_track():
    """One invalid track aborts the batch and leaves every input unchanged."""
    tracks = [
        {
            "name": "valid",
            "sample_rate": 1000,
            "scenes": [_scene(0.0, 1.0, [0.1] * 1000)],
        },
        {
            "name": "invalid",
            "sample_rate": 1000,
            "scenes": [
                _scene(0.0, 1.0, [0.1] * 1000),
                _scene(0.5, 1.5, [0.1] * 1000),
            ],
        },
    ]
    config = {
        "target": -23.0,
        "max_dev": 6.0,
        "block_ms": 100,
        "hop_ms": 50,
        "window_ms": 100,
    }
    snapshot = copy.deepcopy(tracks)
    with pytest.raises(SceneLoudError):
        scene_batch(tracks, config)
    assert tracks == snapshot


def test_block_loudness_rejects_zero_block_ms():
    """block_ms=0 is invalid for direct analysis and for batch config."""
    with pytest.raises(SceneLoudError):
        block_loudness([0.1] * 1600, block_ms=0, sample_rate=16000)
    with pytest.raises(SceneLoudError):
        scene_batch(
            [],
            {
                "target": -23.0,
                "max_dev": 3.0,
                "block_ms": 0,
                "hop_ms": 50,
                "window_ms": 100,
            },
        )


def test_constant_minus_23_lufs_signal_needs_no_gain():
    """A -23 LUFS constant signal needs zero gain within the 0.1 LU tolerance."""
    rate = 16000
    samples = _tone(-23.0, 2 * rate, rate)
    lufs = scene_loudness(samples, block=rate, hop=rate // 2)
    gain = target_gain(lufs, -23.0)
    assert gain == pytest.approx(0.0, abs=0.1)


def test_target_gain_is_zero_when_target_equals_current():
    """Exactly matching target and current loudness yields 0 dB."""
    assert target_gain(-23.0, -23.0) == pytest.approx(0.0, abs=0.1)


def test_empty_scene_list_returns_empty_results():
    """Empty scene collections are valid and return empty collections."""
    assert detect_outliers([], target=-23.0, max_dev=1.0) == []
    assert validate([]) == []
    assert smooth_transition([], window_ms=100.0) == []


def test_scene_batch_isolates_tracks_with_different_sample_rates():
    """Each track must use its own block buffer; a 1 kHz track must not inherit 16 kHz state."""
    tracks = [
        {
            "name": "wide",
            "sample_rate": 16000,
            "scenes": [_scene(0.0, 1.0, _tone(-23.0, 16000, 16000))],
        },
        {
            "name": "narrow",
            "sample_rate": 1000,
            "scenes": [_scene(0.0, 1.0, _tone(-30.0, 1000, 1000, frequency=100.0))],
        },
    ]
    config = {
        "target": -23.0,
        "max_dev": 6.0,
        "block_ms": 100,
        "hop_ms": 50,
        "window_ms": 100,
    }
    result = scene_batch(tracks, config)
    assert result["tracks"][0]["gains"][0] == pytest.approx(0.0, abs=0.2)
    assert result["tracks"][1]["gains"][0] == pytest.approx(7.0, abs=0.2)


def test_scene_batch_preserves_track_order_on_success():
    """A successful batch returns results in the same order as the input tracks."""
    names = ["intro", "dialogue", "outro"]
    tracks = [
        {
            "name": name,
            "sample_rate": 1000,
            "scenes": [
                _scene(
                    float(index),
                    float(index + 1),
                    _tone(-23.0, 1000, 1000, frequency=100.0),
                )
            ],
        }
        for index, name in enumerate(names)
    ]
    config = {
        "target": -23.0,
        "max_dev": 6.0,
        "block_ms": 100,
        "hop_ms": 50,
        "window_ms": 100,
    }
    result = scene_batch(tracks, config)
    assert [track["name"] for track in result["tracks"]] == names
