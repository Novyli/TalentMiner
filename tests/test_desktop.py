import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop import DesktopApi


class FakeWindow:
    def __init__(self, selected):
        self.selected = selected
        self.request = None

    def create_file_dialog(self, dialog_type, **kwargs):
        self.request = (dialog_type, kwargs)
        return self.selected


class DesktopCsvExportTests(unittest.TestCase):
    def test_save_csv_adds_extension_and_utf8_bom(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "联系人"
            window = FakeWindow((str(destination),))
            api = DesktopApi()
            api.window = window
            fake_webview = types.SimpleNamespace(FileDialog=types.SimpleNamespace(SAVE=30))

            with patch.dict(sys.modules, {"webview": fake_webview}):
                result = api.save_csv("姓名,机构\r\n张三,量子实验室\r\n", "contacts.csv")

            saved = destination.with_suffix(".csv")
            self.assertEqual(result["status"], "saved")
            self.assertTrue(saved.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn("张三", saved.read_text(encoding="utf-8-sig"))

    def test_save_csv_preserves_csv_suffix_and_strips_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "authors.csv"
            window = FakeWindow(str(destination))
            api = DesktopApi()
            api.window = window
            fake_webview = types.SimpleNamespace(FileDialog=types.SimpleNamespace(SAVE=30))

            with patch.dict(sys.modules, {"webview": fake_webview}):
                result = api.save_csv("\ufeffname\r\nAda\r\n", "../unsafe/authors.csv")

            self.assertEqual(result["status"], "saved")
            self.assertEqual(window.request[1]["save_filename"], "authors.csv")
            self.assertEqual(destination.read_bytes().count(b"\xef\xbb\xbf"), 1)

    def test_cancelled_save_does_not_write(self):
        api = DesktopApi()
        api.window = FakeWindow(None)
        fake_webview = types.SimpleNamespace(FileDialog=types.SimpleNamespace(SAVE=30))

        with patch.dict(sys.modules, {"webview": fake_webview}):
            result = api.save_csv("name\r\nAda\r\n", "authors.csv")

        self.assertEqual(result, {"status": "cancelled"})


if __name__ == "__main__":
    unittest.main()
