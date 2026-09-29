import json
import logging
import socket
import threading
import time
import unittest

from stage_server import DeviceState, StageServer, handle_request


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def call(state, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return handle_request(state, text)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.state = DeviceState()

    def assert_error(self, payload, code):
        self.assertEqual(call(self.state, payload), {"status": "error", "code": code})

    def test_ping(self):
        self.assertEqual(call(self.state, {"cmd": "ping"}), {"status": "ok", "cmd": "pong"})

    def test_set_level_light(self):
        response = call(self.state, {"cmd": "set_level", "type": "light", "channel": 3, "value": 75})
        self.assertEqual(
            response,
            {"status": "ok", "cmd": "set_level", "type": "light", "channel": 3, "value": 75},
        )
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["light"][2], 75)
        self.assertEqual(snapshot["audio"][2], 0)

    def test_set_level_audio(self):
        call(self.state, {"cmd": "set_level", "type": "audio", "channel": 8, "value": 40})
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["audio"][7], 40)
        self.assertEqual(snapshot["light"][7], 0)

    def test_level_and_channel_boundaries(self):
        for channel in (1, 8):
            for value in (0, 100):
                response = call(
                    self.state,
                    {"cmd": "set_level", "type": "light", "channel": channel, "value": value},
                )
                self.assertEqual(response["status"], "ok")

    def test_invalid_channel(self):
        for channel in (0, 9, -1, "3", None, True, 2.0):
            with self.subTest(channel=channel):
                self.assert_error(
                    {"cmd": "set_level", "type": "light", "channel": channel, "value": 10},
                    "INVALID_CHANNEL",
                )

    def test_missing_channel(self):
        self.assert_error({"cmd": "set_level", "type": "light", "value": 10}, "INVALID_CHANNEL")
        self.assert_error({"cmd": "set_mute", "mute": True}, "INVALID_CHANNEL")

    def test_invalid_value(self):
        for value in (-1, 101, "50", 50.5, None, True):
            with self.subTest(value=value):
                self.assert_error(
                    {"cmd": "set_level", "type": "audio", "channel": 1, "value": value},
                    "INVALID_VALUE",
                )

    def test_missing_value_and_bad_type(self):
        self.assert_error({"cmd": "set_level", "type": "light", "channel": 1}, "INVALID_VALUE")
        self.assert_error({"cmd": "set_level", "type": "video", "channel": 1, "value": 5}, "INVALID_VALUE")
        self.assert_error({"cmd": "set_level", "channel": 1, "value": 5}, "INVALID_VALUE")

    def test_set_mute(self):
        response = call(self.state, {"cmd": "set_mute", "channel": 2, "mute": True})
        self.assertEqual(response, {"status": "ok", "cmd": "set_mute", "channel": 2, "mute": True})
        self.assertTrue(self.state.snapshot()["mute"][1])
        call(self.state, {"cmd": "set_mute", "channel": 2, "mute": False})
        self.assertFalse(self.state.snapshot()["mute"][1])

    def test_set_mute_invalid(self):
        for mute in ("yes", 1, None):
            with self.subTest(mute=mute):
                self.assert_error({"cmd": "set_mute", "channel": 2, "mute": mute}, "INVALID_VALUE")
        self.assert_error({"cmd": "set_mute", "channel": 9, "mute": True}, "INVALID_CHANNEL")

    def test_get_state_initial(self):
        response = call(self.state, {"cmd": "get_state"})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["cmd"], "get_state")
        self.assertEqual(response["light"], [0] * 8)
        self.assertEqual(response["audio"], [0] * 8)
        self.assertEqual(response["mute"], [False] * 8)

    def test_get_state_after_changes(self):
        call(self.state, {"cmd": "set_level", "type": "light", "channel": 1, "value": 10})
        call(self.state, {"cmd": "set_level", "type": "audio", "channel": 5, "value": 90})
        call(self.state, {"cmd": "set_mute", "channel": 5, "mute": True})
        response = call(self.state, {"cmd": "get_state"})
        self.assertEqual(response["light"][0], 10)
        self.assertEqual(response["audio"][4], 90)
        self.assertTrue(response["mute"][4])

    def test_invalid_json(self):
        for text in ("not json", "{", "", "[1,2]", "42", '"text"', "null"):
            with self.subTest(text=text):
                self.assert_error(text, "INVALID_JSON")

    def test_unknown_cmd(self):
        for payload in ({"cmd": "reboot"}, {}, {"cmd": None}, {"cmd": ["ping"]}, {"cmd": "PING"}):
            with self.subTest(payload=payload):
                self.assert_error(payload, "UNKNOWN_CMD")

    def test_failed_command_does_not_change_state(self):
        before = self.state.snapshot()
        call(self.state, {"cmd": "set_level", "type": "light", "channel": 9, "value": 50})
        call(self.state, {"cmd": "set_level", "type": "light", "channel": 1, "value": 500})
        call(self.state, {"cmd": "set_mute", "channel": 1, "mute": "yes"})
        self.assertEqual(self.state.snapshot(), before)


