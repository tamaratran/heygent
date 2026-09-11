"""Markdown out, words in.

Workers answer in Markdown - bold, headings, code spans, bullets - and
those answers become notification bodies, card status lines and spoken
sentences. None of those places renders Markdown: the overlay draws a
plain string, and the voice reads the characters it is given. So "**No
- nothing mentioning FFX has landed.**" reached the user with the
asterisks on, 36 times in 171 notifications. The markers are removed at
the source, once, before the text is stored or said.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^[ \t]*```[^\n]*\n?", re.M)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_STRONG = re.compile(r"(?<!\w)(\*\*|__)(?=\S)(.+?)(?<=\S)\1(?!\w)", re.S)
_EM = re.compile(r"(?<!\w)([*_])(?=[^\s*_])(.+?)(?<=[^\s*_])\1(?!\w)", re.S)
_CODE = re.compile(r"`([^`\n]*)`")


def plain_text(text: str) -> str:
    """The words of a Markdown fragment, without its markers.

    Structure that reads aloud is kept: line breaks and numbered lists.
    Only what exists for a renderer goes: fences, heading marks, bullet
    marks, link targets, emphasis and code delimiters. Text with no
    Markdown in it comes back unchanged.
    """
    if not text:
        return text
    out = _FENCE.sub("", text)
    out = _HEADING.sub("", out)
    out = _BULLET.sub("", out)
    out = _LINK.sub(r"\1", out)
    out = _CODE.sub(r"\1", out)
    for _ in range(2):                    # nested: ***both*** / **`x`**
        out = _STRONG.sub(r"\2", out)
        out = _EM.sub(r"\2", out)
    lines = [ln.rstrip() for ln in out.splitlines()]
    return "\n".join(lines).strip()
