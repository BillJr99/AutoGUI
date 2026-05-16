/**
 * oso_text.ts — Helpers for assembling depth-limited text observation
 * bundles from OS Screen Observer responses.
 *
 * Port of /home/user/AutoGUI/oso_text.py.  Used by tools.ts for the
 * post-action injection on successful state-changing tool calls and by
 * emitRecoveryProbe() for the failure path.
 */

import type { ScreenObserverClient } from "./screen_observer_client.js";

interface TreeNode {
  name?: string;
  role?: string;
  bounds?: { x?: number; y?: number; width?: number; height?: number };
  children?: TreeNode[];
}

export interface TextBundle {
  description: string;
  sketch: string;
  treeText: string;
  depthUsed: number;
  truncated: boolean;
  scope: "active_window" | "screen";
}

export interface TextBundleOptions {
  windowIndex?: number;
  includeSketch: boolean;
  includeTree: boolean;
  treeStartDepth: number;
  treeMinDepth: number;
  treeMaxChars: number;
  maxChars: number;
}

/** Serialise an OSO tree as an indented role-name listing, depth-limited. */
export function flattenTree(node: TreeNode | undefined | null, depthLimit: number): string {
  if (!node) return "";
  const lines: string[] = [];
  const walk = (n: TreeNode, depth: number): void => {
    if (depth > depthLimit) return;
    const indent = "  ".repeat(depth);
    const role = (n.role ?? "").trim() || "?";
    const name = (n.name ?? "").trim();
    const namePart = name ? `  "${name}"` : "";
    const b = n.bounds;
    const boundsPart = b && b.width !== undefined
      ? `  [${Math.trunc(b.x ?? 0)},${Math.trunc(b.y ?? 0)} ${Math.trunc(b.width ?? 0)}x${Math.trunc(b.height ?? 0)}]`
      : "";
    lines.push(`${indent}${role}${namePart}${boundsPart}`);
    for (const child of n.children ?? []) walk(child, depth + 1);
  };
  walk(node, 0);
  return lines.join("\n");
}

/** Decrement depth until serialised tree fits under maxChars; hard-truncate
 *  at minDepth if it still overflows. */
export function trimTree(
  tree: TreeNode | undefined | null,
  opts: { startDepth: number; minDepth: number; maxChars: number },
): { text: string; depthUsed: number; truncated: boolean } {
  if (!tree) return { text: "", depthUsed: 0, truncated: false };
  let depth = Math.max(opts.minDepth, opts.startDepth);
  let text = "";
  while (depth >= opts.minDepth) {
    text = flattenTree(tree, depth);
    if (text.length <= opts.maxChars) {
      return { text, depthUsed: depth, truncated: false };
    }
    depth -= 1;
  }
  if (text.length > opts.maxChars) {
    text = `${text.slice(0, opts.maxChars)}\n… (truncated at ${opts.maxChars} chars)`;
    return { text, depthUsed: opts.minDepth, truncated: true };
  }
  return { text, depthUsed: opts.minDepth, truncated: false };
}

/** Fetch description/sketch/structure in parallel; assemble a length-bounded bundle. */
export async function buildOsoTextBundle(
  oso: ScreenObserverClient,
  opts: TextBundleOptions,
): Promise<TextBundle | null> {
  if (!oso || !oso.enabled) return null;
  const sketchP = opts.includeSketch ? oso.getSketch(opts.windowIndex) : Promise.resolve(null);
  const treeP = opts.includeTree ? oso.getStructure(opts.windowIndex) : Promise.resolve(null);
  const descP = oso.getDescription(opts.windowIndex);
  const [description, sketch, structure] = await Promise.all([descP, sketchP, treeP]);

  if (description === null && sketch === null && structure === null) return null;

  let descText = "";
  if (description) {
    descText = String(
      description.description ?? description.text ?? description.summary ?? "",
    );
  }
  let sketchText = "";
  if (sketch) {
    sketchText = String((sketch as { sketch?: string; text?: string }).sketch ?? (sketch as { text?: string }).text ?? "");
  }

  let treeText = "";
  let depthUsed = 0;
  let treeTruncated = false;
  if (structure) {
    const tree = (structure as { tree?: TreeNode }).tree ?? (structure as TreeNode);
    const trimmed = trimTree(tree, {
      startDepth: opts.treeStartDepth,
      minDepth: opts.treeMinDepth,
      maxChars: opts.treeMaxChars,
    });
    treeText = trimmed.text;
    depthUsed = trimmed.depthUsed;
    treeTruncated = trimmed.truncated;
  }

  const total = descText.length + sketchText.length + treeText.length;
  if (total > opts.maxChars) {
    let over = total - opts.maxChars;
    if (treeText && over > 0) {
      const cut = Math.min(treeText.length, over);
      treeText = treeText.slice(0, treeText.length - cut);
      if (cut > 0) {
        treeText += "\n… (truncated)";
        treeTruncated = true;
      }
      over -= cut;
    }
    if (over > 0 && sketchText) {
      const cut = Math.min(sketchText.length, over);
      sketchText = sketchText.slice(0, sketchText.length - cut);
      over -= cut;
    }
    if (over > 0 && descText) {
      descText = descText.slice(0, Math.max(0, descText.length - over));
    }
  }

  return {
    description: descText,
    sketch: sketchText,
    treeText,
    depthUsed,
    truncated: treeTruncated,
    scope: opts.windowIndex !== undefined ? "active_window" : "screen",
  };
}
