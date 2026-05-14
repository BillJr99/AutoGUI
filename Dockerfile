# AutoGUI mainline — containerised desktop automation agent
#
# Build:
#   docker build -t autogui .
#
# Run (interactive bash shell, self-contained image):
#   docker run -it --rm \
#     -e DISPLAY=$DISPLAY \
#     -v /tmp/.X11-unix:/tmp/.X11-unix \
#     autogui
#
# Then inside the container run `python main.py` or `pi` as needed.
#
# Run (mount your local clone so edits take effect without rebuilding):
#   docker run -it --rm \
#     -e DISPLAY=$DISPLAY \
#     -v /tmp/.X11-unix:/tmp/.X11-unix \
#     -v "$(pwd):/app" \
#     autogui
#
# X11 forwarding is required for the screenshot and input tools on Linux.
# Pass DISPLAY and the X11 socket, or provide an Xvfb virtual display.
#
# Before starting, copy config.json.example to config.json (in /app) and
# fill in your OpenWebUI base_url, api_key, and model name.
#
# Note: pi-extension/node_modules/ lives inside /app.  If you mount a
# local clone that has not had `cd pi-extension && npm install` run, the
# pi-extension will not be available.  All other Python and system deps
# are installed outside /app and survive volume mounts.
#
# ── REST API mode ──────────────────────────────────────────────────────────
# Default: TUI/CLI mode (python main.py)
#
# Run API server (real agent — requires X11 display and OpenWebUI):
#   docker run -p 8002:8002 \
#     -e DISPLAY=$DISPLAY \
#     -v /tmp/.X11-unix:/tmp/.X11-unix \
#     -e OPENWEBUI_BASE_URL=http://host.docker.internal:3000 \
#     -e OPENWEBUI_API_KEY=sk-... \
#     -e OPENWEBUI_MODEL=llama3.1:70b \
#     autogui python api.py
#
# Run API server (dry-run mode — no display or OpenWebUI needed):
#   docker run -p 8002:8002 \
#     -e AUTOGUI_DRY_RUN=true \
#     autogui python api.py
#
# See docs/REST_API.md for the full endpoint reference.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="AutoGUI" \
      org.opencontainers.image.description="Desktop automation agent driven by an LLM tool-calling loop"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# REST API server settings (override at runtime with -e)
ENV AUTOGUI_DRY_RUN=false
ENV AUTOGUI_API_PORT=8002

# ── System packages ────────────────────────────────────────────────────────
# These match what scripts/install-dependencies.sh installs on Linux/X11.
# Installing them here in one layer means the script finds them already
# present, skips the apt-get step, and only runs pip / npm / Playwright.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Utilities needed by the install script and runtime (gnupg for NodeSource key)
    bash curl git gnupg \
    # pyautogui / Pillow
    python3-tk python3-dev libx11-dev \
    # X11 desktop automation (xclip required by get_window_text on X11)
    xdotool wmctrl scrot x11-utils xclip \
    # OCR — desktop_click_text / desktop_find_text
    tesseract-ocr \
    # ImageMagick — Set-of-Mark overlay + failure GIF recording
    imagemagick \
    # AT-SPI — desktop_click_element on Linux
    python3-pyatspi gir1.2-atspi-2.0 \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 20.x ──────────────────────────────────────────────────────────
# Debian bookworm ships Node 18; @earendil-works/pi-coding-agent requires
# >=20.6.  Add the NodeSource APT repo with its signed key explicitly so
# the trust chain is auditable (avoids curl|bash).  Note: nodejs is not
# version-pinned here, so patch releases may vary across builds; pin to
# a specific version (e.g. nodejs=20.x.y-1nodesource1) if you need
# fully reproducible images.
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && chmod 644 /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# The install script uses `sudo apt-get …`; inside Docker we run as root
# so make sudo a transparent pass-through.
RUN printf '#!/bin/sh\nexec "$@"\n' > /usr/local/bin/sudo && chmod +x /usr/local/bin/sudo

# ── Project files ──────────────────────────────────────────────────────────
WORKDIR /app

# Copy the project so the image is self-contained when no volume is
# mounted.  Users may overlay this directory at runtime (see VOLUME below).
COPY . .

# ── Dependency installation via the unified install script ─────────────────
# System binaries are already present above; the script detects this and
# skips apt, running only the pip, Playwright browser, and npm steps.
# Playwright installs its Chromium binary to /root/.cache/ms-playwright —
# outside /app, so it survives volume mounts.
RUN bash scripts/install-dependencies.sh

# Install Playwright's required OS-level shared libraries for Chromium.
# The install script downloads the browser binary but not the system libs;
# playwright install-deps adds them so the browser can actually launch.
# Clean apt lists in the same layer to avoid bloating the image.
RUN python -m playwright install-deps chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Pi Coding Agent ───────────────────────────────────────────────────────
# Install the Pi Coding Agent CLI globally so `pi` is available on PATH.
# Uses @earendil-works/pi-coding-agent (the current package, not the legacy
# unscoped pi-coding-agent which is deprecated).
RUN npm install -g @earendil-works/pi-coding-agent

# ── Volume ────────────────────────────────────────────────────────────────
# /app is the working directory read by main.py.  Mount your local clone
# here to develop without rebuilding:
#   docker run -it -v /path/to/AutoGUI:/app autogui
#
# Pip packages (installed to /usr/local/lib/python3.12/), system tools
# (/usr/bin/), and the Playwright browser (/root/.cache/ms-playwright/)
# all live outside /app and remain available after a mount.
VOLUME ["/app"]

# ── Entry point ────────────────────────────────────────────────────────────
CMD ["bash"]
