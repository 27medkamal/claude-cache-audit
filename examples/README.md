# Examples: small, disposable agents

One way to keep Claude Code's subagent usage down: small agents with one task each, and nobody waiting inside
a big context. Copy what's useful, and adjust the models and limits to your plan.

| File | Copy to | Purpose |
|---|---|---|
| `agents/lean.md` | `~/.claude/agents/` | Read-only scout on a small model: find code, read logs, summarize output |
| `agents/implementer.md` | `~/.claude/agents/` | One well-specified task, then done |
| `agents/reviewer.md` | `~/.claude/agents/` | Reviews one diff against its spec |
| `agents/consult.md` | `~/.claude/agents/` | Optional second opinion on a critical decision |
| `CLAUDE.md` | append to `~/.claude/CLAUDE.md` | When and how the main agent delegates |
| `qrun` | a directory on your `PATH` | Runs a long command with the log in a file and only the outcome on screen |

Why it helps: subagents get a 5-minute prompt cache. An agent that keeps receiving tasks keeps its whole
history and re-reads it on every turn, and one that waits more than five minutes for a test suite writes its
whole context to the cache again. A fresh agent with a written brief starts small. A long command run from the
main session (on a subscription it has a 1-hour cache) costs no agent turns while it runs.

`autoCompactWindow` in the agent files caps how far each agent's context grows before it compacts.
