# -*- coding: utf-8 -*-
"""Tests for the Agent bridge dispatcher (real XML-RPC over loopback).

Headless, so the bridge uses its ``direct`` executor (calls run in the RPC
thread under a lock); the GUI's Qt main-thread executor is exercised by the
end-to-end smoke test with FreeCAD open.
"""

import json
import socket
import unittest
import xmlrpc.client

import FreeCAD

from wavesim_gui import api, bridge


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BridgeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.bridge = bridge.Bridge(cls.port, "secret-token", executor="direct")
        cls.bridge.start()
        cls.proxy = xmlrpc.client.ServerProxy(
            "http://127.0.0.1:{}/RPC2".format(cls.port), allow_none=True)

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def tearDown(self):
        for name in list(FreeCAD.listDocuments()):
            FreeCAD.closeDocument(name)

    def call(self, fname, token="secret-token", **kwargs):
        return json.loads(self.proxy.call(token, fname, json.dumps(kwargs)))

    def test_token_required(self):
        res = self.call("active_document", token="wrong")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_type"], "AuthError")
        info = json.loads(self.proxy.bridge_info("wrong"))
        self.assertFalse(info["ok"])

    def test_info_and_functions(self):
        info = json.loads(self.proxy.bridge_info("secret-token"))["result"]
        self.assertEqual(info["api_version"], api.API_VERSION)
        self.assertEqual(info["protocol"], bridge.PROTOCOL_VERSION)
        funcs = json.loads(self.proxy.list_functions("secret-token"))["result"]
        names = {f["name"] for f in funcs}
        for expected in ("new_simulation", "create_box", "export_job",
                         "new_document", "model_fingerprint", "add_probe"):
            self.assertIn(expected, names)
        # Nothing that executes code is reachable.
        for forbidden in ("eval", "exec", "execute", "run_python",
                          "runCommand", "getattr"):
            self.assertNotIn(forbidden, names)

    def test_whitelist_and_usage_errors(self):
        res = self.call("__import__", name="os")
        self.assertEqual(res["error_type"], "UsageError")
        res = json.loads(self.proxy.call("secret-token", "new_document",
                                         "[1, 2]"))
        self.assertEqual(res["error_type"], "UsageError")
        res = self.call("new_document", bogus=1)
        self.assertEqual(res["error_type"], "UsageError")
        self.assertIn("signature", res["error"])
        res = self.call("new_document", _private=1)
        self.assertEqual(res["error_type"], "UsageError")
        # Methods other than the five endpoints are not exposed.
        with self.assertRaises(xmlrpc.client.Fault):
            self.proxy.system.listMethods()

    def test_end_to_end_calls(self):
        self.assertTrue(self.call("new_document", name="Bridge")["ok"])
        sim = self.call("new_simulation")
        self.assertTrue(sim["ok"])
        self.assertEqual(sim["result"]["label"], "Simulation")
        box = self.call("create_box", label="Sub", length="10 mm", width=10,
                        height=1.6)
        self.assertTrue(box["ok"], box)
        mat = self.call("create_material", label="FR4", eps=4.4,
                        bodies=["Sub"])
        self.assertTrue(mat["ok"], mat)
        err = self.call("create_material", label="FR4")
        self.assertFalse(err["ok"])
        self.assertEqual(err["error_type"], "ApiError")
        setup = self.call("list_setup")
        self.assertEqual(setup["result"]["materials"][2]["bodies"], ["Sub"])
        fp = self.call("model_fingerprint")["result"]
        self.assertEqual(len(fp["model_hash"]), 64)
        status = json.loads(self.proxy.status("secret-token"))["result"]
        self.assertIsNone(status["busy"])
        self.assertGreater(status["calls"], 3)

    def test_binds_loopback_only(self):
        with self.assertRaises(ValueError):
            bridge.Bridge(_free_port(), "t", host="0.0.0.0")
        with self.assertRaises(ValueError):
            bridge.Bridge(_free_port(), "")


if __name__ == "__main__":
    unittest.main()
