"""Names a finding's text mentions (LLM Visualizer): the chips under its message.

An LLM writes "relies on C11 at U2 pin 13", "via 420d87bb" or "the /IB_ADC net" in plain
text. Each such name that is a part, a pad, a net or an item's uuid on the board becomes a
mention with the ids KiCad selects when it is clicked, and what it is ("via, GND"). A
uuid-like code that names nothing on the board is listed too, as not found: a quick sign
the LLM made it up. Parts and nets that are not on the board are left out, as words like
STM32 or PA7 look the same.
"""

from __future__ import annotations

import re

from . import model
from .board_index import SHORT_ID, BoardIndex
from .targets import kind

MAX_MENTIONS = 12  # chips per finding
MAX_IDS = 2_000  # a net's items selected, at most
MIN_NET = 2  # characters: shorter net names ("A") are words too often
_BEFORE = r"A-Za-z0-9_+\-/{}~."  # a net name is whole when no such character runs into it
_AFTER = r"A-Za-z0-9_+\-/{}~"  # after it, a "." or ")" ends the sentence or the bracket
_HEX = re.compile(r"(?<![\w-])(uuid:\s*)?([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{8,32})(?![\w-])")
_PART = re.compile(r"(?<![\w.:/-])([A-Z][A-Za-z_]*\d+[A-Za-z]?)(?:(?:\.| pin | pad )([A-Za-z]{0,2}\d+|EP))?(?![\w-])")


def _net_spans(index: BoardIndex, text: str) -> list[tuple[int, str]]:
    """(start, net) of every net named whole in `text` (a "net:" in front is fine)."""
    found = []
    for net in index.nets:
        if len(net) < MIN_NET or net.isdigit() or net not in text:
            continue
        pattern = rf"(?<![{_BEFORE}]){re.escape(net)}(?![{_AFTER}])"
        found += [(match.start(), net) for match in re.finditer(pattern, text)]
    return found


def _what(record) -> str:
    net = getattr(record, "net", "")
    return f"{kind(record)}, {net}" if net else kind(record)


def find(index: BoardIndex | None, *texts: str) -> list[dict]:
    """The mentions in `texts`, in the order they come, each name once: text (as shown),
    kind (part, pad, net, via, ...), what (a short description), ids, found."""
    if index is None:
        return []
    text = "\n".join(t for t in texts if t)
    spans: list[tuple[int, int, dict]] = []  # (start, end, mention)
    for match in _HEX.finditer(text):
        code = match.group(2)
        ids = index.ids_ending(code) if len(code) >= SHORT_ID else ()
        has_letter, has_digit = any(c.isalpha() for c in code), any(c.isdigit() for c in code)
        if len(ids) == 1:
            records = index.records(ids[0])
            spans.append((match.start(), match.end(), {"text": code.lower(), "kind": kind(records[0]),
                                                       "what": _what(records[0]), "ids": [ids[0]], "found": True}))
        elif match.group(1) or (has_letter and has_digit):
            note = f"{len(ids)} items end in it" if ids else "not on the board"
            spans.append((match.start(), match.end(), {"text": code.lower(), "kind": "", "what": note,
                                                       "ids": [], "found": False}))
    for match in _PART.finditer(text):
        reference, number = match.group(1), match.group(2)
        parts = index.footprints(reference)
        if not parts:
            continue
        pads = index.pads(reference, number) if number else ()
        if pads:
            net = pads[0].net
            spans.append((match.start(), match.end(), {
                "text": f"{reference}.{number}", "kind": "pad",
                "what": ", ".join(filter(None, ("pad", pads[0].pin_name, net))),
                "ids": [pad.id for pad in pads], "found": True}))
        else:
            spans.append((match.start(1), match.end(1), {
                "text": reference, "kind": "part", "what": ", ".join(filter(None, ("part", parts[0].value))),
                "ids": [part.id for part in parts], "found": True}))
    for start, net in _net_spans(index, text):
        items = [record for record in index.net_items(net) if not isinstance(record, model.Outline)]
        spans.append((start, start + len(net), {"text": net, "kind": "net", "what": f"net, {len(items):,} items",
                                                "ids": list(dict.fromkeys(r.id for r in items))[:MAX_IDS],
                                                "found": True}))
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    mentions, seen, end = [], set(), -1
    for start, stop, mention in spans:
        if start < end:  # inside a longer name already taken ("U2" in "U2.13", a net in a pad's)
            continue
        end = stop
        key = (mention["kind"], mention["text"])
        if key in seen:
            continue
        seen.add(key)
        mentions.append(mention)
    return mentions[:MAX_MENTIONS]
