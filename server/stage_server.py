#!/usr/bin/env python3
"""Stage device simulator: newline-delimited JSON over TCP (standard library only)."""

import argparse
import json
import logging
import os
import socket
import threading

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000
CHANNEL_COUNT = 8
MIN_LEVEL = 0
MAX_LEVEL = 100
MAX_LINE_BYTES = 64 * 1024
VALID_TYPES = ("light", "audio")

log = logging.getLogger("stage_server")


class DeviceState:
    """Shared device state, protected by a lock. Channels are 1-based outside, 0-based inside."""

    def __init__(self):
        self._lock = threading.Lock()
        self._levels = {
            "light": [0] * CHANNEL_COUNT,
            "audio": [0] * CHANNEL_COUNT,
        }
        self._mute = [False] * CHANNEL_COUNT

    def set_level(self, level_type, channel, value):
        with self._lock:
            self._levels[level_type][channel - 1] = value

    def set_mute(self, channel, mute):
        with self._lock:
            self._mute[channel - 1] = mute

    def snapshot(self):
        with self._lock:
            return {
                "light": list(self._levels["light"]),
                "audio": list(self._levels["audio"]),
                "mute": list(self._mute),
            }


def _error(code):
    return {"status": "error", "code": code}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_channel(value):
    return _is_int(value) and 1 <= value <= CHANNEL_COUNT


def _valid_level(value):
    return _is_int(value) and MIN_LEVEL <= value <= MAX_LEVEL


def _handle_set_level(state, request):
    level_type = request.get("type")
    channel = request.get("channel")
    value = request.get("value")
    if level_type not in VALID_TYPES:
        return _error("INVALID_VALUE")
    if not _valid_channel(channel):
        return _error("INVALID_CHANNEL")
    if not _valid_level(value):
        return _error("INVALID_VALUE")
    state.set_level(level_type, channel, value)
    return {
        "status": "ok",
        "cmd": "set_level",
        "type": level_type,
        "channel": channel,
        "value": value,
    }


def _handle_set_mute(state, request):
    channel = request.get("channel")
    mute = request.get("mute")
    if not _valid_channel(channel):
        return _error("INVALID_CHANNEL")
    if not isinstance(mute, bool):
        return _error("INVALID_VALUE")
    state.set_mute(channel, mute)
    return {"status": "ok", "cmd": "set_mute", "channel": channel, "mute": mute}


def handle_request(state, text):
    """Parse one JSON line and return the response dict. Never raises on bad input."""
    try:
        request = json.loads(text)
    except (ValueError, RecursionError):
        return _error("INVALID_JSON")
    if not isinstance(request, dict):
        return _error("INVALID_JSON")

    cmd = request.get("cmd")
    if cmd == "ping":
        return {"status": "ok", "cmd": "pong"}
    if cmd == "set_level":
        return _handle_set_level(state, request)
    if cmd == "set_mute":
        return _handle_set_mute(state, request)
    if cmd == "get_state":
        response = {"status": "ok", "cmd": "get_state"}
        response.update(state.snapshot())
        return response
    return _error("UNKNOWN_CMD")


class StageServer:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, state=None):
        self.host = host
        self.port = port
        self.state = state if state is not None else DeviceState()
        self._listener = None
        self._stop_event = threading.Event()
        self._connections = set()
        self._connections_lock = threading.Lock()

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # On Windows SO_REUSEADDR allows port hijacking, so it is skipped there.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(16)
        listener.settimeout(0.5)
        self.port = listener.getsockname()[1]
        self._listener = listener
        log.info("Stage simulator listening on %s:%d", self.host, self.port)

    def serve_forever(self):
        if self._listener is None:
            self.start()
        while not self._stop_event.is_set():
            try:
                conn, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=self._handle_client, args=(conn, addr), daemon=True
            )
            thread.start()
        self._close_listener()

    def stop(self):
        self._stop_event.set()
        self._close_listener()
        with self._connections_lock:
            connections = list(self._connections)
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass

    def _close_listener(self):
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def _handle_client(self, conn, addr):
        peer = "%s:%d" % (addr[0], addr[1])
        conn.settimeout(1.0)
        with self._connections_lock:
            self._connections.add(conn)
        log.info("[%s] client connected", peer)

        buffer = b""
        discarding = False
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break

                if discarding:
                    if b"\n" not in chunk:
                        continue
                    chunk = chunk.split(b"\n", 1)[1]
                    discarding = False

                buffer += chunk
                alive = True
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    if not self._respond(conn, peer, raw_line):
                        alive = False
                        break
                if not alive:
                    break

                if len(buffer) > MAX_LINE_BYTES:
                    log.warning("[%s] line too long, discarding", peer)
                    buffer = b""
                    discarding = True
                    if not self._send(conn, peer, _error("INVALID_JSON")):
                        break
        finally:
            with self._connections_lock:
                self._connections.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            log.info("[%s] client disconnected", peer)

    def _respond(self, conn, peer, raw_line):
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            log.info("[%s] RECV <undecodable bytes>", peer)
            return self._send(conn, peer, _error("INVALID_JSON"))

        log.info("[%s] RECV %s", peer, text[:200])
        try:
            response = handle_request(self.state, text)
        except Exception:  # the server must never crash on bad input
            log.exception("[%s] unexpected error while handling request", peer)
            response = _error("INVALID_JSON")
        return self._send(conn, peer, response)

    def _send(self, conn, peer, response):
        payload = json.dumps(response, separators=(",", ":"))
        log.info("[%s] SEND %s", peer, payload)
        try:
            conn.sendall((payload + "\n").encode("utf-8"))
        except OSError:
            return False
        return True


def main():
    parser = argparse.ArgumentParser(description="Stage device simulator (TCP, newline-delimited JSON)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    server = StageServer(args.host, args.port)
    try:
        server.start()
        server.serve_forever()
    except OSError as exc:
        log.error("Cannot start server: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())