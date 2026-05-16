"""
oso_text.py — Shared helpers for building text observation bundles from
OS Screen Observer responses.

Used by backends/base.py (post-action injection) and agent.py (recovery
probe).  Keeping the depth-trimming + serialisation logic in one place
matches the same surface in pi-extension/src/oso_text.ts.
"""

from __future__ import annotations

from typing import Any


def flatten_tree(node: dict | None, depth_limit: int) -> str:
    """Serialise an OSO structure tree as an indented role-name listing.

    One element per line, truncated at depth_limit.  Compact format chosen
    over JSON to keep text-LLM context cheap and human-scannable.
    """
    if not node:
        return ""
    lines: list[str] = []

    def walk(n: dict, depth: int) -> None:
        if depth > depth_limit:
            return
        indent = "  " * depth
        role = (n.get("role") or "").strip() or "?"
        name = (n.get("name") or "").strip()
        bounds = n.get("bounds") or {}
        name_part = f'  "{name}"' if name else ""
        if bounds and bounds.get("width") is not None:
            bounds_part = (
                f"  [{int(bounds.get('x', 0))},{int(bounds.get('y', 0))} "
                f"{int(bounds.get('width', 0))}x{int(bounds.get('height', 0))}]"
            )
        else:
            bounds_part = ""
        lines.append(f"{indent}{role}{name_part}{bounds_part}")
        for child in n.get("children") or []:
            walk(child, depth + 1)

    walk(node, 0)
    return "\n".join(lines)


def trim_tree(
    tree: dict | None,
    *,
    start_depth: int,
    min_depth: int,
    max_chars: int,
) -> dict:
    """Serialise tree, lowering depth until output fits under max_chars.

    Returns {text, depth_used, truncated}.  Hard-truncates at min_depth
    when even that exceeds the budget.
    """
    if not tree:
        return {"text": "", "depth_used": 0, "truncated": False}
    depth = max(min_depth, start_depth)
    text = ""
    while depth >= min_depth:
        text = flatten_tree(tree, depth)
        if len(text) <= max_chars:
            return {"text": text, "depth_used": depth, "truncated": False}
        depth -= 1
    # min_depth still too big — hard-truncate.
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n… (truncated at {max_chars} chars)"
        return {"text": text, "depth_used": min_depth, "truncated": True}
    return {"text": text, "depth_used": min_depth, "truncated": False}


async def build_text_bundle(
    oso: Any,
    *,
    window_index: int | None,
    include_sketch: bool,
    include_tree: bool,
    tree_start_depth: int,
    tree_min_depth: int,
    tree_max_chars: int,
    max_chars: int,
) -> dict | None:
    """Fetch description / sketch / structure in parallel and assemble a bundle.

    Returns None when OSO is unreachable; otherwise a dict with description,
    sketch, tree_text, depth_used, truncated, scope.  Total text length is
    capped to max_chars (description + sketch + tree combined).
    """
    if oso is None or not getattr(oso, "enabled", False):
        return None
    import asyncio

    sketch_task = (
        oso.get_sketch(window_index=window_index) if include_sketch else _none()
    )
    tree_task = (
        oso.get_structure(window_index=window_index) if include_tree else _none()
    )
    desc_task = oso.get_description(window_index=window_index)

    description, sketch, structure = await asyncio.gather(
        desc_task, sketch_task, tree_task
    )

    if description is None and sketch is None and structure is None:
        return None

    desc_text = ""
    if description:
        # OSO description payload varies — prefer a 'description' field, else
        # join known prose-like keys.  Keep it small.
        desc_text = (
            description.get("description")
            or description.get("text")
            or description.get("summary")
            or ""
        )
    sketch_text = ""
    if sketch:
        sketch_text = sketch.get("sketch") or sketch.get("text") or ""

    tree_text = ""
    depth_used = 0
    tree_truncated = False
    if structure:
        tree = structure.get("tree") or structure
        trimmed = trim_tree(
            tree,
            start_depth=tree_start_depth,
            min_depth=tree_min_depth,
            max_chars=tree_max_chars,
        )
        tree_text = trimmed["text"]
        depth_used = trimmed["depth_used"]
        tree_truncated = trimmed["truncated"]

    # Combined cap — drop tree first, then sketch, then description tail.
    total = len(desc_text) + len(sketch_text) + len(tree_text)
    if total > max_chars:
        over = total - max_chars
        if tree_text and over > 0:
            cut = min(len(tree_text), over)
            tree_text = tree_text[: len(tree_text) - cut]
            if cut > 0:
                tree_text += "\n… (truncated)"
                tree_truncated = True
            over -= cut
        if over > 0 and sketch_text:
            cut = min(len(sketch_text), over)
            sketch_text = sketch_text[: len(sketch_text) - cut]
            over -= cut
        if over > 0 and desc_text:
            desc_text = desc_text[: max(0, len(desc_text) - over)]

    scope = "active_window" if window_index is not None else "screen"
    return {
        "description": desc_text,
        "sketch": sketch_text,
        "tree_text": tree_text,
        "depth_used": depth_used,
        "truncated": tree_truncated,
        "scope": scope,
    }


async def _none():
    return None
