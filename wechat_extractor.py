"""微信 PC 版表情包提取模块（Windows）。"""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import shutil
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO
from pathlib import Path
from typing import Optional

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from PIL import Image

import config


def wechat_data_roots() -> list[str]:
    """返回当前机器上可能存在的微信数据根目录。"""
    roots = [
        os.path.join(os.path.expanduser("~"), "Documents", "xwechat_files"),
        config.WECHAT_FILES_ROOT,
    ]

    unique = []
    seen = set()
    for root in roots:
        if not root:
            continue
        normalized = os.path.normcase(os.path.abspath(root))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(root)
    return unique


def get_wechat_info() -> list[dict]:
    """获取微信账号信息。

    返回值中会显式包含 `running` 字段，避免把仅存在本地目录误判成“已登录”。
    """
    from key_extractor import find_wechat_pid

    pid = find_wechat_pid()
    info_map: dict[str, dict] = {}

    for root_dir in wechat_data_roots():
        if not os.path.isdir(root_dir):
            continue

        for entry in os.listdir(root_dir):
            if not (
                entry.startswith("wxid_")
                or os.path.isdir(os.path.join(root_dir, entry, "db_storage"))
            ):
                continue
            full_dir = os.path.join(root_dir, entry)
            if not os.path.isdir(full_dir):
                continue

            existing = info_map.setdefault(
                entry,
                {
                    "wxid": entry,
                    "wx_dir": full_dir,
                    "pid": pid if pid else 0,
                    "nickname": "",
                    "key": "",
                    "running": bool(pid),
                },
            )
            if not existing.get("wx_dir"):
                existing["wx_dir"] = full_dir

    if pid:
        try:
            from pywxdump import WX_OFFS, get_wx_info as _get_wx_info

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                pywx_infos = _get_wx_info(WX_OFFS, is_print=False) or []

            for info in pywx_infos:
                wxid = info.get("wxid") or ""
                wx_dir = info.get("wx_dir") or ""
                if not wxid and wx_dir:
                    wxid = os.path.basename(wx_dir.rstrip("\\/"))
                if not wxid:
                    continue

                merged = info_map.setdefault(
                    wxid,
                    {
                        "wxid": wxid,
                        "wx_dir": wx_dir,
                        "pid": info.get("pid", pid) or pid,
                        "nickname": "",
                        "key": "",
                        "running": True,
                    },
                )

                if wx_dir:
                    merged["wx_dir"] = wx_dir
                if info.get("nickname"):
                    merged["nickname"] = info["nickname"]
                if info.get("key"):
                    merged["key"] = info["key"]
                merged["pid"] = info.get("pid", pid) or pid
                merged["running"] = True
        except Exception:
            pass

    items = list(info_map.values())
    items.sort(key=lambda item: (not item.get("running", False), item.get("wxid", "")))
    return items


def discover_wechat_users() -> list[str]:
    """扫描本地微信数据目录，发现所有用户目录。"""
    users = set()
    for root_dir in wechat_data_roots():
        if not os.path.isdir(root_dir):
            continue
        for entry in os.listdir(root_dir):
            full_path = os.path.join(root_dir, entry)
            if os.path.isdir(full_path) and (
                entry.startswith("wxid_")
                or os.path.isdir(os.path.join(full_path, "db_storage"))
                ):
                users.add(entry)
    return sorted(users)


def find_emoticon_db(wx_dir: str) -> Optional[str]:
    """在微信数据目录中查找 emoticon.db。"""
    candidates = []

    if wx_dir:
        candidates.extend(
            [
                os.path.join(wx_dir, "db_storage", "emoticon", "emoticon.db"),
                os.path.join(wx_dir, "FileStorage", "Emoticon", "emoticon.db"),
                os.path.join(wx_dir, "Emoticon", "emoticon.db"),
            ]
        )
    else:
        for root_dir in wechat_data_roots():
            if not os.path.isdir(root_dir):
                continue
            for wxid in discover_wechat_users():
                candidates.append(
                    os.path.join(root_dir, wxid, "db_storage", "emoticon", "emoticon.db")
                )

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def decrypt_emoticon_db(key: str, db_path: str, out_path: str) -> bool:
    """使用 PyWxDump 解密 emoticon.db。"""
    try:
        from pywxdump import decrypt

        success, _ = decrypt(key, db_path, out_path)
        return success
    except Exception as exc:
        print(f"[错误] 数据库解密失败: {exc}")
        return False


