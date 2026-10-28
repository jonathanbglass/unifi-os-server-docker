"""BSON/OP_MSG client used for the inform override and device delete."""

from __future__ import annotations

import socket
import struct
import threading
import unittest
from typing import Any

from scripts.unifi_mongo import (
    MongoClient,
    count_device,
    decode_document,
    delete_device,
    encode_document,
    encode_op_msg,
    decode_op_msg,
    inform_override,
    normalize_mac,
)


HELLO = bytes.fromhex("160000000268656c6c6f0006000000776f726c640000")


class FakeMongo:
    def __init__(self) -> None:
        self.docs: dict[str, list[dict[str, Any]]] = {
            "setting": [],
            "device": [],
            "power_supervisor": [],
        }
        self.updates: list[dict[str, Any]] = []
        self.deletes: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            socket.create_connection(("127.0.0.1", self.port), 1).close()
        except OSError:
            pass
        self._sock.close()
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        conn.settimeout(2)
        try:
            while not self._stop.is_set():
                header = _recv_exact(conn, 16)
                if header is None:
                    return
                length = struct.unpack_from("<i", header, 0)[0]
                rest = _recv_exact(conn, length - 16)
                if rest is None:
                    return
                request = decode_op_msg(header + rest)
                reply = self._dispatch(request)
                conn.sendall(encode_op_msg(reply, 1))
        except (OSError, ValueError):
            return
        finally:
            conn.close()

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if "ping" in request:
            return {"ok": 1.0}
        if "find" in request:
            name = str(request["find"])
            matched = [doc for doc in self.docs.get(name, []) if _matches(doc, request.get("filter") or {})]
            return {"cursor": {"id": 0, "ns": f"ace.{name}", "firstBatch": matched}, "ok": 1.0}
        if "update" in request:
            spec = request["updates"][0]
            self.updates.append(spec)
            fields = spec["u"]["$set"]
            matched = 0
            modified = 0
            for doc in self.docs.get(str(request["update"]), []):
                if _matches(doc, spec["q"]):
                    matched += 1
                    if any(doc.get(key) != value for key, value in fields.items()):
                        doc.update(fields)
                        modified += 1
            return {"n": matched, "nModified": modified, "ok": 1.0}
        if "delete" in request:
            spec = request["deletes"][0]
            self.deletes.append((str(request["delete"]), spec))
            name = str(request["delete"])
            kept = []
            removed = 0
            for doc in self.docs.get(name, []):
                if _matches(doc, spec["q"]):
                    removed += 1
                else:
                    kept.append(doc)
            self.docs[name] = kept
            return {"n": removed, "ok": 1.0}
        if "killCursors" in request:
            return {"ok": 1.0}
        return {"ok": 0.0, "errmsg": "unsupported", "code": 1}


def _recv_exact(conn: socket.socket, size: int) -> bytes | None:
    chunks = []
    remaining = size
    while remaining:
        try:
            chunk = conn.recv(remaining)
        except TimeoutError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if isinstance(expected, dict) and "$in" in expected:
            if document.get(key) not in expected["$in"]:
                return False
        elif document.get(key) != expected:
            return False
    return True


class MongoClientTests(unittest.TestCase):
    def test_bson_hello_vector_and_bool_is_not_an_int(self) -> None:
        self.assertEqual(encode_document({"hello": "world"}), HELLO)
        encoded = encode_document({"ok": True, "n": 1})
        decoded, end = decode_document(encoded)
        self.assertEqual(end, len(encoded))
        self.assertIs(decoded["ok"], True)
        self.assertEqual(decoded["n"], 1)
        self.assertEqual(encoded[4], 0x08)

    def test_normalize_mac(self) -> None:
        self.assertEqual(normalize_mac("AA-BB-CC-DD-EE-FF"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(normalize_mac("aabbccddeeff"), "aa:bb:cc:dd:ee:ff")
        with self.assertRaises(ValueError):
            normalize_mac("aa:bb")

    def test_inform_override_is_idempotent(self) -> None:
        server = FakeMongo()
        self.addCleanup(server.close)
        server.docs["setting"].append({"key": "mgmt", "x_ssh_username": "keep-me"})
        with MongoClient("127.0.0.1", server.port, timeout=2) as client:
            self.assertEqual(inform_override(client, "ace", "192.0.2.10"), "changed")
            self.assertEqual(inform_override(client, "ace", "192.0.2.10"), "unchanged")
        doc = server.docs["setting"][0]
        self.assertIs(doc["override_inform_host"], True)
        self.assertEqual(doc["override_inform_host_location"], "192.0.2.10")
        self.assertEqual(doc["x_ssh_username"], "keep-me")
        self.assertTrue(server.updates[0]["multi"])
        self.assertIs(server.updates[0]["upsert"], False)

    def test_missing_mgmt_document(self) -> None:
        server = FakeMongo()
        self.addCleanup(server.close)
        with MongoClient("127.0.0.1", server.port, timeout=2) as client:
            self.assertEqual(inform_override(client, "ace", "192.0.2.10"), "missing")
        self.assertEqual(server.updates, [])

    def test_delete_matches_mac_variants_and_hides_secrets(self) -> None:
        server = FakeMongo()
        self.addCleanup(server.close)
        server.docs["device"].append(
            {
                "mac": "AA:BB:CC:DD:EE:FF",
                "name": "switch",
                "model": "USW",
                "x_authkey": "secret-key",
            }
        )
        server.docs["power_supervisor"].append({"mac": "aabbccddeeff", "name": "smart-power"})
        server.docs["device"].append({"mac": "11:22:33:44:55:66", "name": "other"})
        with MongoClient("127.0.0.1", server.port, timeout=2) as client:
            summary = count_device(client, "ace", "aa-bb-cc-dd-ee-ff")
            self.assertEqual(summary["device"]["count"], 1)
            self.assertEqual(summary["device"]["matches"][0]["name"], "switch")
            self.assertNotIn("x_authkey", summary["device"]["matches"][0])
            deleted = delete_device(client, "ace", "aa:bb:cc:dd:ee:ff")
        self.assertEqual(deleted, {"device": 1, "power_supervisor": 1})
        self.assertEqual(server.deletes[0][1]["limit"], 0)
        self.assertEqual([doc["mac"] for doc in server.docs["device"]], ["11:22:33:44:55:66"])
        self.assertEqual(server.docs["power_supervisor"], [])


if __name__ == "__main__":
    unittest.main()
