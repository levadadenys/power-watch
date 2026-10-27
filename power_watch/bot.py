"""Private /status replies for allowed Telegram users."""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
from collections.abc import Callable

from power_watch.messages import MESSAGES
from power_watch.telegram import Telegram, error_detail

log = logging.getLogger(__name__)

STATUS_WORDS = ("/status", "/start", "?", "status", "статус", "світло")


def reply_for(
    update: dict, allowed: frozenset[int], status: Callable[[], str], lang: str
) -> tuple[int, str] | str | None:
    """What to do with one getUpdates entry: (chat id, reply) for an allowed user's private
    message, a log line for anyone else's, None for the rest (channel posts, edits, ...)."""
    msg = update.get("message")
    if not isinstance(msg, dict) or (msg.get("chat") or {}).get("type") != "private":
        return None
    user = msg.get("from") or {}
    if user.get("id") not in allowed:
        return (
            f"bot: ignored a message from user {user.get('id')} (@{user.get('username', '?')}) — "
            f"add the id to POWER_WATCH_ALLOWED_USERS to let them ask"
        )
    text = (msg.get("text") or "").strip().lower()
    command = text.split()[0].split("@")[0] if text else ""
    if command in STATUS_WORDS:
        return msg["chat"]["id"], status()
    return msg["chat"]["id"], MESSAGES[lang]["help"]


class Commands:
    """Answers private /status messages: long-polls getUpdates (the one consumer of the bot's
    updates — a second one, even `power-watch --find-chats`, gets HTTP 409 while this runs)."""

    def __init__(self, tg: Telegram, allowed: frozenset[int], status: Callable[[], str], lang: str):
        self.tg, self.allowed, self.status, self.lang = tg, allowed, status, lang
        self.strangers: set = set()

    def start(self) -> Commands:
        threading.Thread(target=self._run, name="bot", daemon=True).start()
        return self

    def answer(self, chat_id: int, text: str) -> None:
        for attempt in range(3):  # a few quick tries; an answer minutes late is no use
            status, wait = self.tg.send(text, chat_id)
            if status != "retry":
                return
            time.sleep(min(wait, 10) if attempt < 2 else 0)
        log.warning("bot: could not answer chat %s", chat_id)

    def handle(self, updates: list, now: float) -> None:
        """One reply per chat per batch, for messages under 10 min old: a backlog after an
        outage or restart is answered once, with the current state. The status itself is built
        once per batch too: with a slow or offline purifier each build can take seconds."""
        replies: dict[int, str] = {}
        built: list[str] = []

        def status_once() -> str:
            if not built:
                built.append(self.status())
            return built[0]

        for update in updates:
            msg = update.get("message")
            if isinstance(msg, dict) and now - msg.get("date", now) > 600:
                continue
            try:
                action = reply_for(update, self.allowed, status_once, self.lang)
            except Exception as e:  # noqa: BLE001
                log.warning("bot: cannot build a reply: %s", type(e).__name__)
                continue
            if isinstance(action, str):
                user = (update.get("message") or {}).get("from", {}).get("id")
                if user not in self.strangers:  # log each stranger once per run
                    self.strangers.add(user)
                    log.info(action)
            elif action:
                replies[action[0]] = action[1]
        for chat_id, text in replies.items():
            self.answer(chat_id, text)

    def _run(self) -> None:
        menu_set, offset = False, 0
        while True:
            try:
                if not menu_set:  # the "/" menu; retried until the network is up
                    self.tg.call(
                        "setMyCommands",
                        {"commands": [{"command": "status", "description": MESSAGES[self.lang]["cmd_status"]}]},
                        15,
                    )
                    menu_set = True
                reply = self.tg.call(
                    "getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message"]}, 60
                )
                updates = reply.get("result", [])
                for update in updates:
                    offset = max(offset, update.get("update_id", 0) + 1)
                self.handle(updates, time.time())
            except urllib.error.HTTPError as e:
                # 409 = another getUpdates consumer. Never log e.url: it carries the token.
                log.warning("bot: getUpdates HTTP %s %s", e.code, error_detail(e).get("description", ""))
                time.sleep(30)
            except Exception as e:  # noqa: BLE001 — network down etc.; keep listening
                log.warning("bot: getUpdates %s", type(e).__name__)
                time.sleep(15)
