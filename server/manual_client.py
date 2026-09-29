#!/usr/bin/env python3
"""Manual test client for the stage simulator (no phone needed)."""

import argparse
import json
import socket

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

HELP_TEXT = """Commands:
  ping
  state
  light <channel> <0-100>
  audio <channel> <0-100>
  mute <channel> on|off
  raw <text>      send text as-is (e.g. raw {bad json)
  demo            run a scripted sequence
  help, quit"""

DEMO_MESSAGES = [
    '{"cmd":"ping"}',
    '{"cmd":"get_state"}',
    '{"cmd":"set_level","type":"light","channel":3,"value":75}',
    '{"cmd":"set_level","type":"audio","channel":2,"value":50}',
    '{"cmd":"set_mute","channel":2,"mute":true}',
    '{"cmd":"get_state"}',
    "this is not json",
    '{"cmd":"set_level","type":"light","channel":9,"value":10}',
    '{"cmd":"set_level","type":"light","channel":1,"value":101}',
    '{"cmd":"reboot"}',
]


class LineReader:
    """Reads newline-terminated lines from a socket."""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""

    def read_line(self):
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line.decode("utf-8", errors="replace")


def build_message(line):
    parts = line.split()
    name = parts[0].lower()
    try:
        if name == "ping":
            return json.dumps({"cmd": "ping"})
        if name == "state":
            return json.dumps({"cmd": "get_state"})
        if name in ("light", "audio"):
            return json.dumps({
                "cmd": "set_level",
                "type": name,
                "channel": int(parts[1]),
                "value": int(parts[2]),
            })
        if name == "mute":
            return json.dumps({
                "cmd": "set_mute",
                "channel": int(parts[1]),
                "mute": parts[2].lower() in ("on", "true", "1"),
            })
        if name == "raw":
            text = line.strip()[3:].strip()
            if not text:
                raise ValueError("raw needs some text")
            return text
    except (IndexError, ValueError):
        raise ValueError("bad arguments, type 'help'")
    raise ValueError("unknown command, type 'help'")


def exchange(sock, reader, message):
    print("-> " + message)
    sock.sendall((message + "\n").encode("utf-8"))
    try:
        print("<- " + reader.read_line())
    except socket.timeout:
        print("<- (no response, timeout)")


def run_demo(sock, reader):
    for message in DEMO_MESSAGES:
        exchange(sock, reader, message)


def run_interactive(sock, reader):
    print(HELP_TEXT)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        name = line.split()[0].lower()
        if name in ("quit", "exit"):
            break
        if name == "help":
            print(HELP_TEXT)
            continue
        if name == "demo":
            run_demo(sock, reader)
            continue
        try:
            message = build_message(line)
        except ValueError as exc:
            print(exc)
            continue
        exchange(sock, reader, message)


def main():
    parser = argparse.ArgumentParser(description="Manual test client for the stage simulator")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--demo", action="store_true", help="run the scripted demo and exit")
    args = parser.parse_args()

    try:
        sock = socket.create_connection((args.host, args.port), timeout=args.timeout)
    except OSError as exc:
        print("Cannot connect to %s:%d -> %s" % (args.host, args.port, exc))
        return 1

    reader = LineReader(sock)
    with sock:
        try:
            if args.demo:
                run_demo(sock, reader)
            else:
                run_interactive(sock, reader)
        except OSError as exc:
            print("Connection error: %s" % exc)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())