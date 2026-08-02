# Architecture and Lifecycle

## Components

- `bin/codex-feishu`: registers a terminal, injects the Codex `notify` hook,
  launches Codex, and records process exit.
- `notifier.py hook`: receives Codex completion notifications.
- background worker: tails Codex rollout JSONL, reconciles Goal state, patches
  cards, manages Pins, and sends urgent nodes.
- official `@larksuite/cli`: authenticates the app and calls Feishu/Lark APIs.

## Task boundaries

A terminal launch is only registration. A task starts at Codex's
`task_started` event after the user submits a prompt.

Normal mode uses one card per `turn_id`. Goal mode reads the optional
`goals_1.sqlite` database and uses one card per `goal_id`; individual automatic
turn completions keep that Goal card running until the database reports a
terminal Goal state. The Codex notify hook also fires at each automatic turn
boundary; while the Goal is active, the notifier converts that hook into a
non-urgent progress update.

The terminal rollout and Goal rollout can be different files. A descendant
terminal thread can resolve to a Goal row through `parent_thread_id`, while the
automatic continuation records are appended to the row's `thread_id` root
rollout. The notifier stores separate offsets for both files and tails the Goal
root while its status is active. Completion hooks from descendant threads are
ignored for Goal lifecycle state.

The long-term Goal objective is retained as lifecycle metadata but does not
overwrite the card's current stage. Each `task_started` event resets the stage
summary and timer, the first public commentary supplies the organized stage
goal, and later public commentary supplies the current step. Stage completion
freezes that turn's duration while the card waits for automatic continuation.
Timer-only refreshes are not sent: Feishu clients surface each card PATCH as
message activity, so elapsed time is refreshed only with real progress or a
lifecycle transition.

## State

The private state directory contains:

```text
config.env       local paths, recipient and notification switches
lark-home/       official CLI credentials and tokens
sessions/        current terminal and card metadata
outbox/          retryable notification events
sent/            delivered event markers
probes/          deduplicated 429 recovery probe state (never API keys)
logs/worker.log  background worker errors
```

Outbox filenames are deterministic hashes. Delivery is retried with capped
exponential backoff. Lifecycle alert nodes are idempotent per turn/Goal and
status so worker restarts do not intentionally duplicate urgent notifications.
When a worker must catch up a large rollout backlog, intermediate lifecycle
records are folded into one final-state delivery rather than replayed to Feishu.

## Goal support

Goal support is optional and activates only when:

- `CODEX_TASK_GOALS_DB` exists;
- its `thread_goals` table matches the active rollout thread; and
- the row includes the expected lifecycle columns.

If no Goal database or matching row exists, the notifier safely falls back to
one card per turn.

## Data sent to Feishu

Cards may contain:

- an organized current-stage goal derived from Codex's first public commentary;
- public progress commentary;
- generic tool categories such as "running command" or "editing files";
- result summary, elapsed time, terminal label, project and working directory.

Cards do not contain hidden model reasoning, raw tool arguments, shell command
contents, App Secrets, API keys, or raw prompts as task titles. Token totals are
read from Codex's public `token_count` events or the Goal database.

## Failure, retention, and recovery

`task_complete` is successful only when its payload has no `error`. HTTP 429 is
rendered as rate-limited, while transport and other request errors are rendered
as interrupted. A Goal database row older than the failure cannot overwrite the
newer failure with a completed state.

Completed cards can optionally be recalled after 24 hours. Cleanup is disabled
by default because recall is irreversible; interrupted, blocked, pinned, and
running cards are never removed by this cleanup.

HTTP 429 starts one persistent probe per provider, base URL, and model. The
probe reloads the Codex API key at request time, never persists it, and sends a
single urgent recovery card after a minimal Responses API request succeeds.
