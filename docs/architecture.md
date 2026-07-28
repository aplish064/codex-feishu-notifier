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

## State

The private state directory contains:

```text
config.env       local paths, recipient and notification switches
lark-home/       official CLI credentials and tokens
sessions/        current terminal and card metadata
outbox/          retryable notification events
sent/            delivered event markers
logs/worker.log  background worker errors
```

Outbox filenames are deterministic hashes. Delivery is retried with capped
exponential backoff. Lifecycle alert nodes are idempotent per turn/Goal and
status so worker restarts do not intentionally duplicate urgent notifications.

## Goal support

Goal support is optional and activates only when:

- `CODEX_TASK_GOALS_DB` exists;
- its `thread_goals` table matches the active rollout thread; and
- the row includes the expected lifecycle columns.

If no Goal database or matching row exists, the notifier safely falls back to
one card per turn.

## Data sent to Feishu

Cards may contain:

- an organized goal derived from Codex's first public commentary;
- public progress commentary;
- generic tool categories such as "running command" or "editing files";
- result summary, elapsed time, terminal label, project and working directory.

Cards do not contain hidden model reasoning, raw tool arguments, shell command
contents, tokens, App Secrets, or raw prompts as task titles.