def query_emoji_urls(db_path: str) -> list[dict]:
    """从解密后的 emoticon.db 查询表情 URL。"""
    if not os.path.isfile(db_path):
        return []

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

        emojis = []

        if "kFavEmoticonOrderTable" in tables and "kNonStoreEmoticonTable" in tables:
            cursor.execute(
                """
                SELECT o.md5, n.cdn_url, n.tp_url, n.thumb_url, n.extern_url, n.encrypt_url
                FROM kFavEmoticonOrderTable o
                LEFT JOIN kNonStoreEmoticonTable n ON o.md5 = n.md5
                ORDER BY o.rowid
                """
            )
            for row in cursor.fetchall():
                url = _pick_best_url(row[1:])
                if url:
                    emojis.append({"md5": row[0], "url": url, "source": "fav"})

        if "kCustomEmoticonOrderTable" in tables and "kNonStoreEmoticonTable" in tables:
            existing_md5s = {emoji["md5"] for emoji in emojis}
            cursor.execute(
                """
                SELECT o.md5, n.cdn_url, n.tp_url, n.thumb_url, n.extern_url, n.encrypt_url
                FROM kCustomEmoticonOrderTable o
                LEFT JOIN kNonStoreEmoticonTable n ON o.md5 = n.md5
                ORDER BY o.rowid
                """
            )
            for row in cursor.fetchall():
                if row[0] in existing_md5s:
                    continue
                url = _pick_best_url(row[1:])
                if url:
                    emojis.append({"md5": row[0], "url": url, "source": "custom"})

        if not emojis and "kNonStoreEmoticonTable" in tables:
            cursor.execute(
                """
                SELECT md5, cdn_url, tp_url, thumb_url, extern_url, encrypt_url
                FROM kNonStoreEmoticonTable
                """
            )
            for row in cursor.fetchall():
                url = _pick_best_url(row[1:])
                if url:
                    emojis.append({"md5": row[0], "url": url, "source": "direct"})

        return emojis
    finally:
        conn.close()


def _pick_best_url(urls) -> Optional[str]:
    if not urls:
        return None

    best_url = None
    best_score = -9999

    for url in urls:
        if not url or not isinstance(url, str):
            continue

        score = 0
        if url.startswith("https://"):
            score += 20
        if "/stodownload" in url:
            score += 1000
        if "wxapp.tc.qq.com" in url:
            score += 500
        elif "vweixinf.tc.qq.com" in url:
            score += 400
        if "filekey=" in url:
            score += 100
        if "m=" in url:
            score += 50
        if "mmbiz.qpic.cn" in url:
            score -= 300
        if "/mmemoticon/" in url:
            score -= 100

        if score > best_score:
            best_score = score
            best_url = url

    return best_url


def download_emoji(url: str, timeout: int = 15) -> Optional[bytes]:
    """从微信 CDN 下载表情图片。"""
    for candidate_url in _get_url_candidates(url):
        try:
            request = urllib.request.Request(
                candidate_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
            if data and len(data) > 100 and detect_image_format(data):
                return data
        except Exception:
            continue
    return None


def _get_url_candidates(url: str) -> list[str]:
    exts = ["gif", "jpg", "png", "webp"]
    if "/stodownload" not in url:
        return [url]

    candidates = [url]
    for ext in exts:
        variant = url.replace("/stodownload?", f"/stodownload.{ext}?")
        if variant not in candidates:
            candidates.append(variant)
    return candidates


def scan_custom_emotion_cache(wx_dir: str) -> list[str]:
    """扫描 CustomEmotion 本地缓存目录。"""
    candidates = [
        os.path.join(wx_dir, "FileStorage", "CustomEmotion"),
        os.path.join(wx_dir, "CustomEmotion"),
        os.path.join(wx_dir, "FileStorage", "MsgAttach", "CustomEmotion"),
    ]

    emoji_files = []
    for directory in candidates:
        if not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory):
            for filename in files:
                full_path = os.path.join(root, filename)
                try:
                    if os.path.getsize(full_path) > 100:
                        emoji_files.append(full_path)
                except OSError:
                    continue
    return emoji_files


