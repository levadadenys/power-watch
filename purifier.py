"""Air purifier readings for power-watch's /status, over Xiaomi's encrypted miIO protocol.

Unlike the power probe (the token-free "hello"), reading properties needs the device's 32-hex-digit
token (Xiaomi Cloud Tokens Extractor prints it) and python3-cryptography for AES. Properties are
read with MiOT get_properties; the siid/piid numbers differ per model, so each supported model has
a map below (from its spec on home.miot-spec.com).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PORT = 54321
HELLO = bytes.fromhex("21310020" + "ff" * 28)

# name -> (siid, piid). Optional names (humidity, temperature) only where the model has them.
MODELS = {
    # Xiaomi Smart Air Purifier 4 Compact: PM2.5 only, no humidity/temperature sensor.
    "xiaomi.airp.cpa4": {"on": (2, 1), "fault": (2, 2), "mode": (2, 4), "pm25": (3, 4),
                         "filter_pct": (4, 1), "filter_days": (4, 4)},
}

MESSAGES = {
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
        "unsupported": "🌫 Модель очищувача {m} поки не підтримується.",
    },
}
# PM2.5 µg/m³ upper bounds for the quality words (US EPA 24-h breakpoints, 2024).
PM25_BANDS = (9, 35, 55)


class MiioError(Exception):
    pass


def md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()


class Device:
    """One miIO device. Each call does a fresh handshake (it gives the device id and clock the
    request must carry), so there is no session to go stale between /status requests."""

    def __init__(self, ip: str, token_hex: str, timeout: float = 2.5):
        self.ip, self.timeout = ip, timeout
        self.token = bytes.fromhex(token_hex)
        self.key = md5(self.token)
        self.iv = md5(self.key + self.token)
        self.msg_id = int.from_bytes(os.urandom(2), "big")
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

    def open(self, data: bytes) -> dict:
        """Check and decrypt a reply packet. A wrong checksum means a wrong token."""
        if len(data) < 32 or data[:2] != b"\x21\x31":
            raise MiioError("not a miIO reply")
        if md5(data[:16] + self.token + data[32:]) != data[16:32]:
            raise MiioError("bad checksum (wrong token?)")
        return json.loads(self.decrypt(data[32:]).rstrip(b"\0"))

    def call(self, method: str, params: list | dict) -> object:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(self.timeout)
            try:
                s.sendto(HELLO, (self.ip, PORT))
                hello, _ = s.recvfrom(1024)
                did, stamp = struct.unpack(">II", hello[8:16])
                self.msg_id = (self.msg_id + 1) % 65536
                s.sendto(self.packet(did, stamp + 1, json.dumps(
                    {"id": self.msg_id, "method": method, "params": params}).encode()), (self.ip, PORT))
                while True:  # skip a late reply to an earlier request
                    reply = self.open(s.recvfrom(4096)[0])
                    if reply.get("id") == self.msg_id:
                        break
            except socket.timeout:
                raise MiioError("no reply (wrong IP or token?)") from None
            except OSError as e:
                raise MiioError(type(e).__name__) from None
            except (ValueError, struct.error):  # undecryptable / not JSON: the token is wrong
                raise MiioError("unreadable reply (wrong token?)") from None
        if "error" in reply:
            raise MiioError(f"device error {reply['error']}")
        return reply.get("result")

    def read(self) -> dict:
        """{name: value} for the model's properties; raises MiioError, or KeyError for an unknown model."""
        if not self.model:
            info = self.call("miIO.info", [])
            self.model = info.get("model", "?") if isinstance(info, dict) else "?"
        props = MODELS[self.model]
        result = self.call("get_properties", [{"did": n, "siid": s, "piid": p} for n, (s, p) in props.items()])
        return {r["did"]: r["value"] for r in result or [] if isinstance(r, dict) and r.get("code") == 0}


def pm25_quality(v: float, lang: str) -> str:
    words = MESSAGES[lang]["quality"]
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
        lines.append(msgs["mode"].format(m=msgs["modes"].get(values["mode"], values["mode"])))
    if "filter_pct" in values:
        lines.append(msgs["filter"].format(p=values["filter_pct"], d=values.get("filter_days", "?")))
    if values.get("fault"):
        lines.append(msgs["faults"].get(values["fault"], f"⚠️ fault {values['fault']}"))
    return lines


def status(dev: Device, lang: str) -> list[str]:
    """The purifier's /status lines; never raises (a failure is a line too)."""
    try:
        return status_lines(dev.read(), lang)
    except KeyError:
        return [MESSAGES[lang]["unsupported"].format(m=dev.model)]
    except MiioError:
        return [MESSAGES[lang]["no_reply"]]
