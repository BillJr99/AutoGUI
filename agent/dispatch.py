"""
agent.dispatch — tool registration and tool-result summarization.

Skill/meta tool registration plus the result summarization helpers, split
out of the original agent.py.  ``Agent`` mixes this in; the methods run
against the shared Agent instance state declared below for type checking.
"""

import json
import logging
import re
from typing import TYPE_CHECKING

from app_memory import _normalize_app
from controller import StepStatus

if TYPE_CHECKING:
    from app_memory import AppMemory
    from artifacts import ArtifactStore
    from progress import ProgressStore
    from skills import SkillStore
    from subagent import Subagent
    from tools import ToolRegistry

logger = logging.getLogger(__name__)


class DispatchMixin:
    """Tool registration + result summarization methods of ``Agent``."""

    if TYPE_CHECKING:
        # Typing-only declarations of Agent state used by these methods.
        _artifacts: "ArtifactStore | None"
        _memory: "AppMemory | None"
        _memory_enabled: bool
        _progress: "ProgressStore | None"
        _registry: "ToolRegistry"
        _session_steps: list[dict]
        _skill_store: "SkillStore | None"
        _skills_enabled: bool
        _subagent: "Subagent | None"

    # ------------------------------------------------------------------
    # Skill tool registration
    # ------------------------------------------------------------------

    def _register_skill_tools(self):
        """Add skill_save / skill_list / skill_run to the registry."""
        store = self._skill_store
        registry = self._registry

        async def _skill_save(name: str, keywords=None, app: str = "") -> dict:
            if not self._session_steps:
                return {"error": "No successful steps in this session yet to save."}
            kw = keywords or []
            if isinstance(kw, str):
                kw = [k.strip() for k in re.split(r"[,;\s]+", kw) if k.strip()]
            try:
                skill = store.save(
                    name=str(name),
                    keywords=list(kw),
                    app=str(app or ""),
                    steps=list(self._session_steps),
                )
                return {"success": True, "name": skill["name"], "step_count": len(skill["steps"])}
            except Exception as e:
                return {"error": str(e)}

        async def _skill_list(query: str = "", limit: int = 5) -> dict:
            try:
                results = store.search(str(query) if query else "", limit=int(limit) if limit else 5)
                return {
                    "skills": [
                        {
                            "name": s.get("name"),
                            "app": s.get("app", ""),
                            "keywords": s.get("keywords", []),
                            "step_count": len(s.get("steps", [])),
                            "success_count": s.get("success_count", 0),
                        }
                        for s in results
                    ],
                    "count": len(results),
                }
            except Exception as e:
                return {"error": str(e)}

        async def _skill_run(name: str) -> dict:
            from skills import normalize_skill_steps
            skill = store.get(str(name))
            if not skill:
                return {"error": f"No skill named {name!r}"}
            executed: list[dict] = []
            for step in normalize_skill_steps(skill.get("steps", [])):
                tool = step.get("tool")
                args = step.get("args", {}) or {}
                if not tool:
                    continue
                result_json = await registry.dispatch(tool, args)
                try:
                    result = json.loads(result_json)
                except json.JSONDecodeError:
                    result = {"raw": result_json[:120]}
                executed.append({"tool": tool, "args": args, "ok": "error" not in result})
                if "error" in result:
                    return {
                        "skill": name,
                        "executed": executed,
                        "stopped_at": tool,
                        "error": result["error"],
                    }
            try:
                store.increment_success(str(name))
            except Exception as e:
                logger.debug("[agent] skill success-counter update failed for %r: %s", name, e)
            return {"skill": name, "executed": executed, "step_count": len(executed), "success": True}

        # skill_save creates new on-disk records, so it is only registered
        # when the user has explicitly opted in via skills_enabled=true.
        # skill_list and skill_run are read-only and always registered when
        # a SkillStore exists, so existing libraries remain usable.
        if self._skills_enabled:
            registry.add_tool(
                {"type": "function", "function": {
                    "name": "skill_save",
                    "description": (
                    "Save the sequence of tool calls completed in this session as a "
                    "named, replayable skill. Provide keywords describing when this "
                    "skill applies (e.g. 'open weather forecast in browser'). "
                    "Call this only after the task has succeeded — earlier failed "
                    "attempts in the same session are not included. "
                    "Saved skills can later be invoked by skill_run or replayed "
                    "outside the agent via replay.py. "
                    "NOTE: pixel-coordinate desktop_click steps used for window focus "
                    "are automatically dropped on replay (coordinates change between runs). "
                    "Prefer desktop_activate_window for focus — it is position-independent."
                ),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Short unique identifier for the skill."},
                    "keywords": {"type": "array", "items": {"type": "string"},
                                 "description": "Words/phrases that describe when to use this skill."},
                    "app": {"type": "string",
                            "description": "Primary app or context this skill targets (optional)."},
                }, "required": ["name"]},
            }},
            _skill_save,
        )
        registry.add_tool(
            {"type": "function", "function": {
                "name": "skill_list",
                "description": (
                    "List saved skills, optionally filtered by a search query. "
                    "Use this at the start of a task to check whether a known "
                    "procedure already exists for what the user asked."
                ),
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "Optional keyword filter."},
                    "limit": {"type": "integer", "description": "Max skills to return (default 5)."},
                }},
            }},
            _skill_list,
        )
        registry.add_tool(
            {"type": "function", "function": {
                "name": "skill_run",
                "description": (
                    "Replay every step of a previously saved skill in order. "
                    "Stops at the first failing step. Use only when the current "
                    "screen state and target app match the conditions under which "
                    "the skill was originally recorded."
                ),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string"},
                }, "required": ["name"]},
            }},
            _skill_run,
        )

    # ------------------------------------------------------------------
    # Meta-tools: artifact, plan, checkpoint, ask
    # ------------------------------------------------------------------

    def _register_meta_tools(self):
        """Add get_artifact / list_artifacts / plan_get / plan_update_step /
        checkpoint / ask_subagent to the registry.  Always registered when
        the supporting stores exist, so the model can opt to use them."""
        registry = self._registry
        agent = self

        if self._artifacts is not None:
            async def _get_artifact(id: str) -> dict:
                aid = str(id or "")
                if not aid.startswith("artifact://"):
                    aid = "artifact://" + aid.lstrip("/")
                art = agent._artifacts.get(aid)
                if art is None:
                    return {"error": f"unknown artifact id: {id}"}
                body = agent._artifacts.get_body(aid) or ""
                return {
                    "id": aid,
                    "kind": art.kind,
                    "source": art.source,
                    "summary": art.summary,
                    "bytes": art.bytes_len,
                    "content": body,
                }

            async def _list_artifacts(kind: str = "", limit: int = 10) -> dict:
                items = agent._artifacts.list_recent(kind=kind or None, limit=int(limit) or 10)
                return {
                    "count": len(items),
                    "artifacts": [
                        {"id": a.id, "kind": a.kind, "source": a.source,
                         "summary": a.summary, "bytes": a.bytes_len}
                        for a in items
                    ],
                }

            registry.add_tool(
                {"type": "function", "function": {
                    "name": "get_artifact",
                    "description": (
                        "Fetch the body of a previously stored artifact (file content, "
                        "command output, OCR snippet) by id.  Use this when the agent "
                        "context only shows an artifact summary and you need the full text."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "id": {"type": "string", "description": "artifact://<id> or just the id."},
                    }, "required": ["id"]},
                }},
                _get_artifact,
            )
            registry.add_tool(
                {"type": "function", "function": {
                    "name": "list_artifacts",
                    "description": (
                        "List recent artifacts captured during this task.  Use to find "
                        "a previously-read file before re-reading it from disk."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "kind": {"type": "string"},
                        "limit": {"type": "integer"},
                    }},
                }},
                _list_artifacts,
            )

        async def _plan_get() -> dict:
            if agent._plan is None:
                return {"plan": None, "note": "No structured plan in this session."}
            return {
                "plan": agent._plan.to_dict(),
                "progress": agent._plan.progress_summary(),
            }

        async def _plan_update_step(
            id: str,
            status: str = "",
            notes: str = "",
        ) -> dict:
            if agent._plan is None:
                return {"error": "No structured plan in this session."}
            step = agent._plan.by_id(str(id))
            if step is None:
                return {"error": f"no step with id {id!r}"}
            if status:
                try:
                    step.status = StepStatus(status)
                except ValueError:
                    return {"error": f"invalid status {status!r}"}
            if notes:
                step.notes = (step.notes + "\n" + notes).strip() if step.notes else notes
            agent._persist_progress()
            return {"step": step.to_public()}

        registry.add_tool(
            {"type": "function", "function": {
                "name": "plan_get",
                "description": (
                    "Return the current typed plan (steps, ids, statuses).  Use to "
                    "remind yourself which step the controller expects you to work on."
                ),
                "parameters": {"type": "object", "properties": {}},
            }},
            _plan_get,
        )
        registry.add_tool(
            {"type": "function", "function": {
                "name": "plan_update_step",
                "description": (
                    "Manually mark a plan step done/skipped/blocked, or attach notes.  "
                    "The controller marks STEP_DONE / STEP_BLOCKED automatically; use "
                    "this only to skip an obsolete step or annotate progress."
                ),
                "parameters": {"type": "object", "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": [
                        "pending", "running", "done", "failed", "skipped", "blocked",
                    ]},
                    "notes": {"type": "string"},
                }, "required": ["id"]},
            }},
            _plan_update_step,
        )

        if self._progress is not None:
            async def _checkpoint(label: str = "", data: dict | str = "") -> dict:
                if agent._task_progress is None:
                    return {"note": "no active task progress record"}
                payload: dict = {}
                if isinstance(data, dict):
                    payload = dict(data)
                elif isinstance(data, str) and data:
                    payload = {"note": data}
                if label:
                    payload["label"] = label
                agent._progress.update_checkpoint(agent._task_progress, payload)
                return {"saved": True, "checkpoint": payload}

            registry.add_tool(
                {"type": "function", "function": {
                    "name": "checkpoint",
                    "description": (
                        "Persist a free-form progress marker so the task can resume "
                        "after a crash or abort.  Use after non-trivial milestones "
                        "(\"finished tab 3 of 7\", \"wrote intermediate output\")."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "label": {"type": "string"},
                        "data": {"type": "object"},
                    }},
                }},
                _checkpoint,
            )

        if self._subagent is not None:
            async def _ask_subagent(
                question: str,
                artifact_ids=None,
            ) -> dict:
                fetched: list[tuple[str, str]] = []
                if isinstance(artifact_ids, list) and agent._artifacts is not None:
                    for aid in artifact_ids[:6]:
                        body = agent._artifacts.get_body(str(aid))
                        if body:
                            art = agent._artifacts.get(str(aid))
                            label = art.summary if art else str(aid)
                            fetched.append((label[:80], body))
                result = await agent._subagent.ask(
                    str(question),
                    fetched_artifacts=fetched or None,
                )
                return {
                    "answer": result.answer,
                    "artifact_ids": result.artifact_ids,
                    "tool_calls_made": result.tool_calls_made,
                }

            registry.add_tool(
                {"type": "function", "function": {
                    "name": "ask_subagent",
                    "description": (
                        "Delegate a read-only lookup question to a focused subagent so "
                        "the answer doesn't bloat the main conversation history.  Good "
                        "for \"which of these N files mentions X\", \"summarise this JSON\", "
                        "and similar pure-read tasks.  The subagent has no desktop or shell "
                        "access — only fs_read / fs_list / browser_get_text."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "question": {"type": "string"},
                        "artifact_ids": {"type": "array", "items": {"type": "string"},
                                         "description": "Optional pre-fetched artifact ids."},
                    }, "required": ["question"]},
                }},
                _ask_subagent,
            )

        # ---- App-memory tools ------------------------------------------
        # memory_get is read-only and registers whenever the store
        # exists; memory_note creates new entries and is gated behind
        # agent.memory.enabled so a default install never writes a
        # memory/ directory.
        if self._memory is not None:
            async def _memory_get(app: str = "") -> dict:
                if not app:
                    return {"apps": agent._memory.list_apps()}
                return agent._memory.get(str(app))

            registry.add_tool(
                {"type": "function", "function": {
                    "name": "memory_get",
                    "description": (
                        "Read the per-app memory record for an app (failure "
                        "histogram, success counts, recent notes).  Pass an "
                        "empty app to list every app the memory store has "
                        "ever recorded.  Always available — reads are not "
                        "gated by agent.memory.enabled."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "app": {"type": "string"},
                    }},
                }},
                _memory_get,
            )

            if self._memory_enabled:
                async def _memory_note(app: str, text: str, tag: str = "") -> dict:
                    if not app or not text:
                        return {"error": "app and text are required"}
                    agent._memory.add_note(app=str(app), text=str(text), tag=str(tag or ""))
                    return {"saved": True, "app": _normalize_app(str(app))}

                registry.add_tool(
                    {"type": "function", "function": {
                        "name": "memory_note",
                        "description": (
                            "Attach a free-form note (\"Slack input box doesn't "
                            "respond to ctrl+a\") to an app's memory record so "
                            "future tasks against the same app see the warning."
                        ),
                        "parameters": {"type": "object", "properties": {
                            "app": {"type": "string"},
                            "text": {"type": "string"},
                            "tag": {"type": "string"},
                        }, "required": ["app", "text"]},
                    }},
                    _memory_note,
                )

        # ---- Budget tool -----------------------------------------------
        async def _budget_status() -> dict:
            if agent._budget is None:
                return {"note": "no active task budget"}
            snap = agent._budget.snapshot()
            return {
                "elapsed_seconds": snap.elapsed_seconds,
                "tool_calls": snap.tool_calls,
                "chat_calls": snap.chat_calls,
                "prompt_tokens": snap.prompt_tokens,
                "completion_tokens": snap.completion_tokens,
                "total_tokens": snap.total_tokens,
                "fraction_used": snap.fraction_used,
                "exceeded": snap.exceeded,
            }

        registry.add_tool(
            {"type": "function", "function": {
                "name": "budget_status",
                "description": (
                    "Return the current task's cost telemetry: elapsed time, "
                    "tool/chat call counts, token usage, and how close we are "
                    "to any configured ceiling.  Use periodically on long "
                    "tasks to decide whether to wrap up early."
                ),
                "parameters": {"type": "object", "properties": {}},
            }},
            _budget_status,
        )


    def _maybe_store_artifact(self, tool_name: str, args: dict, result_json: str) -> str:
        """
        For tools whose results are large bodies (fs_read, browser_get_text,
        desktop_get_window_text), store the body as an artifact and return a
        slimmed-down history payload.  Other results pass through unchanged.
        """
        if self._artifacts is None:
            return result_json
        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            return result_json
        if "error" in result:
            return result_json

        body_field, source = None, ""
        if tool_name == "fs_read":
            body_field = "content"
            source = str(args.get("path", ""))
        elif tool_name == "desktop_get_window_text":
            body_field = "text"
        elif tool_name == "browser_get_text":
            body_field = "text"
            source = str(args.get("selector", "") or "page")
        elif tool_name == "shell_run":
            # Stdout only when it's large enough to bloat history.
            stdout = result.get("stdout") or ""
            if isinstance(stdout, str) and len(stdout) > 4096:
                aid = self._artifacts.put(
                    stdout, kind="shell_stdout",
                    source=str(args.get("command", ""))[:120],
                )
                preview = stdout[:400] + "\n...\n[stored as " + aid + "]"
                result["stdout"] = preview
                result["stdout_artifact_id"] = aid
                return json.dumps(result, default=str)
            return result_json

        if body_field is not None:
            body = result.get(body_field)
            if isinstance(body, str) and len(body) > 4096:
                aid = self._artifacts.put(
                    body, kind=tool_name, source=source,
                )
                preview = body[:600] + "\n...\n[truncated; full content stored as " + aid + "]"
                result[body_field] = preview
                result[body_field + "_artifact_id"] = aid
                return json.dumps(result, default=str)
        return result_json


    @staticmethod
    def _result_is_error(result_json: str) -> bool:
        """
        Return True when a tool result clearly indicates failure.
        Checks for the 'error' key, non-zero exit_code, or timed_out flag.
        """
        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            return False
        if "error" in result:
            return True
        if result.get("exit_code") not in (None, 0):
            return True
        if result.get("timed_out"):
            return True
        return False

    @staticmethod
    def _summarize_result(tool_name: str, result_json: str) -> str:
        """
        Produce a short human-readable summary of a tool result for display.
        The full result_json is still appended to the history; this is for UI only.
        """
        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            return result_json[:200]

        if "error" in result:
            return f"[ERROR] {result['error'][:200]}"

        # Tool-specific summaries
        if tool_name == "shell_run":
            rc = result.get("exit_code", "?")
            out = result.get("stdout", "")[:300]
            err = result.get("stderr", "")[:200]
            parts = [f"exit={rc}"]
            if out:
                parts.append(f"stdout: {out}")
            if err:
                parts.append(f"stderr: {err}")
            return " | ".join(parts)

        if tool_name == "desktop_screenshot":
            return f"Screenshot saved: {result.get('path', '?')} ({result.get('width')}×{result.get('height')})"

        if tool_name in ("desktop_click", "desktop_type", "desktop_hotkey", "desktop_scroll"):
            return f"OK: {result}"

        if tool_name == "fs_read":
            n = len(result.get("content", ""))
            trunc = " [truncated]" if result.get("truncated") else ""
            return f"Read {n} chars{trunc}"

        if tool_name == "fs_list":
            return f"Listed {result.get('count', 0)} entries"

        if tool_name == "fs_write":
            return f"Wrote {result.get('bytes_written', '?')} bytes to {result.get('path', '?')}"

        # Generic fallback
        return str(result)[:300]
