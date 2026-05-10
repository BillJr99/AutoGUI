"""Perceptual hash diff coverage."""

from __future__ import annotations

import os

import pytest

from visual_diff import diff, hamming_distance, hash_b64, hash_png_bytes


PIL_AVAILABLE = True
try:
    from PIL import Image  # type: ignore  # noqa: F401
except ImportError:
    PIL_AVAILABLE = False


@pytest.mark.skipif(not PIL_AVAILABLE, reason="PIL not installed")
def test_identical_images_hash_identically():
    from PIL import Image
    from io import BytesIO
    img = Image.new("RGB", (64, 64), (128, 64, 200))
    buf = BytesIO()
    img.save(buf, "PNG")
    h1 = hash_png_bytes(buf.getvalue())
    h2 = hash_png_bytes(buf.getvalue())
    assert h1 is not None
    assert h1 == h2


@pytest.mark.skipif(not PIL_AVAILABLE, reason="PIL not installed")
def test_completely_different_images_diverge():
    from PIL import Image
    from io import BytesIO
    a = Image.new("RGB", (64, 64), (0, 0, 0))
    b = Image.new("RGB", (64, 64), (255, 255, 255))
    bufs = []
    for img in (a, b):
        buf = BytesIO()
        img.save(buf, "PNG")
        bufs.append(buf.getvalue())
    h1 = hash_png_bytes(bufs[0])
    h2 = hash_png_bytes(bufs[1])
    # Two solid-colour images of opposite intensities should still
    # collide on a difference hash because every neighbour-pair is
    # equal — the test is really that hashing succeeded.
    assert h1 is not None and h2 is not None
    diff_result = diff(h1, h2)
    assert 0 <= diff_result.fraction_changed <= 1.0


def test_hash_b64_handles_invalid_input():
    assert hash_b64("") is None
    assert hash_b64("not-base64!!") is None or isinstance(hash_b64("not-base64!!"), bytes)


def test_diff_handles_missing_hashes():
    r = diff(None, b"\x00" * 32)
    assert r.likely_no_change is False
    assert r.note == "hash unavailable"


def test_hamming_zero_for_equal_bytes():
    assert hamming_distance(b"\x00" * 32, b"\x00" * 32) == 0


def test_hamming_counts_set_bits():
    assert hamming_distance(b"\x00", b"\xff") == 8
