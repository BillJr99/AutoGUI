"""
som.py — Set-of-Mark annotation.

Given a screenshot (PIL.Image) and a list of marks (dicts with id, x, y,
width, height, optional name/role), draw numbered bounding boxes on the
image so a vision-capable model can refer to UI elements by mark id
instead of pixel coordinates.

Marks are expressed in screen pixels.  Annotation happens on the
full-resolution image before any downscale, so the boxes align exactly
with screen coordinates after the downscale is applied.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# Distinct colours cycled across mark IDs so adjacent marks are easy to
# tell apart in a screenshot.
_PALETTE = [
    (255,  60,  60),
    ( 60, 200,  90),
    ( 70, 130, 255),
    (255, 165,  30),
    (200,  90, 220),
    ( 30, 200, 200),
    (220, 200,  60),
]


def annotate(img, marks: list[dict]):
    """
    Draw numbered rectangles on a PIL image.

    Parameters
    ----------
    img : PIL.Image.Image
        Full-resolution screenshot.  Modified in place via ImageDraw.
    marks : list[dict]
        Each mark needs id (int), x, y, width, height (screen px).
        Optional "kind" influences box width: window=3, control=2.

    Returns
    -------
    PIL.Image.Image
        The same image, annotated.  Returned for chaining convenience.
    """
    try:
        from PIL import ImageDraw, ImageFont
    except ImportError:
        logger.warning("[som.annotate] Pillow not available; skipping annotation.")
        return img

    draw = ImageDraw.Draw(img, mode="RGBA")

    font = None
    for size in (22, 18, 14):
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
            break
        except (OSError, IOError):
            try:
                font = ImageFont.truetype("Arial Bold.ttf", size)
                break
            except (OSError, IOError):
                continue
    if font is None:
        font = ImageFont.load_default()

    for m in marks:
        try:
            x = int(m["x"]); y = int(m["y"])
            w = int(m.get("width", 0)); h = int(m.get("height", 0))
            if w <= 1 or h <= 1:
                continue
        except (KeyError, TypeError, ValueError):
            continue

        mid = m.get("id", 0)
        colour = _PALETTE[(mid - 1) % len(_PALETTE)] if mid else _PALETTE[0]
        kind = m.get("kind", "control")
        line_w = 3 if kind == "window" else 2

        draw.rectangle([x, y, x + w, y + h], outline=colour + (255,), width=line_w)

        # Label: numbered tag in the top-left corner of the rect.
        tag = str(mid)
        try:
            bbox = draw.textbbox((0, 0), tag, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            tw, th = font.getsize(tag) if hasattr(font, "getsize") else (16, 16)
        pad = 3
        # Place label inside the box if the box is tall enough; otherwise above.
        ly = y + 2 if h > th + 2 * pad else max(0, y - th - 2 * pad)
        lx = x + 2
        draw.rectangle(
            [lx, ly, lx + tw + 2 * pad, ly + th + 2 * pad],
            fill=colour + (230,),
        )
        draw.text((lx + pad, ly + pad), tag, fill=(255, 255, 255, 255), font=font)

    return img
