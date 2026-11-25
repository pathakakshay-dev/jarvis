"""Native messaging host for the JARVIS Browser Bridge extension.

Chrome or Edge launches this process. It reads length-prefixed JSON from stdin,
forwards validated traffic to the local JARVIS bridge ingress, and writes one
length-prefixed JSON reply to stdout. It does not open webpages or use DevTools.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

_MAX_MESSAGE_BYTES = 16384


def main() -> int:
    try:
        raw = _read_message()
        if raw is None:
            return 1
        if len(raw) > _MAX_MESSAGE_BYTES:
            _write_message({"ok": False, "error": "too_large"})
            return 1
        payload = json.loads(raw.decode("utf-8"))
        config = _load_config()
        if config is None:
            _write_message({"ok": False, "error": "config"})
            return 1
        reply = _forward(config, payload, len(raw))
        _write_message(reply)
        return 0 if reply.get("ok") else 1
    except Exception:
        try:
            _write_message({"ok": False, "error": "failed"})
        except Exception:
            pass
        return 1


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_config() -> dict | None:
    path = _project_root() / "data" / "bridge_native.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    host = data.get("host")
    port = data.get("port")
    token = data.get("bridge_token")
    if host != "127.0.0.1":
        return None
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        return None
    if not isinstance(token, str) or len(token) < 32:
        return None
    return {"host": host, "port": port, "bridge_token": token}


def _forward(config: dict, payload: object, raw_size: int) -> dict:
    import urllib.error
    import urllib.request

    body = json.dumps({"message": payload, "raw_size": raw_size}, separators=(",", ":")).encode("utf-8")
    if len(body) > _MAX_MESSAGE_BYTES + 512:
        return {"ok": False, "error": "too_large"}
    request = urllib.request.Request(
        f"http://{config['host']}:{config['port']}/api/browser/bridge/ingress",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Jarvis-Bridge-Token": config["bridge_token"],
            "Host": f"{config['host']}:{config['port']}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(8192)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(8192)
        except Exception:
            return {"ok": False, "error": "http"}
    except Exception:
        return {"ok": False, "error": "unreachable"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"ok": False, "error": "invalid"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid"}
    return data


def _read_message() -> bytes | None:
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        return None
    length = struct.unpack("<I", header)[0]
    if length <= 0 or length > _MAX_MESSAGE_BYTES:
        return None
    body = sys.stdin.buffer.read(length)
    if len(body) != length:
        return None
    return body


def _write_message(payload: dict) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_MESSAGE_BYTES:
        raw = b'{"ok":false,"error":"too_large"}'
    sys.stdout.buffer.write(struct.pack("<I", len(raw)))
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    raise SystemExit(main())
