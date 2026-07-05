"""
config_load.py — configuration loading, CLI overrides and startup validation.

Split out of main.py: load_config / apply_cli_overrides plus the interactive
startup validation (API key prompt, model selection, opportunistic OS Screen
Observer probe).  main.py re-exports these so ``from main import …`` and
monkeypatching via the main module keep working.
"""

import argparse
import json
from pathlib import Path

from client import OpenWebUIClient

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    """
    Load and return the JSON configuration file.

    If the file does not exist, tries to bootstrap it from config.json.example
    in the same directory.  Raises FileNotFoundError only when neither file
    can be found.
    """
    import shutil

    p = Path(path)
    if not p.exists():
        example = p.parent / "config.json.example"
        if example.exists():
            shutil.copy(example, p)
            print(f"Created {path} from config.json.example — update it with your API key.")
        else:
            raise FileNotFoundError(
                f"Configuration file not found: {path}\n"
                "Create config.json with your OpenWebUI base_url and api_key, "
                "or copy config.json.example as a starting point."
            )
    with p.open() as f:
        cfg = json.load(f)
    return cfg


def apply_cli_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    """
    Apply command-line flag overrides to the loaded configuration dict.
    Mutates cfg in place and returns it.
    """
    if args.model:
        cfg.setdefault("openwebui", {})["model"] = args.model
    if args.no_desktop:
        cfg.setdefault("tools", {})["allowed_desktop"] = False
    if args.no_shell:
        cfg.setdefault("tools", {})["allowed_shell"] = False
    return cfg


# ---------------------------------------------------------------------------
# Startup validation: API key + model selection
# ---------------------------------------------------------------------------

_PLACEHOLDER_KEYS = {
    "",
    "sk-your-openwebui-api-key-here",
    "sk-your-key-from-openwebui-settings",
}

_SEP = "─" * 62


def _save_config_fields(config_path: str, section: str, fields: dict) -> bool:
    """
    Deep-merge *fields* into cfg[section] inside config.json.
    Returns True on success, False on any write error.
    """
    try:
        p = Path(config_path)
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        existing.setdefault(section, {}).update(fields)
        p.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        print(f"  Warning: could not write {config_path}: {e}")
        return False