def detect_image_format(data: bytes) -> Optional[str]:
    """根据文件头检测图片格式。"""
    if len(data) < 3:
        return None
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:3] == b"GIF":
        return "gif"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"

    for index in range(min(20, len(data))):
        chunk = data[index:]
        if len(chunk) >= 3 and chunk[:3] == b"GIF":
            return "gif"
        if len(chunk) >= 4 and chunk[:4] == b"\x89PNG":
            return "png"
        if len(chunk) >= 2 and chunk[:2] == b"\xff\xd8":
            return "jpg"
    return None


def list_exported_emoji_files(output_dir: str) -> list[str]:
    """列出输出目录中的已导出表情文件。"""
    supported = {ext.lower() for ext in config.FEISHU_EMOJI_FORMATS}
    files = []
    for path in Path(output_dir).iterdir():
        if path.is_file() and path.suffix.lower() in supported:
            files.append(str(path))
    files.sort()
    return files


def decrypt_v1mmwx(data: bytes, wxid: str) -> bytes:
    """尝试解密旧版 V1MMWX 本地缓存文件。"""
    if not data.startswith(config.V1MMWX_HEADER) or not wxid:
        return data

    encrypted = data[len(config.V1MMWX_HEADER) :]
    if not encrypted:
        return b""

    key = PBKDF2(
        wxid.encode("utf-8"),
        config.AES_KEY_SALT,
        dkLen=config.AES_KEY_LENGTH,
        count=config.AES_KEY_ITERATIONS,
    )
    cipher = AES.new(key, AES.MODE_CBC, iv=config.AES_IV)

    aligned_len = len(encrypted) // 16 * 16
    decrypted_head = b""
    if aligned_len:
        decrypted_head = cipher.decrypt(encrypted[:aligned_len]).rstrip(b"\x00 ")

    tail = encrypted[aligned_len:]
    if tail and len(wxid) >= 2:
        xor_key = ord(wxid[-2])
        decrypted_tail = bytes(byte ^ xor_key for byte in tail)
    else:
        decrypted_tail = tail
    return decrypted_head + decrypted_tail


def process_image(
    data: bytes,
    max_size: int = config.FEISHU_EMOJI_MAX_SIZE_KB * 1024,
) -> bytes:
    """调整图片尺寸与压缩率，尽量满足飞书规格。"""
    fmt = detect_image_format(data)
    if fmt is None:
        return data

    image = Image.open(BytesIO(data))
    max_dim = config.FEISHU_EMOJI_DIMENSION

    if max(image.size) > max_dim:
        ratio = max_dim / max(image.size)
        new_size = (int(image.size[0] * ratio), int(image.size[1] * ratio))
        if fmt == "gif" and getattr(image, "n_frames", 1) > 1:
            return _resize_gif(image, new_size)
        image = image.resize(new_size, Image.Resampling.LANCZOS)

    output = BytesIO()
    save_fmt = "PNG" if fmt == "png" else ("JPEG" if fmt == "jpg" else "GIF")
    if fmt == "webp":
        save_fmt = "WEBP"

    quality = 90
    while quality >= 20:
        output.seek(0)
        output.truncate()
        save_kwargs = {"format": save_fmt}
        if save_fmt in {"JPEG", "WEBP"}:
            save_kwargs["quality"] = quality
        image.save(output, **save_kwargs)
        if output.tell() <= max_size:
            break
        quality -= 10

    return output.getvalue()


