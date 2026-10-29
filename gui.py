"""PyWebView GUI: WeChat Emoji -> Feishu Import."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import webview

import config
import bridge_common


class Api:
    """Python API exposed to frontend JS."""

    def __init__(self):
        # pywebview 会递归导出公开属性，原生窗口必须保留为内部引用。
        self._window = None
        self.emoji_files: list[str] = []
        self.upload_env: dict = {
            "ok": False,
            "message": "还没有检查飞书上传环境",
            "python_package": False,
            "browser_runtime": False,
        }
        self._busy = False
        self._busy_action = ""
        self._pause_event = threading.Event()
        self._pause_event.set()   # 初始未暂停（set = 可运行）
        self._paused = False
        self._current_output_dir = config.EMOJI_OUTPUT_DIR
        # ── 性能优化：单线程 emit 队列 + 节流计时 ──
        self._emit_queue: queue.Queue = queue.Queue()
        self._last_progress_time = 0.0
        self._last_log_time = 0.0
        threading.Thread(target=self._emit_drainer, daemon=True).start()

    def _emit_drainer(self):
        """将所有 evaluate_js 调用序列化到单线程，彻底消除多线程并发导致的 UI 冻结。"""
        while True:
            try:
                js_code = self._emit_queue.get(timeout=0.05)
                if self._window:
                    try:
                        self._window.evaluate_js(js_code)
                    except Exception:
                        pass
            except queue.Empty:
                pass

    def init(self):
        return {
            "outputDir": config.EMOJI_OUTPUT_DIR,
            "uploadStatus": {
                "text": "准备就绪，需要时再检查上传环境，这样启动会更轻快。",
                "tone": "neutral",
            },
            "auditSummary": {
                "tone": "neutral",
                "title": "还没有运行诊断",
                "detail": "诊断会检查微信提取链路和飞书上传环境。",
            },
            "busyState": {"busy": False, "action": ""},
        }

    # ---- WeChat detection ----

    def detectWechat(self):
        self._emit(
            "wechatStatus",
            {"tone": "neutral", "title": "正在检测微信", "detail": "正在读取微信账号和数据目录，请稍等一下。"},
        )
        threading.Thread(target=self._detect_wechat_worker, daemon=True).start()

    def onUserChanged(self, value):
        try:
            data = json.loads(value)
        except Exception:
            data = {}
        self._emit_db_path_for_user(data)

    def browseWechatDir(self):
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG, directory=config.WECHAT_FILES_ROOT
        )
        if not result:
            return
        config.WECHAT_FILES_ROOT = result[0]
        self._log(f"已设置微信数据目录：{result[0]}")
        self.detectWechat()

    def browseOutputDir(self):
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG, directory=config.EMOJI_OUTPUT_DIR
        )
        if not result:
            return
        config.EMOJI_OUTPUT_DIR = result[0]
        self._current_output_dir = result[0]
        self._emit("outputDir", result[0])
        self._log(f"已更新导出目录：{result[0]}")

    def openOutputDir(self):
        folder = config.EMOJI_OUTPUT_DIR
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)
            self._log(f"已打开导出目录：{folder}")
        except Exception as exc:
            self._log(f"打开导出目录失败：{exc}", "error")

    # ---- Upload env check ----

    def checkUploadEnv(self):
        threading.Thread(target=self._check_upload_env_worker, daemon=True).start()

    def _detect_wechat_worker(self):
        try:
            from wechat_extractor import get_wechat_info

            wx_infos = get_wechat_info()
            if wx_infos:
                users = [self._normalize_user(info) for info in wx_infos]
                self._emit("userList", users)
                self._emit_db_path_for_user(users[0])

                running_count = sum(1 for u in users if u.get("running"))
                if running_count > 0:
                    title = f"检测到 {running_count} 个正在运行的微信账号"
                    detail = "可以继续解密 emoticon.db 并导出表情。"
                    tone = "success"
                    self._log(title, "success")
                else:
                    title = f"找到了 {len(users)} 个微信数据目录，但当前没有运行中的微信进程"
                    detail = "数据库解密大概率会失败，工具会尽量回退到本地缓存。"
                    tone = "warning"
                    self._log(title, "warn")

                self._emit("wechatStatus", {"tone": tone, "title": title, "detail": detail})
                return

            self._emit("userList", [])
            self._emit("dbPath", {"text": "没有找到微信数据目录", "ok": False})
            self._emit(
                "wechatStatus",
                {"tone": "error", "title": "没有检测到微信", "detail": "请先登录微信，或者手动选择微信数据目录。"},
            )
            self._log("没有找到微信账号或本地数据目录", "error")
        except Exception as exc:
            self._emit(
                "wechatStatus",
                {"tone": "error", "title": "微信检测失败", "detail": str(exc)},
            )
            self._log(f"微信检测失败：{exc}", "error")

    def _check_upload_env_worker(self):
        self.upload_env = self._resolve_upload_env()
        payload = {
            "ok": bool(self.upload_env.get("ok")),
            "message": self.upload_env.get("message", ""),
            "detail": self._build_upload_env_detail(self.upload_env),
        }
        self._emit("uploadEnv", payload)
        self._log(payload["message"], "success" if payload["ok"] else "warn")

    def _resolve_upload_env(self) -> dict:
        try:
            from feishu_uploader import check_upload_environment

            return check_upload_environment()
        except Exception as exc:
            return {
                "ok": False,
                "python_package": False,
                "browser_runtime": False,
                "message": f"上传环境检查失败：{exc}",
            }

    def runAudit(self):
        if self._busy:
            return

        self._set_busy(True, "audit")
        self._emit(
            "auditSummary",
            {"tone": "neutral", "title": "正在运行诊断", "detail": "正在检查微信提取链路和飞书上传环境…"},
        )
        self._log("开始运行链路诊断…")

        def worker():
            try:
                from feishu_uploader import check_upload_environment
                from wechat_extractor import audit_extraction_pipeline

                extraction = audit_extraction_pipeline(callback=lambda m: self._log(m))
                upload_env = check_upload_environment()
                self.upload_env = upload_env
                self._emit(
                    "uploadEnv",
                    {
                        "ok": bool(upload_env.get("ok")),
                        "message": upload_env.get("message", ""),
                        "detail": self._build_upload_env_detail(upload_env),
                    },
                )

                tone = "success" if extraction.get("ok") and upload_env.get("ok") else "warning"
                detail = (
                    f"微信提取：{'通过' if extraction.get('ok') else '未通过'}；"
                    f"示例下载 {extraction.get('sample_downloaded', 0)} 个；"
                    f"飞书上传环境：{'已就绪' if upload_env.get('ok') else '未就绪'}。"
                )
                self._emit(
                    "auditSummary",
                    {
                        "tone": tone,
                        "title": extraction.get("message", "诊断完成"),
                        "detail": detail,
                    },
                )
                self._log("链路诊断完成", "success" if extraction.get("ok") else "warn")
            except Exception as exc:
                self._emit(
                    "auditSummary",
                    {"tone": "error", "title": "诊断失败", "detail": str(exc)},
                )
                self._log(f"诊断失败：{exc}", "error")
            finally:
                self._set_busy(False, "")

        threading.Thread(target=worker, daemon=True).start()

    # ---- Extract ----

    def startExtract(self, raw_payload: str | None = None):
        if self._busy:
            return

        payload = self._parse_payload(raw_payload)
        user_data = payload.get("selectedUser") or {}
        if isinstance(user_data, str):
            try:
                user_data = json.loads(user_data)
            except Exception:
                user_data = {}
        wxid = user_data.get("wxid", "")
        if not wxid:
            self._log("请先选择一个微信账号", "error")
            return

        output_dir = (payload.get("outputDir") or config.EMOJI_OUTPUT_DIR or "").strip()
        if not output_dir:
            self._log("输出目录不能为空", "error")
            return
        config.EMOJI_OUTPUT_DIR = output_dir
        self._current_output_dir = output_dir

        # 重置暂停状态，确保以未暂停状态开始
        self._pause_event.set()
        self._paused = False
        self._emit("pauseState", {"paused": False})

        self._set_busy(True, "extract")
        self._set_progress("extract", 0)
        self._log(f"开始提取微信表情: {wxid}（并发下载，支持随时暂停）")

        def worker():
            try:
                from wechat_extractor import extract_emojis

                files = extract_emojis(
                    wxid=wxid,
                    output_dir=output_dir,
                    progress_callback=self._on_extract_progress,
                    pause_event=self._pause_event,
                    max_workers=5,
                )
                self._on_extract_done(files)
            except Exception as exc:
                self._log(f"提取失败: {exc}", "error")
                self._on_extract_done([])

        threading.Thread(target=worker, daemon=True).start()

    def pauseExtract(self, _raw_payload: str | None = None):
        """切换暂停/继续状态。"""
        if not self._busy or self._busy_action != "extract":
            return
        if self._paused:
            self._pause_event.set()
            self._paused = False
            self._emit("pauseState", {"paused": False})
            self._log("继续导出…")
        else:
            self._pause_event.clear()
            self._paused = True
            files = self._collect_emoji_files(self._current_output_dir)
            if files:
                self.emoji_files = files
                self._load_emoji_thumbs(files)
            self._emit("pauseState", {"paused": True})
            self._log("已暂停导出。当前已导出的文件已刷新到预览，可点「继续」恢复，或直接导入已导出的部分。", "warn")

    # ---- Load from folder ----

    def loadFromFolder(self):
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=config.EMOJI_OUTPUT_DIR)
        if not result:
            return

        folder = result[0]
        files = self._collect_emoji_files(folder)
        if not files:
            self._log("选中的目录里没有找到表情图片", "error")
            return

        config.EMOJI_OUTPUT_DIR = folder
        self._current_output_dir = folder
        self.emoji_files = files
        self._emit("outputDir", folder)
        self._load_emoji_thumbs(files)
        self._log(f"已从目录加载 {len(files)} 个表情文件")

    def loadEmojiFiles(self):
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=config.EMOJI_OUTPUT_DIR,
            allow_multiple=True,
            file_types=("Image files (*.png;*.gif;*.jpg;*.jpeg;*.webp)",),
        )
        if not result:
            return

        files = []
        supported = {ext.lower() for ext in config.FEISHU_EMOJI_FORMATS}
        for raw in result:
            path = Path(raw)
            if path.is_file() and path.suffix.lower() in supported:
                files.append(str(path))

        if not files:
            self._log("没有选中受支持的表情图片", "error")
            return

        files = sorted(dict.fromkeys(files))
        try:
            config.EMOJI_OUTPUT_DIR = os.path.commonpath(files)
        except ValueError:
            config.EMOJI_OUTPUT_DIR = str(Path(files[0]).parent)
        self._current_output_dir = config.EMOJI_OUTPUT_DIR
        self.emoji_files = files
        self._emit("outputDir", config.EMOJI_OUTPUT_DIR)
        self._load_emoji_thumbs(files)
        self._log(f"已从文件选择器加载 {len(files)} 个表情文件")

    # ---- Upload ----

    def startUpload(self, raw_payload: str | None = None):
        if self._busy:
            return

        payload = self._parse_payload(raw_payload)
        files = payload.get("selectedFiles") or self.emoji_files
        if not files:
            self._log("没有可上传的表情文件", "error")
            return

        mode = (payload.get("mode") or "personal").strip()
        pack_name = (payload.get("packName") or "wechat_emoji_pack").strip()
        self._clear_stop_upload_signal()

        if mode == "enterprise" and len(files) < config.FEISHU_EMOJI_PACK_MIN:
            self._log(
                f"企业表情包模式至少需要 {config.FEISHU_EMOJI_PACK_MIN} 个表情，当前只选中了 {len(files)} 个",
                "error",
            )
            self._emit(
                "uploadStatus",
                {"text": f"企业表情包模式至少需要 {config.FEISHU_EMOJI_PACK_MIN} 个表情。", "tone": "error"},
            )
            return

        self._set_busy(True, "upload")
        self._set_progress("upload", 0)
        self._emit("uploadStatus", {"text": "正在检查飞书上传环境…", "tone": "neutral"})
        self._log(f"开始导入飞书：模式={mode}，文件数={len(files)}")

        def worker():
            uploader = None
            try:
                # 环境检查放在 worker 线程内，避免阻塞 UI 线程（Chromium 启动约需 1-2 秒）
                self.upload_env = self._resolve_upload_env()
                self._emit(
                    "uploadEnv",
                    {
                        "ok": bool(self.upload_env.get("ok")),
                        "message": self.upload_env.get("message", ""),
                        "detail": self._build_upload_env_detail(self.upload_env),
                    },
                )
                if not self.upload_env.get("ok"):
                    self._emit(
                        "uploadStatus",
                        {"text": self.upload_env.get("message", "飞书上传环境还没准备好"), "tone": "error"},
                    )
                    self._log("飞书上传环境未就绪，已取消本次导入", "error")
                    self._on_upload_done({"success": 0, "failed": len(files)})
                    return

                self._emit("uploadStatus", {"text": "正在启动飞书浏览器窗口…", "tone": "neutral"})
                from feishu_uploader import FeishuUploader

                uploader = FeishuUploader(headless=False, progress_callback=self._on_upload_progress)
                uploader.start()
                if not uploader.login(stop_check=self._should_stop_upload):
                    if not self._should_stop_upload():
                        uploader.wait_for_browser_close(stop_check=self._should_stop_upload)
                    self._on_upload_done({"success": 0, "failed": 0, "stopped": self._should_stop_upload()})
                    return

                if mode == "personal":
                    result = uploader.upload_personal_emojis(files, stop_check=self._should_stop_upload)
                else:
                    result = uploader.upload_enterprise_emojis(
                        files,
                        pack_name=pack_name,
                        stop_check=self._should_stop_upload,
                    )
                if (result.get("failed") or result.get("skipped")) and not self._should_stop_upload():
                    if result.get("message"):
                        self._log(result["message"], "warn")
                    uploader.wait_for_browser_close(stop_check=self._should_stop_upload)
                result["stopped"] = self._should_stop_upload()
                self._on_upload_done(result)
            except Exception as exc:
                self._log(f"飞书导入失败：{exc}", "error")
                self._emit(
                    "uploadStatus",
                    {"text": f"飞书导入失败：{exc}", "tone": "error"},
                )
                if uploader and not self._should_stop_upload():
                    uploader.wait_for_browser_close(stop_check=self._should_stop_upload)
                self._on_upload_done({"success": 0, "failed": len(files)})
            finally:
                if uploader:
                    try:
                        uploader.close()
                    except Exception:
                        pass
                self._clear_stop_upload_signal()

        threading.Thread(target=worker, daemon=True).start()

    def stopUpload(self, _raw_payload: str | None = None):
        if not self._busy or self._busy_action != "upload":
            return
        os.makedirs(os.path.dirname(self._stop_upload_signal_path()), exist_ok=True)
        Path(self._stop_upload_signal_path()).touch()
        self._log("正在中止上传，请稍等一下…", "warn")

    # ---- Progress callbacks ----

    def _on_extract_progress(self, current, total, message):
        percent = int(current / total * 100) if total > 0 else 0
        filename = ""
        if "→ " in message:
            filename = message.split("→ ")[-1].strip()
        now = time.monotonic()
        finished = total > 0 and current >= total
        # 限速：UI 更新最多 10次/秒，完成时强制刷新
        if finished or now - self._last_progress_time >= 0.1:
            self._emit("progress", {"which": "extract", "percent": percent, "filename": filename})
            self._last_progress_time = now
        # 限速：日志最多 5次/秒，避免 DOM 大量插入
        if finished or now - self._last_log_time >= 0.2:
            self._log(message)
            self._last_log_time = now

    def _on_extract_done(self, files):
        # 恢复暂停事件，避免下次提取被之前的 clear() 阻塞
        self._pause_event.set()
        self._paused = False
        self._emit("pauseState", {"paused": False})

        self._set_busy(False, "")

        # 即使 files 为空，也尝试从输出目录加载已有文件（支持暂停后直接导入）
        if not files:
            output_dir = self._current_output_dir
            files = self._collect_emoji_files(output_dir)

        if files:
            self._set_progress("extract", 100)
            self.emoji_files = files
            self._load_emoji_thumbs(files)
            self._emit(
                "auditSummary",
                {
                    "tone": "success",
                    "title": f"已导出 {len(files)} 个表情",
                    "detail": f"输出目录: {self._current_output_dir}",
                },
            )
            self._log(f"导出完成，共 {len(files)} 个表情", "success")
        else:
            self._set_progress("extract", 0)
            self._log("未导出任何表情", "error")

    def _on_upload_progress(self, current, total, message):
        if total > 0:
            self._set_progress("upload", int(current / total * 100))
        self._emit("uploadStatus", {"text": message, "tone": "neutral"})
        self._log(message)

    def _on_upload_done(self, result):
        success = result.get("success", 0)
        failed = result.get("failed", 0)
        stopped = bool(result.get("stopped"))
        skipped = result.get("skipped", 0)
        if skipped:
            summary = result.get("message", f"导入已中断：成功 {success}，失败 {failed}，未尝试 {skipped}")
            processed = success + failed
            self._set_progress("upload", int(100 * processed / (processed + skipped)))
            self._emit("uploadStatus", {"text": summary, "tone": "warning"})
            self._log(summary, "warn")
            self._set_busy(False, "")
            return
        if stopped:
            summary = f"导入已停止：成功 {success}，失败 {failed}"
            self._emit("uploadStatus", {"text": summary, "tone": "warning"})
            self._log(summary, "warn")
            self._set_busy(False, "")
            return
        tone = "success" if failed == 0 and success > 0 else ("warning" if success > 0 else "error")
        self._set_progress("upload", 100 if success or failed else 0)
        self._emit(
            "uploadStatus",
            {"text": f"导入完成：成功 {success}，失败 {failed}", "tone": tone},
        )
        self._log(f"飞书导入完成：成功 {success}，失败 {failed}", "success" if failed == 0 else "warn")
        self._set_busy(False, "")

    # ---- Emoji thumbs ----

    def _load_emoji_thumbs(self, files: list[str]):
        import base64
        from io import BytesIO
        from PIL import Image

        self._emit("emojiList", [])  # 立刻清空旧列表，UI 立即响应

        def worker():
            BATCH = 20  # 每批 20 张，每批约 80-150KB，避免单次大包卡顿
            batch = []
            for filepath in files:
                try:
                    image = Image.open(filepath)
                    try:
                        image.seek(0)  # GIF 跳到第一帧
                    except (AttributeError, EOFError):
                        pass
                    image = image.convert('RGBA')
                    image.thumbnail((80, 80))
                    buffer = BytesIO()
                    # JPEG 体积比 PNG 小 60-80%，大幅降低桥接传输压力
                    bg = Image.new('RGB', image.size, (255, 255, 255))
                    bg.paste(image, mask=image.getchannel('A'))
                    bg.save(buffer, format="JPEG", quality=72)
                    thumb = base64.b64encode(buffer.getvalue()).decode("ascii")
                    batch.append({
                        "file": filepath,
                        "name": os.path.basename(filepath),
                        "size_kb": round(os.path.getsize(filepath) / 1024, 1),
                        "thumb": f"data:image/jpeg;base64,{thumb}",
                    })
                    if len(batch) >= BATCH:
                        self._emit("emojiListAppend", batch)
                        batch = []
                except Exception:
                    continue

            if batch:
                self._emit("emojiListAppend", batch)
            self._emit("emojiListReady", len(files))

        threading.Thread(target=worker, daemon=True).start()

    # ---- Helpers ----

    def _normalize_user(self, info: dict) -> dict:
        return bridge_common.normalize_user(info)

    def _emit_db_path_for_user(self, user: dict):
        try:
            bridge_common.emit_db_path_for_user(self._emit, user)
        except Exception as exc:
            self._emit("dbPath", {"text": f"数据库检测失败：{exc}", "ok": False})

    def _parse_payload(self, raw_payload: str | None) -> dict:
        if not raw_payload:
            return {}
        try:
            payload = json.loads(raw_payload)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _stop_upload_signal_path(self) -> str:
        return bridge_common.stop_upload_signal_path()

    def _clear_stop_upload_signal(self):
        try:
            os.remove(self._stop_upload_signal_path())
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _should_stop_upload(self) -> bool:
        return os.path.exists(self._stop_upload_signal_path())

    def _collect_emoji_files(self, folder: str) -> list[str]:
        return bridge_common.collect_emoji_files(folder)

    def _set_progress(self, which: str, percent: int):
        self._emit("progress", {"which": which, "percent": max(0, min(percent, 100))})

    def _set_busy(self, busy: bool, action: str):
        self._busy = busy
        self._busy_action = action if busy else ""
        self._emit("busyState", {"busy": busy, "action": action})

    def _build_upload_env_detail(self, env: dict) -> str:
        return bridge_common.build_upload_env_detail(env)

    def _log(self, message: str, level: str = "info"):
        self._emit(
            "log",
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": message,
                "level": level,
            },
        )

    def _emit(self, event: str, data):
        self._emit_js(
            f"window.onPythonEvent({json.dumps(event, ensure_ascii=False)}, "
            f"{json.dumps(data, ensure_ascii=False)});"
        )

    def _emit_js(self, js_code: str):
        if not self._window:
            return
        self._emit_queue.put(js_code)  # 不直接调用，入队后由 drainer 串行处理

def run_gui():
    api = Api()
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")

    window = webview.create_window(
        "微信表情乐园 · 飞书贴纸站",
        url=html_path,
        js_api=api,
        width=1040,
        height=920,
        min_size=(860, 720),
        resizable=True,
        text_select=False,
    )
    api._window = window
    webview.start(debug=False, gui="edgechromium")


if __name__ == "__main__":
    run_gui()
