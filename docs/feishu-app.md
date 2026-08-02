# Feishu/Lark App Setup

This notifier uses a custom app bot to send direct Card 2.0 messages. It does
not require a webhook server or a chatbot conversation handler.

## 1. Create the app

1. Open the [Feishu Open Platform](https://open.feishu.cn/app) or
   [Lark Developer Console](https://open.larksuite.com/app).
2. Create a custom/internal app.
3. Enable the **Bot** capability.
4. Copy the App ID and App Secret from **Credentials & Basic Info**.

Use a dedicated app for this notifier. Do not commit either value.

## 2. Add permissions

Enable these bot scopes in **Permissions & Scopes**:

| Scope | Purpose |
|---|---|
| `im:message:send_as_bot` | Send and patch Card 2.0 messages |
| `im:message.pins:write_only` | Pin running cards and unpin terminal cards |
| `im:message.urgent` | Send in-app urgent notifications to phones |
| `im:message:recall` | Recall superseded cards and cards inactive for two hours |

Depending on tenant policy, the console may offer the broader `im:message`
scope instead of one of the granular scopes. Prefer the granular scope when
available.

No event subscription or callback URL is required.

## 3. Publish and make the app available

1. Create a version and publish it.
2. Set the app availability range to include the recipient.
3. Ask a tenant administrator to approve the app if your organization requires
   review.

An unpublished app or a recipient outside the availability range cannot send a
bot direct message even when the credentials are correct.

## 4. Configure locally

Run:

```bash
./setup.sh
```

The script initializes the official `@larksuite/cli` in an isolated directory.
You can either enter an app-specific user Open ID (`ou_...`) or leave it blank.
When blank, the script opens a Feishu/Lark authorization flow and reads your
Open ID from the resulting user identity.

Open IDs are app-specific. An Open ID copied from another app may not work.

## 5. Validate

At the end of setup, choose **Send a test card now**. A successful setup prints
a message ID beginning with `om_` and sends a completed test card to the target
user.

If validation fails:

```bash
source ~/.local/share/codex-feishu-notifier/config.env
python3 notifier.py doctor
tail -n 100 ~/.local/share/codex-feishu-notifier/logs/worker.log
```

Common causes:

- `missing_scope`: enable the scope from the error's developer-console link,
  publish a new app version, then retry.
- `invalid user`: confirm the `ou_...` belongs to this app and is in its
  availability range.
- urgent fails but cards work: add `im:message.urgent`, republish, and ensure the
  bot itself sent the original card.
- Pin fails: add `im:message.pins:write_only` and republish.

## Credential storage

The official CLI stores encrypted credentials under:

```text
~/.local/share/codex-feishu-notifier/lark-home/
```

The generated `config.env` stores the target Open ID and local paths, not the
App Secret. The state directory is created with mode `0700`; `config.env` uses
mode `0600`.