def _resize_gif(image: Image.Image, new_size: tuple[int, int]) -> bytes:
    frames = []
    durations = []
    try:
        for frame_index in range(image.n_frames):
            image.seek(frame_index)
            frame = image.copy().resize(new_size, Image.Resampling.LANCZOS)
            frames.append(frame)
            durations.append(image.info.get("duration", 100))

        output = BytesIO()
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=image.info.get("loop", 0),
            optimize=True,
        )
        return output.getvalue()
    except Exception:
        output = BytesIO()
        image.seek(0)
        fallback = image.copy().resize(new_size, Image.Resampling.LANCZOS)
        fallback.save(output, format="GIF")
        return output.getvalue()


def extract_emojis(
    wxid: Optional[str] = None,
    output_dir: Optional[str] = None,
    process: bool = True,
    progress_callback=None,
    pause_event: Optional[threading.Event] = None,
    max_workers: int = 5,
) -> list[str]:
    """从微信提取表情到本地目录。"""

    def _log(message: str) -> None:
        print(message)
        if progress_callback:
            progress_callback(0, 0, message)

    if output_dir is None:
        output_dir = config.EMOJI_OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)

    extracted = []
    downloaded_md5s = set()

    _log("[Step 1] 检测微信账号信息...")
    wx_infos = get_wechat_info()
    if not wx_infos:
        _log("[错误] 未检测到任何微信数据目录")
        return []

    target_info = None
    if wxid:
        for info in wx_infos:
            current_wxid = info.get("wxid", "")
            if current_wxid == wxid or current_wxid.startswith(wxid):
                target_info = info
                break
    else:
        target_info = wx_infos[0]

    if not target_info:
        _log(f"[错误] 未找到指定微信用户: {wxid}")
        return []

    wxid = target_info.get("wxid", "")
    wx_dir = target_info.get("wx_dir", "")
    running = bool(target_info.get("running"))
    _log(f"[信息] 目标微信用户: {wxid}")

    if not running:
        _log("[警告] 当前没有检测到运行中的微信进程，数据库解密大概率会失败，将优先尝试本地缓存兜底")

    _log("[Step 2] 查找表情数据库...")
    db_path = find_emoticon_db(wx_dir)
    if not db_path:
        _log("[警告] 未找到 emoticon.db，改为扫描本地缓存")
        return _extract_from_local_cache(wxid, output_dir, process, progress_callback, wx_dir)

    _log(f"[信息] 找到数据库: {db_path}")

    _log("[Step 3] 提取密钥并解密数据库...")
    from key_extractor import extract_and_decrypt

    os.makedirs(config.TMP_DIR, exist_ok=True)
    temp_work_dir = tempfile.mkdtemp(prefix="wxemoji_db_", dir=config.TMP_DIR)
    try:
        decrypted_db = extract_and_decrypt(db_path, temp_work_dir, callback=_log)
        if not decrypted_db:
            _log("[错误] 数据库解密失败，改为扫描本地缓存")
            return _extract_from_local_cache(wxid, output_dir, process, progress_callback, wx_dir)

        _log("[Step 4] 查询表情记录...")
        emoji_entries = query_emoji_urls(decrypted_db)
        if not emoji_entries:
            _log("[警告] 数据库中没有表情记录，改为扫描本地缓存")
            return _extract_from_local_cache(wxid, output_dir, process, progress_callback, wx_dir)

        # 断点续传：跳过已经存在于 output_dir 的文件（按 md5 文件名匹配）
        existing_files = {Path(name).stem for name in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, name))}
        for entry in emoji_entries:
            if entry["md5"] in existing_files:
                downloaded_md5s.add(entry["md5"])
        if downloaded_md5s:
            _log(f"[信息] 发现 {len(downloaded_md5s)} 个已存在文件，跳过重复下载")

        _log(f"[信息] 共找到 {len(emoji_entries)} 条表情记录")
        _log(f"[Step 5] 开始从微信 CDN 并发下载表情（{max_workers} 线程）...")

        counter_lock = threading.Lock()
        md5_lock = threading.Lock()
        success_count = [len(downloaded_md5s)]
        failed_count = [0]
        completed_count = [len(downloaded_md5s)]
        total = len(emoji_entries)

        def _download_entry(entry):
            """单条下载任务，返回 (output_path_or_None, md5)。"""
            if pause_event is not None:
                pause_event.wait()

            md5_value = entry["md5"]
            with md5_lock:
                if md5_value in downloaded_md5s:
                    return None, md5_value

            try:
                data = download_emoji(entry["url"])
                if not data:
                    return None, md5_value

                if process:
                    try:
                        data = process_image(data)
                    except Exception:
                        pass

                image_format = detect_image_format(data) or "gif"
                filename = f"{md5_value}.{image_format}" if md5_value else f"emoji.{image_format}"
                output_path = os.path.join(output_dir, filename)
                with open(output_path, "wb") as handle:
                    handle.write(data)

                with md5_lock:
                    downloaded_md5s.add(md5_value)

                return output_path, md5_value
            except Exception:
                return None, md5_value

        pending = [entry for entry in emoji_entries if entry["md5"] not in downloaded_md5s]
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {pool.submit(_download_entry, entry): entry for entry in pending}
            for future in as_completed(future_map):
                output_path, _ = future.result()
                with counter_lock:
                    completed_count[0] += 1
                    if output_path:
                        extracted.append(output_path)
                        success_count[0] += 1
                    else:
                        failed_count[0] += 1
                    done = completed_count[0]
                    success = success_count[0]
                    failed = failed_count[0]

                if progress_callback:
                    filename = os.path.basename(output_path) if output_path else ""
                    progress_callback(
                        done,
                        total,
                        f"下载中 {done}/{total} 成功 {success} 失败 {failed}{' → ' + filename if filename else ''}",
                    )

        _log(f"[完成] CDN 下载完成: 成功 {success_count[0]}，失败 {failed_count[0]}")

        cache_files = scan_custom_emotion_cache(wx_dir)
        if cache_files:
            _log(f"[补充] 发现 {len(cache_files)} 个本地缓存文件")
            cache_added = _process_local_files(
                cache_files,
                output_dir,
                process,
                downloaded_md5s,
                progress_callback,
                wxid=wxid,
            )
            extracted.extend(cache_added)
            _log(f"[补充] 从本地缓存补充 {len(cache_added)} 个表情")

        all_files = list_exported_emoji_files(output_dir)
        _log(f"[完成] 共导出 {len(all_files)} 个表情 → {output_dir}")
        return all_files
    finally:
        shutil.rmtree(temp_work_dir, ignore_errors=True)


