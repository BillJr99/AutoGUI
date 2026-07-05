"""
tools.shell_fs — shell and filesystem tool implementations + registration.

Platform-agnostic implementations (shell_run via subprocess; fs_read /
fs_write / fs_list via pathlib), the destructive-command guard, the
LLM-argument coercion helpers, and the mixin that registers the shell and
filesystem tools on the ToolRegistry.  Split out of the original tools.py.
"""

import asyncio
import json
import logging
import re
import traceback
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Destructive command guard
# ---------------------------------------------------------------------------

_DESTRUCTIVE_PATTERNS = [
    r"\brm\s+-[rRf]",
    r"\brmdir\b",
    r"\bformat\b",
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r"\bshred\b",
    r"\btruncate\b",
    r"DROP\s+TABLE",
    r"DROP\s+DATABASE",
]


def _is_destructive(command: str) -> bool:
    for pat in _DESTRUCTIVE_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return True
    return False


def _coerce_path(path, default: str = "") -> str:
    """
    Normalize a path/string argument that the LLM may send as a dict instead of a string.
    Returns a plain string suitable for Path(), subprocess args, or cwd.
    """
    if path is None:
        return default
    if isinstance(path, (str, bytes)):
        return path.decode() if isinstance(path, bytes) else path
    if isinstance(path, dict):
        # Try common key names the LLM uses when it hallucinates a dict
        for key in (
            "path", "file", "filename",
            "dir", "directory",
            "name", "application", "app", "executable", "cmd", "command",
            "value", "text", "content",
        ):
            if key in path:
                return str(path[key])
        # Single-value dict: take the only value
        if len(path) == 1:
            return str(next(iter(path.values())))
        return default
    return str(path)


def _coerce_args(args) -> list[str]:
    """
    Normalize the `args` parameter that the LLM may send as a JSON string
    (e.g. "[]" or "[\"--flag\"]") instead of an actual list.
    Always returns a list of strings.
    """
    if args is None:
        return []
    if isinstance(args, list):
        return [str(a) for a in args]
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [str(a) for a in parsed]
            return [str(parsed)]
        except (json.JSONDecodeError, ValueError):
            return [stripped]
    return [str(a) for a in args] if hasattr(args, "__iter__") else [str(args)]


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------

async def shell_run(
    command: str,
    working_dir=None,
    timeout: int = 30,
    confirm_destructive: bool = True,
) -> dict:
    """Execute a shell command; return stdout, stderr, exit_code, timed_out."""
    command = _coerce_path(command) if not isinstance(command, str) else command
    # LLM sometimes sends working_dir as a dict; normalize to string or None.
    if working_dir is not None:
        working_dir = _coerce_path(working_dir) or None
    if confirm_destructive and _is_destructive(command):
        return {
            "stdout": "",
            "stderr": (
                f"SAFETY BLOCK: '{command}' matches a destructive pattern. "
                "Confirm with the user before running."
            ),
            "exit_code": -1,
            "timed_out": False,
        }

    logger.info("[tools.py:shell_run] cmd=%r cwd=%s", command, working_dir)

    import platform as _platform
    if _platform.system() == "Windows":
        args = ["cmd", "/C", command]
    else:
        args = ["/bin/sh", "-c", command]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            # Reap the killed process so it doesn't linger as a zombie.
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            stdout_b, stderr_b = b"", b""
            timed_out = True

        return {
            "stdout": stdout_b.decode("utf-8", errors="replace").strip(),
            "stderr": stderr_b.decode("utf-8", errors="replace").strip(),
            "exit_code": proc.returncode if not timed_out else -1,
            "timed_out": timed_out,
        }
    except Exception as e:
        print(f"[tools.py:shell_run] {e}")
        traceback.print_exc()
        return {"stdout": "", "stderr": str(e), "exit_code": -1, "timed_out": False}


# ---------------------------------------------------------------------------
# Filesystem tools
# ---------------------------------------------------------------------------

async def fs_read(path: str, max_bytes: int = 65536) -> dict:
    path = _coerce_path(path)
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return {"error": f"Path does not exist: {path}"}
        if p.is_dir():
            return {"error": f"Path is a directory; use fs_list instead: {path}"}
        # Report the true file size from stat(), but only read up to
        # max_bytes + 1 bytes: enough to decide truncation without pulling a
        # potentially huge file into memory.
        size_bytes = p.stat().st_size
        with p.open("rb") as fh:
            content = fh.read(max_bytes + 1)
        truncated = len(content) > max_bytes
        return {
            "content": content[:max_bytes].decode("utf-8", errors="replace"),
            "truncated": truncated,
            "size_bytes": size_bytes,
        }
    except Exception as e:
        print(f"[tools.py:fs_read] {e}")
        traceback.print_exc()
        return {"error": str(e)}


