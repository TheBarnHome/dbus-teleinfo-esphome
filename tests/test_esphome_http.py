#!/usr/bin/env python3
"""HTTP tests against a local stand-in for the ESPHome web server.

The fake server speaks the real protocol: one JSON entity per path, ``value``
null when nothing was published, 404 for an unknown entity. It also records the
raw request paths, which is the only way to prove the URL quoting is right.
"""

import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esphome_http import EspHomeClient, EspHomeError  # noqa: E402

ENTITIES = {
    "sensor/PAPP": {"id": "sensor/PAPP", "value": 918, "state": "918 W"},
    "sensor/PAPP 1": {"id": "sensor/PAPP 1", "value": 0, "state": "0 VA"},
    "sensor/Injection": {"id": "sensor/Injection", "value": None, "state": "NA"},
    "sensor/njouf+1": {"id": "sensor/njouf+1", "value": 1, "state": "1 Wh"},
    "sensor/bbrhcjb": {"id": "sensor/bbrhcjb", "value": 12480.25, "state": "12480.250 kWh"},
    "text_sensor/PTEC": {"id": "text_sensor/PTEC", "value": "HPJB", "state": "HPJB"},
}


class _Handler(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).requests.append(self.path)
        target = unquote(self.path.lstrip("/"))
        if target == "sensor/garbage":
            body = b"<html>not an entity</html>"
        elif target in ENTITIES:
            body = json.dumps(ENTITIES[target]).encode()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.requests = []
        self.client = EspHomeClient("127.0.0.1", self.port, timeout=2.0)

    def test_reads_a_sensor(self):
        self.assertEqual(self.client.fetch("sensor/PAPP"), 918)

    def test_reads_a_text_sensor(self):
        self.assertEqual(self.client.fetch("text_sensor/PTEC"), "HPJB")

    def test_null_is_no_data(self):
        self.assertIsNone(self.client.fetch("sensor/Injection"))

    def test_space_is_escaped(self):
        self.assertEqual(self.client.url_for("sensor/PAPP 1"), "/sensor/PAPP%201")
        self.assertEqual(self.client.fetch("sensor/PAPP 1"), 0)
        self.assertEqual(_Handler.requests, ["/sensor/PAPP%201"])

    def test_plus_is_escaped(self):
        # A bare '+' in a URL means a space: the entity name would never match.
        self.assertEqual(self.client.url_for("njouf+1"), "/sensor/njouf%2B1")
        self.assertEqual(self.client.fetch("njouf+1"), 1)
        self.assertEqual(_Handler.requests, ["/sensor/njouf%2B1"])

    def test_unknown_entity_raises(self):
        with self.assertRaises(EspHomeError) as caught:
            self.client.fetch("sensor/does-not-exist")
        self.assertIn("404", str(caught.exception))

    def test_non_json_answer_raises(self):
        with self.assertRaises(EspHomeError):
            self.client.fetch("sensor/garbage")

    def test_unreachable_host_raises(self):
        # Port 1 on loopback: connection refused, the common case when the ESP is
        # off or the firewall rule is missing.
        dead = EspHomeClient("127.0.0.1", 1, timeout=0.5)
        with self.assertRaises(EspHomeError):
            dead.fetch("sensor/PAPP")

    def test_fetch_all_survives_one_failure(self):
        values, errors = self.client.fetch_all(
            ["sensor/PAPP", "sensor/does-not-exist", "text_sensor/PTEC"]
        )
        self.assertEqual(values["sensor/PAPP"], 918)
        self.assertEqual(values["text_sensor/PTEC"], "HPJB")
        self.assertEqual(len(errors), 1)
        self.assertIn("404", errors[0])

    def test_fetch_all_returns_bare_names_canonically(self):
        values, _ = self.client.fetch_all(["PAPP", "text_sensor/PTEC"])
        self.assertIn("sensor/PAPP", values)
        self.assertIn("text_sensor/PTEC", values)

    def test_timeout_is_bounded(self):
        # A silent socket must fail in about the configured timeout, never block
        # the caller for longer: the poll thread can be stuck, the loop cannot.
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        port = silent.getsockname()[1]
        client = EspHomeClient("127.0.0.1", port, timeout=0.5)
        try:
            with self.assertRaises(EspHomeError):
                client.fetch("sensor/PAPP")
        finally:
            silent.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
