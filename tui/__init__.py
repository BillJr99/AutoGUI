"""
tui — the Textual chat interface, decomposed from the original tui.py.

The public import surface is unchanged: ``from tui import AgentTUI`` keeps
working exactly as before.  The implementation now lives in:

  app.py         AgentTUI — composition, mount/unmount, input handling,
                 logging-bridge install/uninstall
  render.py      agent-event rendering worker, status watcher, REST-API
                 task event display
  actions.py     action_* handlers + config persistence helpers
  screens.py     modal overlays (help, model picker, input modal)
  commands.py    Ctrl+P command palette provider
  log_bridge.py  stderr proxy + logging handler for the conversation pane

``AgentTUI`` remains a single class; render.py and actions.py contribute
mixins.
"""

from .app import AgentTUI

__all__ = ["AgentTUI"]
