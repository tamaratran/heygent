# cmux surface spike

The engineering spec asks (§39) for twelve operations to be proved end to
end before cmux replaces the current worker surface. This is what happened
when they were, against **cmux 0.64.22** on macOS.

Short version: the primitives are there and they are better than what we
drive today. Two things bite immediately, one of them dangerous.

## Setup, which is not optional

cmux ships `automation.socketControlMode: "cmuxOnly"` — only processes
started *inside* cmux may use the socket. A Conductor running outside it is
refused:

```
ERROR: Access denied - only processes started inside cmux can connect
```

§28 says configure this deliberately rather than opening it up, so the
setting used here is **`"password"`** with a generated `socketPassword`,
not `allowAll`. That grants us access without handing cmux control to every
process on the machine.

```jsonc
// ~/.config/cmux/cmux.json      (back it up first; cmux says so too)
"automation": {
  "socketControlMode": "password",
  "socketPassword": "…"
}
```
then `cmux reload-config`. The password lives at
`~/.config/cmux/.voice-agent-socket-password`, mode 0600.

Second: **the socket exists only while cmux is running.** There is no
daemon. "Is cmux available" is a real question — §27's fallback is not
hypothetical.

## What was proved

| § | operation | result |
| --- | --- | --- |
| 39.1 | detect + ping | `PONG`, `access_mode: password` |
| 39.2-3 | create workspace with name and cwd | `workspace:2` |
| 39.5 | stable ids | `9DA832B9-…` / surface `E1260737-…` |
| 39.4 | launch Claude Code **inside** the surface | `Opus 5 · Claude Pro` |
| 39.6 | send initial task input | `⏺ RUNNING IN CMUX` |
| 39.8 | follow-up to the same surface | `⏺ 42`, still one workspace |
| 39.7 | focus that exact workspace | `OK workspace:2` |
| 39.9 | surface health | `surface:2 type=terminal in_window=true` |
| 39.12 | three concurrent, targeted by id | each answered only in its own |

The concurrency test is the one that matters most, because it is the
invariant the whole spec turns on:

```
CA2E1308 (Alpha) → I AM Alpha
1C0CB6FA (Beta)  → I AM Beta
B5F198F7 (Gamma) → I AM Gamma
```

Addressed by surface UUID, never by "whichever is focused".

## §18: verified, and it contradicts the spec

Closing a cmux workspace **kills the provider**. Measured by identifying
the process through its working directory rather than a name pattern:

```
provider pid: 21780
alive before close: True
alive AFTER close : False
```

§18 says "surface loss must not mean provider loss". That cannot hold
while §1 does. If the Claude Code process genuinely runs *inside* cmux -
which §1 requires, and which is the whole point - then the surface is its
host, and closing the host ends it. tmux behaves identically; this is not
a cmux shortcoming.

The reconciliation is that a **session** outlives its **process**. The
provider session id survives, and `claude --resume <id>` recovers the
conversation, so surface loss costs a process rather than the work. §18 is
achievable if recovery means resume-by-session-id, and not achievable if it
means reattach-to-process.

That distinction should be written into the spec before `CmuxSurface` is
built, because §16-§21's recovery model reads as though the process can be
recovered, and it cannot.

### §39.11: resume into a recreated surface — works

```
1. original surface  ->  session bf9e6542-…, told it PLATYPUS-7731
2. close workspace   ->  process killed (§18)
3. NEW workspace     ->  claude --resume bf9e6542-…
4. "what was the codeword?"  ->  PLATYPUS-7731
```

Different workspace, different surface, dead process, conversation intact.

### The rule the spec should state

> **Surface loss costs a process, never the work.**

§18's intent holds; its mechanism does not. The provider *process* cannot
outlive its surface — §1 guarantees that, by requiring the process to run
inside cmux. The provider *session* outlives everything, and resuming it
into a recreated surface restores the worker.

Three consequences for the recovery model in §16-§21:

- `CmuxSurface.recover()` is `create workspace -> claude --resume <id>`.
  There is nothing to reattach to.
- "surface recovery" and "provider recovery" are the same operation here,
  where §17-§18 treat them as separate.
- §20 allows provider creation only for `new_task` or
  `confirmed_missing_session_recovery`. After a closed workspace the
  process is *always* missing, so that reason would fire constantly. The
  gate that actually distinguishes a recovery from a duplicate is whether
  the **session id still resumes** — not whether a process is running.

## The dangerous finding (fixed)

Claude Code's trust dialog appears in cmux with the options **reversed**:

```
in a tmux pane          in a cmux surface
❯ 1. Yes, I trust…      ❯ No, exit
  2. No, exit             Yes, I trust this folder
```

