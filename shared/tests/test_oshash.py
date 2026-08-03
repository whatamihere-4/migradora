"""Tests for OSHash fingerprinting."""

from __future__ import annotations

import struct
import tempfile
import unittest

from migradora.oshash import compute_oshash, verify_oshash

_BLOCK = 65536


def _stash_oshash_from_chunks(size: int, head: bytes, tail: bytes) -> str:
    h = size
    chunks = _BLOCK // 8
    fmt = "<" + "Q" * chunks
    for v in struct.unpack_from(fmt, head, 0):
        h += v
    for v in struct.unpack_from(fmt, tail, 0):
        h += v
    h &= (1 << 64) - 1
    return f"{h:016x}"


class OshashTests(unittest.TestCase):
    def test_matches_stash_algorithm(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            data = b"a" * _BLOCK + b"b" * _BLOCK
            tmp.write(data)
            path = tmp.name
        head = data[:_BLOCK]
        tail = data[_BLOCK:]
        expected = _stash_oshash_from_chunks(len(data), head, tail)
        self.assertEqual(compute_oshash(path), expected)
        self.assertEqual(len(expected), 16)

    def test_large_file_tail_change(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"a" * 70000)
            tmp.write(b"b" * 70000)
            path = tmp.name
        h1 = compute_oshash(path)
        with open(path, "r+b") as f:
            f.seek(135000)
            f.write(b"z" * 1000)
        h2 = compute_oshash(path)
        self.assertNotEqual(h1, h2)

    def test_too_small_raises(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"small")
            path = tmp.name
        with self.assertRaises(ValueError):
            compute_oshash(path)


if __name__ == "__main__":
    unittest.main()
