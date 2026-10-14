# claude-cache-audit

Audits Claude Code's prompt-cache usage from its local transcripts. It prices every API request at list
prices and shows where the money went, with the focus on what Claude Code's own `/cost` does not show:
subagents (including how much comes from agents whose context grew past 500K), cache rebuilds by cause,
idle compactions, and whether a 1-hour cache for subagents would pay off.
It runs locally, reads only your own transcript files, and makes no network calls.

The numbers and definitions follow the method of the write-up at <https://27medkamal.com/blog/claude-code-usage/>.

## Install and run

    uvx claude-cache-audit
    pipx run claude-cache-audit

Python 3.9 or later, standard library only.

    claude-cache-audit                         # last 30 days
    claude-cache-audit --all                   # everything on disk
    claude-cache-audit --since 7d              # also: 12h, 2026-09-01, 2026-09-01T15:00
    claude-cache-audit --since 2026-06-11 --until 2026-10-12T15:00
    claude-cache-audit rebuilds --top 20       # one line per rebuild, costliest first
    claude-cache-audit --json                  # every analysis, schema_version 1
    claude-cache-audit --share                 # short Markdown block, safe to paste publicly
    claude-cache-audit --config-dir ~/.claude-work --config-dir ~/.claude

A bare date as `--until` includes that whole day. Times without an offset are local time.

From a checkout: `uv run --directory <repo> claude-cache-audit`, or `PYTHONPATH=src python3 -m claude_cache_audit`.

## What it reads, and what it never sends

- It reads `projects/**/*.jsonl` under every `--config-dir`. Without the option it uses `$CLAUDE_CONFIG_DIR` if set,
  `~/.claude`, and every `~/.claude-*` directory that has a `projects/` folder. Files are deduplicated by real
  path, so a symlinked config directory is counted once. Subagent transcripts
  (`<session>/subagents/agent-*.jsonl` and their `.meta.json`) are included.
- From each assistant message it keeps token counts (input, cache read, cache write with the 5 minute and 1 hour
  split, output, thinking), the model, speed, timestamp, stop reason, tool names, session id, working directory
  and Claude Code version. From compaction records it keeps the trigger and token counts. From the subagent
  metadata it keeps the agent type and task kind.
- It does not print, store or send prompt text, assistant text, tool inputs, command text or file contents.
  The one thing derived from a Bash tool call is whether it ran in the background or with a timeout above five
  minutes; that is folded into the tool name, and the input itself is dropped.
- There is no network access anywhere in the package. `--share` prints ratios and percentages only: no dollar
  figures, project names, paths or session ids.
- The `rebuilds` command shows the project folder name and the first 8 characters of the session id, so do not
  paste its output publicly without a look.

## How it counts

**Requests.** One record per API request, deduplicated by message id and request id. Subagent files are read
first, so a request shared with its parent session is attributed to the subagent. Streaming duplicates in one file are merged.

**Cost.** Input, output and cache-read prices come from a table in `pricing.py` (see "Pricing" below). A 5 minute
cache write costs 1.25 times the input price and a 1 hour write costs 2 times. Fast mode doubles the price for Opus 5.5, Opus 5 and Opus 4.8, the models the pricing page lists it for.
Models that are not in the table are priced at 0 and listed in the report footer.

**Agent kinds.** Main session, subagent, teammate (`taskKind: in_process_teammate`) and fork (`agentType: fork`).

**Cold start.** The first request of a transcript. It writes the prompt for the first time, so it is never a
rebuild. The report shows cold starts separately.

**Rebuild.** A request that wrote to the cache tokens that were already cached by the previous request in the
same transcript. For a request with previous prompt size `pt_prev`, cache read `cr` and cache write `cw`:
`rewritten = clamp(pt_prev - cr, 0, cw)`. Rewrites under 1,000 tokens are ignored as boundary noise.

**Premium.** What the rebuild cost beyond a cache hit: `rewritten x (write price - cache-read price)`.
This is the headline measure, summed over every cause except compaction.

**The post's rule.** The write-up counted a request as a rebuild when `cw >= 20K` and `cw >= 0.5 x pt`, with
the premium taken on all of `cw`. The report prints it as a cross-check line; on real data the two land within
about 1% of each other.

**Causes**, checked in this order:

1. *compaction*: a compaction (auto, manual or idle) sits between the previous request and this one. The new
   prompt is new content, so this is reported apart and is not an avoidable rebuild.
2. *model switch*: the model differs from the previous request's.
3. *expired*: the gap is at least the TTL of the latest cache write before it (1 hour if that write was a 1 hour write, else 5 minutes).
4. *prefix change*: anything else, such as an edited system prompt, a changed tool list or a different cache breakpoint.

Rebuilds are sliced by cause, agent kind, gap (`<5m`, `5-60m`, `1-8h`, `>8h`) and what the previous request was
waiting on (`user` after an `end_turn`, `tool:<name>` after a tool call, otherwise `stop:<reason>`, e.g. `stop:max_tokens`, or `stop:none`). `--json` has every slice.

