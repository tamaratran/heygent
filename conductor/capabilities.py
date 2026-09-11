"""What this build can actually do, asked of the running objects.

The Manager is a supervisor, and a supervisor that does not know its own
reach either offers work it cannot do or explains to the user how to do
something by hand that it could have done itself. Both are worse than
saying plainly "Codex is not wired up here".

Everything is probed, never declared: the provider list comes from a real
PATH lookup and the runtime's own methods, so a build without a Codex
binary reports Codex missing without anyone remembering to edit a
constant. A hardcoded snapshot is a lie waiting to happen - it would keep
claiming a capability for exactly as long as it took someone to remove it.
"""

from __future__ import annotations

import shutil

from .task_types import PROVIDERS

# The command each provider is driven through. Presence on PATH is the
# honest test of whether we could start one.
PROVIDER_BINARIES = {"claude-code": ("claude",), "codex": ("codex",),
                     "gemini": ("gemini",),
                     "cursor": ("cursor-agent", "agent")}
PROVIDER_NAMES = {"claude-code": "Claude Code", "codex": "Codex",
                  "gemini": "Gemini CLI", "cursor": "Cursor"}


def all_providers() -> list[str]:
    """The built-in providers, then every CLI providers.json describes."""
    from .configured_adapter import CONFIGURED
    return [*PROVIDERS, *(name for name in CONFIGURED if name not in PROVIDERS)]


def provider_binaries(provider: str) -> tuple[str, ...]:
    if provider in PROVIDER_BINARIES:
        return PROVIDER_BINARIES[provider]
    from .configured_adapter import CONFIGURED, binaries
    return binaries(CONFIGURED[provider]) if provider in CONFIGURED \
        else (provider,)


def provider_label(provider: str) -> str:
    """How the Boss reads a provider in its capability list. A configured
    one carries the name create_task takes, since its display name is
    whatever the user wrote."""
    if provider in PROVIDER_NAMES:
        return PROVIDER_NAMES[provider]
    from .configured_adapter import CONFIGURED
    display = (CONFIGURED.get(provider) or {}).get("display")
    return f"{display} (provider \"{provider}\")" if display \
        else f"provider \"{provider}\""


def _can(obj, *methods: str) -> bool:
    """Whether obj really implements every one of these."""
    if obj is None:
        return False
    return all(callable(getattr(obj, name, None)) for name in methods)


def _provider_state(conductor, provider: str) -> tuple[bool, str]:
    binaries = provider_binaries(provider)
    if not any(shutil.which(binary) for binary in binaries):
        return False, f"no {' or '.join(binaries)} on PATH"
    runtime = getattr(conductor, "runtime", None)
    if not _can(runtime, "create_session"):
        return False, "no runtime to drive it"
    # One runtime drives this build. If it names the providers it handles,
    # believe it; otherwise assume the one it was written for.
    handled = getattr(runtime, "providers", None) or \
        (getattr(runtime, "provider", None) and [runtime.provider]) or \
        ["claude-code"]
    if provider not in handled:
        return False, "runtime does not drive it"
    return True, ""


def computer_state() -> tuple[bool, str]:
    """Whether a worker could drive this machine's GUI right now.

    Probed like everything else - and probed again the moment a computer
    task is created, so a grant made mid-run counts without a restart. A
    missing grant is not the same thing as no backend at all: it is
    something the user can turn on right now, so its reason opens with
    "needs", names the grants, and says where to grant them."""
    from . import computer
    from .gui_permissions import LABELS
    try:
        driver = computer.make_driver()
    except computer.ComputerError as exc:
        return False, str(exc)
    missing = [LABELS.get(name, name)
               for name, granted in driver.permissions().items()
               if not granted]
    if missing:
        return False, (f"needs {' and '.join(missing)} - "
                       f"{driver.remedy}, then ask again")
    return True, ""


def snapshot(conductor) -> dict[str, tuple[bool, str]]:
    """capability -> (available, why not). Probed from live objects."""
    runtime = getattr(conductor, "runtime", None)
    locator = getattr(conductor, "locator", None)
    surfaces = getattr(conductor, "surfaces", None) or {}

    out: dict[str, tuple[bool, str]] = {}
    out["project discovery"] = (
        _can(locator, "search") or _can(locator, "find"),
        "no project locator")
    for provider in all_providers():
        ok, why = _provider_state(conductor, provider)
        out[provider_label(provider)] = (ok, why)
    out["session creation"] = (_can(runtime, "create_session"),
                               "runtime cannot start sessions")
    out["session messaging"] = (_can(runtime, "send"),
                                "runtime cannot deliver messages")
    out["session focus"] = (
        _can(conductor, "focus_task") and bool(surfaces),
        "no visible surface to raise")
    out["approval supervision"] = (
        _can(runtime, "pending_approvals", "resolve_approval"),
        "runtime reports no approvals")
    out["historical session search/resume"] = (
        _can(conductor, "search_sessions") and _can(conductor, "resume_task"),
        "no session history")
    out["computer use (a worker driving this machine's GUI)"] = \
        computer_state()
    return out


def capability_block(conductor) -> str:
    """The snapshot as the Manager reads it, one line each.

    Unavailable entries keep their reason: "not available" alone invites
    the model to retry it, while "not available (no codex on PATH)" is
    something it can tell the user.

    A reason that opens with "needs" is a grant the user can make right
    now, so its line leads with what to do and where, never with "not
    available" - a flat refusal is exactly what the user should not hear
    for something one Settings pane away.
    """
    lines = ["Current capabilities:"]
    for name, (ok, why) in snapshot(conductor).items():
        if ok:
            lines.append(f"- {name}: available")
        elif why.startswith("needs "):
            lines.append(f"- {name}: {why}")
        else:
            lines.append(f"- {name}: not available"
                         f"{f' ({why})' if why else ''}")
    return "\n".join(lines)
