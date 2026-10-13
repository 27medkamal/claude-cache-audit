"""Find, parse and dedupe Claude Code transcripts.

Only usage numbers, model names, timestamps, stop reasons and tool names are kept.
Prompt text, tool inputs and file contents are never stored. A few booleans derived
from a Bash call's input (run in background, long timeout) are folded into the tool
label, and the input itself is dropped.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import sys
import time

IDLE_TEXT = "Compacted while idle"
IDLE_MATCH_SECONDS = 120
LONG_TIMEOUT_MS = 300_000

_FRACTION = re.compile(r"\.(\d+)")


def parse_ts(s):
    """ISO-8601 timestamp to epoch seconds, or None."""
    if not s or not isinstance(s, str):
        return None
    s = s.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        pass
    try:  # Python 3.9 only takes 3 or 6 fractional digits
        s2 = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        return dt.datetime.fromisoformat(s2).timestamp()
    except ValueError:
        return None


class Req:
    """One API request."""

    __slots__ = (
        "owner", "agent_file", "project", "is_sub", "ts", "mid", "model", "speed", "version",
        "session", "cwd", "inp", "cr", "cw", "cw1h", "cw5m", "out", "think", "stop", "tools",
        # filled in by analysis
        "pt", "c_in", "c_cw5m", "c_cw1h", "c_cw", "c_cr", "c_out", "cost", "fam", "kind",
        "agent_type", "prev", "unknown_model", "price_in", "price_cr", "price_out",
    )

    def __init__(self):
        self.tools = []
        self.prev = None
        self.unknown_model = False

    @property
    def key(self):
        return (self.owner, self.agent_file)


class Compaction:
    __slots__ = ("owner", "agent_file", "project", "ts", "trigger", "pre", "post", "version",
                 "idle", "kind", "session", "cwd")

    @property
    def key(self):
        return (self.owner, self.agent_file)


class Scan:
    def __init__(self):
        self.requests = []
        self.compactions = []
        self.meta = {}  # (owner, agent_file) -> {'agentType':..., 'taskKind':...}
        self.files = 0
        self.main_files = 0
        self.sub_files = 0
        self.malformed = 0
        self.config_dirs = []
        self.skipped_old = 0


# ---------------------------------------------------------------------------- discovery

def default_config_dirs(env=None, home=None):
    env = os.environ if env is None else env
    home = os.path.expanduser("~") if home is None else home
    dirs = []
    if env.get("CLAUDE_CONFIG_DIR"):
        dirs.append(os.path.expanduser(env["CLAUDE_CONFIG_DIR"]))
    dirs.append(os.path.join(home, ".claude"))
    for p in sorted(glob.glob(os.path.join(glob.escape(home), ".claude-*"))):
        dirs.append(p)
    return [d for d in dirs if os.path.isdir(os.path.join(d, "projects"))]


def dedupe_dirs(dirs):
    seen, out = set(), []
    for d in dirs:
        real = os.path.realpath(d)
        if real in seen:
            continue
        seen.add(real)
        out.append(d)
    return out


def find_files(config_dirs):
    """Return [(path, projects_root)] for every transcript, deduped by realpath."""
    seen, out = set(), []
    for d in dedupe_dirs(config_dirs):
        root = os.path.join(d, "projects")
        for path in glob.glob(os.path.join(glob.escape(root), "**", "*.jsonl"), recursive=True):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            out.append((path, root))
    # subagent files first, so a request id shared with the parent goes to the subagent
    out.sort(key=lambda pr: (0 if "/subagents/" in pr[0] else 1, pr[0]))
    return out


# ---------------------------------------------------------------------------- parsing

def _tool_label(c):
    name = c.get("name") or "?"
    if name.startswith("mcp__"):
        return "mcp"  # the full name carries the server name
    if name == "Bash":
        inp = c.get("input") if isinstance(c.get("input"), dict) else {}
        if inp.get("run_in_background"):
            name += ":bg"
        try:
            if float(inp.get("timeout") or 0) > LONG_TIMEOUT_MS:
                name += ":long-timeout"
        except (TypeError, ValueError):
            pass
    return name


def _read_meta(path):
    meta = path[:-6] + ".meta.json"
    try:
        with open(meta) as f:
            m = json.load(f)
        return {"agentType": m.get("agentType"), "taskKind": m.get("taskKind")}
    except (OSError, ValueError, AttributeError):
        return None


def parse_file(path, root, scan, seen):
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    project = parts[0]
    is_sub = "subagents" in parts[:-1]
    agent_file = os.path.basename(path)[:-6] if is_sub else None
    owner = parts[1] if is_sub and len(parts) > 1 else os.path.basename(path)[:-6]
    if is_sub:
        m = _read_meta(path)
        if m is not None:
            scan.meta[(owner, agent_file)] = m
    per_file = {}
    compactions = []
    idles = []
    with open(path, errors="replace") as f:
        for line in f:
            if '"assistant"' not in line and "compact_boundary" not in line and IDLE_TEXT not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                scan.malformed += 1
                continue
            if not isinstance(e, dict):
                scan.malformed += 1
                continue
            t = e.get("type")
            if t == "system":
                sub = e.get("subtype")
                if sub == "compact_boundary":
                    cm = e.get("compactMetadata") or {}
                    c = Compaction()
                    c.owner, c.agent_file, c.project = owner, agent_file, project
                    c.ts = parse_ts(e.get("timestamp"))
                    c.trigger = cm.get("trigger")
                    c.pre, c.post = cm.get("preTokens"), cm.get("postTokens")
                    c.version = e.get("version")
                    c.session, c.cwd = e.get("sessionId"), e.get("cwd")
                    c.idle = False
                    compactions.append(c)
                elif sub == "informational":
                    content = e.get("content")
                    if isinstance(content, str) and content.startswith(IDLE_TEXT):
                        idles.append((parse_ts(e.get("timestamp")), e))
                continue
            if t != "assistant":
                continue
            msg = e.get("message") or {}
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if not u:
                continue
            mid = msg.get("id") or e.get("uuid")
            key = (mid, e.get("requestId"))
            content = msg.get("content")
            if not isinstance(content, list):
                content = []
            rec = per_file.get(key)
            if rec is None:
                rec = Req()
                rec.owner, rec.agent_file, rec.project, rec.is_sub = owner, agent_file, project, is_sub
                rec.ts = parse_ts(e.get("timestamp"))
                rec.mid = mid
                rec.model = msg.get("model")
                rec.speed = u.get("speed")
                rec.version = e.get("version")
                rec.session, rec.cwd = e.get("sessionId"), e.get("cwd")
                its = u.get("iterations") or []
                cc = u.get("cache_creation") or {}
                rec.inp = u.get("input_tokens") or 0
                rec.cr = u.get("cache_read_input_tokens") or 0
                rec.cw = u.get("cache_creation_input_tokens") or 0
                rec.cw1h = cc.get("ephemeral_1h_input_tokens")
                rec.cw5m = cc.get("ephemeral_5m_input_tokens")
                rec.out = u.get("output_tokens") or 0
                rec.think = (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
                rec.stop = msg.get("stop_reason")
                # the reference adopts the sum of `iterations` when it exceeds the top-level usage
                if its:
                    s = lambda k: sum((x.get(k) or 0) for x in its)  # noqa: E731
                    tin = s("input_tokens")
                    tcr = s("cache_read_input_tokens")
                    tcw = s("cache_creation_input_tokens")
                    tout = s("output_tokens")
                    if tin + tcr + tcw + tout > rec.inp + rec.cr + rec.cw + rec.out:
                        rec.inp, rec.cr, rec.cw, rec.out = tin, tcr, tcw, tout
                        rec.cw1h = sum(((x.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0) for x in its)
                        rec.cw5m = sum(((x.get("cache_creation") or {}).get("ephemeral_5m_input_tokens") or 0) for x in its)
                per_file[key] = rec
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    rec.tools.append(_tool_label(c))
            if msg.get("stop_reason"):
                rec.stop = msg.get("stop_reason")
    # idle markers: attach to the nearest boundary in this transcript, else keep alone
    for ts, e in idles:
        best, bd = None, None
        for c in compactions:
            if c.ts is None or ts is None or c.idle:
                continue
            d = abs(c.ts - ts)
            if d <= IDLE_MATCH_SECONDS and (bd is None or d < bd):
                best, bd = c, d
        if best is not None:
            best.idle = True
        else:
            c = Compaction()
            c.owner, c.agent_file, c.project = owner, agent_file, project
            c.ts, c.trigger, c.pre, c.post = ts, None, None, None
            c.version = e.get("version")
            c.session, c.cwd = e.get("sessionId"), e.get("cwd")
            c.idle = True
            compactions.append(c)
    for c in compactions:
        c.kind = "idle" if c.idle else ("manual" if c.trigger == "manual" else "auto")
    scan.compactions.extend(compactions)
    for key, rec in per_file.items():
        if key in seen:
            continue
        seen.add(key)
        if rec.model in (None, "<synthetic>"):
            continue
        scan.requests.append(rec)
    return is_sub


def scan_transcripts(config_dirs, since=None, progress=None):
    """Read every transcript under the config dirs.

    `since` (epoch seconds) lets files that were last modified long before the window be
    skipped: they cannot hold a request inside it.
    """
    scan = Scan()
    scan.config_dirs = list(config_dirs)
    files = find_files(config_dirs)
    seen = set()
    start = time.time()
    last_report = 0.0
    n = len(files)
    for i, (path, root) in enumerate(files):
        if since is not None:
            try:
                if os.path.getmtime(path) < since - 86400:
                    scan.skipped_old += 1
                    continue
            except OSError:
                continue
        try:
            is_sub = parse_file(path, root, scan, seen)
        except OSError:
            continue
        scan.files += 1
        if is_sub:
            scan.sub_files += 1
        else:
            scan.main_files += 1
        now = time.time()
        if progress and now - start > 2 and now - last_report > 1:
            last_report = now
            progress("scanning transcripts: %d/%d files, %d requests" % (i + 1, n, len(scan.requests)))
    if progress and last_report:
        progress("")
    return scan


def stderr_progress(msg):
    if msg:
        sys.stderr.write("\r" + msg[:90].ljust(90))
    else:
        sys.stderr.write("\r" + " " * 90 + "\r")
    sys.stderr.flush()
