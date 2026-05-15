'''
agent.py — Core agentic loop.

The agent implements a ReAct-style (Reason + Act) loop:

  1. Append the user message (or initial task) to the conversation history.
  2. Send the full history plus the tool catalog to the LLM via the client.
  3. Examine finish_reason:
       "stop"       → model produced a final text reply; yield it and exit.
       "tool_calls" → model issued one or more tool calls; dispatch each,
                      append results as role="tool" messages, and loop.
       "length"     → context length exceeded; yield a warning and exit.
  4. Guard against infinite loops with a configurable max_iterations ceiling.

The loop yields AgentEvent objects rather than returning a single final value,
so that the TUI (tui.py) and the CLI one-shot path (main.py) can both consume
a streaming event sequence and render incrementally.  This is a generator-based
approach using "yield" rather than async generators with "async yield", keeping
the event delivery compatible with asyncio without requiring AsyncGenerator
plumbing in the callers.

AgentEvent types
----------------
  "text"       — A text segment from the assistant.
  "tool_call"  — The model is about to invoke a tool (name + args).
  "tool_result" — The result of a tool call.
  "error"      — An error occurred (message included).
  "done"       — Loop has ended; includes finish_reason and iteration count.
'''
