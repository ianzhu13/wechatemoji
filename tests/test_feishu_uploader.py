"""登录回归：只使用模拟页面或本地拦截的页面，不登录账号或上传文件。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import config
from feishu_uploader import FeishuUploader


LOGIN_URL = "https://accounts.feishu.cn/accounts/page/login"
MESSENGER_URL = "https://fixture.feishu.cn/next/messenger/"
UPLOAD_FIXTURE = """
<div class="feed-main-list">
  <button class="a11y_feed_card_item" onclick="selectChat(0)">read-only notification</button>
  <button class="a11y_feed_card_item" onclick="selectChat(1)">editable chat</button>
</div>
<div id="editor" style="display:none">
  <div contenteditable="true">fixture editor</div>
  <div class="lark__editor--toolbar">
    <div><button onclick="window.attachmentClicks++">attachment</button></div>
    <div><button aria-label="表情" onclick="openPanel()">emoji</button></div>
  </div>
</div>
<div class="emoji customized-sticker-panel" id="panel" style="display:none">
  <button class="add-sticker-btn" onclick="document.querySelector('#files').click()">添加</button>
  <div id="stickers"><div class="customized-sticker-item"><img src="/base.gif"></div></div>
  <input id="files" type="file" hidden onchange="addSticker(this)">
