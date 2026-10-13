"""Synthetic Claude Code transcripts, built in code. Nothing here reads the real ~/.claude."""

import _path  # noqa: F401
import datetime as dt
import json
import os
import tempfile
import unittest

from claude_cache_audit import analysis, transcripts

BASE = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()
SESSION = "0123abcd-4567-89ef-0123-456789abcdef"


def iso(offset):
    t = dt.datetime.fromtimestamp(BASE + offset, dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def asst(t, mid, model="claude-opus-5", inp=0, cr=0, cw=0, cw1h=None, cw5m=None, out=10, stop="end_turn",
         tools=(), speed=None, req=None, cwd="/home/someone/secretproj", version="2.1.296", think=0,
         session=SESSION, text="SECRETPROMPT"):
    usage = {"input_tokens": inp, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw,
             "output_tokens": out}
    if cw1h is not None or cw5m is not None:
        usage["cache_creation"] = {"ephemeral_1h_input_tokens": cw1h or 0, "ephemeral_5m_input_tokens": cw5m or 0}
    if speed:
        usage["speed"] = speed
    if think:
        usage["output_tokens_details"] = {"thinking_tokens": think}
    content = [{"type": "text", "text": text}]
    for name in tools:
        content.append({"type": "tool_use", "name": name,
                        "input": {"command": "rm -rf SECRETCMD", "timeout": 1}})
    return {"type": "assistant", "timestamp": iso(t), "sessionId": session, "cwd": cwd, "version": version,
            "requestId": req or "req_" + mid,
            "message": {"id": mid, "model": model, "usage": usage, "stop_reason": stop, "content": content}}


def boundary(t, trigger="auto", pre=200000, post=10000):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": iso(t), "version": "2.1.296",
            "compactMetadata": {"trigger": trigger, "preTokens": pre, "postTokens": post}}


def idle_marker(t):
    return {"type": "system", "subtype": "informational", "timestamp": iso(t), "version": "2.1.296",
            "content": "Compacted while idle, before the prompt cache expired"}


def user(t, text="SECRETPROMPT"):
    return {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": text}}


class Env(unittest.TestCase):
    """A temporary Claude config dir with a projects tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.config = os.path.join(self.root, "claude")
        os.makedirs(os.path.join(self.config, "projects", "-home-someone-secretproj"))

    def proj(self, *parts):
        return os.path.join(self.config, "projects", "-home-someone-secretproj", *parts)

    def write(self, path, lines):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            for ln in lines:
                f.write(ln if isinstance(ln, str) else json.dumps(ln))
                f.write("\n")
        return path

    def main_file(self, lines, session=SESSION):
        return self.write(self.proj(session + ".jsonl"), lines)

    def sub_file(self, lines, agent="agent-a1", session=SESSION, meta=None):
        p = self.write(self.proj(session, "subagents", agent + ".jsonl"), lines)
        if meta is not None:
            with open(p[:-6] + ".meta.json", "w") as f:
                json.dump(meta, f)
        return p

    def scan(self, since=None):
        return transcripts.scan_transcripts(transcripts.dedupe_dirs([self.config]), since=since)

    def compute(self, since=None, until=None, scan_since=None):
        return analysis.compute(self.scan(scan_since), since=since, until=until, now=BASE + 10 * 86400)

    def rebuilds_by_cause(self, rebuilds):
        return {b.cause: b for b in rebuilds}


def pair(first_t=0, gap=600, model2="claude-opus-5", prev_stop="end_turn", prev_tools=(), prev_1h=False,
         cw=100000, cr2=0, second_cw=100000):
    """Two requests of one main session: a cold start, then a request that rewrites the prompt."""
    a = asst(first_t, "m1", cw=cw, cw1h=cw if prev_1h else 0, cw5m=0 if prev_1h else cw, stop=prev_stop,
             tools=prev_tools)
    b = asst(first_t + gap, "m2", model=model2, cr=cr2, cw=second_cw, cw5m=second_cw, inp=1)
    return [a, b]
