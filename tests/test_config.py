import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tutoring_demo import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.db_dir = self.root / "xwechat_files"
        (self.db_dir / "wxid_fake_account" / "db_storage").mkdir(parents=True)
        self.path = self.runtime / "demo-config.json"
        self.data = {"db_dir": str(self.db_dir), "account": "wxid_fake_account", "test_friend": "补习Demo测试"}

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, data=None, encoding="utf-8"):
        self.path.write_text(json.dumps(self.data if data is None else data, ensure_ascii=False), encoding=encoding)
        return self.path

    def test_valid_config_uses_only_three_local_fields(self):
        result = config.load_config(self.write_config())
        self.assertEqual(result.db_dir, self.db_dir.resolve())
        self.assertEqual(result.account, "wxid_fake_account")
        self.assertEqual(result.test_friend, "补习Demo测试")
        self.assertNotIn("wxid_fake_account", result.binding)
        self.assertEqual(len(result.binding), 64)

    def test_missing_extra_and_invalid_fields_are_rejected(self):
        cases = [
            {"db_dir": str(self.db_dir), "account": "wxid_fake_account"},
            dict(self.data, api_key="should-not-be-here"),
            dict(self.data, test_friend=" "),
            dict(self.data, account=123),
            [self.data],
        ]
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(config.ConfigError):
                    config.load_config(self.write_config(data))

    def test_account_cannot_escape_its_parent_directory(self):
        for account in (".", "..", "../wxid_fake_account", "..\\wxid_fake_account", "C:\\account", "/absolute", "nested/account"):
            with self.subTest(account=account):
                with self.assertRaises(config.ConfigError):
                    config.load_config(self.write_config(dict(self.data, account=account)))

    def test_nonexistent_parent_or_missing_db_storage_is_rejected(self):
        for db_dir, account in ((self.root / "missing", "wxid_fake_account"), (self.db_dir, "missing-account")):
            with self.subTest(db_dir=db_dir, account=account):
                with self.assertRaises(config.ConfigError):
                    config.load_config(self.write_config(dict(self.data, db_dir=str(db_dir), account=account)))

    def test_utf8_bom_from_powershell_is_supported(self):
        result = config.load_config(self.write_config(encoding="utf-8-sig"))
        self.assertEqual(result.test_friend, "补习Demo测试")

    def test_binding_changes_when_friend_or_account_changes(self):
        one = config.DemoConfig(self.db_dir, "wxid_fake_account", "补习Demo测试")
        other_friend = config.DemoConfig(self.db_dir, "wxid_fake_account", "另一个测试")
        other_account = config.DemoConfig(self.db_dir, "wxid_other", "补习Demo测试")
        self.assertNotEqual(one.binding, other_friend.binding)
        self.assertNotEqual(one.binding, other_account.binding)

    def test_environment_key_has_priority_without_reading_secret_file(self):
        (self.runtime / "secrets.json").write_text("not valid json", encoding="utf-8")
        with patch.object(config, "RUNTIME", self.runtime), patch.dict(config.os.environ, {"DEEPSEEK_API_KEY": "  fake-env-key-12345  "}):
            self.assertEqual(config.load_api_key(), "fake-env-key-12345")

    def test_local_secret_file_supports_utf8_bom(self):
        (self.runtime / "secrets.json").write_text(json.dumps({"DEEPSEEK_API_KEY": "fake-file-key-12345"}), encoding="utf-8-sig")
        with patch.object(config, "RUNTIME", self.runtime), patch.dict(config.os.environ, {"DEEPSEEK_API_KEY": ""}):
            self.assertEqual(config.load_api_key(), "fake-file-key-12345")

    def test_invalid_key_errors_and_output_do_not_disclose_secret(self):
        output = io.StringIO()
        with patch.object(config, "RUNTIME", self.runtime), patch.dict(config.os.environ, {"DEEPSEEK_API_KEY": "shortkey"}), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(config.ConfigError) as caught:
                config.load_api_key()
        self.assertNotIn("shortkey", str(caught.exception))
        self.assertEqual(output.getvalue(), "")

    def test_loading_valid_secret_does_not_print_it(self):
        output = io.StringIO()
        with patch.object(config, "RUNTIME", self.runtime), patch.dict(config.os.environ, {"DEEPSEEK_API_KEY": "fake-private-key-123456"}), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(config.load_api_key(), "fake-private-key-123456")
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
