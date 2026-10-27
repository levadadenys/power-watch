"""Air purifier readings for power-watch's /status, over Xiaomi's encrypted miIO protocol.

Unlike the power probe (the token-free "hello"), reading properties needs the device's 32-hex-digit
token (Xiaomi Cloud Tokens Extractor prints it) and python3-cryptography for AES. Properties are
read with MiOT get_properties; the siid/piid numbers differ per model, so each supported model has
a map below (from its spec on home.miot-spec.com). The wire format follows python-miio.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import time
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PORT = 54321
HELLO = bytes.fromhex("21310020" + "ff" * 28)
# Device errors worth a fresh handshake and another try (python-miio retries the same ones):
# -9999 "user ack timeout", -30001 "resp invalid".
RETRYABLE_CODES = (-9999, -30001)

# name -> (siid, piid). Optional names (humidity, temperature) only where the model has them.
MODELS = {
    # Xiaomi Smart Air Purifier 4 Compact: PM2.5 only, no humidity/temperature sensor.
    "xiaomi.airp.cpa4": {"on": (2, 1), "fault": (2, 2), "mode": (2, 4), "pm25": (3, 4),
                         "filter_pct": (4, 1), "filter_days": (4, 4)},
}

# Per language: format strings, plus the quality words (tuple) and mode/fault names (dicts).
MESSAGES: dict[str, dict[str, Any]] = {
    "en": {
        "pm25": "🌫 PM2.5: {v} µg/m³ ({q})",
        "quality": ("good", "moderate", "unhealthy for sensitive groups", "unhealthy"),
        "climate": "💧 {h}%   🌡 {t} °C",
        "mode": "Purifier: {m}",
        "modes": {0: "auto", 1: "sleep", 2: "favorite"},
        "off": "Purifier: off",
        "filter": "🧻 Filter: {p}% (~{d} days left)",
        "faults": {2: "⚠️ Purifier: motor stuck", 3: "⚠️ Purifier: sensor lost"},
        "no_reply": "🌫 The purifier isn't answering.",
        "no_readings": "🌫 The purifier answered, but without readings.",
        "unsupported": "🌫 Purifier model {m} isn't supported yet.",
    },
    "uk": {
        "pm25": "🌫 PM2.5: {v} мкг/м³ ({q})",
        "quality": ("добре", "помірно", "шкідливо для чутливих", "шкідливо"),
        "climate": "💧 {h}%   🌡 {t} °C",
        "mode": "Очищувач: {m}",
        "modes": {0: "авто", 1: "нічний", 2: "улюблений"},
        "off": "Очищувач: вимкнено",
        "filter": "🧻 Фільтр: {p}% (≈ {d} дн.)",
        "faults": {2: "⚠️ Очищувач: заблоковано мотор", 3: "⚠️ Очищувач: втрачено датчик"},
        "no_reply": "🌫 Очищувач не відповідає.",
        "no_readings": "🌫 Очищувач відповів, але без показників.",
        "unsupported": "🌫 Модель очищувача {m} поки не підтримується.",
    },
}
# PM2.5 µg/m³ upper bounds for the quality words (US EPA 24-h breakpoints, 2024).
PM25_BANDS = (9, 35, 55)


class MiioError(Exception):
    pass


class Retry(MiioError):
    """A failure a fresh handshake may fix (lost packet, device busy)."""


class NoReadings(MiioError):
    """The device answered get_properties, but no property came back with code 0."""


class UnsupportedModel(Exception):
    def __init__(self, model: str):
        super().__init__(model)
        self.model = model


def md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()


class Device:
    """One miIO device. Each request does a fresh handshake (it gives the device id and clock the
    request must carry), so there is no session to go stale between /status requests."""

    def __init__(self, ip: str, token_hex: str, timeout: float = 2.5, attempts: int = 2, budget: float = 5.0):
        # timeout: one handshake + request; budget: a whole read() with its retries, which runs
        # on the bot's thread, so /status never waits longer than this for the purifier.
        self.ip, self.timeout, self.attempts, self.budget = ip, timeout, attempts, budget
        self.port = PORT
        self.token = bytes.fromhex(token_hex)
        if len(self.token) != 16:
            raise ValueError("a miIO token is 32 hex digits")
        self.key = md5(self.token)
        self.iv = md5(self.key + self.token)
        self.msg_id = int.from_bytes(os.urandom(2), "big") % 9998
        self.model = ""

    def encrypt(self, data: bytes) -> bytes:
        padder = padding.PKCS7(128).padder()
        enc = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
        return enc.update(padder.update(data) + padder.finalize()) + enc.finalize()

    def decrypt(self, data: bytes) -> bytes:
        dec = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(dec.update(data) + dec.finalize()) + unpadder.finalize()

    def packet(self, did: int, stamp: int, payload: bytes) -> bytes:
        data = self.encrypt(payload)
        header = struct.pack(">HHIII", 0x2131, 32 + len(data), 0, did, stamp)
        return header + md5(header + self.token + data) + data

    def open(self, data: bytes) -> object:
        """Check and decrypt a reply packet. A wrong checksum means a wrong token."""
        if len(data) < 32 or data[:2] != b"\x21\x31":
            raise MiioError("not a miIO reply")
        if not hmac.compare_digest(md5(data[:16] + self.token + data[32:]), data[16:32]):
            raise MiioError("bad checksum (wrong token?)")
        try:
            return json.loads(self.decrypt(data[32:]).rstrip(b"\0"))
        except ValueError:
            raise MiioError("unreadable reply (wrong token?)") from None

    def request(self, method: str, params: list | dict, until: float) -> dict:
        """One handshake + request, within self.timeout and before `until` (monotonic). The socket
        is connected, so the kernel drops datagrams from any other host or port; a stray packet
        that still gets through is skipped, not trusted."""
        deadline = min(time.monotonic() + self.timeout, until)
        last_error = "no reply (wrong IP or token?)"

        def recv(s: socket.socket) -> bytes:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout
            s.settimeout(left)
            return s.recv(4096)

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect((self.ip, self.port))
                s.send(HELLO)
                while True:
                    hello = recv(s)
                    if len(hello) >= 32 and hello[:2] == b"\x21\x31":
                        break
                did, stamp = struct.unpack(">II", hello[8:16])
                self.msg_id = self.msg_id % 9998 + 1  # python-miio's range: 1..9998
                body = json.dumps({"id": self.msg_id, "method": method, "params": params}).encode() + b"\0"
                s.send(self.packet(did, stamp + 1, body))
                while True:
                    try:
                        reply = self.open(recv(s))
                    except MiioError as e:  # garbage, or a reply under another token: keep waiting
                        last_error = str(e)
                        continue
                    if isinstance(reply, dict) and reply.get("id") == self.msg_id:
                        break
            except socket.timeout:
                raise Retry(last_error) from None
            except OSError as e:
                raise Retry(type(e).__name__) from None
        error = reply.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            raise (Retry if code in RETRYABLE_CODES else MiioError)(f"device error {error}")
        return reply

    def call(self, method: str, params: list | dict, until: float | None = None) -> object:
        """request() with retries on lost packets and the device's transient errors, all before
        `until` (default: one budget from now)."""
        until = time.monotonic() + self.budget if until is None else until
        for attempt in range(self.attempts):
            try:
                return self.request(method, params, until).get("result")
            except Retry:
                if attempt == self.attempts - 1 or time.monotonic() >= until:
                    raise
        raise MiioError("no attempts configured")

    def read(self) -> dict:
        """{name: value} for the model's properties. Raises MiioError (NoReadings when nothing
        came back) or UnsupportedModel. Takes at most self.budget seconds."""
        until = time.monotonic() + self.budget
        if not self.model:
            info = self.call("miIO.info", [], until)
            model = info.get("model") if isinstance(info, dict) else None
            if not isinstance(model, str):
                raise MiioError("miIO.info without a model")
            self.model = model  # only a real answer is cached
        if self.model not in MODELS:
            raise UnsupportedModel(self.model)
        props = MODELS[self.model]
        result = self.call("get_properties", [{"did": n, "siid": s, "piid": p} for n, (s, p) in props.items()], until)
        if not isinstance(result, list):
            raise MiioError("get_properties result is not a list")
        rows = [r for r in result if isinstance(r, dict)]
        values = {r["did"]: r["value"] for r in rows
                  if r.get("code") == 0 and "value" in r and r.get("did") in props}
        if not values:
            raise NoReadings(f"no readings (codes {[r.get('code') for r in rows]})")
        return values


def pm25_quality(v: object, lang: str) -> str:
    words = MESSAGES[lang]["quality"]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "?"
    return next((w for bound, w in zip(PM25_BANDS, words) if v <= bound), words[-1])


def status_lines(values: dict, lang: str) -> list[str]:
    msgs = MESSAGES[lang]
    lines = []
    if "pm25" in values:
        lines.append(msgs["pm25"].format(v=values["pm25"], q=pm25_quality(values["pm25"], lang)))
    if "humidity" in values and "temperature" in values:
        lines.append(msgs["climate"].format(h=values["humidity"], t=values["temperature"]))
    if values.get("on") is False:
        lines.append(msgs["off"])
    elif "mode" in values:
        mode = values["mode"]
        lines.append(msgs["mode"].format(m=msgs["modes"].get(mode, mode) if isinstance(mode, int) else mode))
    if "filter_pct" in values:
        lines.append(msgs["filter"].format(p=values["filter_pct"], d=values.get("filter_days", "?")))
    fault = values.get("fault")
    if fault:
        lines.append(msgs["faults"].get(fault, f"⚠️ fault {fault}") if isinstance(fault, int) else f"⚠️ fault {fault}")
    return lines


def status(dev: Device, lang: str, log: Callable[[str], None] = lambda _msg: None) -> list[str]:
    """The purifier's /status lines. Never raises: the power status must not depend on it."""
    msgs = MESSAGES[lang]
    try:
        return status_lines(dev.read(), lang)
    except UnsupportedModel as e:
        return [msgs["unsupported"].format(m=e.model)]
    except NoReadings as e:
        log(f"purifier: {e}")
        return [msgs["no_readings"]]
    except Exception as e:  # noqa: BLE001 — a device quirk becomes a line, never a lost reply
        log(f"purifier: {type(e).__name__}: {e}")  # messages here never include the token
        return [msgs["no_reply"]]
