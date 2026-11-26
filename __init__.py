"""Keep Hermes in every Slack channel: join new public channels at once and sweep for the rest.

Stock Hermes only hears channels its bot user is a member of, and nothing in Hermes adds it to a
channel. This plugin does three things:

* **New channels, at once.** A global Bolt middleware sees ``channel_created`` and
  ``channel_unarchive`` events and calls ``conversations.join``. Middleware is used instead of
  ``app.event(...)`` because Bolt runs only the first matching listener, and Hermes registers a
  catch-all ``app.event(re.compile(".*"))`` before plugin handlers are wired, so a plugin listener
  for these events would never run. The app manifest must subscribe to both bot events.
* **Every public channel, on a timer.** Every ``SLACK_AUTO_JOIN_INTERVAL`` seconds (default 60) it
  lists public channels and joins any it isn't in. This catches channels created while the
  gateway was down and doubles as a backstop for missed events.
* **Private channels, if a user token is set.** A bot can't join a private channel; a member has
  to invite it. With ``SLACK_AUTO_JOIN_USER_TOKEN`` (a user token holding only ``groups:read`` and
  ``groups:write.invites``), each sweep lists that user's private channels and invites Hermes to
  the ones it isn't in.

Removing Hermes from a channel is respected: a channel it was seen in and then wasn't is
remembered as left and never rejoined, until someone adds Hermes back. State lives in
``$HERMES_HOME/plugin-data/slack-auto-join/state.json``.

Group DMs and DMs between other people can't take a bot, so they're out of reach.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from ._bridge import acall_with_host, call

logger = logging.getLogger(__name__)

PLUGIN_NAME = "slack-auto-join"
USER_TOKEN_ENV = "SLACK_AUTO_JOIN_USER_TOKEN"
INTERVAL_ENV = "SLACK_AUTO_JOIN_INTERVAL"
DEFAULT_INTERVAL = 60.0
MIN_INTERVAL = 15.0
START_DELAY = 5.0

_JOINER_ATTR = "_slack_auto_join"
_WIRED_ATTR = "_slack_auto_join_wired"


# --------------------------------------------------------------------------------------------
# Small helpers


def _get(resp: Any, key: str, default: Any = None) -> Any:
    """Read a field from a dict or an ``AsyncSlackResponse``."""
    try:
        value = resp.get(key, default)
    except AttributeError:
        value = getattr(resp, "data", {}).get(key, default)
    return default if value is None else value


def _slack_error(exc: BaseException) -> str:
    """The Slack ``error`` code of a ``SlackApiError``, or the exception text."""
    response = getattr(exc, "response", None)
    if response is not None:
        code = _get(response, "error", "")
        if code:
            return str(code)
    return str(exc) or exc.__class__.__name__


async def _pages(method: Callable[..., Awaitable[Any]], key: str, **params: Any) -> AsyncIterator[dict]:
    """Yield every item of a cursor-paginated Slack list method."""
    cursor = ""
    while True:
        resp = await method(**params, **({"cursor": cursor} if cursor else {}))
        for item in _get(resp, key, []) or []:
            yield item
        cursor = (_get(resp, "response_metadata", {}) or {}).get("next_cursor") or ""
        if not cursor:
            return


def _secret(name: str) -> str:
    """Read a credential the way Hermes's platform adapters do, falling back to the environment."""
    try:
        from gateway.platforms._shared import get_scoped_secret

        value = get_scoped_secret(name, None)
        if value is not None:
            return str(value).strip()
    except Exception:
        pass
    return (os.environ.get(name) or "").strip()


def _interval() -> float:
    raw = _secret(INTERVAL_ENV)
    try:
        return max(MIN_INTERVAL, float(raw)) if raw else DEFAULT_INTERVAL
    except ValueError:
        logger.warning("slack-auto-join: %s=%r is not a number; using %ss", INTERVAL_ENV, raw,
                       DEFAULT_INTERVAL)
        return DEFAULT_INTERVAL


def _state_path() -> Optional[Path]:
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_NAME) / "state.json"
    except Exception:
        home = os.environ.get("HERMES_HOME")
        if not home:
            return None
        directory = Path(home) / "plugin-data" / PLUGIN_NAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return directory / "state.json"


# --------------------------------------------------------------------------------------------
# State


class State:
    """Channels Hermes has been seen in (``members``) and channels it was removed from (``left``).

    ``members`` is what makes removals detectable: a channel that was in it and now lists Hermes as
    a non-member was left on purpose, so it moves to ``left`` and is never rejoined. Seeing Hermes
    in a ``left`` channel again (someone re-added it) clears that.
    """

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self.members: set[str] = set()
        self.left: set[str] = set()
        self._dirty = False
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text())
                self.members = set(data.get("members") or [])
                self.left = set(data.get("left") or [])
            except (OSError, ValueError):
                logger.warning("slack-auto-join: could not read %s; starting fresh", path,
                               exc_info=True)

    def seen_member(self, channel_id: str) -> None:
        if channel_id not in self.members or channel_id in self.left:
            self.members.add(channel_id)
            self.left.discard(channel_id)
            self._dirty = True

    def seen_non_member(self, channel_id: str) -> bool:
        """Record that Hermes isn't in ``channel_id``; return True if it should be added."""
        was_member = channel_id in self.members
        rejoin = call(__file__, "shouldRejoin", was_member, channel_id in self.left)
        if was_member:
            self.members.discard(channel_id)
            self.left.add(channel_id)
            self._dirty = True
        return rejoin

    def save(self) -> None:
        if not self._dirty or self.path is None:
            return
        payload = json.dumps({"version": 1, "members": sorted(self.members),
                              "left": sorted(self.left)}, indent=2)
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            logger.warning("slack-auto-join: could not write %s", self.path, exc_info=True)