async def fs_write(
    path: str,
    content: str,
    mode: str = "w",
    snapshot_dir: str = "",
) -> dict:
    """
    Write content to a file. When overwriting an existing file and
    snapshot_dir is non-empty, copy the original aside first so the
    write is recoverable.
    """
    path = _coerce_path(path)
    content = content if isinstance(content, str) else str(content)
    try:
        p = Path(path).expanduser()
        snapshot_path: str | None = None
        if mode == "w" and snapshot_dir and p.exists() and p.is_file():
            try:
                import shutil
                snap_dir = Path(snapshot_dir).expanduser()
                snap_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(p))
                snap = snap_dir / f"{ts}__{slug.strip('_')[:120]}"
                shutil.copy2(p, snap)
                snapshot_path = str(snap)
            except Exception as e:
                logger.warning("[fs_write] Snapshot failed for %s: %s", p, e)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open(mode, encoding="utf-8") as f:
            f.write(content)
        result = {"success": True, "path": str(p), "bytes_written": len(content.encode())}
        if snapshot_path:
            result["snapshot"] = snapshot_path
        return result
    except Exception as e:
        print(f"[tools.py:fs_write] {e}")
        traceback.print_exc()
        return {"error": str(e)}


async def fs_list(path: str, pattern: str = "*", max_entries: int = 200) -> dict:
    path = _coerce_path(path)
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return {"error": f"Path does not exist: {path}"}
        entries = []
        for item in sorted(p.glob(pattern))[:max_entries]:
            stat = item.stat()
            entries.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return {"entries": entries, "count": len(entries), "path": str(p)}
    except Exception as e:
        print(f"[tools.py:fs_list] {e}")
        traceback.print_exc()
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class ShellFsToolsMixin:
    """Registers the shell and filesystem tools on the ToolRegistry."""

    def _build_shell_fs_tools(self):
        shell_ok = self._tools_cfg.get("allowed_shell", True)
        fs_ok = self._tools_cfg.get("allowed_filesystem", True)
        confirm = self._agent_cfg.get("confirm_destructive", True)
        shell_timeout = self._tools_cfg.get("shell_timeout_seconds", 30)

        # ── Shell ──────────────────────────────────────────────────────────────
        if shell_ok:
            self._register(
                {
                    "type": "function",
                    "function": {
                        "name": "shell_run",
                        "description": (
                            "Execute a shell command and return stdout, stderr, and exit code. "
                            "Destructive patterns are blocked unless the user confirms."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string"},
                                "working_dir": {"type": "string"},
                            },
                            "required": ["command"],
                        },
                    },
                },
                # Parameter names must exactly match the schema keys so that
                # fn(**arguments) binds correctly when the LLM calls the tool.
                lambda command, working_dir=None: shell_run(
                    command, working_dir=working_dir,
                    timeout=shell_timeout, confirm_destructive=confirm,
                ),
            )

        # ── Filesystem ─────────────────────────────────────────────────────────
        if fs_ok:
            self._register(
                {"type": "function", "function": {
                    "name": "fs_read",
                    "description": "Read file contents. Returns text and truncation flag.",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                }},
                fs_read,
            )
            snapshot_dir = self._safety_cfg.get(
                "fs_write_snapshot_dir", ""
            )  # empty string = snapshots disabled
            self._register(
                {"type": "function", "function": {
                    "name": "fs_write",
                    "description": "Write or append content to a file.",
                    "parameters": {"type": "object", "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "mode": {"type": "string", "enum": ["w", "a"]},
                    }, "required": ["path", "content"]},
                }},
                lambda path, content, mode="w": fs_write(
                    path, content, mode=mode, snapshot_dir=snapshot_dir,
                ),
            )
            self._register(
                {"type": "function", "function": {
                    "name": "fs_list",
                    "description": "List files and directories. Supports glob patterns.",
                    "parameters": {"type": "object", "properties": {
                        "path": {"type": "string"},
                        "pattern": {"type": "string"},
                    }, "required": ["path"]},
                }},
                fs_list,
            )