**Idle compaction.** Claude Code 2.1.286 and later can compact a session while it is idle, before the prompt cache
expires, and records it as a `Compacted while idle` notice next to a compaction boundary (the boundary itself may
say `auto` or `manual`). The compaction call itself is not in the transcript, so its cost is estimated: the
pre-compaction tokens read from cache plus a summary of about the post-compaction size at the output price. The
saving is also an estimate, and it is credited only when the cache would have expired: the next request came
at least one TTL (5 minutes, or 1 hour if the latest cache write was a 1 hour write) after the last request before the
compaction. Then it compares rewriting the pre-compaction tokens cold at that request's write price with the
compaction's estimated cost plus the cache write that request really made. If the next request came sooner, the
cache was still warm and the compaction only cost money; those are counted as pure cost, like idle compactions
never followed by a request.

**1-hour subagent cache.** `r` is the share of a subagent kind's cache-write tokens that were expired rebuilds
after 5 to 60 minute gaps, which a 1 hour TTL would have kept. A 1 hour write costs 0.75 x input more than a
5 minute one, and each avoided rebuild turns a 2 x write into a read, so the break-even is

    r = 0.75 / (2 - read_ratio)        read_ratio = cache-read price / input price

With a read ratio of 0.05 (Opus 5.5 and Sonnet 5.5) that is 0.75 / 1.95, about 38.5%. The ratio is weighted by the
kind's cache-write tokens. The dollar counterfactual (extra write premium, rebuilds avoided, net) follows the
write-up's script. The verdict needs at least 100 requests of that kind.

**Hit rate by idle gap.** For requests with a prompt of at least 30K tokens on the same model as the previous
request: a hit read at least half the prompt from cache; a rebuild wrote at least half.

## Example