# --------------------------------------------------------------------------------------------
# The joiner


class AutoJoiner:
    """One per Slack adapter; survives reconnects, which rebuild the adapter's clients."""

    def __init__(self, adapter: Any, state: State) -> None:
        self.adapter = adapter
        self.state = state
        self.lock = asyncio.Lock()
        self.task: Optional[asyncio.Task] = None
        self._event_tasks: set[asyncio.Task] = set()
        self._warned: set[tuple[str, str]] = set()
        self._user_client: Any = None
        self._user_token = ""
        self._user_team = ""
        self._user_broken = False

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # The middleware starts it on the first request, which runs on the loop.
        self.task = loop.create_task(self._run(), name="slack-auto-join")

    async def _run(self) -> None:
        await asyncio.sleep(START_DELAY)
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("slack-auto-join: sweep failed", exc_info=True)
            await asyncio.sleep(_interval())

    # -- clients ----------------------------------------------------------------------------

    def _teams(self) -> list[tuple[str, Any, str]]:
        """``(team_id, bot client, bot user id)`` for every connected workspace."""
        if not getattr(self.adapter, "_running", False):
            return []
        clients = dict(getattr(self.adapter, "_team_clients", None) or {})
        bot_ids = dict(getattr(self.adapter, "_team_bot_user_ids", None) or {})
        if not clients and getattr(getattr(self.adapter, "_app", None), "client", None) is not None:
            clients = {"": self.adapter._app.client}
            bot_ids = {"": getattr(self.adapter, "_bot_user_id", "") or ""}
        return [(team, client, bot_ids.get(team) or getattr(self.adapter, "_bot_user_id", "") or "")
                for team, client in clients.items()]

    def _client_for(self, team_id: str) -> Any:
        for team, client, _bot in self._teams():
            if not team_id or not team or team == team_id:
                return client
        return None

    async def _user(self) -> tuple[Any, str]:
        """The private-channel inviter client and its workspace, or ``(None, "")``."""
        token = _secret(USER_TOKEN_ENV)
        if not token:
            return None, ""
        if token != self._user_token:
            self._user_token, self._user_client, self._user_team = token, None, ""
            self._user_broken = False
        if self._user_broken:
            return None, ""
        if self._user_client is None:
            client = self._new_client(token)
            try:
                auth = await client.auth_test()
            except Exception as exc:
                self._user_broken = True
                logger.warning("slack-auto-join: %s doesn't authenticate (%s); private channels are "
                               "skipped until it changes", USER_TOKEN_ENV, _slack_error(exc))
                return None, ""
            if _get(auth, "bot_id"):
                self._user_broken = True
                logger.warning("slack-auto-join: %s is a bot token; it must be a user token (xoxp-) "
                               "of someone in the private channels", USER_TOKEN_ENV)
                return None, ""
            self._user_client, self._user_team = client, str(_get(auth, "team_id", ""))
            logger.info("slack-auto-join: inviting Hermes to %s's private channels",
                        _get(auth, "user", "the token owner"))
        return self._user_client, self._user_team

    def _new_client(self, token: str) -> Any:
        factory = getattr(self.adapter, "_new_web_client", None)
        if callable(factory):
            return factory(token, getattr(self.adapter, "_proxy_url", None))
        from slack_sdk.web.async_client import AsyncWebClient

        return AsyncWebClient(token=token)

    # -- actions ----------------------------------------------------------------------------

    def _warn_once(self, channel_id: str, what: str, error: str) -> None:
        if (channel_id, error) in self._warned:
            return
        self._warned.add((channel_id, error))
        logger.warning("slack-auto-join: could not %s %s: %s", what, channel_id, error)

    async def _membership_action(self, client, action, channel_id, name, bot_user_id=""):
        try:
            if action == "join":
                response = await client.conversations_join(channel=channel_id)
            else:
                response = await client.conversations_invite(channel=channel_id, users=bot_user_id)
        except Exception as exc:
            error = _slack_error(exc)
            result = call(__file__, "membershipResult", action, error, "")
            if result == "retry":
                raise
            if result == "failed":
                self._warn_once(channel_id, "join" if action == "join" else "invite Hermes to", error)
                return False
        else:
            result = call(__file__, "membershipResult", action, "", _get(response, "warning", ""))
            name = name or (_get(response, "channel", {}) or {}).get("name", "")
        self.state.seen_member(channel_id)
        if result == "joined":
            description = "joined" if action == "join" else "invited Hermes to private"
            logger.info("slack-auto-join: %s #%s (%s)", description, name or channel_id, channel_id)
        return result != "alreadyJoined"

    async def _join(self, client, channel_id, name=""):
        return await self._membership_action(client, "join", channel_id, name)

    async def _invite(self, client, channel_id, bot_user_id, name):
        return await self._membership_action(client, "invite", channel_id, name, bot_user_id)

    def _note_left(self, channel_id, name):
        logger.info("slack-auto-join: Hermes was removed from #%s (%s); it stays out until "
                    "someone adds it back", name or channel_id, channel_id)

    async def sweep(self) -> dict[str, list[str]]:
        """Join every public channel and, with a user token, every private one Hermes is missing."""
        done: dict[str, list[str]] = {"joined": [], "invited": []}
        async with self.lock:
            try:
                for team_id, client, bot_user_id in self._teams():
                    await self._sweep_team(team_id, client, bot_user_id, done)
            except Exception as exc:
                if _slack_error(exc) != "ratelimited":
                    raise
                logger.info("slack-auto-join: rate limited; continuing next sweep")
            finally:
                self.state.save()
        return done

    async def _sweep_team(self, team_id: str, client: Any, bot_user_id: str,
                          done: dict[str, list[str]]) -> None:
        bot_channels = _pages(client.conversations_list, "channels",
                              types="public_channel,private_channel", exclude_archived=True, limit=200)
        user_client = private_channels = None

        async def inviter_team():
            nonlocal user_client
            user_client, team = await self._user()
            return team if user_client is not None else None

        async def next_private():
            nonlocal private_channels
            if private_channels is None:
                private_channels = _pages(user_client.users_conversations, "channels",
                                          types="private_channel", exclude_archived=True, limit=200)
            return await anext(private_channels, None)

        try:
            await acall_with_host(__file__, "sweepTeam", {
                "nextBotChannel": lambda: anext(bot_channels, None),
                "nextPrivateChannel": next_private, "inviterTeam": inviter_team,
                "seenMember": self.state.seen_member, "seenNonMember": self.state.seen_non_member,
                "join": lambda channel, name: self._join(client, channel, name),
                "invite": lambda channel, name: self._invite(user_client, channel, bot_user_id, name),
                "noteLeft": self._note_left, "joined": done["joined"].append, "invited": done["invited"].append,
            }, team_id, bot_user_id, list(self.state.members))
        finally:
            await bot_channels.aclose()
            if private_channels is not None:
                await private_channels.aclose()

    async def on_channel_event(self, team_id: str, channel_id: str, name: str = "") -> None:
        """A channel was created or unarchived: join it now unless Hermes was removed from it."""
        async with self.lock:
            if channel_id in self.state.left:
                return
            client = self._client_for(team_id)
            if client is None:
                return
            try:
                await self._join(client, channel_id, name)
            except Exception as exc:
                logger.info("slack-auto-join: could not join %s yet (%s); the next sweep retries",
                            channel_id, _slack_error(exc))
            finally:
                self.state.save()

    def schedule_channel_event(self, body: dict) -> None:
        event = call(__file__, "channelEvent", body)
        if event is None:
            return
        task = asyncio.get_running_loop().create_task(
            self.on_channel_event(str(event["team"]), str(event["id"]), event["name"]))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)


