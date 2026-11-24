# @-/com.leonsilicon.hermes.plugins.slack-auto-join

Hermes plugin that keeps Hermes in every Slack channel, by default and without anyone inviting it.

Stock Hermes only hears channels its bot user is a member of, and nothing in Hermes ever joins one. This plugin does that:

- **New public channels, at once.** A global Bolt middleware sees `channel_created` and `channel_unarchive` and calls `conversations.join`. It's middleware rather than `app.event(...)` because Bolt runs only the first matching listener, and Hermes registers a catch-all `app.event(re.compile(".*"))` before plugin handlers are wired, so a plugin listener for these events never runs. A test pins this.
- **Every public channel, on a timer.** Every `SLACK_AUTO_JOIN_INTERVAL` seconds (default 60, minimum 15) it lists public channels and joins the ones Hermes is missing. That covers channels created while the gateway was down and any missed events.
- **Private channels, with a user token.** A bot can't join a private channel; a member has to invite it. If `SLACK_AUTO_JOIN_USER_TOKEN` holds a user token (`xoxp-`) with only `groups:read` and `groups:write.invites`, each sweep lists that person's private channels and invites Hermes to the ones it isn't in. Without the token, private channels are skipped.

Removing Hermes from a channel sticks: a channel Hermes was seen in and then wasn't is remembered as left and never rejoined, until someone adds Hermes back. State is in `$HERMES_HOME/plugin-data/slack-auto-join/state.json`.

Out of reach: group DMs and DMs between other people (Slack can't add a bot to them), and private channels the token's owner isn't in.

## Slack app

Subscribe the app to the `channel_created` and `channel_unarchive` bot events (both need `channels:read`). For private channels, add the user scopes `groups:read` and `groups:write.invites`, reinstall the app, and put the "User OAuth Token" in `SLACK_AUTO_JOIN_USER_TOKEN`. Reinstalling keeps the bot token.

## Install

```sh
hermes plugins install leonsiliconAI/hermes-plugin-slack-auto-join --enable --yes-deps
```

See [migration steps](https://github.com/leonsilicon/leonsilicon.ai/blob/main/DEVELOPMENT.md#hermes-plugin-mirrors) for an existing install. Restart the gateway. The gateway log then shows `slack-auto-join: keeping Hermes in every Slack channel` and a `joined #…` line per channel.

## Test

The tests drive Hermes's real Slack adapter and Bolt app, so they need a Hermes checkout's environment plus `pytest` and `pytest-asyncio`:

```sh
git -C ~/.hermes/hermes-agent worktree add --detach /tmp/hermes-v0.23.0 v0.23.0
cd /tmp/hermes-v0.23.0 && UV_PROJECT_ENVIRONMENT=/tmp/hermes-venv uv sync --frozen --extra slack
VIRTUAL_ENV=/tmp/hermes-venv uv pip install pytest pytest-asyncio
cd - && HERMES_HOME=$(mktemp -d) /tmp/hermes-venv/bin/python -m pytest -q -p no:cacheprovider __tests__
```
