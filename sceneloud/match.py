"""Scene loudness matching library.

Loudness values in this module are LUFS.  Results are expected to be stable
within 0.1 LU.  A scene is a mapping of the form::

    {"start": 0.0, "end": 1.0, "samples": [0.0, 0.1, ...]}

``start`` and ``end`` are seconds on the programme timeline; ``samples`` is
mono PCM, nominally in [-1.0, 1.0].  Scene boundaries are half-open:
[start, end).
"""

import math

LUFS_FLOOR = -70.0
MAX_GAIN_DB = 24.0
RELATIVE_GATE_LU = 10.0


class SceneLoudError(ValueError):
    """Raised when loudness data, scene data, or batch config is invalid."""


def _is_positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value > 0


def _ms_to_lufs(mean_square):
    """Simplified K-weighting energy proxy, floored at -70.0 LUFS."""
    if mean_square <= 0.0:
        return LUFS_FLOOR
    return max(LUFS_FLOOR, -0.691 + 10.0 * math.log10(mean_square))


def _block_mean_square(samples, start, size):
    chunk = samples[start:start + size]
    return sum(sample * sample for sample in chunk) / len(chunk)


def _iter_block_starts(total, block, hop):
    """Yield block start offsets; the final block may be truncated."""
    start = 0
    while start < total:
        yield start
        start += hop


