# Routing evals

Does natural language reach the right agent, with the right action?

```
env -u ANTHROPIC_API_KEY python3 -m conductor.evals evals/gold
```

`env -u ANTHROPIC_API_KEY` matters: with a stale key set, the `claude` CLI
takes the API path, gets a 401, and blocks with no TTY to prompt on. Unset,
it uses the subscription login and answers.

## The suites

| file | what it measures |
| --- | --- |
| `gold/routing.json` | new work vs follow-up, constraints, status, steering |
| `gold/routing2.json` | the same, harder |
| `gold/reference.json` | conversational reference resolution |
| `gold/discovery.json` | finding and registering unknown projects |
| `gold/projects.json` | routing across several projects |
| `adversarial/` | confusing corrections, dangerous cancels |

## Read the severity score, not the pass rate

`severity_score` weights failures by what they cost: cancelling the wrong
task is 9, interrupting the wrong one is 7, a needless clarifying question
is 1. Asking is cheap; acting on the wrong agent is not.

The two numbers can move in opposite directions, and when they do the
severity score is the one telling the truth. Defining an ordinal convention
in the prompt took `reference.json` from 21/25 to 23/25 while severity went
**4 → 12**: it converted four safe refusals into three confidently wrong
actions. A second attempt at wording it reached 16. Only moving the
ordinal out of the prompt and into the data - every unfinished task carries
its own `On screen: 2nd of 3` - got 25/25 at severity 0.

The lesson generalises: when the model declines to resolve something,
giving it the fact beats instructing it to derive the fact.

## Nondeterminism

Individual cases flip between runs. `ord-second-stop` clarified in one run
and resolved correctly in the next with no change in between. Treat a
single run as a sample: a one-case difference is noise, and a change worth
keeping should move the severity score across several runs.

## Writing a case

```json
{"name": "ord-second-stop",
 "tasks": [{"task_id": "task_auth", "title": "...", "goal": "...",
            "status": "running"}],
 "user_message": "Tell the second one to stop.",
 "expected": {"action": "interrupt_task", "task_id": "task_dash"}}
```

`expected.action` may be `clarify` - asking is a correct answer when a
reference genuinely matches more than one candidate, and the suite has to
be able to say so, or it will reward a system that always guesses.

Use `also_accept` when several read-only routes answer equally well. "Did
the billing one finish?" is answered by inspecting that task or by
searching sessions for it, and once the task is retired from the open
roster searching is the better route. Pinning one of them measures the
harness's taste rather than the Manager's reference resolution.
`also_accept` never licenses a mutation.

Seed tasks oldest-first; ordinal positions are derived from recency, so the
order you list them in is the stack the user would see.