async def _probe_screen_observer_and_offer_enable(cfg: dict, config_path: str) -> None:
    """Probe OS Screen Observer at startup; offer to enable when running but unset.

    Runs after the OpenWebUI handshake, before the TUI launches.  When OSO is
    reachable at the configured base_url but ``screen_observer.enabled`` is
    false (or the section is missing), the user is asked once whether to
    enable it; on yes, ``cfg`` is mutated in-memory AND config.json is
    persisted so subsequent runs auto-attach.  When OSO is already enabled,
    or when ``disabled: true`` is explicitly set, the probe is skipped.

    Silent on probe failure — this is purely opportunistic.  All paths are
    best-effort; any exception is swallowed so a flaky OSO can't block the
    main TUI startup.
    """
    try:
        oso_cfg = cfg.get("screen_observer", {}) or {}
        # Explicit opt-out: respect it.
        if oso_cfg.get("disabled") is True:
            return
        # Already enabled — nothing to ask.
        if oso_cfg.get("enabled") is True:
            return
        # Probe via a one-shot client with enabled=True so /healthz fires.
        from screen_observer_client import ScreenObserverClient
        probe_cfg = {
            "enabled": True,
            "base_url": oso_cfg.get("base_url", "http://127.0.0.1:5001"),
            "timeout_seconds": float(oso_cfg.get("timeout_seconds", 1.5)),
        }
        client = ScreenObserverClient(probe_cfg)
        reachable = await client.is_available()
        if not reachable:
            return
        caps = client.oso_capabilities
        print(_SEP)
        print(f"OS Screen Observer detected at {probe_cfg['base_url']}.")
        print("It can provide accessibility-tree perception, faster window")
        print("activation, and post-action observation diffs to AutoGUI.")
        cap_summary = ", ".join(k for k, v in caps.items() if v) or "basic"
        print(f"Capabilities: {cap_summary}")
        print(_SEP)
        try:
            answer = input("  Enable Screen Observer integration? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer in ("", "y", "yes"):
            cfg.setdefault("screen_observer", {})
            cfg["screen_observer"]["enabled"] = True
            cfg["screen_observer"].setdefault("base_url", probe_cfg["base_url"])
            cfg["screen_observer"].setdefault("timeout_seconds", probe_cfg["timeout_seconds"])
            if _save_config_fields(config_path, "screen_observer", {
                "enabled": True,
                "base_url": probe_cfg["base_url"],
                "timeout_seconds": probe_cfg["timeout_seconds"],
            }):
                print(f"  Saved screen_observer.enabled=true to {config_path}\n")
            else:
                print("  Enabled for this session (config.json not updated).\n")
        else:
            print("  Skipped — re-run AutoGUI to be asked again, or toggle from Ctrl+P.\n")
    except Exception:
        # Best-effort probe — never let it block startup.
        return


def _prompt_save(label: str, config_path: str, section: str, fields: dict) -> None:
    """Ask the user whether to persist *fields* to config.json."""
    try:
        answer = input(f"  Save {label} to config.json? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer == "y":
        if _save_config_fields(config_path, section, fields):
            print(f"  Saved to {config_path}")


async def validate_and_configure(cfg: dict, config_path: str) -> None:
    """
    Interactive startup validation — runs before components are built.

    Steps
    -----
    1. If the API key is unset / a placeholder, prompt for one.
    2. Connect to OpenWebUI and fetch the model list.
       - On HTTP 401: re-prompt for the key (up to 3 attempts total).
       - On connection error: warn and offer to continue anyway.
       Save-to-config is only offered after the key is confirmed working.
    3. If the configured model is absent from the server's list, show a
       numbered menu.  Save-to-config is offered after selection.
    """
    import getpass

    ow_cfg = cfg.setdefault("openwebui", {})
    original_key = ow_cfg.get("api_key", "").strip()

    # ── 1. Prompt for a key if none is configured ────────────────────────
    if original_key in _PLACEHOLDER_KEYS:
        print(_SEP)
        print("No API key configured — OpenWebUI requires one.")
        print("Find yours: OpenWebUI → Settings → Account → API Keys")
        print(_SEP)
        try:
            key = getpass.getpass("  API key (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipping — requests will likely fail without a key.")
            return
        if not key:
            print("  No key entered — requests will likely fail.\n")
        else:
            ow_cfg["api_key"] = key
            # Don't offer to save yet — we'll do that only after it works.
            print()

    # ── 2. Connect and fetch model list (up to 3 key attempts) ────────────
    original_base_url = ow_cfg.get("base_url", "http://localhost:3000")
    base_url = original_base_url
    models: list[str] = []
    key_changed = ow_cfg.get("api_key", "").strip() != original_key
    url_changed = False

    for attempt in range(3):
        tmp = OpenWebUIClient(
            base_url=base_url,
            api_key=ow_cfg.get("api_key", ""),
            model=ow_cfg.get("model", ""),
            api_path=ow_cfg.get("api_path") or "/api/chat/completions",
            timeout_seconds=10,
        )
        print(f"Connecting to {base_url} … ", end="", flush=True)
        try:
            models = await tmp.fetch_models()
            n = len(models)
            print(f"OK  ({n} model{'s' if n != 1 else ''} available)\n")

            # Values confirmed working — offer to save anything that changed.
            saves: dict[str, str] = {}
            if key_changed:
                saves["api_key"] = ow_cfg["api_key"]
            if url_changed:
                saves["base_url"] = base_url
            if saves:
                labels = " and ".join(
                    k.replace("_", " ") for k in saves
                )
                _prompt_save(labels, config_path, "openwebui", saves)
                print()
            break

        except PermissionError:
            print("FAILED\n")
            print("  The API key was rejected (HTTP 401).")
            print("  Check your key: OpenWebUI → Settings → Account → API Keys.")
            if attempt == 2:
                print("  Too many failed attempts — continuing without a verified key.\n")
                break
            try:
                key = getpass.getpass("  New API key (or Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not key:
                print("  Skipping key retry.\n")
                break
            ow_cfg["api_key"] = key
            key_changed = True
            print()

        except ConnectionError:
            print("FAILED\n")
            print(f"  Could not reach OpenWebUI at {base_url}.")
            print("  Check that the server is running and the address is correct.")
            if attempt == 2:
                print("  Continuing without a verified connection.\n")
                break
            try:
                new_url = input("  New base URL (or Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not new_url:
                print("  Continuing without a verified connection.\n")
                break
            base_url = new_url.rstrip("/")
            ow_cfg["base_url"] = base_url
            url_changed = True
            print()

        except Exception as e:
            print("FAILED\n")
            print(f"  Unexpected error: {e}")
            print("  Continuing — tool calls may fail.\n")
            break

    # ── 3. Model selection ─────────────────────────────────────────────
    if not models:
        return  # couldn't reach the server; nothing to validate against

    configured_model = ow_cfg.get("model", "")
    if configured_model in models:
        return  # configured model is available — nothing to do

    print()
    if configured_model:
        print(f"  Configured model '{configured_model}' is not in the server's model list.")
    else:
        print("  No model is configured.")

    print(f"\n  Available models ({len(models)}):\n")
    col_w = len(str(len(models)))
    for i, m in enumerate(models, 1):
        print(f"    {i:{col_w}}. {m}")
    print()

    while True:
        keep_hint = f", or Enter to keep '{configured_model}'" if configured_model else ""
        try:
            choice = input(f"  Select [1–{len(models)}]{keep_hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not choice and configured_model:
            print(f"  Keeping '{configured_model}'.\n")
            break
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                selected = models[idx]
                ow_cfg["model"] = selected
                print(f"\n  Model set to: {selected}\n")
                _prompt_save(f"model '{selected}'", config_path, "openwebui",
                             {"model": selected})
                print()
                break
            print(f"  Enter a number between 1 and {len(models)}.")
        except ValueError:
            print("  Please enter a number.")

    # Opportunistic OS Screen Observer probe — runs once after the model
    # handshake.  Silent when OSO isn't running or is already enabled;
    # prompts only when reachable but not yet configured.
    await _probe_screen_observer_and_offer_enable(cfg, config_path)