def block_loudness(samples, block_ms, sample_rate) -> list[float]:
    """Measure short-term loudness for every analysis block, in LUFS.

    Blocks overlap by 50% (hop = block / 2) and a final truncated block is
    included, so a scene shorter than one block still gets a measurement.
    """
    if not _is_positive_number(block_ms):
        raise SceneLoudError(
            "block_ms must be a positive number of milliseconds, got %r" % (block_ms,)
        )
    if not _is_positive_number(sample_rate):
        raise SceneLoudError(
            "sample_rate must be a positive number of Hz, got %r" % (sample_rate,)
        )
    if not samples:
        return []

    block = int(round(block_ms / 1000.0 * sample_rate))
    if block <= 0:
        raise SceneLoudError(
            "block_ms=%r is too short for sample_rate=%r" % (block_ms, sample_rate)
        )
    hop = max(1, block // 2)

    values = []
    for start in _iter_block_starts(len(samples), block, hop):
        mean_square = _block_mean_square(samples, start, block)
        values.append(_ms_to_lufs(mean_square))
    return values


def scene_loudness(samples, block, hop) -> float:
    """Return the gated overall loudness of one scene, in LUFS.

    Gating is relative to the material: an absolute gate at -70.0 LUFS is
    followed by a relative gate 10 LU below the mean of the surviving blocks.
    """
    if not isinstance(block, int) or block <= 0:
        raise SceneLoudError("block must be a positive frame count, got %r" % (block,))
    if not isinstance(hop, int) or hop <= 0:
        raise SceneLoudError("hop must be a positive frame count, got %r" % (hop,))
    if not samples:
        raise SceneLoudError("cannot measure loudness of an empty sample list")

    # (lufs, linear mean-square energy) per block; silence floors at -70.
    blocks = []
    for start in _iter_block_starts(len(samples), block, hop):
        mean_square = _block_mean_square(samples, start, block)
        blocks.append((_ms_to_lufs(mean_square), mean_square))

    # Absolute gate at -70.0 LUFS: the floored silent blocks do not pass.
    absolute = [(lufs, energy) for lufs, energy in blocks if lufs > LUFS_FLOOR]
    if not absolute:
        return LUFS_FLOOR

    # Relative gate: 10 LU below the (energy) mean level of the survivors.
    mean_energy = sum(energy for _, energy in absolute) / len(absolute)
    mean_lufs = -0.691 + 10.0 * math.log10(mean_energy)
    relative_gate = mean_lufs - RELATIVE_GATE_LU

    gated = [
        (lufs, energy)
        for lufs, energy in absolute
        if lufs > relative_gate
    ]
    gated_energy = sum(energy for _, energy in gated) / len(gated)
    return -0.691 + 10.0 * math.log10(gated_energy)


def target_gain(lufs, target) -> float:
    """Return the gain in dB needed to move ``lufs`` to ``target``, clamped."""
    return max(-MAX_GAIN_DB, min(MAX_GAIN_DB, target - lufs))


def apply_scene_gain(samples, db) -> list[float]:
    """Apply a linear gain; fall back to the originals instead of clipping."""
    if not samples:
        return []
    factor = 10.0 ** (db / 20.0)
    scaled = [factor * sample for sample in samples]
    if any(sample > 1.0 or sample < -1.0 for sample in scaled):
        # Never clip and never partially attenuate: return an unchanged copy.
        return list(samples)
    return scaled


def smooth_transition(scenes, window_ms) -> list[dict]:
    """Crossfade adjacent scene boundaries without moving the timeline."""
    if not isinstance(window_ms, (int, float)) or isinstance(window_ms, bool) \
            or not math.isfinite(window_ms) or window_ms < 0:
        raise SceneLoudError(
            "window_ms must be a non-negative number, got %r" % (window_ms,)
        )
    if not scenes:
        return []

    result = []
    rates = []
    for index, scene in enumerate(scenes):
        samples = scene.get("samples")
        if not samples:
            raise SceneLoudError("scene %d has an empty samples list" % index)
        start = scene.get("start")
        end = scene.get("end")
        if start is None or end is None or start < 0 or not end > start:
            raise SceneLoudError("scene %d has invalid time bounds" % index)
        rates.append(len(samples) / (end - start))
        result.append({"start": start, "end": end, "samples": list(samples)})

    if window_ms == 0:
        return result

    for index in range(len(result) - 1):
        previous = result[index]
        current = result[index + 1]
        rate = min(rates[index], rates[index + 1])
        n = int(round(window_ms / 1000.0 * rate))
        # Keep every scene's sample count intact; never grow past either end.
        n = min(n, len(previous["samples"]), len(current["samples"]))
        if n <= 0:
            continue

        prev_samples = previous["samples"]
        next_samples = current["samples"]
        prev_tail = len(prev_samples) - n
        for k in range(n):
            fade_out = 1.0 - (k + 1) / n
            fade_in = (k + 1) / n
            mixed = (
                prev_samples[prev_tail + k] * fade_out
                + next_samples[k] * fade_in
            )
            prev_samples[prev_tail + k] = mixed
            next_samples[k] = mixed

    return result


def detect_outliers(scenes, target, max_dev) -> list[int]:
    """Return indices of scenes more than ``max_dev`` LU from ``target``."""
    if not scenes:
        return []
    outliers = []
    for index, scene in enumerate(scenes):
        samples = scene.get("samples")
        if not samples:
            raise SceneLoudError("scene %d has an empty samples list" % index)
        # Full-scene gated loudness: one block spanning the whole scene.
        lufs = scene_loudness(samples, len(samples), len(samples))
        if abs(lufs - target) > max_dev:
            outliers.append(index)
    return outliers


def validate(scenes) -> list[str]:
    """Return a list of human-readable scene problems; [] means valid."""
    problems = []
    bounds_valid = []

    for index, scene in enumerate(scenes):
        start = scene.get("start")
        end = scene.get("end")
        samples = scene.get("samples")

        valid_bounds = (
            start is not None
            and end is not None
            and start >= 0
            and end > start
        )
        bounds_valid.append(valid_bounds)
        if not valid_bounds:
            problems.append(
                "scene %d has invalid time bounds" % index
            )

        if not samples:
            problems.append("scene %d has empty samples" % index)
        else:
            if any(
                not isinstance(sample, (int, float))
                or not math.isfinite(sample)
                or sample < -1.0
                or sample > 1.0
                for sample in samples
            ):
                problems.append(
                    "scene %d has out-of-bounds sample amplitudes" % index
                )

    # Half-open intervals [start, end): touching boundaries are not overlaps.
    for i in range(len(scenes)):
        if not bounds_valid[i]:
            continue
        for j in range(i + 1, len(scenes)):
            if not bounds_valid[j]:
                continue
            a = scenes[i]
            b = scenes[j]
            if a["start"] < b["end"] and b["start"] < a["end"]:
                problems.append(
                    "scene %d overlaps scene %d" % (i, j)
                )

    return problems


def _validate_config(config):
    required = ("target", "max_dev", "block_ms", "hop_ms", "window_ms")
    try:
        values = [config[key] for key in required]
    except (KeyError, TypeError) as exc:
        raise SceneLoudError("batch config is missing required keys") from exc

    target, max_dev, block_ms, hop_ms, window_ms = values
    for name, value in (
        ("target", target),
        ("max_dev", max_dev),
        ("block_ms", block_ms),
        ("hop_ms", hop_ms),
        ("window_ms", window_ms),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or not math.isfinite(value):
            raise SceneLoudError(
                "config field %r must be a finite number, got %r" % (name, value)
            )
    # block_ms=0 (and any non-positive size/step) would divide by zero.
    if block_ms <= 0:
        raise SceneLoudError("block_ms must be positive, got %r" % (block_ms,))
    if hop_ms <= 0:
        raise SceneLoudError("hop_ms must be positive, got %r" % (hop_ms,))
    if window_ms < 0:
        raise SceneLoudError(
            "window_ms must be non-negative, got %r" % (window_ms,)
        )
    if max_dev < 0:
        raise SceneLoudError("max_dev must be non-negative, got %r" % (max_dev,))


def scene_batch(tracks, config) -> dict:
    """Match loudness for a batch of tracks as one all-or-nothing operation."""
    _validate_config(config)
    if not tracks:
        return {"tracks": []}

    # Validate every track and scene before anything is changed or returned.
    for track_index, track in enumerate(tracks):
        rate = track.get("sample_rate")
        if not _is_positive_number(rate):
            raise SceneLoudError(
                "track %r has an invalid sample_rate %r"
                % (track.get("name"), rate)
            )
        problems = validate(track.get("scenes", []))
        if problems:
            raise SceneLoudError(
                "track %r failed validation: %s"
                % (track.get("name"), "; ".join(problems))
            )

    result_tracks = []
    for track in tracks:
        # All per-track state (buffers, frames, scenes) is local: tracks can
        # never inherit each other's analysis buffers.
        rate = track["sample_rate"]
        block = int(round(config["block_ms"] / 1000.0 * rate))
        hop = int(round(config["hop_ms"] / 1000.0 * rate))
        if block <= 0 or hop <= 0:
            raise SceneLoudError(
                "block/hop frame counts must be positive for sample_rate=%r" % rate
            )

        processed_scenes = []
        gains = []
        for scene in track["scenes"]:
            samples = scene["samples"]
            lufs = scene_loudness(samples, block, hop)
            gain = target_gain(lufs, config["target"])
            gains.append(gain)
            processed_scenes.append(
                {
                    "start": scene["start"],
                    "end": scene["end"],
                    "samples": apply_scene_gain(samples, gain),
                }
            )

        processed_scenes = smooth_transition(
            processed_scenes, config["window_ms"]
        )
        outliers = detect_outliers(
            processed_scenes, config["target"], config["max_dev"]
        )

        result_tracks.append(
            {
                "name": track["name"],
                "sample_rate": rate,
                "scenes": processed_scenes,
                "gains": gains,
                "outliers": outliers,
            }
        )

    return {"tracks": result_tracks}