def _extract_from_local_cache(
    wxid: Optional[str],
    output_dir: str,
    process: bool,
    progress_callback,
    wx_dir: str = "",
) -> list[str]:
    def _log(message: str) -> None:
        print(message)
        if progress_callback:
            progress_callback(0, 0, message)

    if not wx_dir:
        if not wxid:
            users = discover_wechat_users()
            if not users:
                _log("[错误] 未找到微信本地用户目录")
                return []
            wxid = users[0]

        for root_dir in wechat_data_roots():
            candidate = os.path.join(root_dir, wxid)
            if os.path.isdir(candidate):
                wx_dir = candidate
                break

    cache_files = scan_custom_emotion_cache(wx_dir)
    if not cache_files:
        _log("[错误] 本地缓存中也没有找到可用表情文件")
        return []

    _log(f"[信息] 正在处理 {len(cache_files)} 个本地缓存文件")
    _process_local_files(
        cache_files,
        output_dir,
        process,
        existing_md5s=set(),
        progress_callback=progress_callback,
        wxid=wxid,
    )
    return list_exported_emoji_files(output_dir)


def _process_local_files(
    files: list[str],
    output_dir: str,
    process: bool,
    existing_md5s: set,
    progress_callback,
    wxid: Optional[str] = None,
) -> list[str]:
    extracted = []

    for index, filepath in enumerate(files, start=1):
        try:
            with open(filepath, "rb") as handle:
                data = handle.read()

            image_format = detect_image_format(data)
            if image_format is None and wxid:
                decrypted = decrypt_v1mmwx(data, wxid)
                if decrypted != data:
                    data = decrypted
                    image_format = detect_image_format(data)

            if image_format is None:
                continue

            digest = hashlib.md5(data).hexdigest()
            if digest in existing_md5s:
                continue

            if process:
                try:
                    data = process_image(data)
                    image_format = detect_image_format(data) or image_format
                except Exception:
                    pass

            filename = f"{digest}.{image_format}"
            output_path = os.path.join(output_dir, filename)
            with open(output_path, "wb") as handle:
                handle.write(data)

            extracted.append(output_path)
            existing_md5s.add(digest)
        except Exception:
            continue

        if progress_callback and (index % 20 == 0 or index == len(files)):
            progress_callback(index, len(files), f"处理本地文件 {index}/{len(files)}")

    return extracted


