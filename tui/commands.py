"""
tui.commands — the Ctrl+P command palette provider.

Replaces Textual's default SystemCommands with the agent's own command set.
Split out of the original tui.py.
"""

import logging

from textual.command import Hit, Hits, Provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command palette provider — all agent commands for Ctrl+P
# ---------------------------------------------------------------------------

class _AgentCommands(Provider):
    """
    Surfaces all agent-specific commands in the Ctrl+P command palette.

    Items appear immediately when the palette is opened (empty query) and are
    filtered by fuzzy-match as the user types.  All config.json settings are
    exposed here so every tuneable parameter can be changed without editing
    the file by hand.
    """

    def _build_items(self):
        """Build the full command list at call time so lambdas capture live app state."""
        app = self.app

        def toggle(dot_path, label, help_text):
            return (label, lambda: app._action_toggle_cfg(dot_path, label), help_text)

        def edit(dot_path, label, help_text):
            return (label, lambda: app._action_edit_cfg(dot_path, label), help_text)

        def pick(dot_path, label, choices, help_text):
            return (label, lambda: app._action_pick_cfg(dot_path, label, choices), help_text)

        return [
            # ── Core session actions ──────────────────────────────────────
            ("Change Model",       app.action_pick_model,    "Switch to a different model (live list from API)"),
            ("Toggle Vision",      app.action_toggle_vision, "Turn screenshot vision on/off for vision-capable models"),
            ("Reset Conversation", app.action_reset,          "Clear conversation history and start fresh"),
            ("Save History",       app.action_save,           "Append conversation to logs/history.jsonl"),
            ("Toggle Tool Output", app.action_toggle_tools,   "Show or hide tool call / result lines"),
            ("Help",               app.action_help,           "Key bindings and registered tool list"),
            # ── openwebui ────────────────────────────────────────────────
            edit("openwebui.temperature",    "Set Temperature",       "LLM sampling temperature (0.0–2.0)"),
            edit("openwebui.max_tokens",     "Set Max Tokens",        "Maximum tokens per response"),
            edit("openwebui.timeout_seconds","Set Request Timeout",   "API request timeout in seconds"),
            edit("openwebui.base_url",       "Set API Base URL",      "OpenWebUI server base URL"),
            edit("openwebui.api_path",       "Set API Path",          "Completions endpoint path"),
            # ── agent (top-level) ────────────────────────────────────────
            edit("agent.max_iterations",    "Set Max Iterations",     "Maximum agent iterations per task"),
            toggle("agent.skills_enabled",  "Toggle Skills Recording","Allow the agent to save new skills to disk"),
            toggle("agent.suggest_skills",  "Toggle Suggest Skills",  "Show skill suggestions at task start"),
            toggle("agent.record_trace",    "Toggle Trace Recording", "Record step-by-step execution traces"),
            edit("agent.trace_dir",         "Set Trace Directory",    "Directory where traces are written"),
            edit("agent.skills_path",       "Set Skills Path",        "Path to the skills JSONL file"),
            # ── agent.planner ────────────────────────────────────────────
            toggle("agent.planner.enabled", "Toggle Planner",         "Enable pre-execution planning pass"),
            # ── agent.controller ────────────────────────────────────────
            toggle("agent.controller.enabled",              "Toggle Controller",             "Step-by-step executor (off = legacy ReAct loop)"),
            edit("agent.controller.step_max_iterations",    "Set Step Max Iterations",       "Max iterations allowed per controller step"),
            edit("agent.controller.step_max_retries",       "Set Step Max Retries",          "Max retries before a step is marked failed"),
            toggle("agent.controller.auto_resume",          "Toggle Controller Auto-Resume", "Automatically resume after a step failure"),
            toggle("agent.controller.replan_on_block",      "Toggle Replan on Block",        "Replan when the controller is stuck"),
            toggle("agent.controller.critique_enabled",     "Toggle Plan Critique",          "LLM reviews the plan before execution"),
            toggle("agent.controller.preflight_enabled",    "Toggle Preflight Checks",       "Verify resource availability before UI actions"),
            toggle("agent.controller.predicate_check_enabled", "Toggle Predicate Checks",    "Verify typed post-conditions after each step"),
            toggle("agent.controller.visual_diff_enabled",  "Toggle Visual Diff",            "Flag steps whose screen pixels barely changed"),
            edit("agent.controller.watchdog_stall_threshold","Set Watchdog Stall Threshold", "Iterations before flagging a step as stuck (0=off)"),
            # ── agent.bon ────────────────────────────────────────────────
            toggle("agent.bon.enabled",                          "Toggle Best-of-N Sampling",          "Sample N completions and pick the best on uncertain steps"),
            edit("agent.bon.n",                                  "Set BoN Sample Count",               "Number of completions to sample for best-of-N"),
            edit("agent.bon.temperature",                        "Set BoN Temperature",                "Temperature used for best-of-N sampling"),
            toggle("agent.bon.trigger_on_recent_failure",        "Toggle BoN on Recent Failure",        "Trigger best-of-N after a recent step failure"),
            toggle("agent.bon.trigger_on_validator_disagreement","Toggle BoN on Validator Disagreement","Trigger best-of-N when the validator disagrees"),
            # ── agent.budget ─────────────────────────────────────────────
            edit("agent.budget.max_tool_calls",  "Set Budget: Max Tool Calls",  "Hard ceiling on tool calls per task (0=unlimited)"),
            edit("agent.budget.max_chat_calls",  "Set Budget: Max Chat Calls",  "Hard ceiling on LLM calls per task (0=unlimited)"),
            edit("agent.budget.max_total_tokens","Set Budget: Max Total Tokens","Hard ceiling on tokens per task (0=unlimited)"),
            edit("agent.budget.max_seconds",     "Set Budget: Max Seconds",     "Hard ceiling on wall-clock seconds per task (0=unlimited)"),
            # ── agent.memory ─────────────────────────────────────────────
            toggle("agent.memory.enabled", "Toggle Memory Recording", "Allow recording app failure/success memory"),
            edit("agent.memory.dir",       "Set Memory Directory",    "Directory for app memory files"),
            # ── agent.subagent ───────────────────────────────────────────
            toggle("agent.subagent.enabled",       "Toggle Sub-agent",              "Enable read-only subagent for lookup questions"),
            edit("agent.subagent.max_tool_calls",  "Set Sub-agent Max Tool Calls",  "Maximum tool calls the subagent may make"),
            # ── agent.drift_anchor ───────────────────────────────────────
            toggle("agent.drift_anchor.enabled",      "Toggle Drift Anchor",       "Capture world snapshot after each step"),
            toggle("agent.drift_anchor.capture_phash","Toggle Drift Anchor pHash", "Include screenshot perceptual hash in drift anchor"),
            # ── agent.screen_record ──────────────────────────────────────
            toggle("agent.screen_record.enabled",  "Toggle Screen Recording",      "Record rolling screen buffer; flush GIF on failure"),
            edit("agent.screen_record.fps",         "Set Screen Record FPS",        "Frame rate for the screen recording buffer"),
            edit("agent.screen_record.buffer_seconds","Set Screen Record Buffer",   "Rolling buffer duration in seconds"),
            edit("agent.screen_record.max_width",   "Set Screen Record Max Width",  "Maximum pixel width of recorded frames"),
            edit("agent.screen_record.out_dir",     "Set Screen Record Output Dir", "Directory for failure GIFs"),
            # ── agent.artifacts / progress ──────────────────────────────
            edit("agent.artifacts.dir", "Set Artifacts Directory", "Directory for large-observation artifact store"),
            edit("agent.progress.dir",  "Set Progress Directory",  "Directory for task progress markers"),
            # ── tools ────────────────────────────────────────────────────
            toggle("tools.allowed_shell",      "Toggle Shell Tools",        "Enable shell command execution tools"),
            toggle("tools.allowed_filesystem", "Toggle Filesystem Tools",   "Enable filesystem read/write tools"),
            toggle("tools.allowed_desktop",    "Toggle Desktop Tools",      "Enable mouse/keyboard/screenshot tools"),
            toggle("tools.allowed_browser",    "Toggle Browser Tools",      "Enable Playwright browser automation tools"),
            edit("tools.shell_timeout_seconds","Set Shell Timeout",         "Timeout in seconds for shell commands"),
            edit("tools.max_screenshot_width", "Set Max Screenshot Width",  "Resize screenshots to this pixel width before sending"),
            edit("tools.screenshot_dir",       "Set Screenshot Directory",  "Directory where screenshots are saved"),
            edit("tools.perception_cache_ttl_seconds","Set Perception Cache TTL","Seconds to cache OCR/SOM perception results"),
            # ── browser ──────────────────────────────────────────────────
            toggle("browser.headless",        "Toggle Headless Browser",   "Run the browser without a visible window"),
            edit("browser.screenshot_dir",    "Set Browser Screenshot Dir","Directory for browser screenshots"),
            edit("browser.user_data_dir",     "Set Browser User Data Dir", "Browser profile/user-data directory"),
            # ── logging ──────────────────────────────────────────────────
            pick("logging.level",  "Set Log Level",        ["DEBUG", "INFO", "WARNING", "ERROR"], "Logging verbosity level"),
            edit("logging.file",   "Set Log File",         "Path to the rotating log file"),
            edit("logging.max_bytes",    "Set Log Max Bytes",   "Maximum size of the log file before rotation"),
            edit("logging.backup_count", "Set Log Backup Count","Number of rotated log files to keep"),
            # ── tui ──────────────────────────────────────────────────────
            edit("tui.history_file",     "Set History File",      "Path for the conversation history JSONL"),
            edit("tui.theme",            "Set TUI Theme",         "Textual theme name (e.g. dark, light, nord)"),
            # ── safety ───────────────────────────────────────────────────
            edit("safety.command_confirm_delay_seconds","Set Command Confirm Delay","Seconds to pause before executing each tool (0=off)"),
            toggle("safety.dry_run",       "Toggle Dry Run",          "Simulate tool calls without actually executing them"),
            edit("safety.fs_write_snapshot_dir","Set FS Snapshot Dir","Directory for filesystem write snapshots (empty=off)"),
            # ── screen_observer ──────────────────────────────────────────
            toggle("screen_observer.enabled",         "Toggle Screen Observer",        "Enable OSScreenObserver integration"),
            edit("screen_observer.base_url",          "Set Screen Observer URL",       "OSScreenObserver server base URL"),
            edit("screen_observer.timeout_seconds",   "Set Screen Observer Timeout",   "Screen Observer request timeout in seconds"),
            # ── top-level ────────────────────────────────────────────────
            toggle("install_dependencies", "Toggle Install Dependencies on Startup","Run the install script at each startup"),
            edit("prompts_dir",            "Set Prompts Directory",                 "Directory containing prompt template files"),
        ]

    async def discover(self) -> Hits:
        """Show all commands when the palette opens with no query."""
        for label, action_fn, help_text in self._build_items():
            yield Hit(1.0, label, action_fn, help_text)

    async def search(self, query: str) -> Hits:
        """Filter commands by fuzzy match as the user types."""
        matcher = self.matcher(query)
        for label, action_fn, help_text in self._build_items():
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), action_fn, help_text)
