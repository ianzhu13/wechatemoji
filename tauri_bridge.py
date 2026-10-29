"""Tauri Python worker bridge.

Protocol (line-delimited JSON on stdout):
- Event lines: {"name":"...","data":...}        — emitted immediately
- Result line: {"payload":...,"error":...}      — exactly one, last line
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import config
import bridge_common

# ──────────────────── Context ────────────────────


class BridgeContext:
    """Collects events and streams them to stdout in real-time."""

    def __init__(self) -> None:
        self._finished = False

    def emit(self, name: str, data: Any) -> None:
        event = {"name": name, "data": data}
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def log(self, message: str, level: str = "info") -> None:
        self.emit(
            "log",
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": message,
                "level": level,
            },
        )

    def finish(self, payload: Any = None, error: str | None = None) -> None:
        self._finished = True
        result = {"payload": payload, "error": error}
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()


# ──────────────────── Helpers ────────────────────


def _build_upload_env_detail(env: dict[str, Any]) -> str:
    return bridge_common.build_upload_env_detail(env)


def _normalize_user(info: dict[str, Any]) -> dict[str, Any]:
    return bridge_common.normalize_user(info)


def _emit_db_path_for_user(ctx: BridgeContext, user: dict[str, Any]) -> None:
    bridge_common.emit_db_path_for_user(ctx.emit, user)


def _collect_emoji_files(folder: str) -> list[str]:
    return bridge_common.collect_emoji_files(folder)


def _load_emoji_thumbs(ctx: BridgeContext, files: list[str]) -> None:
    bridge_common.load_emoji_thumbs(ctx.emit, files)


def _pause_signal_path() -> str:
    return bridge_common.pause_signal_path()


def _stop_upload_signal_path() -> str:
    return bridge_common.stop_upload_signal_path()


# ──────────────────── Commands ────────────────────


def cmd_init(ctx: BridgeContext, _payload: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "outputDir": config.EMOJI_OUTPUT_DIR,
        "uploadStatus": {
            "text": "ready. upload env check is now on-demand to keep startup responsive.",
            "tone": "neutral",
        },
        "auditSummary": {
            "tone": "neutral",
            "title": "audit not run",
            "detail": "audit will verify wechat extraction and feishu upload env.",
        },
        "busyState": {"busy": False, "action": ""},
    }
    return payload


def cmd_detect_wechat(ctx: BridgeContext, _payload: dict[str, Any]) -> None:
    ctx.emit(
        "wechatStatus",
        {"tone": "neutral", "title": "detecting wechat", "detail": "reading wechat accounts and dirs..."},
    )
    try:
        from wechat_extractor import get_wechat_info

        wx_infos = get_wechat_info()
        if wx_infos:
            users = [_normalize_user(info) for info in wx_infos]
            ctx.emit("userList", users)
            _emit_db_path_for_user(ctx, users[0])

            running_count = sum(1 for u in users if u.get("running"))
            if running_count > 0:
                title = f"detected {running_count} running wechat account(s)"
                detail = "can decrypt emoticon.db and export emojis."
                tone = "success"
                ctx.log(title, "success")
            else:
                title = f"found {len(users)} local wechat dirs, but no running process"
                detail = "db decryption will likely fail, tool will fall back to local cache."
                tone = "warning"
                ctx.log(title, "warn")

            ctx.emit("wechatStatus", {"tone": tone, "title": title, "detail": detail})
            return

        ctx.emit("userList", [])
        ctx.emit("dbPath", {"text": "no wechat data dir found", "ok": False})
        ctx.emit(
            "wechatStatus",
            {"tone": "error", "title": "wechat not detected", "detail": "please login wechat or select data dir manually."},
        )
        ctx.log("no wechat account or local data dir found", "error")
    except Exception as exc:
        ctx.emit(
            "wechatStatus",
            {"tone": "error", "title": "wechat detection failed", "detail": str(exc)},
        )
        ctx.log(f"wechat detection failed: {exc}", "error")


def cmd_on_user_changed(ctx: BridgeContext, payload: dict[str, Any]) -> None:
    raw = payload.get("value", "")
    try:
        user = json.loads(raw) if raw else {}
    except Exception:
        user = {}
    try:
        _emit_db_path_for_user(ctx, user)
    except Exception as exc:
        ctx.emit("dbPath", {"text": f"db detection failed: {exc}", "ok": False})


def cmd_set_wechat_dir(ctx: BridgeContext, payload: dict[str, Any]) -> None:
    path = payload.get("path", "")
    if not path or not os.path.isdir(path):
        ctx.log(f"invalid directory: {path}", "error")
        return
    config.WECHAT_FILES_ROOT = path
    ctx.log(f"wechat data root set to: {path}")
    ctx.emit("outputDir", config.EMOJI_OUTPUT_DIR)
    cmd_detect_wechat(ctx, {})


def cmd_set_output_dir(ctx: BridgeContext, payload: dict[str, Any]) -> dict[str, Any]:
    path = payload.get("path", "")
    if not path or not os.path.isdir(path):
        ctx.log(f"invalid directory: {path}", "error")
        return {"ok": False}
    config.EMOJI_OUTPUT_DIR = path
    ctx.emit("outputDir", path)
    ctx.log(f"output dir updated to: {path}")
    return {"ok": True}


def cmd_open_output_dir(ctx: BridgeContext, _payload: dict[str, Any]) -> dict[str, Any]:
    folder = config.EMOJI_OUTPUT_DIR
    os.makedirs(folder, exist_ok=True)
    try:
        os.startfile(folder)
        ctx.log(f"opened output dir: {folder}")
        return {"ok": True}
    except Exception as exc:
        ctx.log(f"failed to open output dir: {exc}", "error")
        return {"ok": False, "error": str(exc)}


def cmd_check_upload_env(ctx: BridgeContext, _payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from feishu_uploader import check_upload_environment

        upload_env = check_upload_environment()
    except Exception as exc:
        upload_env = {
            "ok": False,
            "python_package": False,
            "browser_runtime": False,
            "message": f"upload env check failed: {exc}",
        }

    payload = {
        "ok": bool(upload_env.get("ok")),
        "message": upload_env.get("message", ""),
        "detail": _build_upload_env_detail(upload_env),
    }
    ctx.emit("uploadEnv", payload)
    ctx.log(payload["message"], "success" if payload["ok"] else "warn")
    return payload


def cmd_load_from_folder(ctx: BridgeContext, payload: dict[str, Any]) -> None:
    folder = payload.get("path", "")
    if not folder or not os.path.isdir(folder):
        ctx.log("no folder selected", "error")
        return

    files = _collect_emoji_files(folder)
    if not files:
        ctx.log("no emoji files found in selected dir", "error")
        return

    config.EMOJI_OUTPUT_DIR = folder
    ctx.emit("outputDir", folder)
    _load_emoji_thumbs(ctx, files)
    ctx.log(f"loaded {len(files)} emoji files from dir")


def cmd_load_emoji_files(ctx: BridgeContext, payload: dict[str, Any]) -> None:
    raw_paths = payload.get("paths", [])
    if not raw_paths:
        ctx.log("no files selected", "error")
        return

    supported = {ext.lower() for ext in config.FEISHU_EMOJI_FORMATS}
    files = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() in supported:
            files.append(str(path))

    if not files:
        ctx.log("no supported emoji image files selected", "error")
        return

    files = sorted(dict.fromkeys(files))
    try:
        config.EMOJI_OUTPUT_DIR = os.path.commonpath(files)
    except ValueError:
        config.EMOJI_OUTPUT_DIR = str(Path(files[0]).parent)
    ctx.emit("outputDir", config.EMOJI_OUTPUT_DIR)
    _load_emoji_thumbs(ctx, files)
    ctx.log(f"loaded {len(files)} emoji files from file picker")


def cmd_start_extract(ctx: BridgeContext, payload: dict[str, Any]) -> None:
    from wechat_extractor import extract_emojis

    wxid = payload.get("wxid", "")
    output_dir = payload.get("output_dir", "") or config.EMOJI_OUTPUT_DIR
    if not wxid:
        ctx.log("请先选择一个微信账号", "error")
        ctx.finish(payload={"ok": False})
        return

    # Clear any existing pause signal
    pause_path = _pause_signal_path()
    if os.path.exists(pause_path):
        os.remove(pause_path)

    ctx.emit("busyState", {"busy": True, "action": "extract"})
    ctx.emit("progress", {"which": "extract", "percent": 0, "filename": ""})
    ctx.emit("pauseState", {"paused": False})
    ctx.log(f"开始提取微信表情: {wxid}（并发下载，支持随时暂停）")

    last_progress_time = [0.0]
    last_log_time = [0.0]

    def progress_cb(current, total, message):
        percent = int(current / total * 100) if total > 0 else 0
        filename = ""
        if "→ " in message:
            filename = message.split("→ ")[-1].strip()
        now = time.monotonic()
        finished = total > 0 and current >= total
        if finished or now - last_progress_time[0] >= 0.1:
            ctx.emit("progress", {"which": "extract", "percent": percent, "filename": filename})
            last_progress_time[0] = now
        if finished or now - last_log_time[0] >= 0.2:
            ctx.log(message)
            last_log_time[0] = now
        # Check pause signal
        if os.path.exists(pause_path):
            ctx.emit("pauseState", {"paused": True})
            files = _collect_emoji_files(output_dir)
            if files:
                _load_emoji_thumbs(ctx, files)
            ctx.log("已暂停导出。当前已导出的文件已刷新到预览，可点「继续」恢复，或直接导入已导出的部分。", "warn")
            while os.path.exists(pause_path):
                time.sleep(0.5)
            ctx.emit("pauseState", {"paused": False})
            ctx.log("继续导出…")

    try:
        files = extract_emojis(
            wxid=wxid,
            output_dir=output_dir,
            progress_callback=progress_cb,
            pause_event=None,  # we use file-based pause instead
            max_workers=5,
        )
    except Exception as exc:
        ctx.log(f"提取失败: {exc}", "error")
        files = []

    # Clean up pause signal
    if os.path.exists(pause_path):
        os.remove(pause_path)

    ctx.emit("pauseState", {"paused": False})

    if not files:
        output_dir_check = output_dir
        files = _collect_emoji_files(output_dir_check)

    if files:
        ctx.emit("progress", {"which": "extract", "percent": 100, "filename": ""})
        _load_emoji_thumbs(ctx, files)
        ctx.emit(
            "auditSummary",
            {"tone": "success", "title": f"已导出 {len(files)} 个表情", "detail": f"输出目录: {output_dir}"},
        )
        ctx.log(f"导出完成，共 {len(files)} 个表情，可以开始导入飞书", "success")
        ctx.finish(payload={"ok": True, "count": len(files)})
    else:
        ctx.emit("progress", {"which": "extract", "percent": 0, "filename": ""})
        ctx.log("未导出任何表情", "error")
        ctx.finish(payload={"ok": False})


def cmd_pause_extract(ctx: BridgeContext, _payload: dict[str, Any]) -> None:
    pause_path = _pause_signal_path()
    paused = os.path.exists(pause_path)
    if paused:
        os.remove(pause_path)
        ctx.emit("pauseState", {"paused": False})
        ctx.log("继续导出…")
    else:
        os.makedirs(os.path.dirname(pause_path), exist_ok=True)
        Path(pause_path).touch()
        files = _collect_emoji_files(config.EMOJI_OUTPUT_DIR)
        if files:
            _load_emoji_thumbs(ctx, files)
        ctx.emit("pauseState", {"paused": True})
        ctx.log("已暂停导出。当前已导出的文件已刷新到预览，可点「继续」恢复，或直接导入已导出的部分。", "warn")
    ctx.finish(payload={"paused": not paused})


def cmd_run_audit(ctx: BridgeContext, _payload: dict[str, Any]) -> None:
    from feishu_uploader import check_upload_environment
    from wechat_extractor import audit_extraction_pipeline

    ctx.emit("busyState", {"busy": True, "action": "audit"})
    ctx.emit(
        "auditSummary",
        {"tone": "neutral", "title": "running audit", "detail": "verifying wechat extraction and feishu upload env..."},
    )
    ctx.log("starting pipeline audit...")

    try:
        extraction = audit_extraction_pipeline(callback=lambda m: ctx.log(m))
        upload_env = check_upload_environment()

        payload = {
            "ok": bool(upload_env.get("ok")),
            "message": upload_env.get("message", ""),
            "detail": _build_upload_env_detail(upload_env),
        }
        ctx.emit("uploadEnv", payload)

        tone = "success" if extraction.get("ok") and upload_env.get("ok") else "warning"
        detail = (
            f"wechat extraction: {'pass' if extraction.get('ok') else 'fail'}, "
            f"sample downloaded {extraction.get('sample_downloaded', 0)}; "
            f"feishu upload env: {'ready' if upload_env.get('ok') else 'not ready'}."
        )
        ctx.emit("auditSummary", {"tone": tone, "title": extraction.get("message", "audit done"), "detail": detail})
        ctx.log("pipeline audit complete", "success" if extraction.get("ok") else "warn")
        ctx.finish(payload={"ok": True})
    except Exception as exc:
        ctx.emit("auditSummary", {"tone": "error", "title": "audit failed", "detail": str(exc)})
        ctx.log(f"audit failed: {exc}", "error")
        ctx.finish(payload={"ok": False}, error=str(exc))
    finally:
        ctx.emit("busyState", {"busy": False, "action": ""})


def cmd_start_upload(ctx: BridgeContext, payload: dict[str, Any]) -> None:
    from feishu_uploader import FeishuUploader, check_upload_environment

    files = payload.get("files", [])
    if not files:
        ctx.log("no emoji files to upload", "error")
        ctx.finish(payload={"ok": False})
        return

    mode = payload.get("mode", "personal")
    pack_name = payload.get("pack_name", "wechat_emoji_pack")

    if mode == "enterprise" and len(files) < config.FEISHU_EMOJI_PACK_MIN:
        ctx.log(f"enterprise mode needs at least {config.FEISHU_EMOJI_PACK_MIN} emojis, only {len(files)} selected", "error")
        ctx.finish(payload={"ok": False})
        return

    # 清除上次可能残留的停止信号
    stop_path = _stop_upload_signal_path()
    if os.path.exists(stop_path):
        os.remove(stop_path)

    ctx.emit("busyState", {"busy": True, "action": "upload"})
    ctx.emit("progress", {"which": "upload", "percent": 0})
    ctx.emit("uploadStatus", {"text": "checking feishu env...", "tone": "neutral"})
    ctx.log(f"准备导入飞书: mode={mode}, 选中 {len(files)} 个文件")

    # 打印前 3 个文件名帮助确认选择是否正确
    for f in files[:3]:
        ctx.log(f"  - {os.path.basename(f)}")
    if len(files) > 3:
        ctx.log(f"  ... 还有 {len(files) - 3} 个文件")

    try:
        upload_env = check_upload_environment()
        if not upload_env.get("ok"):
            ctx.emit("uploadStatus", {"text": upload_env.get("message", "upload env not ready"), "tone": "error"})
            ctx.log("feishu upload env not ready, cancelled", "error")
            ctx.emit("busyState", {"busy": False, "action": ""})
            ctx.finish(payload={"ok": False})
            return

        ctx.emit("uploadStatus", {"text": "launching feishu browser...", "tone": "neutral"})

        last_progress_time = [0.0]
        last_log_time = [0.0]

        def progress_cb(current, total, message):
            now = time.monotonic()
            if total > 0 and (now - last_progress_time[0] >= 0.1 or current >= total):
                ctx.emit("progress", {"which": "upload", "percent": int(current / total * 100)})
                last_progress_time[0] = now
            if now - last_log_time[0] >= 0.2 or current >= total:
                ctx.emit("uploadStatus", {"text": message, "tone": "neutral"})
                ctx.log(message)
                last_log_time[0] = now

        def should_stop():
            return os.path.exists(stop_path)

        uploader = FeishuUploader(headless=False, progress_callback=progress_cb)
        uploader.start()

        try:
            if not uploader.login(stop_check=should_stop):
                if not should_stop():
                    uploader.wait_for_browser_close(stop_check=should_stop)
                ctx.emit("busyState", {"busy": False, "action": ""})
                ctx.finish(payload={"ok": False, "stopped": should_stop()})
                return

            if mode == "personal":
                result = uploader.upload_personal_emojis(files, stop_check=should_stop)
            else:
                result = uploader.upload_enterprise_emojis(files, pack_name=pack_name, stop_check=should_stop)

            if (result.get("failed") or result.get("skipped")) and not should_stop():
                if result.get("message"):
                    ctx.log(result["message"], "warn")
                uploader.wait_for_browser_close(stop_check=should_stop)
            stopped = os.path.exists(stop_path)
            if stopped:
                os.remove(stop_path)
                ctx.log("上传已被用户中止", "warn")
                ctx.emit("uploadStatus", {"text": "上传已中止", "tone": "warning"})
            else:
                success = result.get("success", 0)
                failed = result.get("failed", 0)
                skipped = result.get("skipped", 0)
                tone = "warning" if skipped else ("success" if failed == 0 and success > 0 else ("warning" if success > 0 else "error"))
                processed = success + failed
                percent = int(100 * processed / (processed + skipped)) if processed + skipped else 0
                summary = result.get("message", f"import done: success {success}, failed {failed}")
                ctx.emit("progress", {"which": "upload", "percent": percent})
                ctx.emit("uploadStatus", {"text": summary, "tone": tone})
                ctx.log(summary, "success" if tone == "success" else "warn")

            ctx.finish(payload={"ok": not stopped and not result.get("failed") and not result.get("skipped"), "stopped": stopped, **result})
        except Exception:
            if not should_stop():
                uploader.wait_for_browser_close(stop_check=should_stop)
            raise
        finally:
            try:
                uploader.close()
            except Exception:
                pass

    except Exception as exc:
        ctx.log(f"feishu import failed: {exc}", "error")
        ctx.emit("uploadStatus", {"text": f"feishu import failed: {exc}", "tone": "error"})
        ctx.finish(payload={"ok": False}, error=str(exc))
    finally:
        ctx.emit("busyState", {"busy": False, "action": ""})


def cmd_stop_upload(ctx: BridgeContext, _payload: dict[str, Any]) -> None:
    stop_path = _stop_upload_signal_path()
    os.makedirs(os.path.dirname(stop_path), exist_ok=True)
    Path(stop_path).touch()
    ctx.log("正在中止上传...", "warn")
    ctx.finish(payload={"ok": True})


def cmd_not_implemented(ctx: BridgeContext, payload: dict[str, Any]) -> None:
    method = payload.get("method", "unknown")
    ctx.log(f"`{method}` is not migrated yet.", "warn")
    ctx.finish(payload={"ok": False})


# ──────────────────── Registry ────────────────────

COMMANDS: dict[str, Any] = {
    "init": cmd_init,
    "detectWechat": cmd_detect_wechat,
    "onUserChanged": cmd_on_user_changed,
    "setWechatDir": cmd_set_wechat_dir,
    "setOutputDir": cmd_set_output_dir,
    "openOutputDir": cmd_open_output_dir,
    "checkUploadEnv": cmd_check_upload_env,
    "loadFromFolder": cmd_load_from_folder,
    "loadEmojiFiles": cmd_load_emoji_files,
    "startExtract": cmd_start_extract,
    "pauseExtract": cmd_pause_extract,
    "runAudit": cmd_run_audit,
    "startUpload": cmd_start_upload,
    "stopUpload": cmd_stop_upload,
}


# ──────────────────── Entry point ────────────────────

def main() -> int:
    method = sys.argv[1] if len(sys.argv) > 1 else ""
    raw_payload = sys.argv[2] if len(sys.argv) > 2 else "{}"
    try:
        payload = json.loads(raw_payload) if raw_payload else {}
    except Exception:
        payload = {}

    ctx = BridgeContext()
    handler = COMMANDS.get(method, cmd_not_implemented)
    if handler is cmd_not_implemented:
        handler(ctx, {"method": method, **payload})
        if not ctx._finished:
            ctx.finish(payload={"ok": False})
    else:
        result = handler(ctx, payload)
        if not ctx._finished:
            ctx.finish(payload=result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
