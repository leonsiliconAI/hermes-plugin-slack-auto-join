"""Run against a Hermes checkout (see README): the Bolt tests use Hermes's real Slack adapter."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import logging
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_sdk.errors import SlackApiError

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter

_spec = importlib.util.spec_from_file_location(
    "slack_auto_join", pathlib.Path(__file__).resolve().parents[1] / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plugin
_spec.loader.exec_module(plugin)

TEAM, BOT = "T_TEAM", "U_BOT"


def _channel(cid: str, name: str, member: bool = False, private: bool = False) -> dict:
    return {"id": cid, "name": name, "is_member": member, "is_private": private,
            "is_archived": False}


def _pages(*pages: list[dict]):
    """``side_effect`` for a paginated list call: one response per page, linked by cursors."""
    responses = [{"ok": True, "channels": page,
                  "response_metadata": {"next_cursor": f"c{i + 1}" if i + 1 < len(pages) else ""}}
                 for i, page in enumerate(pages)]
    return AsyncMock(side_effect=lambda **kwargs: responses[int(kwargs.get("cursor", "c0")[1:])])


def _slack_error(code: str) -> SlackApiError:
    return SlackApiError(code, {"ok": False, "error": code})


@pytest.fixture()
def bot():
    client = AsyncMock()
    client.conversations_list = _pages([])
    client.conversations_join = AsyncMock(return_value={"ok": True})
    return client


@pytest.fixture()
def adapter(bot):
    a = SlackAdapter(PlatformConfig(enabled=True, token="***"))
    a._app = MagicMock()
    a._app.client = bot
    a._team_clients = {TEAM: bot}
    a._team_bot_user_ids = {TEAM: BOT}
    a._bot_user_id = BOT
    a._running = True
    return a


@pytest.fixture()
def joiner(adapter, tmp_path, monkeypatch):
    monkeypatch.delenv(plugin.USER_TOKEN_ENV, raising=False)
    j = plugin.AutoJoiner(adapter, plugin.State(tmp_path / "state.json"))
    setattr(adapter, plugin._JOINER_ATTR, j)
    yield j
    if j.task is not None:
        j.task.cancel()


def _joined(bot) -> list[str]:
    return [c.kwargs["channel"] for c in bot.conversations_join.call_args_list]


# --- public channels ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_joins_every_public_channel_hermes_is_missing(joiner, bot):
    bot.conversations_list = _pages(
        [_channel("C1", "agents", member=True), _channel("C2", "social")],
        [_channel("C3", "todo"), _channel("G1", "secret", member=True, private=True)])

    done = await joiner.sweep()

    assert _joined(bot) == ["C2", "C3"]
    assert done["joined"] == ["C2", "C3"]
    assert joiner.state.members == {"C1", "C2", "C3", "G1"}
    assert bot.conversations_list.call_args.kwargs["types"] == "public_channel,private_channel"


@pytest.mark.asyncio
async def test_removed_channel_is_not_rejoined_until_hermes_is_added_back(joiner, bot, caplog):
    bot.conversations_list = _pages([_channel("C1", "agents", member=True)])
    await joiner.sweep()

    bot.conversations_list = _pages([_channel("C1", "agents")])  # Someone removed Hermes.
    with caplog.at_level(logging.INFO):
        await joiner.sweep()
        await joiner.sweep()
    assert _joined(bot) == []
    assert joiner.state.left == {"C1"}
    assert sum("was removed from #agents" in r.message for r in caplog.records) == 1

    bot.conversations_list = _pages([_channel("C1", "agents", member=True)])  # Added back.
    await joiner.sweep()
    assert joiner.state.left == set() and "C1" in joiner.state.members


@pytest.mark.asyncio
async def test_state_survives_restarts(joiner, bot, adapter, tmp_path):
    bot.conversations_list = _pages([_channel("C1", "agents", member=True)])
    await joiner.sweep()
    bot.conversations_list = _pages([_channel("C1", "agents")])
    await joiner.sweep()

    reloaded = plugin.AutoJoiner(adapter, plugin.State(tmp_path / "state.json"))
    bot.conversations_list = _pages([_channel("C1", "agents")])
    await reloaded.sweep()
    assert _joined(bot) == []
    assert reloaded.state.left == {"C1"}


@pytest.mark.asyncio
async def test_join_errors_are_logged_once_and_rate_limits_end_the_sweep(joiner, bot, caplog):
    bot.conversations_list = _pages([_channel("C1", "shared"), _channel("C2", "a"),
                                     _channel("C3", "b")])
    bot.conversations_join = AsyncMock(side_effect=[
        _slack_error("method_not_supported_for_channel_type"), _slack_error("ratelimited"),
        _slack_error("method_not_supported_for_channel_type"), {"ok": True}, {"ok": True}])
    with caplog.at_level(logging.WARNING):
        await joiner.sweep()  # C1 fails, C2 is rate limited: C3 waits for the next sweep.
        assert _joined(bot) == ["C1", "C2"]
        await joiner.sweep()
    assert _joined(bot) == ["C1", "C2", "C1", "C2", "C3"]
    assert sum("could not join C1" in r.message for r in caplog.records) == 1
    assert joiner.state.members == {"C2", "C3"}


@pytest.mark.asyncio
async def test_already_in_channel_is_not_reported_as_a_join(joiner, bot):
    bot.conversations_join = AsyncMock(return_value={"ok": True, "warning": "already_in_channel"})
    await joiner.on_channel_event(TEAM, "C9")
    assert joiner.state.members == {"C9"}


@pytest.mark.asyncio
async def test_disconnected_adapter_is_left_alone(joiner, bot, adapter):
    adapter._running = False
    await joiner.sweep()
    bot.conversations_list.assert_not_called()


# --- private channels ----------------------------------------------------------------------------


@pytest.fixture()
def user(adapter, monkeypatch):
    client = AsyncMock()
    client.auth_test = AsyncMock(return_value={"ok": True, "team_id": TEAM, "user": "leon"})
    client.users_conversations = _pages([])
    client.conversations_invite = AsyncMock(return_value={"ok": True})
    monkeypatch.setenv(plugin.USER_TOKEN_ENV, "xoxp-test")
    adapter._new_web_client = MagicMock(return_value=client)
    return client


def _invited(user) -> list[tuple[str, str]]:
    return [(c.kwargs["channel"], c.kwargs["users"])
            for c in user.conversations_invite.call_args_list]


@pytest.mark.asyncio
async def test_no_user_token_means_no_private_channel_calls(joiner, bot, adapter):
    adapter._new_web_client = MagicMock()
    await joiner.sweep()
    adapter._new_web_client.assert_not_called()


@pytest.mark.asyncio
async def test_user_token_invites_hermes_to_private_channels(joiner, bot, user):
    bot.conversations_list = _pages([_channel("G1", "in-already", member=True, private=True)])
    user.users_conversations = _pages(
        [_channel("G1", "in-already", private=True), _channel("G2", "new-private", private=True)])

    done = await joiner.sweep()

    assert _invited(user) == [("G2", BOT)]
    assert done["invited"] == ["G2"]
    assert user.users_conversations.call_args.kwargs["types"] == "private_channel"
    assert {"G1", "G2"} <= joiner.state.members


@pytest.mark.asyncio
async def test_hermes_removed_from_a_private_channel_stays_out(joiner, bot, user):
    bot.conversations_list = _pages([_channel("G1", "p", member=True, private=True)])
    user.users_conversations = _pages([_channel("G1", "p", private=True)])
    await joiner.sweep()

    bot.conversations_list = _pages([])  # Hermes no longer sees it: it was removed.
    user.users_conversations = _pages([_channel("G1", "p", private=True)])
    await joiner.sweep()
    assert _invited(user) == []
    assert joiner.state.left == {"G1"}


@pytest.mark.asyncio
async def test_bot_token_in_user_slot_is_refused_once(joiner, bot, user, caplog):
    user.auth_test = AsyncMock(return_value={"ok": True, "team_id": TEAM, "bot_id": "B1"})
    with caplog.at_level(logging.WARNING):
        await joiner.sweep()
        await joiner.sweep()
    user.users_conversations.assert_not_called()
    user.auth_test.assert_awaited_once()
    assert sum("is a bot token" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_user_token_from_another_workspace_is_ignored(joiner, bot, user):
    user.auth_test = AsyncMock(return_value={"ok": True, "team_id": "T_OTHER", "user": "x"})
    await joiner.sweep()
    user.users_conversations.assert_not_called()


# --- events through Hermes's real Bolt app ------------------------------------------------------


async def _authorize(**_kwargs):
    return AuthorizeResult(enterprise_id=None, team_id=TEAM, bot_user_id=BOT, bot_id="B_BOT",
                           bot_token="xoxb-test")


@pytest.fixture()
def bolt_adapter(adapter):
    adapter._app = AsyncApp(authorize=_authorize, signing_secret="unused")
    adapter._register_bolt_handlers()  # Includes Hermes's catch-all event listener.
    return adapter


def _event_request(event: dict) -> AsyncBoltRequest:
    return AsyncBoltRequest(mode="socket_mode", body={
        "type": "event_callback", "team_id": TEAM, "api_app_id": "A1", "event_id": "Ev1",
        "event_time": 1, "event": event})


async def _settle(joiner) -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    if joiner._event_tasks:
        await asyncio.gather(*joiner._event_tasks)


@pytest.mark.asyncio
async def test_plain_listener_never_sees_channel_created(bolt_adapter):
    """Why the plugin uses middleware: Hermes's catch-all listener wins first-match dispatch."""
    seen = []

    @bolt_adapter._app.event("channel_created")
    async def _listener(event):
        seen.append(event)

    await bolt_adapter._app.async_dispatch(_event_request(
        {"type": "channel_created", "channel": {"id": "C_NEW", "name": "new", "creator": "U1"}}))
    await asyncio.sleep(0.05)
    assert seen == []