`TmuxClaudeRuntime.unstick()` answered boot dialogs by sending bare
`Enter`. In cmux that selects **No, exit** — the watchdog built to unstick
workers would have killed them.

Fixed: dialogs are now answered by finding the wanted option and moving to
it. `choose_option()` returns how many `Down` presses reach it, `0` when it
is already selected, and `-1` when it is not on screen — in which case
nothing is pressed at all, because a worker that waits can be rescued and
one told to exit cannot.

## Notes for the implementation

- `new-workspace`, `list-workspaces`, `select-workspace`, `close-workspace`
  are aliases of `cmux workspace …`; both work, and `CMUX_QUIET=1` silences
  the notice.
- `--id-format both` is what yields UUIDs; the default prints only refs
  like `workspace:2`, which are positional and **not stable**.
- `read-screen` needs `--scrollback` to see anything above the fold.
- `surface-health` reports `type` and `in_window`, which maps onto the
  spec's `surfaceHealth` without inventing anything.
- cmux has its own notification and status APIs (`notify`, `set-status`,
  `set-progress`). §23 says not to let those become a second global
  notification system; they are useful as local cues only.


## Parity, measured against the migration spec

The spec's requirement is that cmux change the terminal UI and nothing
else. Same worker, two hosts, same probe:

| | tmux | cmux |
| --- | --- | --- |
| `$SHELL`, `$HOME` | yes | yes |
| git identity | yes | yes |
| node / PATH | yes | yes |
| auto mode on | yes | yes |

§43, which the spec calls a main benefit and asks for explicitly - the
Conductor restarting without disturbing running workers:

```
1. worker running, workspaces for it: 1
2. a brand new runtime, empty maps: places={}
3. it finds the existing workspace: True
4. same conversation: True          (QUOKKA-5150 recalled)
5. new workspaces created: 0
```

A fresh runtime re-resolves workspaces by the session name it gave them,
so restarting the app finds the workers rather than replacing them.

## Closed since the spike

**§18 / §42 - input arbitration.** send-keys APPENDS to the input box and
then presses Enter, so a follow-up arriving while somebody is mid-sentence
submits their unfinished words welded to ours - one prompt neither of us
wrote, with the half they were editing gone. `_wait_for_input` now treats a
non-empty input box as "not ready" and holds the message until it clears,
and says who is typing if it never does rather than reporting a wedged
worker. An empty box is the prompt character followed by U+00A0, measured
off a live worker; the provider's own placeholder sits in the same place
and does not count as input.

The remaining race is small and real: the box can be empty when we look
and have a character in it 300ms later, when we type. Closing it would need
an input lock cmux does not offer.

**§15 - personal workspaces.** Every workspace we create is stamped
`conductor-managed agent session`, and nothing without that mark is driven
- a personal workspace the user happened to name `cond_task_*` used to be
adopted, typed into, and eventually closed under them. One exception, kept
deliberately: a workspace running inside the Conductor's own workspaces
directory is ours even without the mark, because refusing those would
orphan live workers across the upgrade, and an orphaned worker is worse
than a loose rule - the parent calls the session gone and resumes it
somewhere else, which is two processes on one conversation.

**§32 - cmux restarting.** Cached workspace uuids are verified against
cmux before they are trusted. After a restart every uuid we remember
belongs to a workspace that no longer exists, so a stale cache would have
answered `has-session` with yes and sent follow-ups into nowhere. Now the
cache entry is dropped, the workspace is re-resolved by the title we
assigned (the only identity that survives), and a worker cmux is no longer
showing reads as gone - which is what triggers recovery, and recovery
resumes the provider session rather than starting new work.

## Not covered, and worth knowing

**§16 - grouping managed sessions in the sidebar.** cmux has real
workspace groups and the socket API cannot create one: `--group` demands an
existing group id ("Error: invalid_params: Missing or invalid group_id"),
and `workspace-action` offers pin/rename/set-description/set-color and
nothing about groups. Managed workspaces all get one colour instead, so
they read as a set; grouping proper stays something the user does by hand.

**§32, the destructive half - now measured.** cmux was terminated with
three workers running. All three died with it (pids 38609, 43223, 47921),
so cmux dying takes its worker processes with it, as inferred. Two things
the measurement changed:

  - the app leaves its socket FILE behind, so a dead cmux answers
    "Failed to connect to socket ... (Connection refused, errno 61)" and
    NOT "Socket not found" - the only string the callers recognised. A
    dead app was reported as an ordinary failed command.
  - the runtime side was already right: the stale cache was dropped, the
    lookup returned None, and has-session answered 1, which is what
    triggers recovery.

An AppleScript quit timed out against a busy cmux; SIGTERM worked. Nothing
depends on which one is used.
