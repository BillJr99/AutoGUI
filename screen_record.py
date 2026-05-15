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
- Capture uses Pillow.ImageGrab (cross-platform, no extra deps beyond
  the existing Pillow requirement).
- Frames are stored as raw PIL Images in a deque(maxlen=N), so memory
  usage is bounded.
- All timing parameters are config-driven; the default is a 5-second
  buffer at 5 FPS = 25 frames.
- `flush()` writes a GIF.  GIF was chosen over MP4 because no ffmpeg
  dependency is required; Pillow can write animated GIFs natively.
- The recorder is a singleton attached to the agent at construction.
- If the underlying ``ImageGrab`` call fails repeatedly (e.g. an X11
  ``BadMatch`` when ``all_screens=True`` overflows the root window
  geometry), the loop self-disables instead of spamming the log every
  ``1/fps`` seconds.  A single warning is logged that points at the
  config knob to suppress it permanently.
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
# Set high enough to ride out a transient glitch (e.g. the screen blanking
# briefly) but low enough that a misconfigured X11 session does not spam the
# debug log for the entire session.
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
        # Sticky flag: once we drop ``all_screens=True`` because it raised a
        # non-TypeError exception (e.g. X11 BadMatch on a multi-monitor setup
        # where the virtual root extends past any real screen), don't try it
        # again for the rest of the session.
        self._all_screens_supported = True

    @staticmethod
    def _check_capture() -> bool:
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
            logger.warning("[screen_record] Pillow.ImageGrab unavailable; recorder disabled.")
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

    def _grab_once(self):
        """Single capture attempt with a multi-screen → single-screen fallback.

        ``ImageGrab.grab(all_screens=True)`` is the right call on most
        multi-monitor setups but can raise X11 ``BadMatch`` when the
        virtual root has an awkward geometry (e.g. one monitor is taller
        than the other and Xinerama's bounding rect extends past either
        real screen).  When that happens we permanently drop the kwarg
        for the rest of the session and retry without it.
        """
        from PIL import ImageGrab
        if self._all_screens_supported:
            try:
                return ImageGrab.grab(all_screens=True)
            except TypeError:
                # Old Pillow without the kwarg.  Permanent fallback.
                self._all_screens_supported = False
            except Exception as exc:
                # Could be an X11 BadMatch ("X get_image failed error 8 …")
                # or any other backend-specific failure.  Try once more
                # without ``all_screens`` and remember the failure for
                # next time so we don't pay this cost every frame.
                self._all_screens_supported = False
                logger.info(
                    "[screen_record] ImageGrab.grab(all_screens=True) failed (%s); "
                    "falling back to single-screen capture for the rest of the session.",
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