class TcpTests(unittest.TestCase):
    def setUp(self):
        self.server = StageServer("127.0.0.1", 0)
        self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients = []

    def tearDown(self):
        for sock, reader in self.clients:
            reader.close()
            sock.close()
        self.server.stop()
        self.thread.join(timeout=3)

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=3)
        reader = sock.makefile("rb")
        client = (sock, reader)
        self.clients.append(client)
        return client

    def read_json(self, client):
        line = client[1].readline()
        self.assertTrue(line, "connection closed by server")
        return json.loads(line.decode("utf-8"))

    def exchange(self, client, text):
        client[0].sendall(text.encode("utf-8") + b"\n")
        return self.read_json(client)

    def test_ping_over_tcp(self):
        client = self.connect()
        self.assertEqual(self.exchange(client, '{"cmd":"ping"}'), {"status": "ok", "cmd": "pong"})

    def test_state_shared_between_clients(self):
        first = self.connect()
        second = self.connect()
        self.exchange(first, '{"cmd":"set_level","type":"light","channel":4,"value":60}')
        state = self.exchange(second, '{"cmd":"get_state"}')
        self.assertEqual(state["light"][3], 60)

    def test_invalid_json_keeps_connection_open(self):
        client = self.connect()
        self.assertEqual(
            self.exchange(client, "{broken"), {"status": "error", "code": "INVALID_JSON"}
        )
        self.assertEqual(self.exchange(client, '{"cmd":"ping"}')["cmd"], "pong")

    def test_invalid_utf8(self):
        client = self.connect()
        client[0].sendall(b"\xff\xfe\n")
        self.assertEqual(self.read_json(client), {"status": "error", "code": "INVALID_JSON"})
        self.assertEqual(self.exchange(client, '{"cmd":"ping"}')["cmd"], "pong")

    def test_two_lines_in_one_packet(self):
        client = self.connect()
        client[0].sendall(b'{"cmd":"ping"}\n{"cmd":"get_state"}\n')
        self.assertEqual(self.read_json(client)["cmd"], "pong")
        self.assertEqual(self.read_json(client)["cmd"], "get_state")

    def test_partial_line_is_reassembled(self):
        client = self.connect()
        client[0].sendall(b'{"cmd":')
        time.sleep(0.1)
        client[0].sendall(b'"ping"}\n')
        self.assertEqual(self.read_json(client)["cmd"], "pong")

    def test_oversized_line_is_rejected_once(self):
        client = self.connect()
        client[0].sendall(b"x" * 70000 + b"\n")
        self.assertEqual(self.read_json(client), {"status": "error", "code": "INVALID_JSON"})
        self.assertEqual(self.exchange(client, '{"cmd":"ping"}')["cmd"], "pong")

    def test_abrupt_disconnect_does_not_break_server(self):
        sock, reader = self.connect()
        sock.sendall(b'{"cmd":"pi')
        reader.close()
        sock.close()
        time.sleep(0.1)
        client = self.connect()
        self.assertEqual(self.exchange(client, '{"cmd":"ping"}')["cmd"], "pong")


if __name__ == "__main__":
    unittest.main()