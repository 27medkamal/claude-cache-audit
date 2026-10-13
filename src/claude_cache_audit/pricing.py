"""API list prices, $ per million tokens.

Cache writes are derived: 5 minute writes cost 1.25x the input price and 1 hour
writes cost 2x. Fast mode doubles the price, but only for the models the pricing page lists it for. Models that are not in the table
are priced at 0 and reported, never guessed.
"""

from __future__ import annotations

AS_OF = "2026-10-13"
SOURCE_URL = "https://platform.claude.com/docs/en/about-claude/pricing"

WRITE_5M = 1.25
WRITE_1H = 2.0
FAST_MULTIPLIER = 2

# (model id prefix, (input, output, cache read)). The first matching prefix wins, so
# more specific prefixes come before the ones they extend.
TABLE = [
    ("claude-opus-5-5", (4, 20, 0.20)),
    ("claude-opus-5", (5, 25, 0.50)),
    ("claude-opus-4-1", (15, 75, 1.5)),
    ("claude-opus-4-0", (15, 75, 1.5)),  # alias of Opus 4
    ("claude-opus-4-2025", (15, 75, 1.5)),  # Opus 4 (retired), dated ids only
    ("claude-opus-4", (5, 25, 0.50)),  # Opus 4.5 to 4.8
    ("claude-sonnet-5-5", (2, 10, 0.10)),
    ("claude-sonnet-5", (2, 10, 0.20)),
    ("claude-sonnet-4", (3, 15, 0.30)),
    ("claude-fable-5-1", (10, 50, 0.25)),
    ("claude-fable-5", (10, 50, 1.00)),
    ("claude-mythos-5-1", (10, 50, 0.25)),
    ("claude-mythos-5", (10, 50, 1.00)),
    ("claude-haiku-5-5", (0.10, 0.50, 0.01)),
    ("claude-haiku-4", (1, 5, 0.10)),
    ("claude-3-5-haiku", (0.80, 4, 0.08)),
]

# Fast mode is listed for Opus 5.5, Opus 5 and Opus 4.8 only; other models bill at standard rates.
FAST_MODELS = ("claude-opus-5", "claude-opus-4-8")

HAIKU_5_5 = "claude-haiku-5-5"
HAIKU_5_5_LONG_THRESHOLD = 100_000
HAIKU_5_5_LONG = (0.50, 2.50, 0.05)

# Model family names for the report, most specific first.
FAMILIES = (
    "opus-5-5", "sonnet-5-5", "fable-5-1", "haiku-5-5", "mythos-5-1", "opus-5", "sonnet-5",
    "fable-5", "mythos-5", "opus-4", "sonnet-4", "haiku-4", "3-5-haiku",
)


def price(model, prompt_total, speed):
    """Return (input, output, cache read) in $/MTok, or None for an unknown model."""
    m = model or ""
    for prefix, p in TABLE:
        if m.startswith(prefix):
            pin, pout, pcr = p
            if prefix == HAIKU_5_5 and prompt_total > HAIKU_5_5_LONG_THRESHOLD:
                pin, pout, pcr = HAIKU_5_5_LONG
            k = FAST_MULTIPLIER if speed == "fast" and m.startswith(FAST_MODELS) else 1
            return pin * k, pout * k, pcr * k
    return None


def family(model):
    m = model or "?"
    for f in FAMILIES:
        if "claude-" + f in m:
            return f
    return m