@pytest.mark.asyncio
async def test_channel_created_joins_at_once(bolt_adapter, joiner, bot):
    plugin.wire(bolt_adapter._app, bolt_adapter)
    resp = await bolt_adapter._app.async_dispatch(_event_request(
        {"type": "channel_created", "channel": {"id": "C_NEW", "name": "new", "creator": "U1"}}))
    await _settle(joiner)

    assert resp.status == 200
    assert _joined(bot) == ["C_NEW"]
    assert "C_NEW" in joiner.state.members


@pytest.mark.asyncio
async def test_channel_unarchive_joins_unless_hermes_was_removed(bolt_adapter, joiner, bot):
    plugin.wire(bolt_adapter._app, bolt_adapter)
    joiner.state.left.add("C_OLD")
    await bolt_adapter._app.async_dispatch(_event_request(
        {"type": "channel_unarchive", "channel": "C_OLD", "user": "U1"}))
    await bolt_adapter._app.async_dispatch(_event_request(
        {"type": "channel_unarchive", "channel": "C_BACK", "user": "U1"}))
    await _settle(joiner)
    assert _joined(bot) == ["C_BACK"]


@pytest.mark.asyncio
async def test_messages_still_reach_hermes_and_wiring_is_idempotent(bolt_adapter, joiner):
    bolt_adapter._handle_slack_message = AsyncMock()
    plugin.wire(bolt_adapter._app, bolt_adapter)
    plugin.wire(bolt_adapter._app, bolt_adapter)
    custom = [m for m in bolt_adapter._app._async_middleware_list
              if "slack_auto_join_middleware" in getattr(m, "name", "")]
    assert len(custom) == 1

    # Re-register Hermes's listeners so the patched message handler is the one Bolt calls.
    bolt_adapter._app._async_listeners.clear()
    bolt_adapter._register_bolt_handlers()
    resp = await bolt_adapter._app.async_dispatch(_event_request(
        {"type": "message", "channel": "C1", "user": "U1", "text": "hi", "ts": "1.0"}))
    await asyncio.sleep(0.05)
    assert resp.status == 200
    bolt_adapter._handle_slack_message.assert_awaited_once()