Output on synthetic transcripts, generated for this README (not anyone's real usage). No project names, paths,
session ids or MCP server names appear in the default report.

```
claude-cache-audit 0.1.0
========================================
Window:        2026-09-01 to 2026-09-30 (30.0 days)
Requests:      23,307
Transcripts:   12 main, 120 subagent (132 files scanned)
Claude Code:   2.1.290 to 2.1.296
Total:         $2,449  (API-equivalent prices as of 2026-10-13; not your bill)

1. Usage split (API-equivalent)
                      cost  share  tokens
  cache reads       $1,227  50.1%   7.51B
  cache writes, 5m    $464  19.0%  191.0M
  cache writes, 1h    $461  18.8%   55.6M
  uncached input     $0.31  <0.1%    105K
  output              $296  12.1%   17.9M
  thinking is 10.6% of output tokens

2. By agent kind
              cost  share  requests  transcripts
  main      $1,679  68.6%    12,367           12
  subagent  $50.30   2.0%     3,406           83
  teammate    $574  23.4%     4,722           12
  fork        $146   6.0%     2,812           25

3. Subagents by peak context (largest prompt of each agent)
  peak      agents  of agents    cost  of cost
  <100K         41      34.2%  $20.58     2.7%
  100-250K      48      40.0%  $78.28    10.2%
  250-500K      25      20.8%    $208    27.1%
  >=500K         6       5.0%    $463    60.1%
  an agent is one subagent, teammate or fork transcript; shares are of these agents' cost.

4. Cache rebuilds
  Cold starts (first request of a transcript, never a rebuild): 132, write cost $18.93
  Rebuild premium, headline: $714, 29.2% of total  (639 rebuilds, 208.7M tokens rewritten)
  Cross-check, the post's rule (write >= 20K and >= half the prompt): $718, 29.3% of total

  cause          rebuilds  rewritten  premium  of total
  model switch          8       2.6M   $35.30      1.4%
  expired             605     199.2M     $638     26.0%
  prefix change        26       6.9M   $41.06      1.7%
    model switch  different model than the previous request
    expired       gap at least the TTL of the latest cache write
    prefix change cache lost with no gap or model change
  Compaction, reported apart and not in the headline (new content, not an avoidable rebuild):
    15 rebuilds, 295K rewritten, $2.30, <0.1% of total
    compaction rebuilds by kind: auto 11, manual 2, idle 2

  Premium by who and gap (cause totals above; full slices in --json):
  who          <5m   5-60m    1-8h     >8h
  main      $63.78   $7.47    $200  $36.45
  subagent   $0.15   $6.35   $1.15   $0.00
  teammate   $2.65    $281  $37.87  $26.88
  fork       $2.31  $43.61   $4.47   $0.00

  Premium by what the previous request was waiting on (top 5):
  waiting on  rebuilds  premium
  user             307     $437
  tool:Bash        197     $144
  tool:Read         45   $36.37
  tool:Grep         20   $30.00
  tool:Edit         13   $23.08

  Top tools subagents waited on before a rebuild: Bash 194 ($135), Read 40 ($22.14),
    Bash:bg 18 ($15.53), Bash:long-timeout 17 ($13.92), Grep 16 ($10.14)

5. Cache hit rate by idle gap (prompts of 30K+ tokens, same model as the previous request)
  who       gap      requests    hit  rebuild
  main      0-5m       11,622  99.8%     0.1%
  main      5-30m         387  99.7%     0.3%
  main      30-55m        179  98.9%     1.1%
  main      55-65m         69  73.9%    26.1%
  main      65-120m        12   0.0%   100.0%
  main      >120m          53   0.0%   100.0%
  subagent  0-5m       10,297  99.9%     0.1%
  subagent  5-30m         333   0.0%   100.0%
  subagent  30-55m         98   0.0%   100.0%
  subagent  55-65m          4   0.0%   100.0%
  subagent  65-120m        18   0.0%   100.0%
  subagent  >120m          70   0.0%   100.0%
  cache-write tokens by TTL: main 1h 55.6M, 5m 0; subagent 1h 0, 5m 191.0M

6. Context size (cost by prompt size)
  prompt    requests    cost  share
  <200K        8,276    $341  13.9%
  200-400K     7,370    $677  27.6%
  >=400K       7,661  $1,431  58.4%

7. Idle compactions
  compactions in window: auto 11, manual 2, idle 2
  Compaction calls are not logged; their cost is estimated (read the old context, write a summary):
    estimated cost of the 2 idle compaction calls: $1.33
    followed by another request: 2; never followed (pure cost, $0.00): 0
    next request came before the cache expired (pure cost, $0.00): 0
    ESTIMATED saving vs rewriting the old context cold, expired cases only: $7.71

8. Would a 1-hour subagent cache pay off?
  kind      requests      r  break-even   extra  avoided     net  verdict
  subagent     3,406  30.4%       38.9%  $12.72   $10.33  -$2.39  would not pay off
  teammate     4,722  77.1%       38.5%    $228     $457    $229  would pay off
  fork         2,812  72.2%       38.5%  $37.75   $70.86  $33.11  would pay off
  all subagents: extra write premium $279, rebuilds avoided $538, net $260
  r = share of the kind's cache-write tokens that were expired rebuilds after 5-60 minute gaps.

9. Try this
  - Subagent rebuilds after tool waits are 9% of usage: run long commands outside subagents.
    https://27medkamal.com/blog/claude-code-usage/
  - Teammate rebuilds are 77% of their cache writes, above the 38.5% break-even: set
    subagentPromptCacheTtl, or experimental.cacheTtl: 1h in that agent's frontmatter.
    https://27medkamal.com/blog/claude-code-usage/
  - Fork rebuilds are 72% of their cache writes, above the 38.5% break-even: set
    subagentPromptCacheTtl, or experimental.cacheTtl: 1h in that agent's frontmatter.
    https://27medkamal.com/blog/claude-code-usage/
  - Contexts over 400K are 58% of usage: prefer smaller, disposable contexts, and set
    autoCompactWindow for subagents (Claude Code 2.1.296+).
    https://27medkamal.com/blog/claude-code-usage/
  - 60% of subagent usage came from agents whose context passed 500K: give each agent one task,
    then start a fresh one. https://27medkamal.com/blog/claude-code-usage/

  * malformed lines skipped: 0
```

## Acting on it

[`examples/`](https://github.com/27medkamal/claude-cache-audit/tree/main/examples) has four subagent definitions, delegation rules for `CLAUDE.md` and a wrapper for long
commands: one way to keep subagents small and to stop agents from waiting inside a big context.

## Pricing

`pricing.py` holds one table, dated 2026-10-13, checked against
<https://platform.claude.com/docs/en/about-claude/pricing>. Prices change; a report is only as current as that
date, and the date is printed in every report. Fix the table and send a pull request if it is out of date.

## How it relates to other tools

- Claude Code's built-in `/cost` shows spend for the current session. This tool reads all your sessions,
  including subagents, and explains cache behaviour.
- [ccusage](https://github.com/ryoppippi/ccusage) reports spend totals from the same transcripts. This tool
  is narrower: it is about cache rebuilds and their causes.
- [cachemiss](https://github.com/CedricConday/cachemiss) lists every prompt-cache rebuild with its reason, cost
  and the sub-agent that caused it, and has a live `watch` mode.
- [ctxprof](https://github.com/kraftaa/ctxprof) is a status-line heartbeat with a live dashboard, per-session
  audit and explain commands, and idle-rebuild waste across sessions.
- This tool's focus is the picture across sessions and subagents over weeks: rebuilds by cause, idle
  compactions, and the 1-hour subagent verdict.

## Limitations

- The transcript format is not documented by Anthropic and may change. A format change can make numbers
  wrong or empty; the tool skips lines it cannot read and prints the count.
- Dollar figures are API-equivalent list prices. They are not your bill: a Claude subscription is billed
  differently, and some usage may not be priced the way this table assumes.
- Prices are as of the date in the report. Unknown models are priced at 0.
- Compaction calls are not logged, so their cost is an estimate.
- Files last modified more than a day before the window starts are skipped for speed.
- Timestamps and gaps come from the transcript, not from the API's own clock.

Not affiliated with Anthropic. Claude and Claude Code are Anthropic's products.

## License

MIT
