"""离线合成夹具；不访问微信，不包含真实密钥或用户记录。"""

import hashlib
import hmac
import os
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from Crypto.Cipher import AES

import key_extractor as k


KEY = bytes(range(32))
SALT = bytes(range(160, 176))


def sqlite_fixture():
    """空 schema 页 + 一个 freelist trunk 页，覆盖非首页认证。"""
    page = bytearray(4096)
    page[:16] = b"SQLite format 3\x00"
    page[16:18] = struct.pack(">H", 4096)
    page[18:24] = bytes([2, 2, 80, 64, 32, 32])
    for offset, value in ((24, 1), (28, 2), (32, 2), (36, 1), (44, 4), (56, 1), (92, 1)):
        struct.pack_into(">I", page, offset, value)
    page[100] = 13
    struct.pack_into(">H", page, 105, 4016)
    return bytes(page) + bytes(4096)


def encrypt_fixture(plaintext):
    mac_key = hashlib.pbkdf2_hmac("sha512", KEY, bytes(v ^ 0x3A for v in SALT), 2, 32)
    result = bytearray()
    for index in range(len(plaintext) // 4096):
        page = plaintext[index * 4096:(index + 1) * 4096]
        start = 16 if index == 0 else 0
        iv = bytes([index + 1]) * 16
        encrypted = AES.new(KEY, AES.MODE_CBC, iv).encrypt(page[start:4016])
        tag = hmac.digest(mac_key, encrypted + iv + struct.pack("<I", index + 1), "sha512")
        result.extend((SALT if index == 0 else b"") + encrypted + iv + tag)
    return bytes(result)


class KeyExtractorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "emoticon.db"
        self.encrypted = encrypt_fixture(sqlite_fixture())
        self.db.write_bytes(self.encrypted)

    def extract(self, memory):
        stats, logs = {}, []

        def read(handle, address, size):
            for base, data in memory.items():
                if base <= address < base + len(data):
                    return data[address - base:address - base + size]
            return None

        with ExitStack() as stack:
            stack.enter_context(patch.object(k, "find_wechat_pid", return_value=123))
            stack.enter_context(patch.object(k.kernel32, "OpenProcess", return_value=123))
            close = stack.enter_context(patch.object(k.kernel32, "CloseHandle"))
            stack.enter_context(patch.object(k, "enum_regions", return_value=[
                (base, len(data)) for base, data in memory.items()
            ]))
            stack.enter_context(patch.object(k, "read_mem", side_effect=read))
            result = k.extract_key_for_db(str(self.db), logs.append, diagnostics=stats)
            close.assert_called_once_with(123)
        log = "\n".join(logs)
        self.assertNotIn(KEY.hex(), log)
        self.assertNotIn(SALT.hex(), log)
        self.assertTrue(all(type(value) is int for value in stats.values()))
        return result, stats

    def test_valid_database_and_atomic_output(self):
        self.assertTrue(k.verify_key_for_db(KEY, self.encrypted[:4096]))
        output = self.root / "plain.db"
        self.assertTrue(k.decrypt_database(str(self.db), KEY.hex(), str(output)))
        with closing(sqlite3.connect(output)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        self.assertEqual(self.db.read_bytes(), self.encrypted)

    def test_wrong_key_short_page_and_tampered_first_page(self):
        self.assertFalse(k.verify_key_for_db(bytes(32), self.encrypted[:4096]))
        self.assertFalse(k.verify_key_for_db(KEY, self.encrypted[:4095]))
        self.assertFalse(k.verify_key_for_db(b"short", self.encrypted[:4096]))
        for offset in (0, 100, 4020, 4095):
            with self.subTest(offset=offset):
                page = bytearray(self.encrypted[:4096])
                page[offset] ^= 1
                self.assertFalse(k.verify_key_for_db(KEY, page))

    def test_later_page_corruption_preserves_existing_output(self):
        corrupt = bytearray(self.encrypted)
        corrupt[4200] ^= 1
        self.db.write_bytes(corrupt)
        output = self.root / "existing.db"
        output.write_bytes(b"keep existing output")
        self.assertTrue(k.verify_key_for_db(KEY, corrupt[:4096]))
        self.assertFalse(k.decrypt_database(str(self.db), KEY.hex(), str(output)))
        self.assertEqual(output.read_bytes(), b"keep existing output")
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_authenticated_but_invalid_sqlite_is_rejected(self):
        corrupt = bytearray(sqlite_fixture())
        corrupt[100] = 0
        self.db.write_bytes(encrypt_fixture(corrupt))
        self.assertTrue(k.verify_key_for_db(KEY, self.db.read_bytes()[:4096]))
        self.assertIsNone(k._verified_database(str(self.db), KEY))

    def test_authenticated_invalid_header_is_rejected(self):
        corrupt = bytearray(sqlite_fixture())
        corrupt[20] = 0
        self.assertFalse(k.verify_key_for_db(KEY, encrypt_fixture(corrupt)[:4096]))

    def test_truncated_database_is_rejected(self):
        self.db.write_bytes(self.encrypted[:-1])
        self.assertIsNone(k._verified_database(str(self.db), KEY))

    def test_source_and_hardlink_output_are_protected(self):
        self.assertFalse(k.decrypt_database(str(self.db), KEY.hex(), str(self.db)))
        link = self.root / "alias.db"
        os.link(self.db, link)
        self.assertFalse(k.decrypt_database(str(self.db), KEY.hex(), str(link)))
        self.assertEqual(self.db.read_bytes(), self.encrypted)

    def test_legacy_uppercase_and_extended_literals(self):
        for run in (KEY.hex(), (KEY + SALT).hex(), (KEY + bytes(16) + SALT).hex()):
            with self.subTest(length=len(run)):
                result, stats = self.extract({0x10000: b"X'" + run.upper().encode() + b"'"})
                self.assertEqual(result, KEY.hex())
                self.assertEqual(stats["legacy_matches"], 1)
                self.assertEqual(stats["integrity_verified"], 1)

    def test_matching_salt_is_insufficient(self):
        literal = b"x'" + (bytes(32) + SALT).hex().encode() + b"'"
        result, stats = self.extract({0x10000: literal})
        self.assertIsNone(result)
        self.assertEqual(stats["target_candidates"], 1)
        self.assertEqual(stats["integrity_verified"], 0)

    def test_config_cipher_path(self):
        marker, node, config, blob_address = 0x10000, 0x20000, 0x30000, 0x40000
        node_bytes = bytearray(64)
        struct.pack_into("<QQ", node_bytes, 0x10, marker, len(k.CONFIG_CIPHER_NAME))
        struct.pack_into("<Q", node_bytes, 0x28, config)
        literal = b"x'" + (KEY + SALT).hex().encode() + b"'"
        blob = bytes(v ^ k.CONFIG_XOR_MASK[i % 32] for i, v in enumerate(literal))
        obj = bytearray(0xA0)
        struct.pack_into("<QQ", obj, 0x90, blob_address, len(blob))
        result, stats = self.extract({
            marker: k.CONFIG_CIPHER_NAME, node: bytes(node_bytes),
            config: bytes(obj), blob_address: blob,
        })
        self.assertEqual(result, KEY.hex())
        self.assertEqual(stats["legacy_matches"], 0)
        self.assertEqual(stats["cipher_matches"], 1)
        self.assertEqual(stats["integrity_verified"], 1)

    def test_invalid_config_object_falls_back(self):
        marker, node = 0x10000, 0x20000
        node_bytes = bytearray(64)
        struct.pack_into("<QQ", node_bytes, 0x10, marker, len(k.CONFIG_CIPHER_NAME))
        result, stats = self.extract({
            marker: k.CONFIG_CIPHER_NAME, node: bytes(node_bytes),
            0x30000: b"x'" + KEY.hex().encode() + b"'",
        })
        self.assertEqual(result, KEY.hex())
        self.assertEqual(stats["cipher_objects"], 0)

    def test_extraction_rejects_corrupt_database(self):
        self.db.write_bytes(self.encrypted[:-1])
        result, stats = self.extract({0x10000: b"x'" + KEY.hex().encode() + b"'"})
        self.assertIsNone(result)
        self.assertEqual(stats["page1_verified"], 1)
        self.assertEqual(stats["integrity_verified"], 0)

    def test_cross_chunk_literal_is_counted_once(self):
        literal = b"x'" + (KEY + SALT).hex().encode() + b"'"
        with patch.object(k, "SCAN_CHUNK", 256):
            result, stats = self.extract({0x10000: b"\x00" * 240 + literal + b"\x00" * 400})
        self.assertEqual(result, KEY.hex())
        self.assertEqual(stats["legacy_matches"], 1)
        self.assertEqual(stats["regions_read"], 1)

    def test_partial_reads_and_unreadable_gap(self):
        stats = dict.fromkeys(("regions_read", "bytes_read", "partial_reads", "read_failures"), 0)
        with patch.object(k, "read_mem", side_effect=[b"partial", None, b"suffix"]):
            chunks = list(k._scan_memory(1, [(0x10000, 4109)], stats, set()))
        self.assertEqual(chunks, [(0x10000, b"partial"), (0x11007, b"suffix")])
        self.assertEqual(stats["partial_reads"], 1)
        self.assertEqual(stats["read_failures"], 1)
        self.assertEqual(stats["bytes_read"], 13)

    def test_region_modifiers_guard_and_large_region(self):
        entries = iter(((0x10000, 600 * 1024**2, 0x204), (0x30000000, 4096, 0x104)))

        def query(handle, address, output, size):
            try:
                base, length, protect = next(entries)
            except StopIteration:
                return 0
            region = output._obj
            region.BaseAddress, region.RegionSize = base, length
            region.State, region.Protect = k.MEM_COMMIT, protect
            return size

        with patch.object(k.kernel32, "VirtualQueryEx", side_effect=query):
            self.assertEqual(k.enum_regions(1), [(0x10000, 600 * 1024**2)])

    def test_process_enumeration_failure_keeps_public_contract(self):
        with patch.object(k.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertEqual(k.find_wechat_pid(), 0)
        stats, logs = {}, []
        with patch.object(k, "find_wechat_pid", return_value=0):
            self.assertIsNone(k.extract_key_for_db(str(self.db), logs.append, diagnostics=stats))
        self.assertEqual(stats["regions_read"], 0)
        self.assertIn("integrity_verified=0", "\n".join(logs))


if __name__ == "__main__":
    unittest.main()
