#!/usr/bin/env python3
"""
atspi_find.py — Tiny AT-SPI element finder used by the pi-extension's
Linux backend.  Usage:

    atspi_find.py --name <substring> [--role <substring>]
                  [--window <substring>] [--index <n>]

Prints JSON of the form:
    {"name": "...", "controlType": "...", "rect": {"x":..,"y":..,"width":..,"height":..}}
or {"error": "..."} on failure.

Kept deliberately minimal so the Node side just needs to spawn it and
parse one line of stdout.
"""

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="")
    ap.add_argument("--role", default="")
    ap.add_argument("--window", default="")
    ap.add_argument("--index", type=int, default=0)
    args = ap.parse_args()

    try:
        import pyatspi  # type: ignore
    except ImportError:
        print(json.dumps({
            "error": "pyatspi not installed. Install with: sudo apt install python3-pyatspi gir1.2-atspi-2.0",
        }))
        return 1

    target_role = args.role.lower().strip() or None
    target_name = args.name.lower().strip() or None
    wanted_window = args.window.lower().strip() or None

    if not target_role and not target_name:
        print(json.dumps({"error": "Provide --name or --role"}))
        return 1

    matches = []

    def node_to_dict(node):
        try:
            role_name = node.getRoleName()
        except Exception:
            role_name = ""
        try:
            n = node.name or ""
        except Exception:
            n = ""
        rect = None
        try:
            comp = node.queryComponent()
            extents = comp.getExtents(pyatspi.DESKTOP_COORDS)
            rect = {
                "x": int(extents.x),
                "y": int(extents.y),
                "width": int(extents.width),
                "height": int(extents.height),
            }
        except Exception:
            pass
        return {"name": n, "controlType": role_name, "rect": rect}

    def recurse(node):
        if len(matches) > 50:
            return
        info = node_to_dict(node)
        ok_name = (target_name is None) or (target_name in info["name"].lower())
        ok_role = (target_role is None) or (target_role in info["controlType"].lower())
        if info["rect"] and ok_name and ok_role and (info["name"] or info["controlType"]):
            matches.append(info)
        try:
            for child in node:
                recurse(child)
        except Exception:
            pass

    try:
        desktop = pyatspi.Registry.getDesktop(0)
    except Exception as e:
        print(json.dumps({"error": f"AT-SPI desktop unavailable: {e}"}))
        return 1

    try:
        for app in desktop:
            try:
                for top in app:
                    try:
                        top_name = (top.name or "").lower()
                    except Exception:
                        top_name = ""
                    if wanted_window and wanted_window not in top_name:
                        continue
                    recurse(top)
            except Exception:
                continue
    except Exception as e:
        print(json.dumps({"error": f"AT-SPI walk failed: {e}"}))
        return 1

    if not matches:
        print(json.dumps({"error": "No matching element found via AT-SPI."}))
        return 1

    idx = max(0, min(args.index, len(matches) - 1))
    print(json.dumps(matches[idx]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
