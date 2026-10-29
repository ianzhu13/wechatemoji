"""真实桥接导出逻辑回归，不启动桌面窗口或访问微信。"""

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import webview.util

import config
import gui


class FakeWindow:
    def __init__(self, api):
        self._js_api = api
        self._functions = {}
        self._expose_lock = threading.Lock()
        self._scripts = []
        self._native_reads = 0
        self.events = SimpleNamespace(
            before_load=threading.Event(),
            _pywebviewready=threading.Event(),
            loaded=threading.Event(),
        )

    @property
    def native(self):
        self._native_reads += 1
        raise AssertionError("Native window must not be traversed when exposing the API")

    def run_js(self, script):
        self._scripts.append(script)


class GuiStartupTests(unittest.TestCase):
    def test_startup_exports_actions_without_traversing_native_window(self):
        windows = []

        def create_window(*args, js_api, **kwargs):
            window = FakeWindow(js_api)
            windows.append(window)
            return window

        def start(**kwargs):
            window = windows[0]
            webview.util.inject_pywebview("edgechromium", window)
            self.assertTrue(window.events.loaded.wait(3), "Bridge registration did not finish")

        with patch("gui.threading.Thread"), patch("gui.webview.create_window", side_effect=create_window), patch(
            "gui.webview.start", side_effect=start
        ), patch("webview.util.load_js_files", return_value=("", "%(functions)s")):
            gui.run_gui()

        window = windows[0]
        functions = {item["func"] for item in json.loads(window._scripts[-1])}
        self.assertTrue({"init", "detectWechat", "startUpload", "openOutputDir"}.issubset(functions))
        self.assertFalse(any(name.startswith("window.") for name in functions))
        self.assertEqual(window._native_reads, 0)
        self.assertIs(window._js_api._window, window)

    def test_folder_picker_uses_window_without_exposing_it(self):
        with patch("gui.threading.Thread"):
            api = gui.Api()
        api._window = Mock()
        api._window.create_file_dialog.return_value = None
        api.browseOutputDir()
        api._window.create_file_dialog.assert_called_once_with(
            gui.webview.FOLDER_DIALOG, directory=config.EMOJI_OUTPUT_DIR,
        )
        self.assertNotIn("window", vars(api))


if __name__ == "__main__":
    unittest.main()
