#!/usr/bin/env python3
"""Small MongoDB client for the Network application database.

The UniFi OS Server image ships mongod and the legacy mongo shell, not
mongodump. Host backup uses mongodump through the container network
namespace. This client is what startup and the device helper use to update
ace.setting and to delete a device record. It speaks OP_MSG with no
authentication, which is the bundled server on port 27117 and the compose
MongoDB 4.4 server when MONGO_USER is empty.

The wire protocol is little-endian BSON. Bool is checked before int because
bool is a subclass of int.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import struct
import sys
from typing import Any

OP_MSG = 2013
MAX_MESSAGE = 16 * 1024 * 1024


class MongoError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def encode_cstring(value: str) -> bytes:
    raw = value.encode("utf-8")
    if b"\x00" in raw:
        raise ValueError("BSON cstring contains NUL")
    return raw + b"\x00"


def encode_string(value: str) -> bytes:
    raw = value.encode("utf-8") + b"\x00"
    return struct.pack("<i", len(raw)) + raw


def encode_document(document: dict[str, Any]) -> bytes:
    body = b"".join(encode_element(key, value) for key, value in document.items())
    return struct.pack("<i", len(body) + 5) + body + b"\x00"


def encode_element(key: str, value: Any) -> bytes:
    name = encode_cstring(key)
    if isinstance(value, bool):
        return b"\x08" + name + (b"\x01" if value else b"\x00")
    if isinstance(value, int) and not isinstance(value, bool):
        if -2_147_483_648 <= value <= 2_147_483_647:
            return b"\x10" + name + struct.pack("<i", value)
        if -2**63 <= value < 2**63:
            return b"\x12" + name + struct.pack("<q", value)
        raise ValueError(f"integer out of BSON range: {value}")
    if isinstance(value, float):
        return b"\x01" + name + struct.pack("<d", value)
    if isinstance(value, str):
        return b"\x02" + name + encode_string(value)
    if isinstance(value, dict):
        return b"\x03" + name + encode_document(value)
    if isinstance(value, list):
        array = {str(index): item for index, item in enumerate(value)}
        return b"\x04" + name + encode_document(array)
    if value is None:
        return b"\x0a" + name
    raise TypeError(f"cannot encode {type(value).__name__}")


def _need(buf: bytes, offset: int, size: int) -> None:
    if offset + size > len(buf):
        raise ValueError("truncated BSON")


def decode_document(buf: bytes, offset: int = 0) -> tuple[dict[str, Any], int]:
    _need(buf, offset, 4)
    length = struct.unpack_from("<i", buf, offset)[0]
    if length < 5 or offset + length > len(buf):
        raise ValueError("invalid BSON document length")
    end = offset + length
    if buf[end - 1] != 0:
        raise ValueError("BSON document is not NUL terminated")
    cursor = offset + 4
    document: dict[str, Any] = {}
    while cursor < end - 1:
        kind = buf[cursor]
        cursor += 1
        name, cursor = _decode_cstring(buf, cursor)
        value, cursor = decode_value(kind, buf, cursor)
        document[name] = value
    return document, end


def _decode_cstring(buf: bytes, offset: int) -> tuple[str, int]:
    end = buf.find(b"\x00", offset)
    if end < 0:
        raise ValueError("unterminated BSON cstring")
    return buf[offset:end].decode("utf-8"), end + 1


def decode_value(kind: int, buf: bytes, offset: int) -> tuple[Any, int]:
    if kind == 0x01:
        _need(buf, offset, 8)
        return struct.unpack_from("<d", buf, offset)[0], offset + 8
    if kind == 0x02:
        _need(buf, offset, 4)
        size = struct.unpack_from("<i", buf, offset)[0]
        if size < 1 or offset + 4 + size > len(buf):
            raise ValueError("invalid BSON string")
        raw = buf[offset + 4 : offset + 4 + size]
        if raw[-1:] != b"\x00":
            raise ValueError("BSON string is not NUL terminated")
        return raw[:-1].decode("utf-8"), offset + 4 + size
    if kind in (0x03, 0x04):
        document, end = decode_document(buf, offset)
        if kind == 0x04:
            return _array_from_document(document), end
        return document, end
    if kind == 0x05:
        _need(buf, offset, 5)
        size = struct.unpack_from("<i", buf, offset)[0]
        if size < 0 or offset + 5 + size > len(buf):
            raise ValueError("invalid BSON binary")
        return buf[offset + 5 : offset + 5 + size], offset + 5 + size
    if kind == 0x07:
        _need(buf, offset, 12)
        return buf[offset : offset + 12].hex(), offset + 12
    if kind == 0x08:
        _need(buf, offset, 1)
        return buf[offset] == 1, offset + 1
    if kind == 0x09:
        _need(buf, offset, 8)
        return struct.unpack_from("<q", buf, offset)[0], offset + 8
    if kind == 0x0A:
        return None, offset
    if kind == 0x10:
        _need(buf, offset, 4)
        return struct.unpack_from("<i", buf, offset)[0], offset + 4
    if kind == 0x11:
        _need(buf, offset, 8)
        return struct.unpack_from("<q", buf, offset)[0], offset + 8
    if kind == 0x12:
        _need(buf, offset, 8)
        return struct.unpack_from("<q", buf, offset)[0], offset + 8
    raise ValueError(f"unsupported BSON type {kind:#x}")


def _array_from_document(document: dict[str, Any]) -> list[Any]:
    if not document:
        return []
    try:
        indexes = sorted((int(key) for key in document), key=int)
    except ValueError:
        return list(document.values())
    return [document[str(index)] for index in indexes]


def encode_op_msg(document: dict[str, Any], request_id: int) -> bytes:
    body = encode_document(document)
    section = b"\x00" + body
    payload = struct.pack("<I", 0) + section
    length = 16 + len(payload)
    header = struct.pack("<iiii", length, request_id, 0, OP_MSG)
    return header + payload


def decode_op_msg(buf: bytes) -> dict[str, Any]:
    if len(buf) < 20:
        raise ValueError("truncated OP_MSG")
    length, _request_id, _response_to, opcode = struct.unpack_from("<iiii", buf, 0)
    if opcode != OP_MSG:
        raise ValueError(f"expected OP_MSG, got opcode {opcode}")
    if length != len(buf):
        raise ValueError("OP_MSG length does not match the buffer")
    flags = struct.unpack_from("<I", buf, 16)[0]
    end = length - 4 if flags & 1 else length
    offset = 20
    body: dict[str, Any] | None = None
    while offset < end:
        kind = buf[offset]
        offset += 1
        if kind == 0:
            document, offset = decode_document(buf, offset)
            body = document
        elif kind == 1:
            _need(buf, offset, 4)
            size = struct.unpack_from("<i", buf, offset)[0]
            if size < 4 or offset + size > end:
                raise ValueError("invalid OP_MSG document sequence")
            offset += size
        else:
            raise ValueError(f"unsupported OP_MSG section kind {kind}")
    if body is None:
        raise ValueError("OP_MSG reply has no body")
    return body


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise MongoError("MongoDB closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _command_ok(reply: dict[str, Any]) -> bool:
    ok = reply.get("ok", 0)
    try:
        return float(ok) == 1.0
    except (TypeError, ValueError):
        return False


class MongoClient:
    def __init__(self, host: str, port: int, timeout: float = 10) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._request_id = 0

    def __enter__(self) -> MongoClient:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as exc:
            raise MongoError(f"cannot connect to {self.host}:{self.port}: {exc}") from exc
        sock.settimeout(self.timeout)
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def command(self, database: str, body: dict[str, Any]) -> dict[str, Any]:
        self.connect()
        assert self._sock is not None
        document = dict(body)
        document["$db"] = database
        self._request_id += 1
        payload = encode_op_msg(document, self._request_id)
        self._sock.sendall(payload)
        header = _recv_exact(self._sock, 16)
        length = struct.unpack_from("<i", header, 0)[0]
        if length < 16 or length > MAX_MESSAGE:
            raise MongoError(f"MongoDB sent an invalid message length {length}")
        rest = _recv_exact(self._sock, length - 16)
        reply = decode_op_msg(header + rest)
        if not _command_ok(reply):
            code = reply.get("code")
            errmsg = str(reply.get("errmsg") or reply)
            raise MongoError(errmsg, code if isinstance(code, int) else None)
        return reply

    def ping(self) -> None:
        self.command("admin", {"ping": 1})

    def find(
        self,
        database: str,
        collection: str,
        query: dict[str, Any],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        reply = self.command(
            database,
            {"find": collection, "filter": query, "limit": limit},
        )
        cursor = reply.get("cursor")
        if not isinstance(cursor, dict):
            raise MongoError("find reply has no cursor")
        batch = cursor.get("firstBatch")
        if not isinstance(batch, list):
            raise MongoError("find reply has no firstBatch")
        cursor_id = cursor.get("id", 0)
        if isinstance(cursor_id, int) and cursor_id != 0:
            try:
                self.command(
                    database,
                    {"killCursors": collection, "cursors": [cursor_id]},
                )
            except MongoError:
                pass
        return [item for item in batch if isinstance(item, dict)]

    def update_many(
        self,
        database: str,
        collection: str,
        query: dict[str, Any],
        fields: dict[str, Any],
    ) -> tuple[int, int]:
        reply = self.command(
            database,
            {
                "update": collection,
                "updates": [
                    {
                        "q": query,
                        "u": {"$set": fields},
                        "upsert": False,
                        "multi": True,
                    }
                ],
            },
        )
        matched = reply.get("n", 0)
        modified = reply.get("nModified", matched)
        return int(matched), int(modified)

    def delete_many(self, database: str, collection: str, query: dict[str, Any]) -> int:
        reply = self.command(
            database,
            {
                "delete": collection,
                "deletes": [{"q": query, "limit": 0}],
            },
        )
        return int(reply.get("n", 0))


def normalize_mac(value: str) -> str:
    hexdigits = re.sub(r"[^0-9a-fA-F]", "", value)
    if len(hexdigits) != 12 or not re.fullmatch(r"[0-9a-fA-F]+", hexdigits):
        raise ValueError(f"not a MAC address: {value}")
    hexdigits = hexdigits.lower()
    return ":".join(hexdigits[index : index + 2] for index in range(0, 12, 2))


def mac_query(mac: str) -> dict[str, Any]:
    canonical = normalize_mac(mac)
    bare = canonical.replace(":", "")
    return {"mac": {"$in": [canonical, canonical.upper(), bare]}}


_SUMMARY_FIELDS = ("mac", "name", "model", "type", "state", "adopted")


def summarize_device(document: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if "_id" in document:
        summary["id"] = str(document["_id"])
    for key in _SUMMARY_FIELDS:
        if key in document and isinstance(document[key], (str, int, float, bool)):
            summary[key] = document[key]
    return summary


def inform_override(client: MongoClient, database: str, location: str) -> str:
    docs = client.find(database, "setting", {"key": "mgmt"}, limit=20)
    if not docs:
        return "missing"
    already = all(
        doc.get("override_inform_host") is True
        and doc.get("override_inform_host_location") == location
        for doc in docs
    )
    if already:
        return "unchanged"
    matched, _modified = client.update_many(
        database,
        "setting",
        {"key": "mgmt"},
        {
            "override_inform_host": True,
            "override_inform_host_location": location,
        },
    )
    if matched < 1:
        return "missing"
    return "changed"


def count_device(client: MongoClient, database: str, mac: str) -> dict[str, Any]:
    query = mac_query(mac)
    result: dict[str, Any] = {"mac": normalize_mac(mac)}
    for collection in ("device", "power_supervisor"):
        docs = client.find(database, collection, query, limit=20)
        result[collection] = {
            "count": len(docs),
            "matches": [summarize_device(doc) for doc in docs],
        }
    return result


def delete_device(client: MongoClient, database: str, mac: str) -> dict[str, int]:
    query = mac_query(mac)
    return {
        "device": client.delete_many(database, "device", query),
        "power_supervisor": client.delete_many(database, "power_supervisor", query),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--db", default="ace")
    parser.add_argument("--timeout", type=float, default=10)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping")
    sub.add_parser("normalize").add_argument("mac")

    inform = sub.add_parser("inform-override")
    inform.add_argument("--location", required=True)

    count = sub.add_parser("count-device")
    count.add_argument("--mac", required=True)

    delete = sub.add_parser("delete-device")
    delete.add_argument("--mac", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "normalize":
        try:
            print(normalize_mac(args.mac))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    try:
        with MongoClient(args.host, args.port, args.timeout) as client:
            if args.command == "ping":
                client.ping()
                print("ok")
                return 0
            if args.command == "inform-override":
                status = inform_override(client, args.db, args.location)
                print(status)
                return 2 if status == "missing" else 0
            if args.command == "count-device":
                print(json.dumps(count_device(client, args.db, args.mac), sort_keys=True))
                return 0
            if args.command == "delete-device":
                print(json.dumps(delete_device(client, args.db, args.mac), sort_keys=True))
                return 0
    except (MongoError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"unknown command {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
