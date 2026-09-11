"""What is said aloud leaves the links on the screen.

The Boss answers with what it found, and what it found is often a link:
"Opened https://github.com/tamaratran/heygent/pull/217". Said aloud,
that is half a minute of letters and slashes before the one thing the
user wanted - which PR. A file path or a commit sha is the same noise.

So the text that reaches the voice says the short human version - "PR
217", "your drafts", "plain_text.py" - and the link itself stays where it
was written: the Boss's window, where it can be clicked. Nothing here
touches what is shown; it runs at the one place speech is made
(VoiceAgent.announce).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .plain_text import plain_text

# A URL runs to whitespace or a quote/bracket; trailing sentence
# punctuation is not part of it.
_URL = re.compile(r"<?\b(?:https?://|www\.)[^\s<>\"'`]+>?", re.I)
_URL_TAIL = ".,;:!?)]}>'\""

# A path: rooted (/, ~/, ./, ../), or relative with a file extension or
# at least two slashes. "and/or", "yes/no" and "9/11" are words, not paths.
_PATH = re.compile(
    r"(?<![\w/.~:@-])"
    r"(?:~/|\.{1,2}/|/)?"
    r"(?:[\w.@+-]+/)+[\w.@+-]*")

# Ids nobody wants to hear: a prefixed id (task_955a1c0c, session_01Ab3...),
# a uuid, a git sha, or any long run of letters and digits.
_PREFIXED_ID = re.compile(
    r"\b([A-Za-z]+)_(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,}\b")
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# Not inside a file name: conduct-console-57615bc.log keeps its sha.
_SHA = re.compile(r"(?<![\w.-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)"
                  r"[0-9a-f]{7,40}(?![\w-]|\.\w)")
_TOKEN = re.compile(r"(?<![\w.-])(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)"
                    r"[A-Za-z0-9]{20,}(?![\w-]|\.\w)")
# Where something was dropped, until _tidy takes it out with the little
# word that pointed at it: "merged as 57615bc" -> "merged".
_GONE = "\x00"
_POINTER = re.compile(r"[ \t]*(?:\b(?:at|in|as|on|see|here)\b)?[ \t]*"
                      r"[:=\u2014-]*[ \t]*\x00", re.I)

# What a prefixed id is called when the sentence needs a noun for it.
_ID_NOUN = {"task": "the task", "proj": "the project", "project": "the project",
            "session": "the session", "run": "the run"}
# A noun just before an id already says what it was: "task task_x" -> "task".
_NAMED_BEFORE = re.compile(
    r"\b(?:task|session|project|commit|sha|id|run|workflow|worker|agent|"
    r"branch|build|job)\s*[:#]?\s*$", re.I)

_GOOGLE_MAIL = {"drafts": "your drafts", "inbox": "your inbox",
                "sent": "your sent mail", "starred": "your starred mail"}
_GOOGLE_DOCS = {"document": "a Google Doc", "spreadsheets": "a Google Sheet",
                "presentation": "a Google Slides deck",
                "forms": "a Google Form"}


def spoken_text(text: str) -> str:
    """text as it should be said: links, paths and ids in few words.

    Markdown is removed first (plain_text), so a [label](url) is its
    label. A link or path wrapped in parentheses after the thing it
    points to - "PR 217 (https://...)" - is dropped, since the words
    before it already name it. Text with none of these comes back as
    plain_text left it.
    """
    if not text:
        return text
    plain = plain_text(text)
    out = _replace_urls(_without_link_lines(plain))
    out = _replace_paths(out)
    out = _UUID.sub(_drop_id, out)
    out = _PREFIXED_ID.sub(_prefixed_id, out)
    out = _SHA.sub(_drop_id, out)
    out = _TOKEN.sub(_drop_id, out)
    return plain if out == plain else _tidy(out)


# -- links ------------------------------------------------------------------
_LINK_LINE = re.compile(r"[ \t]*(?:[\w ]{1,24}:[ \t]*)?<?(?:https?://|www\.)"
                        r"[^\s<>]+>?[ \t]*", re.I)


def _without_link_lines(text: str) -> str:
    """A link on a line of its own is there to be clicked, not heard:
    the sentence above it already said what it is."""
    lines = text.splitlines()
    kept = [ln for ln in lines if not _LINK_LINE.fullmatch(ln)]
    if not any(ln.strip() for ln in kept):
        return text                     # the link is all there is
    return "\n".join(kept) if len(kept) < len(lines) else text


def _replace_urls(text: str) -> str:
    def one(match: re.Match) -> str:
        raw = match.group(0)
        url = raw.strip("<>")
        tail = ""
        while url and url[-1] in _URL_TAIL:
            # A ) that closes a ( inside the URL belongs to it.
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            tail = url[-1] + tail
            url = url[:-1]
        name = url_name(url)
        before = text[:match.start()]
        if _already_named(before, name):
            return _GONE + tail
        return name + tail
    return _URL.sub(one, text)


def url_name(url: str) -> str:
    """The few words a link is called out loud."""
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "a link"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segments = [s for s in parts.path.split("/") if s]
    fragment = parts.fragment.lower()

    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return "a local page"
    if host == "github.com" and len(segments) >= 2:
        repo = segments[1]
        kind = segments[2] if len(segments) > 2 else ""
        number = segments[3] if len(segments) > 3 else ""
        if kind == "pull" and number.isdigit():
            return f"PR {number}"
        if kind == "issues" and number.isdigit():
            return f"issue {number}"
        if kind in ("commit", "commits"):
            return "the commit"
        if kind in ("blob", "tree") and len(segments) > 4:
            return segments[-1]
        if kind == "actions":
            return "the GitHub Actions run"
        return f"the {repo} repo"
    if host.endswith("gitlab.com") and "merge_requests" in segments:
        number = segments[segments.index("merge_requests") + 1:][:1]
        if number and number[0].isdigit():
            return f"merge request {number[0]}"
    if host == "mail.google.com":
        folder = fragment.split("/")[0]
        return _GOOGLE_MAIL.get(folder, "your Gmail")
    if host == "docs.google.com" and segments:
        return _GOOGLE_DOCS.get(segments[0], "a Google Doc")
    if host == "drive.google.com":
        return "your Google Drive"
    if host == "calendar.google.com":
        return "your calendar"
    if host == "linear.app":
        issue = next((s for s in segments
                      if re.fullmatch(r"[A-Za-z]+-\d+", s)), "")
        return f"Linear {issue.upper()}" if issue else "Linear"
    if host.endswith("atlassian.net"):
        issue = next((s for s in segments
                      if re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", s)), "")
        return f"ticket {issue}" if issue else "Jira"
    return f"a link on {_site(host)}" if host else "a link"


def _site(host: str) -> str:
    """example.com for docs.api.example.com; bbc.co.uk stays whole."""
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net",
                                           "gov", "ac", "edu"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# -- paths --------------------------------------------------------------------
def _replace_paths(text: str) -> str:
    def one(match: re.Match) -> str:
        path = match.group(0)
        if not _is_path(path):
            return path
        tail = ""
        while path and path[-1] in ".,;:":
            tail = path[-1] + tail
            path = path[:-1]
        name = path_name(path)
        if _already_named(text[:match.start()], name):
            return _GONE + tail
        return name + tail
    return _PATH.sub(one, text)


def _is_path(candidate: str) -> bool:
    body = candidate.rstrip(".,;:")
    if body.startswith(("/", "~/", "./", "../")):
        return len(body) > 1
    if re.fullmatch(r"[\d/.-]+", body):       # 9/11/2026, 3/4
        return False
    last = body.rstrip("/").rsplit("/", 1)[-1]
    if _PREFIXED_ID.search(body) or _SHA.search(body):  # agent/task_955a1c0c
        return True
    return body.count("/") >= 2 or bool(re.search(r"\.[A-Za-z]\w{0,5}$", last))


def path_name(path: str) -> str:
    """A path out loud is its last part: the file, or the folder."""
    stripped = path.rstrip("/")
    if stripped in ("~", ""):
        return "your home folder"
    return stripped.rsplit("/", 1)[-1]


# -- ids ----------------------------------------------------------------------
def _prefixed_id(match: re.Match) -> str:
    before = match.string[:match.start()]
    if _NAMED_BEFORE.search(before) or before.rstrip().endswith("("):
        return _GONE
    return _ID_NOUN.get(match.group(1).lower(), _GONE)


def _drop_id(match: re.Match) -> str:
    return _GONE


# -- tidying ------------------------------------------------------------------
def _already_named(before: str, name: str) -> bool:
    """The words just before a link already say what it is: "PR #217
    (https://.../pull/217)", "Opened plain_text.py at conductor/plain_text.py".
    """
    near = re.sub(r"[#\s]+", " ", before[-60:]).lower()
    said = re.sub(r"[#\s]+", " ", name).lower().strip()
    return bool(said) and not said.startswith(("a ", "the ", "your ")) \
        and said in near


def _tidy(text: str) -> str:
    out = _POINTER.sub("", text)
    out = re.sub(r"\(\s*[,;:]?\s*\)", "", out)           # emptied parentheses
    out = re.sub(r"\[\s*\]", "", out)
    out = re.sub(r"[ \t]*\((?:task|session|branch|commit|id)\)", "", out,
                 flags=re.I)                             # "(branch)" said nothing
    out = re.sub(r"[ \t]*[:=-]+[ \t]*(?=[.,;!?]|$)", "", out,
                 flags=re.M)                             # "at: ." -> "at."
    out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)        # "done ." -> "done."
    out = re.sub(r"([.,;:!?])(?:[ \t]*\1)+", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    lines = [ln.strip() for ln in out.splitlines()]
    return "\n".join(lines).strip()