def audit_extraction_pipeline(
    wxid: Optional[str] = None,
    sample_downloads: int = 5,
    callback=None,
) -> dict:
    """执行真实提取链路体检。"""

    def _log(message: str) -> None:
        print(message)
        if callback:
            callback(message)

    result = {
        "ok": False,
        "wxid": "",
        "wx_dir": "",
        "db_path": "",
        "emoji_rows": 0,
        "sample_downloaded": 0,
        "sample_dir": "",
        "running": False,
        "message": "",
    }

    wx_infos = get_wechat_info()
    if not wx_infos:
        result["message"] = "未检测到微信数据目录"
        _log(f"[审计] {result['message']}")
        return result

    target_info = None
    if wxid:
        for info in wx_infos:
            current_wxid = info.get("wxid", "")
            if current_wxid == wxid or current_wxid.startswith(wxid):
                target_info = info
                break
    else:
        target_info = wx_infos[0]

    if not target_info:
        result["message"] = f"未找到指定微信用户: {wxid}"
        _log(f"[审计] {result['message']}")
        return result

    result["wxid"] = target_info.get("wxid", "")
    result["wx_dir"] = target_info.get("wx_dir", "")
    result["running"] = bool(target_info.get("running"))

    db_path = find_emoticon_db(result["wx_dir"])
    result["db_path"] = db_path or ""
    if not db_path:
        result["message"] = "未找到 emoticon.db"
        _log(f"[审计] {result['message']}")
        return result

    work_dir = tempfile.mkdtemp(prefix="wxemoji_audit_")
    result["sample_dir"] = work_dir
    _log(f"[审计] 临时审计目录: {work_dir}")

    try:
        from key_extractor import extract_and_decrypt

        decrypted_db = extract_and_decrypt(db_path, work_dir, callback=_log)
        if not decrypted_db:
            result["message"] = "数据库解密失败"
            _log(f"[审计] {result['message']}")
            return result

        rows = query_emoji_urls(decrypted_db)
        result["emoji_rows"] = len(rows)
        if not rows:
            result["message"] = "数据库中没有查到表情记录"
            _log(f"[审计] {result['message']}")
            return result

        _log(f"[审计] 共查询到 {len(rows)} 条表情记录")
        downloaded = 0
        for entry in rows[:sample_downloads]:
            data = download_emoji(entry["url"])
            if not data:
                continue
            data = process_image(data)
            image_format = detect_image_format(data) or "gif"
            out_path = os.path.join(work_dir, f"{entry['md5']}.{image_format}")
            with open(out_path, "wb") as handle:
                handle.write(data)
            downloaded += 1
            _log(f"[审计] 样本下载成功: {os.path.basename(out_path)}")

        result["sample_downloaded"] = downloaded
        result["ok"] = downloaded > 0
        result["message"] = (
            f"已验证微信提取链路，可查询 {len(rows)} 条记录，样本成功下载 {downloaded} 个"
            if downloaded > 0
            else "能解密数据库，但样本下载未成功"
        )
        _log(f"[审计] {result['message']}")
        return result
    finally:
        # Clean up temp directory
        try:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    exported = extract_emojis()
    if exported:
        print(f"\n成功提取 {len(exported)} 个表情!")
    else:
        print("\n未能提取任何表情。")
