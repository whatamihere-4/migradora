"""Tests for OSHash fingerprinting."""

from __future__ import annotations

import tempfile
import unittest

from migradora.oshash import compute_oshash, verify_oshash


class OshashTests(unittest.TestCase):
    def test_small_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"hello world")
            path = tmp.name
        h = compute_oshash(path)
        self.assertEqual(len(h), 32)
        self.assertTrue(verify_oshash(path, h))

    def test_large_file_reads_head_and_tail(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
