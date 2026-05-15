"""
screen_record.py — Rolling screen buffer for failure post-mortem.

A background thread captures one frame every N ms into a fixed-size deque.
When a tool call fails, the agent asks for the buffer to be flushed, which
writes the most recent frames out as an animated GIF.

Why a buffer instead of always recording?
-----------------------------------------
Continuous recording is expensive and most of the time you don't care.
A short rolling window only costs you when something goes wrong, and
gives you a video of *how the agent got into trouble* rather than just
the final-state screenshot the auto-verify already captures.

Design
------
- Capture prefers mss (already a project dependency) which enumerates
  physical monitors individually — this handles asymmetric multi-monitor
  X11 correctly (no BadMatch, monitors at different heights supported).
  Falls back to Pillow.ImageGrab when mss is not importable.
- Frames are stored as raw PIL Images in a deque(maxlen=N), so memory
  usage is bounded.
- All timing parameters are config-driven; the default is a 5-second
  buffer at 5 FPS = 25 frames.
- `flush()` writes a GIF.  GIF was chosen over MP4 because no ffmpeg
  dependency is required; Pillow can write animated GIFs natively.
- The recorder is a singleton attached to the agent at construction.
- If the underlying capture call fails repeatedly, the loop self-disables
  instead of spamming the log every 1/fps seconds.  A single warning is
  logged that points at the config knob to suppress it permanently.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# After this many consecutive capture failures the loop gives up and exits.
_MAX_CONSECUTIVE_FAILURES = 5


class ScreenRecorder:
    """Lightweight rolling screen buffer.  Safe to run for the whole session."""

    def __init__(
        self,
        out_dir: str = "screenshots/failures",
        fps: int = 5,
        buffer_seconds: float = 5.0,
        max_width: int = 960,
    ):
        self.out_dir = Path(out_dir).expanduser()
        self.fps = max(1, int(fps))
        self.max_frames = max(1, int(self.fps * float(buffer_seconds)))
        self.max_width = max(0, int(max_width))

        self._frames: deque = deque(maxlen=self.max_frames)
        self._frames_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture_available = self._check_capture()
        # None = not yet attempted; False = permanently disabled (import failed
        # or runtime error).
        self._use_mss: bool | None = None
        # Sticky flag: if all_screens=True raises on Pillow ImageGrab, fall
        # back to single-screen for the rest of the session.
        self._all_screens_supported = True

    @staticmethod
    def _check_capture() -> bool:
        try:
            import mss  # noqa: F401
            return True
        except ImportError:
            pass
        try:
            from PIL import ImageGrab  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if not self._capture_available:
            logger.warning("[screen_record] No capture backend (mss or Pillow); recorder disabled.")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name="ScreenRecorder", daemon=True
        )
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None
        with self._frames_lock:
            self._frames.clear()

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    @staticmethod
    def _grab_mss():
        """Capture all physical monitors using mss and stitch into one PIL Image.

        mss enumerates actual physical monitors (sct.monitors[1:]) and captures
        each at its real geometry, so it works correctly on Linux X11 with
        asymmetric multi-monitor setups (monitors at different heights or with
        different origins) where Pillow ImageGrab.grab(all_screens=True) raises
        BadMatch (X error 8).

        Monitors are composited onto a canvas whose bounding box covers all of
        them, preserving relative positions.  Black fill covers any gaps.
        """
        import mss as _mss
        from PIL import Image

        with _mss.mss() as sct:
            monitors = sct.monitors[1:]  # skip index 0 (virtual combined rect)
            if not monitors:
                monitors = sct.monitors  # ultra-fallback

            if len(monitors) == 1:
                raw = sct.grab(monitors[0])
                return Image.frombytes("RGB", raw.size, raw.rgb)

            # Multi-monitor: determine bounding box and stitch
            left   = min(m["left"]              for m in monitors)
            top    = min(m["top"]               for m in monitors)
            right  = max(m["left"] + m["width"] for m in monitors)
            bottom = max(m["top"] + m["height"] for m in monitors)
            canvas = Image.new("RGB", (right - left, bottom - top), (0, 0, 0))

            for monitor in monitors:
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.rgb)
                canvas.paste(img, (monitor["left"] - left, monitor["top"] - top))

            return canvas

    def _grab_once(self):
        """Single capture with multi-monitor support.

        Preference order:
        1. mss — captures each physical monitor individually and stitches them;
           handles asymmetric multi-monitor X11 correctly.
        2. Pillow ImageGrab(all_screens=True) — correct on Windows/macOS; may
           raise BadMatch on X11 with uneven monitor geometry.
        3. Pillow ImageGrab() — primary monitor only (last resort).
        """
        # --- mss path (preferred) ---
        if self._use_mss is not False:
            try:
                img = self._grab_mss()
                self._use_mss = True
                return img
            except ImportError:
                self._use_mss = False  # mss not installed; skip permanently
            except Exception as exc:
                self._use_mss = False
                logger.info(
                    "[screen_record] mss capture failed (%s); trying Pillow fallback.",
                    exc,
                )

        # --- Pillow path (fallback) ---
        from PIL import ImageGrab
        if self._all_screens_supported:
            try:
                return ImageGrab.grab(all_screens=True)
            except TypeError:
                # Old Pillow without the kwarg.  Permanent fallback.
                self._all_screens_supported = False
            except Exception as exc:
                self._all_screens_supported = False
                logger.info(
                    "[screen_record] ImageGrab.grab(all_screens=True) failed (%s); "
                    "falling back to single-screen capture.",
                    exc,
                )
        return ImageGrab.grab()

    def _capture_loop(self):
        from PIL import Image
        interval = 1.0 / self.fps
        next_tick = time.monotonic()
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                img = self._grab_once()
                if self.max_width and img.width > self.max_width:
                    ratio = self.max_width / img.width
                    img = img.resize(
                        (self.max_width, int(img.height * ratio)),
                        Image.LANCZOS,
                    )
                with self._frames_lock:
                    self._frames.append((time.monotonic(), img))
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures == 1:
                    logger.warning(
                        "[screen_record] capture failed (%s); will retry %d more time(s) "
                        "before disabling the recorder. Set "
                        "agent.screen_record.enabled=false in config.json to suppress.",
                        e,
                        _MAX_CONSECUTIVE_FAILURES - 1,
                    )
                else:
                    logger.debug("[screen_record] capture failed: %s", e)
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "[screen_record] %d consecutive capture failures; "
                        "stopping the screen recorder for this session. "
                        "Set agent.screen_record.enabled=false in config.json "
                        "to skip recorder startup entirely.",
                        consecutive_failures,
                    )
                    return

            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                self._stop.wait(timeout=sleep_for)
            else:
                next_tick = time.monotonic()  # got behind; reset

    # ------------------------------------------------------------------
    # Flush
    # ------------------------------------------------------------------

    def flush(self, label: str = "failure") -> str | None:
        """Write the current buffer to an animated GIF.  Returns the path
        on success, or None if there are no frames."""
        with self._frames_lock:
            frames = list(self._frames)
        if not frames:
            return None
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)[:60]
            path = self.out_dir / f"{ts}_{safe}.gif"
            imgs = [img for _t, img in frames]
            duration_ms = int(1000 / self.fps)
            imgs[0].save(
                str(path),
                save_all=True,
                append_images=imgs[1:],
                duration=duration_ms,
                loop=0,
                optimize=True,
            )
            return str(path)
        except Exception as e:
            logger.warning("[screen_record] flush failed: %s", e)
            return None
