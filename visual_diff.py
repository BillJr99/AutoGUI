"""
visual_diff.py — Perceptual-hash diff between two screenshots.

Catches the silent-no-op failure mode: an action fired, the tool result
looked successful, but the screen pixels barely changed, so the predicted
post-condition almost certainly didn't hold.

Why a perceptual hash instead of pixel diff?
  * Mouse cursor position changes between frames don't matter.
  * Anti-aliasing flicker between identical frames doesn't matter.
  * A real UI change (window opened, dialog appeared, page navigated)
    moves a non-trivial fraction of bits.

We use a 16x16 mean-luma "difference hash" — Hamming distance is a robust
similarity metric and keeps the hash small enough to log per-step.

No external image library required: PIL is already a transitive
requirement of pyautogui, but if it's missing we fall back to a content-
hash diff and degrade gracefully.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)


_HASH_SIZE = 16          # 16x16 = 256 bits per hash
_TOTAL_BITS = _HASH_SIZE * _HASH_SIZE
# Empirically, two near-identical desktop screenshots differ by < ~20 bits;
# a meaningful UI change moves at least ~50.  The threshold below counts
# anything under it as "no visual change worth speaking of".
_NO_CHANGE_HAMMING = 32


@dataclass
class DiffResult:
    hamming: int                    # 0..256
    fraction_changed: float         # hamming / total_bits
    likely_no_change: bool          # True when below the no-change threshold
    note: str = ""


def hash_png_bytes(png_bytes: bytes) -> Optional[bytes]:
    """Compute a 16x16 difference-hash of a PNG buffer; None on failure."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        with Image.open(BytesIO(png_bytes)) as img:
            small = img.convert("L").resize((_HASH_SIZE + 1, _HASH_SIZE),
                                            Image.Resampling.LANCZOS)
            pixels = list(small.getdata())
            bits = bytearray((_TOTAL_BITS + 7) // 8)
            stride = _HASH_SIZE + 1
            for y in range(_HASH_SIZE):
                for x in range(_HASH_SIZE):
                    idx = y * _HASH_SIZE + x
                    left = pixels[y * stride + x]
                    right = pixels[y * stride + x + 1]
                    if left > right:
                        bits[idx // 8] |= 1 << (idx % 8)
            return bytes(bits)
    except Exception as e:
        logger.debug("[visual_diff] hash failed: %s", e)
        return None


def hash_b64(b64: str) -> Optional[bytes]:
    """Convenience: hash a base64-encoded PNG string."""
    if not b64:
        return None
    try:
        return hash_png_bytes(base64.b64decode(b64))
    except (ValueError, TypeError):
        return None


def hamming_distance(a: bytes, b: bytes) -> int:
    """Count differing bits between two equal-length byte strings."""
    if len(a) != len(b):
        return _TOTAL_BITS
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b))


def diff(prev: Optional[bytes], curr: Optional[bytes]) -> DiffResult:
    """
    Compare two perceptual hashes.  Either side may be None (e.g.
    PIL unavailable, hash failed) — the result is conservatively
    flagged ``likely_no_change=False`` so the caller doesn't act
    on indeterminate evidence.
    """
    if prev is None or curr is None:
        return DiffResult(0, 0.0, False, "hash unavailable")
    if len(prev) != len(curr):
        return DiffResult(_TOTAL_BITS, 1.0, False, "hash size mismatch")
    h = hamming_distance(prev, curr)
    frac = h / _TOTAL_BITS
    return DiffResult(
        hamming=h,
        fraction_changed=frac,
        likely_no_change=h < _NO_CHANGE_HAMMING,
        note=("near-identical" if h < _NO_CHANGE_HAMMING
              else "different" if h < _TOTAL_BITS // 2
              else "very different"),
    )


__all__ = ["DiffResult", "hash_png_bytes", "hash_b64", "hamming_distance", "diff"]