</div>
<div id="history">聊天历史中的上传成功文字<button onclick="window.unrelatedClicks++">保存</button></div>
<script>
window.chatClicks = []; window.panelOpens = 0; window.uploadNames = []; window.attachmentClicks = 0;
window.unrelatedClicks = 0;
function selectChat(index) {
  window.chatClicks.push(index);
  document.querySelector('#editor').style.display = index === 1 ? 'block' : 'none';
}
function openPanel() {
  window.panelOpens++;
  document.querySelector('#panel').style.display = 'block';
}
function addSticker(input) {
  for (const file of input.files) {
    window.uploadNames.push(file.name);
    const sticker = document.createElement('div'); sticker.className = 'customized-sticker-item';
    const image = document.createElement('img'); image.src = '/stickers/' + file.name;
    sticker.append(image); document.querySelector('#stickers').prepend(sticker);
  }
  input.value = '';
}
</script>
"""


def fake_page(url, visible=()):
    page = Mock()
    page.url = url
    page.locator.side_effect = lambda selector: SimpleNamespace(
        first=SimpleNamespace(is_visible=lambda: selector in visible)
    )
    return page


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.uploader = FeishuUploader()
        self.uploader._report = Mock()

    def test_avatar_sidebar_and_skeleton_are_not_ready(self):
        self.uploader._page = fake_page(MESSENGER_URL, (
            '[class*="sidebar"]', '[role="main"]', '.feed-main-list',
        ))
        self.assertFalse(self.uploader._is_logged_in())

    def test_visible_chat_list_or_editor_is_ready(self):
        for selector in ('.a11y_feed_card_item', '.lark__editor--toolbar', '[contenteditable="true"]'):
            with self.subTest(selector=selector):
                self.uploader._page = fake_page(MESSENGER_URL, (selector,))
                self.assertTrue(self.uploader._is_logged_in())
                self.assertTrue(self.uploader._wait_for_messenger_ready())

    def test_account_page_is_not_ready(self):
        self.uploader._page = fake_page(LOGIN_URL, ('[contenteditable="true"]',))
        self.assertFalse(self.uploader._is_logged_in())

    def test_login_pumps_navigation_events(self):
        page = fake_page(LOGIN_URL, ('.a11y_feed_card_item',))
        self.uploader._page = page
        waits = 0

        def pump(milliseconds):
            nonlocal waits
            waits += 1
            if waits >= 2:
                page.url = MESSENGER_URL

        page.wait_for_timeout.side_effect = pump
        with patch.object(config, "LOGIN_TIMEOUT", 1000), patch(
            "feishu_uploader.time.sleep", side_effect=AssertionError("login must pump browser events")
        ):
            self.assertTrue(self.uploader.login())
        self.assertEqual(page.wait_for_timeout.call_count, 2)

    def test_stop_during_login(self):
        self.uploader._page = fake_page(LOGIN_URL)
        self.assertFalse(self.uploader.login(stop_check=lambda: True))

    def test_personal_upload_keeps_ready_tenant_page(self):
        self.uploader._page = fake_page(MESSENGER_URL, ('.a11y_feed_card_item',))
        with patch.object(self.uploader, "_upload_personal_auto", return_value=True) as upload, patch(
            "feishu_uploader.time.sleep"
        ):
            result = self.uploader.upload_personal_emojis(["synthetic.gif"])
        self.assertEqual(result, {"success": 1, "failed": 0})
        upload.assert_called_once_with("synthetic.gif", stop_check=None)
        self.uploader._page.goto.assert_not_called()

    def test_close_persistent_context_before_driver(self):
        calls = Mock()
        self.uploader._context = calls.context
        self.uploader._browser = calls.browser
        self.uploader._playwright_driver = calls.driver
        self.uploader.close()
        self.assertEqual(calls.mock_calls, [
            call.context.close(), call.browser.close(), call.driver.stop(),
        ])

    def test_unknown_upload_stops_batch_without_skipping_to_next_file(self):
        with patch.object(self.uploader, "_upload_personal_auto", return_value=False) as upload:
            result = self.uploader.upload_personal_emojis(["one.gif", "two.gif", "three.gif"])
        upload.assert_called_once_with("one.gif", stop_check=None)
        self.assertEqual((result["success"], result["failed"], result["skipped"]), (0, 1, 2))
        self.assertNotIn(call(unittest.mock.ANY, 3, 3), self.uploader._report.call_args_list)

    def test_manual_fallback_retries_current_file_before_submission(self):
        with patch.object(self.uploader, "_is_sticker_panel_visible", return_value=False), patch.object(
            self.uploader, "_try_open_first_chat", return_value=False
        ), patch.object(self.uploader, "_screenshot"), patch.object(
            self.uploader, "_upload_personal_semi_auto", return_value=True
        ) as manual:
            result = self.uploader.upload_personal_emojis(["one.gif"])
        manual.assert_called_once_with("one.gif", stop_check=None)
        self.assertEqual(result, {"success": 1, "failed": 0})

    def test_submission_error_does_not_retry_or_duplicate(self):
        self.uploader._page = MagicMock()
        chooser = Mock()
        chooser.set_files.side_effect = RuntimeError("synthetic submission error")
        self.uploader._page.expect_file_chooser.return_value.__enter__.return_value.value = chooser
        with patch.object(self.uploader, "_is_sticker_panel_visible", return_value=True), patch.object(
            self.uploader, "_capture_custom_sticker_signature", return_value=[]
        ), patch.object(self.uploader, "_capture_upload_feedback", return_value=()), patch.object(
            self.uploader, "_try_click_add_emoji", return_value=True
        ), patch.object(self.uploader, "_upload_personal_semi_auto") as manual:
            self.assertFalse(self.uploader._upload_personal_auto("one.gif"))
        chooser.set_files.assert_called_once_with("one.gif")
        manual.assert_not_called()

    def test_manual_wait_can_stop_and_removes_listener(self):
        page = Mock()
        self.uploader._page = page
        self.assertFalse(self.uploader._upload_personal_semi_auto("one.gif", stop_check=lambda: True))
        self.assertEqual(page.on.call_args.args, page.remove_listener.call_args.args)

    def test_gui_keeps_partial_progress_and_message(self):
        from gui import Api

        api = Mock()
        result = {"success": 1, "failed": 1, "skipped": 8, "message": "synthetic interrupted batch"}
        Api._on_upload_done(api, result)
        api._set_progress.assert_called_once_with("upload", 20)
        api._emit.assert_called_once_with("uploadStatus", {
            "text": result["message"], "tone": "warning",
        })
        self.assertEqual(api.mock_calls[-1], call._set_busy(False, ""))

    def test_tauri_keeps_partial_progress_and_message(self):
        import tauri_bridge

        ctx = Mock()
        result = {"success": 1, "failed": 1, "skipped": 8, "message": "synthetic interrupted batch"}
        with tempfile.TemporaryDirectory() as directory, patch(
            "feishu_uploader.check_upload_environment", return_value={"ok": True}
        ), patch("feishu_uploader.FeishuUploader") as factory, patch.object(
            tauri_bridge, "_stop_upload_signal_path", return_value=str(Path(directory) / "stop")
        ):
            factory.return_value.login.return_value = True
            factory.return_value.upload_personal_emojis.return_value = result
            tauri_bridge.cmd_start_upload(ctx, {"files": ["one.gif"] * 10, "mode": "personal"})
        ctx.emit.assert_any_call("progress", {"which": "upload", "percent": 20})
        ctx.emit.assert_any_call("uploadStatus", {"text": result["message"], "tone": "warning"})
        ctx.finish.assert_called_once_with(payload={"ok": False, "stopped": False, **result})

    def test_inspection_wait_pumps_events_without_closing_browser(self):
        context, page = Mock(), Mock()
        context.pages = [page]
        page.is_closed.return_value = False
        page.wait_for_timeout.side_effect = lambda milliseconds: setattr(context, "pages", [])
        self.uploader._context = context
        self.uploader.wait_for_browser_close()
        page.wait_for_timeout.assert_called_once_with(250)
        context.close.assert_not_called()
        page.close.assert_not_called()

    def test_inspection_wait_respects_stop(self):
        self.uploader._context = Mock()
        self.uploader.wait_for_browser_close(stop_check=lambda: True)
        self.uploader._context.close.assert_not_called()

    def test_headless_upload_never_waits_for_manual_close(self):
        self.uploader.headless = True
        self.uploader._context = Mock()
        self.uploader.wait_for_browser_close()
        self.uploader._report.assert_not_called()

    def test_gui_failure_keeps_browser_before_releasing_upload_worker(self):
        from gui import Api

        api = Mock()
        api._busy = False
        api._parse_payload.return_value = {"selectedFiles": ["one.gif"], "mode": "personal"}
        api._resolve_upload_env.return_value = {"ok": True}
        api._should_stop_upload.return_value = False
        with patch("feishu_uploader.FeishuUploader") as factory, patch(
            "gui.threading.Thread", side_effect=lambda *, target, daemon: SimpleNamespace(start=target)
        ):
            uploader = factory.return_value
            uploader.login.return_value = True
            uploader.upload_personal_emojis.return_value = {"success": 0, "failed": 1}

            def inspect(**kwargs):
                api._on_upload_done.assert_not_called()
                uploader.close.assert_not_called()

            uploader.wait_for_browser_close.side_effect = inspect
            Api.startUpload(api)
        uploader.wait_for_browser_close.assert_called_once_with(stop_check=api._should_stop_upload)
        api._on_upload_done.assert_called_once_with({"success": 0, "failed": 1, "stopped": False})
        uploader.close.assert_called_once()

    def test_context_manager_preserves_browser_on_exception_then_cleans_up(self):
        with patch.object(self.uploader, "wait_for_browser_close") as inspect, patch.object(
            self.uploader, "close"
        ) as close:
            self.uploader.__exit__(RuntimeError, RuntimeError("synthetic error"), None)
        inspect.assert_called_once_with()
        close.assert_called_once_with()


class BrowserLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.driver = sync_playwright().start()
        if not Path(cls.driver.chromium.executable_path).is_file():
            cls.driver.stop()
            raise unittest.SkipTest("浏览器回归需要已安装 Playwright Chromium")
        cls.browser = cls.driver.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.uploader = FeishuUploader()
        self.uploader._page = self.page
        self.uploader._report = Mock()

    def test_delayed_login_redirect_and_chat_loading(self):
        def serve(route):
            if route.request.url == LOGIN_URL:
                body = (
                    '<script>setTimeout(() => location.replace("' + MESSENGER_URL + '"), 2000)</script>'
                    '<div>synthetic login page</div>'
                )
            elif route.request.url == MESSENGER_URL:
                body = (
                    '<div class="feed-main-list">loading</div><script>'
                    'setTimeout(() => { const item = document.createElement("div");'
                    'item.className = "a11y_feed_card_item"; item.textContent = "fixture";'
                    'document.body.append(item); }, 300);</script>'
                )
            else:
                route.abort()
                return
            route.fulfill(status=200, content_type="text/html", body=body)

        self.page.route("**/*", serve)
        with patch.object(config, "FEISHU_MESSENGER_URL", LOGIN_URL), patch.object(config, "LOGIN_TIMEOUT", 6000):
            self.assertTrue(self.uploader.login())
        self.assertEqual(self.page.url, MESSENGER_URL)
        self.uploader._report.assert_any_call("[登录] 登录成功")

    def test_real_browser_rejects_hidden_chat_and_skeleton(self):
        self.page.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html",
            body='<div class="sidebar">avatar</div><div class="feed-main-list">loading</div>'
                 '<div class="a11y_feed_card_item" style="display:none">hidden</div>',
        ))
        self.page.goto(MESSENGER_URL)
        self.assertFalse(self.uploader._is_logged_in())

    def serve_upload_fixture(self):
        self.page.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body=UPLOAD_FIXTURE,
        ))
        self.page.goto(MESSENGER_URL)

    def test_three_files_skip_read_only_chat_and_reuse_sticker_panel(self):
        self.serve_upload_fixture()
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / name for name in ("one.gif", "two.gif", "three.gif")]
            for file in files:
                file.write_bytes(b"GIF89a synthetic fixture")
            result = self.uploader.upload_personal_emojis([str(file) for file in files])
        self.assertEqual(result, {"success": 3, "failed": 0})
        self.assertEqual(self.page.evaluate("window.chatClicks"), [0, 1])
        self.assertEqual(self.page.evaluate("window.panelOpens"), 1)
        self.assertEqual(self.page.evaluate("window.attachmentClicks"), 0)
        self.assertEqual(self.page.evaluate("window.unrelatedClicks"), 0)
        self.assertEqual(self.page.evaluate("window.uploadNames"), ["one.gif", "two.gif", "three.gif"])

    def test_manual_filechooser_uses_same_file_then_resumes_batch(self):
        self.serve_upload_fixture()
        self.uploader._screenshot = Mock()

        def simulate_manual_entry(message, *args):
            if message.startswith("[半自动]"):
                self.page.evaluate("setTimeout(() => { selectChat(1); openPanel(); document.querySelector('#files').click(); }, 50)")

        self.uploader._report.side_effect = simulate_manual_entry
        with tempfile.TemporaryDirectory() as directory, patch.object(
            self.uploader, "_try_open_first_chat", return_value=False
        ):
            files = [Path(directory) / name for name in ("one.gif", "two.gif")]
            for file in files:
                file.write_bytes(b"GIF89a synthetic fixture")
            result = self.uploader.upload_personal_emojis([str(file) for file in files])
        self.assertEqual(result, {"success": 2, "failed": 0})
        self.assertEqual(self.page.evaluate("window.uploadNames"), ["one.gif", "two.gif"])

    def test_chat_text_and_stale_toast_do_not_confirm_upload(self):
        self.serve_upload_fixture()
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div role=alert>添加成功</div>')")
        baseline = self.uploader._capture_upload_feedback()
        self.assertEqual(baseline, ("添加成功",))
        result, _ = self.uploader._wait_feedback(timeout_ms=20, fail_on_timeout=False, before_feedback=baseline)
        self.assertIsNone(result)
        self.page.evaluate("document.querySelector('[role=alert]').remove()")
        result, _ = self.uploader._wait_feedback(timeout_ms=20, fail_on_timeout=False)
        self.assertIsNone(result)

    def test_reordered_existing_stickers_do_not_confirm_upload(self):
        with patch.object(self.uploader, "_wait_feedback", return_value=(None, "")), patch.object(
            self.uploader, "_capture_custom_sticker_signature", return_value=["second", "first"]
        ):
            result, _ = self.uploader._wait_personal_upload_result(["first", "second"], timeout_ms=20)
        self.assertFalse(result)

    def test_personal_confirmation_only_clicks_inside_upload_dialog(self):
        self.serve_upload_fixture()
        self.uploader._try_confirm_upload(dialog_only=True)
        self.assertEqual(self.page.evaluate("window.unrelatedClicks"), 0)
        self.page.evaluate("""document.body.insertAdjacentHTML('beforeend',
            '<div role="dialog"><button onclick="window.confirmed=true">确定</button></div>')""")
        self.uploader._try_confirm_upload(dialog_only=True)
        self.assertTrue(self.page.evaluate("window.confirmed"))

    def test_inspection_keeps_real_browser_open_until_stop(self):
        self.serve_upload_fixture()
        self.uploader._context = self.context
        checks = 0

        def should_stop():
            nonlocal checks
            checks += 1
            return checks >= 3

        self.uploader.wait_for_browser_close(stop_check=should_stop)
        self.assertFalse(self.page.is_closed())
        self.assertTrue(self.page.locator('.feed-main-list').is_visible())


if __name__ == "__main__":
    unittest.main()