# --------------------------------------------------------------------------------------------
# Hermes wiring


def _joiner_for(adapter: Any) -> AutoJoiner:
    joiner = getattr(adapter, _JOINER_ATTR, None)
    if joiner is None:
        joiner = AutoJoiner(adapter, State(_state_path()))
        setattr(adapter, _JOINER_ATTR, joiner)
    return joiner


def wire(native: Any, adapter: Any) -> None:
    """``register_platform_handler`` factory, called with each new Bolt ``AsyncApp``."""
    joiner = _joiner_for(adapter)
    if native is not None and not getattr(native, _WIRED_ATTR, False):
        add_middleware = getattr(native, "middleware", None)
        if callable(add_middleware):
            async def slack_auto_join_middleware(body, next):  # noqa: A002 - Bolt injects by name
                try:
                    if isinstance(body, dict):
                        joiner.start()
                        joiner.schedule_channel_event(body)
                except Exception:
                    logger.warning("slack-auto-join: could not handle a channel event",
                                   exc_info=True)
                return await next()

            add_middleware(slack_auto_join_middleware)
            setattr(native, _WIRED_ATTR, True)
        else:
            logger.warning("slack-auto-join: the Slack app has no middleware hook; new channels "
                           "wait for the next sweep")
    joiner.start()
    logger.info("slack-auto-join: keeping Hermes in every Slack channel (sweep every %ss%s)",
                int(_interval()), ", private channels too" if _secret(USER_TOKEN_ENV) else "")


def register(ctx: Any) -> None:
    ctx.register_platform_handler("slack", wire)
