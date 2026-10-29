"""微信 PC 版密钥提取 & 数据库解密模块

优先只读定位 WCDB Config.Cipher 对象，保留旧版明文密钥扫描回退。
候选必须通过目标库逐页 HMAC、解密及 SQLite integrity_check；不记录密钥。
页面参数为 SQLCipher 4（4096 字节页 / SHA512 / 80 字节保留区）。
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import os
import re
import sqlite3
import struct
import subprocess
import tempfile
import time
from typing import Optional

from Crypto.Cipher import AES

import config

# ==================== 常量 ====================

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
SCAN_CHUNK = 4 * 1024 * 1024
SCAN_OVERLAP = 256
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
SQLITE_HDR = b"SQLite format 3\x00"
WECHAT_PROCESS_NAMES = {"WeChat.exe", "Weixin.exe"}
HEX_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
# Layout/mask reference: https://github.com/fanyuantaier/wechatauto-replica/blob/main/wechatauto/db.py
# shortcut: x64 WCDB 对象偏移和混淆掩码依赖微信版本，升级后须重新验证。
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b450048844c2448488944254048584c24"
)


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD),
        ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("_pad2", wt.DWORD),
    ]


kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.VirtualQueryEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL


# ==================== 进程查找 ====================

def find_wechat_pid(callback=None) -> int:
    """查找微信主进程 PID（占内存最大的那个）"""
    for name in WECHAT_PROCESS_NAMES:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode:
            if callback:
                callback("无法枚举微信进程：请检查当前运行环境的进程访问权限")
            continue
        best = (0, 0)
        for line in (r.stdout or "").strip().splitlines():
            if not line.strip():
                continue
            parts = line.strip('"').split('","')
            if len(parts) >= 5:
                try:
                    pid = int(parts[1])
                    mem = int(parts[4].replace(",", "").replace(" K", "").strip() or "0")
                    if mem > best[1]:
                        best = (pid, mem)
                except (ValueError, IndexError):
                    continue
        if best[0]:
            return best[0]
    return 0


def read_mem(handle, addr, size):
    """安全读取进程内存"""
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    kernel32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(n))
    if n.value:
        return buf.raw[: n.value]
    return None


def enum_regions(handle):
    """枚举所有可读内存区域"""
    regions = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
            break
        if mbi.State == MEM_COMMIT and not mbi.Protect & 0x100 and mbi.Protect & 0xFF in READABLE and mbi.RegionSize > 0:
            regions.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regions


def _scan_memory(handle, regions, stats, read_regions):
    """分块扫描，重叠覆盖跨块候选；读取失败按系统页继续。"""
    for base, size in regions:
        offset = 0
        tail = b""
        while offset < size:
            count = min(SCAN_CHUNK, size - offset)
            data = read_mem(handle, base + offset, count)
            if data:
                read_regions.add(base)
                stats["regions_read"] = len(read_regions)
                stats["bytes_read"] += len(data)
                if len(data) < count:
                    stats["partial_reads"] += 1
                yield base + offset - len(tail), tail + data
                tail = (tail + data)[-SCAN_OVERLAP:]
                offset += len(data)
            else:
                stats["read_failures"] += 1
                tail = b""
                offset += min(PAGE_SZ, size - offset)


# ==================== 密钥验证 ====================

def verify_key_for_db(enc_key: bytes, db_page1: bytes) -> bool:
    """验证目标库首页 HMAC 和解密后的 SQLite 头结构。"""
    if len(enc_key) != KEY_SZ or len(db_page1) != PAGE_SZ:
        return False
    mac_key = _mac_key(enc_key, db_page1[:SALT_SZ])
    if not _verify_page(mac_key, db_page1, 1):
        return False
    page = _decrypt_page(enc_key, db_page1, 1)
    return (
        int.from_bytes(page[16:18], "big") == PAGE_SZ
        and page[18] in (1, 2) and page[19] in (1, 2)
        and page[20] == RESERVE_SZ and page[21:24] == b"\x40\x20\x20"
    )


def _mac_key(enc_key, salt):
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def _verify_page(mac_key, page, pgno):
    if len(page) != PAGE_SZ:
        return False
    start = SALT_SZ if pgno == 1 else 0
    hmac_data = page[start : PAGE_SZ - HMAC_SZ]
    h = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    h.update(struct.pack("<I", pgno))
    return hmac_mod.compare_digest(h.digest(), page[-HMAC_SZ:])


def _verified_database(db_path, enc_key):
    """只读源库，逐页认证；明文仅在本进程内存中用于完整性验证。"""
    try:
        with open(db_path, "rb") as source:
            before = os.fstat(source.fileno())
            if before.st_size < PAGE_SZ or before.st_size % PAGE_SZ:
                return None
            page1 = source.read(PAGE_SZ)
            if not verify_key_for_db(enc_key, page1):
                return None
            mac_key = _mac_key(enc_key, page1[:SALT_SZ])
            plaintext = bytearray(_decrypt_page(enc_key, page1, 1))
            for pgno in range(2, before.st_size // PAGE_SZ + 1):
                page = source.read(PAGE_SZ)
                if not _verify_page(mac_key, page, pgno):
                    return None
                plaintext.extend(_decrypt_page(enc_key, page, pgno))
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return None
        # 独立快照不带 WAL；内存 SQLite 连接须使用 rollback 格式。
        plaintext[18:20] = b"\x01\x01"
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(plaintext)
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                return None
        finally:
            connection.close()
        return plaintext
    except (OSError, sqlite3.Error):
        return None


# ==================== 密钥提取 ====================

def _literal_candidates(run):
    """解析旧版/Config.Cipher 的十六进制串，不依赖字母大小写。"""
    if len(run) % 2:
        return
    raw = bytes.fromhex(run.decode("ascii"))
    starts = {0}
    if len(raw) > 48:
        starts.update(range(16, len(raw) - KEY_SZ + 1, 16))
        starts.add(len(raw) - KEY_SZ)
    for start in sorted(starts):
        key = raw[start:start + KEY_SZ]
        salt = raw[start + KEY_SZ:start + KEY_SZ + SALT_SZ]
        yield key, salt if len(salt) == SALT_SZ else None
    if len(raw) > 48:
        yield raw[:KEY_SZ], raw[-SALT_SZ:]


def _config_cipher_blob(handle, ref_addr, markers):
    node = read_mem(handle, ref_addr - 0x10, 0x40)
    if not node or len(node) != 0x40:
        return None
    name_ptr, name_len = struct.unpack_from("<QQ", node, 0x10)
    config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
    if name_ptr not in markers or name_len != len(CONFIG_CIPHER_NAME):
        return None
    if not 0x10000 <= config_ptr < 0x800000000000:
        return None
    obj = read_mem(handle, config_ptr + 0x88, 0x18)
    if not obj or len(obj) != 0x18:
        return None
    data_ptr, data_len = struct.unpack_from("<QQ", obj, 8)
    if not (0x10000 <= data_ptr < 0x800000000000 and 0 < data_len <= 1024):
        return None
    blob = read_mem(handle, data_ptr, data_len)
    if not blob or len(blob) != data_len:
        return None
    return bytes(value ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)] for i, value in enumerate(blob))


def extract_key_for_db(db_path: str, callback=None, *, diagnostics=None) -> Optional[str]:
    """返回经目标库完整验证的 64 字符 raw key；diagnostics 仅包含安全计数。"""
    stats = diagnostics if diagnostics is not None else {}
    stats.clear()
    stats.update(dict.fromkeys((
        "regions_total", "regions_read", "bytes_read", "read_failures", "partial_reads",
        "legacy_matches", "cipher_markers", "cipher_refs", "cipher_objects", "cipher_matches",
        "target_candidates", "validation_attempts", "page1_verified", "integrity_verified",
    ), 0))
    log = callback or (lambda message: None)
    handle = None
    started = time.monotonic()
    try:
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            log("需要 64 位 Python 才能扫描当前微信内存")
            return None
        if not hasattr(sqlite3.Connection, "deserialize"):
            log("当前 Python/SQLite 缺少内存完整性检查支持（需要 Python 3.11+）")
            return None
        with open(db_path, "rb") as source:
            db_page1 = source.read(PAGE_SZ)
        if len(db_page1) != PAGE_SZ:
            log("数据库文件过小")
            return None
        if db_page1.startswith(SQLITE_HDR):
            log("目标库为明文头格式；当前 32 字节密钥接口不支持显式 salt 模式")
            return None
        db_salt = db_page1[:SALT_SZ]
        pid = find_wechat_pid(log)
        if not pid:
            log("未找到运行中的微信进程")
            return None
        log(f"微信进程 PID={pid}")
        handle = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not handle:
            log(f"无法只读打开微信进程（Windows 错误码 {ctypes.get_last_error()}）")
            return None
        regions = enum_regions(handle)
        stats["regions_total"] = len(regions)
        log(f"扫描 {len(regions)} 个区域（每轮约 {sum(s for _, s in regions) / 1024**2:.0f} MB）")
        read_regions = set()
        markers = set()
        legacy = set()
        literal_addresses = set()
        log("第一轮：定位 Config.Cipher 标记并收集旧版候选...")
        for base, data in _scan_memory(handle, regions, stats, read_regions):
            pos = data.find(CONFIG_CIPHER_NAME)
            while pos >= 0:
                markers.add(base + pos)
                pos = data.find(CONFIG_CIPHER_NAME, pos + 1)
            for match in HEX_RE.finditer(data):
                address = base + match.start()
                if address not in literal_addresses:
                    literal_addresses.add(address)
                    stats["legacy_matches"] += 1
                    legacy.update(_literal_candidates(match.group(1)))
        stats["cipher_markers"] = len(markers)
        tested = set()
        associated = set()

        def try_candidate(key, salt):
            if salt == db_salt:
                associated.add(key)
                stats["target_candidates"] = len(associated)
            if key in tested:
                return None
            tested.add(key)
            stats["validation_attempts"] += 1
            if not verify_key_for_db(key, db_page1):
                return None
            associated.add(key)
            stats["target_candidates"] = len(associated)
            stats["page1_verified"] += 1
            if _verified_database(db_path, key) is None:
                log("候选通过首页验证，但全库认证/解密完整性失败（也可能是数据库正在变化）")
                return None
            stats["integrity_verified"] += 1
            return key.hex()

        if markers:
            log("第二轮：定位 Config.Cipher 对象并验证目标库...")
            references = re.compile(b"|".join(
                re.escape(struct.pack("<QQ", address, len(CONFIG_CIPHER_NAME)))
                for address in sorted(markers)
            ))
            visited = set()
            for base, data in _scan_memory(handle, regions, stats, read_regions):
                for match in references.finditer(data):
                    address = base + match.start()
                    if address in visited:
                        continue
                    visited.add(address)
                    stats["cipher_refs"] += 1
                    blob = _config_cipher_blob(handle, address, markers)
                    if blob is None:
                        continue
                    stats["cipher_objects"] += 1
                    for literal in HEX_RE.finditer(blob):
                        stats["cipher_matches"] += 1
                        for key, salt in _literal_candidates(literal.group(1)):
                            result = try_candidate(key, salt)
                            if result:
                                log("Config.Cipher 密钥已通过目标库逐页认证及 SQLite 完整性验证")
                                return result
        log("尝试旧版明文候选回退...")
        for key, salt in sorted(legacy, key=lambda item: (item[0], item[1] or b"")):
            result = try_candidate(key, salt)
            if result:
                log("旧版候选已通过目标库逐页认证及 SQLite 完整性验证")
                return result
        log("未找到通过目标库完整验证的密钥")
        return None
    except OSError:
        log("提取失败：数据库读取或微信进程访问受限；请检查权限及微信登录状态")
        return None
    finally:
        if handle:
            kernel32.CloseHandle(handle)
        log("诊断统计（区域数去重，读取字节包含各轮扫描）: " + ", ".join(
            f"{name}={value}" for name, value in stats.items()
        ))
        log(f"提取耗时 {time.monotonic() - started:.1f}s")


# ==================== 数据库解密 ====================

def decrypt_database(db_path: str, enc_key_hex: str, out_path: str, callback=None) -> bool:
    """解密 SQLCipher 加密的数据库文件

    Args:
        db_path: 加密的数据库路径
        enc_key_hex: 64 字符 hex 密钥
        out_path: 解密输出路径
        callback: 进度回调 callback(current_page, total_pages)

    Returns:
        是否成功
    """
    temporary_path = None
    try:
        if os.path.normcase(os.path.realpath(db_path)) == os.path.normcase(os.path.realpath(out_path)):
            return False
        if os.path.exists(out_path) and os.path.samefile(db_path, out_path):
            return False
        enc_key = bytes.fromhex(enc_key_hex)
        plaintext = _verified_database(db_path, enc_key)
        if plaintext is None:
            if callback:
                callback(0, 0)
            return False
        out_dir = os.path.dirname(out_path) or "."
        os.makedirs(out_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=out_dir, prefix=".wxemoji_", delete=False) as output:
            temporary_path = output.name
            output.write(plaintext)
        os.replace(temporary_path, out_path)
        temporary_path = None
        if callback:
            total_pages = len(plaintext) // PAGE_SZ
            callback(total_pages, total_pages)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if temporary_path is not None:
            os.unlink(temporary_path)


def _decrypt_page(enc_key, page_data, pgno):
    """解密单个页面"""
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]

    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        page = bytearray(SQLITE_HDR + decrypted + b"\x00" * RESERVE_SZ)
        return bytes(page)
    else:
        encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b"\x00" * RESERVE_SZ


# ==================== 便捷接口 ====================

def extract_and_decrypt(db_path: str, out_dir: str = None, callback=None) -> Optional[str]:
    """一键提取密钥并解密数据库

    Returns:
        解密后的数据库路径，或 None
    """
    if out_dir is None:
        out_dir = os.path.join(config.OUTPUT_DIR, "decrypted")
    os.makedirs(out_dir, exist_ok=True)

    db_name = os.path.basename(db_path)
    out_path = os.path.join(out_dir, db_name)

    # 提取密钥
    if callback:
        callback("正在从微信进程内存提取密钥...")
    key = extract_key_for_db(db_path, callback)

    if not key:
        if callback:
            callback("密钥提取失败")
        return None

    if callback:
        callback("密钥提取成功")
        callback("正在解密数据库...")

    # 解密
    def decrypt_progress(current, total):
        if total > 0 and callback:
            callback(f"解密进度: {current}/{total} 页 ({100 * current // total}%)")

    ok = decrypt_database(db_path, key, out_path, decrypt_progress)
    if ok:
        if callback:
            callback(f"解密完成: {out_path}")
        return out_path
    else:
        if callback:
            callback("解密失败")
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读提取并验证微信表情数据库密钥")
    parser.add_argument("database", help="目标 emoticon.db 路径")
    parser.add_argument("--diagnose", action="store_true", help="仅验证并输出安全统计，不保存密钥或明文库")
    args = parser.parse_args()
    if args.diagnose:
        result = extract_key_for_db(args.database, callback=print)
        print("验证成功" if result else "验证失败")
    else:
        result = extract_and_decrypt(args.database, callback=print)
        print(f"Decrypted: {result}" if result else "Failed")
    raise SystemExit(0 if result else 1)
