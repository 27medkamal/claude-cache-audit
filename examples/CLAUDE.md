## Delegation and token budget

Usage grows with context size × turns and with cache rebuilds, while starting a fresh agent is cheap.

- Do easy or small work yourself. Delegate big, self-contained work, one task per agent.
- Agents are disposable: one task, one fresh agent, then done. Send a follow-up only to an agent that finished
  less than 5 minutes ago and is still small; otherwise start a fresh one with a written brief.
- Agent types (`~/.claude/agents/`):
  - `lean`: locate code, read docs or logs, summarize output.
  - `implementer`: one well-specified task.
  - `reviewer`: one diff against its spec.
  - `consult`: an optional second opinion on a critical decision. Give it the evidence and ask for a critique
    and its own recommendation.
- Nobody waits inside a big context, because subagent caches expire after 5 minutes. Run long builds, test
  suites and wait loops from the main session in the background with `qrun -- <command>`, then hand the
  failure summary to a fresh agent.
- Fork only while the main context is small (under ~150K tokens); above that, start a fresh agent with a brief.
- Agent replies: at most ~300 words plus file paths; details go into files.
- Keep tool output small: quiet test flags, `rg`/`head`/line ranges instead of whole files or logs.
- Keep the plan, decisions and task list in a file. At natural breakpoints, update it and suggest
  `/compact <focus>` while the cache is warm; for an unrelated task, suggest a fresh session from that file.
