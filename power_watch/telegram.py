"""Telegram Bot API: channel posts that survive outages, plus the helpers the CLI needs.

The bot token is part of every request URL, so nothing here ever logs a URL or a request.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/"


def error_detail(e: urllib.error.HTTPError) -> dict:
    """Telegram's JSON error body as a dict; {} for anything else (empty, truncated, not JSON)."""
    try:
        detail = json.loads(e.read() or b"{}")
    except Exception:  # noqa: BLE001
        return {}
    return detail if isinstance(detail, dict) else {}


class Telegram:
    """Posts in order from a background thread, retrying until Telegram is reachable again
    (the network is often down right when power returns), so probing never blocks on it.
    The outbox is persisted by the caller (pending()) and after every send (on_sent)."""

    def __init__(
        self, token: str, chat_id: str, outbox: list[str] | None = None, on_sent: Callable[[], None] | None = None
    ):
        self.base = API.format(token=token)
        self.url = self.base + "sendMessage"
        self.chat_id = chat_id
        self.outbox = list(outbox or [])
        self.cv = threading.Condition()
        self.on_sent = on_sent or (lambda: None)

    def start(self) -> Telegram:
        threading.Thread(target=self._run, name="telegram-outbox", daemon=True).start()
        return self

    def post(self, text: str) -> None:
        with self.cv:
            self.outbox.append(text)
            self.cv.notify()

    def pending(self) -> list[str]:
        with self.cv:
            return list(self.outbox)

    def send(self, text: str, chat_id: str | int | None = None) -> tuple[str, float]:
        """One attempt: ("sent" | "dropped" | "retry", seconds to wait before retrying). A permanent
        rejection (bad token or chat) is logged and dropped rather than retried forever. chat_id
        defaults to the channel."""
        body = urllib.parse.urlencode({"chat_id": chat_id or self.chat_id, "text": text}).encode()
        try:
            with urllib.request.urlopen(self.url, data=body, timeout=15) as r:
                r.read()
            return "sent", 0
        except urllib.error.HTTPError as e:
            detail = error_detail(e)
            if e.code == 429:
                try:
                    return "retry", float(detail.get("parameters", {}).get("retry_after", 30))
                except (AttributeError, TypeError, ValueError):
                    return "retry", 30
            if 400 <= e.code < 500:
                log.warning("telegram: HTTP %s %s — message dropped", e.code, detail.get("description", ""))
                return "dropped", 0
            log.warning("telegram: send failed, HTTP %s", e.code)
            return "retry", 30
        except Exception as e:  # noqa: BLE001 — anything else (garbage replies too) is transient
            log.warning("telegram: send failed, %s", type(e).__name__)
            return "retry", 30

    def call(self, method: str, params: dict, timeout: float) -> dict:
        """Any other Bot API method; raises on failure (callers decide how to retry)."""
        body = urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
        ).encode()
        with urllib.request.urlopen(self.base + method, data=body, timeout=timeout) as r:
            return json.loads(r.read())

    def _run(self) -> None:
        while True:
            try:
                with self.cv:
                    while not self.outbox:
                        self.cv.wait()
                    text = self.outbox[0]
                delay = 10.0
                while True:
                    status, wait = self.send(text)
                    if status != "retry":
                        break
                    time.sleep(max(wait, delay))
                    delay = min(delay * 2, 120)
                with self.cv:
                    self.outbox.pop(0)
                self.on_sent()
            except Exception as e:  # noqa: BLE001 — the worker must outlive any surprise
                log.warning("telegram: worker %s — retrying in 30 s", type(e).__name__)
                time.sleep(30)


def chats_from_updates(updates: dict) -> dict[int, str]:
    """{chat id: "type title"} for every chat in a getUpdates reply."""
    chats = {}
    for u in updates.get("result", []):
        for kind in ("my_chat_member", "channel_post", "message", "chat_member"):
            chat = (u.get(kind) or {}).get("chat")
            if chat and "id" in chat:
                chats[chat["id"]] = f"{chat.get('type', '?')} {chat.get('title') or chat.get('username') or ''}".strip()
    return chats


def fetch_chats(token: str) -> dict[int, str]:
    """The chats the bot has seen recently (Telegram keeps updates for 24 h). allowed_updates is
    sticky on Telegram's side and the service asks for messages only, so ask for channel updates
    explicitly. Raises on failure."""
    return chats_from_updates(
        Telegram(token, "").call("getUpdates", {"allowed_updates": ["message", "channel_post", "my_chat_member"]}, 15)
    )
