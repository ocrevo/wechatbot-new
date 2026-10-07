import contextlib
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tutoring_demo import cli
from tutoring_demo.config import ConfigError


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.runtime_patch = patch.object(cli, "RUNTIME", self.runtime)
        self.runtime_patch.start()

    def tearDown(self):
        self.runtime_patch.stop()
        self.temp.cleanup()

    def write_stages(self, binding="binding-a", username="wxid_fake_target"):
        report = {
            "receive": {"passed": True, "binding": binding, "target": cli.target_fingerprint(username)},
            "model": {"passed": True, "binding": binding},
        }
        (self.runtime / "stages.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    def test_missing_stage_or_failed_stage_refuses_run(self):
        with self.assertRaises(ConfigError):
            cli.require_stages("binding-a", "wxid_fake_target")
        for name in ("receive", "model"):
            report = self.write_stages()
            report.pop(name)
            (self.runtime / "stages.json").write_text(json.dumps(report), encoding="utf-8")
            with self.subTest(missing=name), self.assertRaises(ConfigError):
                cli.require_stages("binding-a", "wxid_fake_target")
            report = self.write_stages()
            report[name]["passed"] = False
            (self.runtime / "stages.json").write_text(json.dumps(report), encoding="utf-8")
            with self.subTest(failed=name), self.assertRaises(ConfigError):
                cli.require_stages("binding-a", "wxid_fake_target")

    def test_old_binding_refuses_run_even_when_stages_passed(self):
        self.write_stages(binding="old-binding")
        with self.assertRaises(ConfigError):
            cli.require_stages("new-binding", "wxid_fake_target")

    def test_reassigned_remark_target_refuses_run(self):
        self.write_stages(username="wxid_original_person")
        with self.assertRaises(ConfigError):
            cli.require_stages("binding-a", "wxid_different_person")

    def test_legacy_report_without_target_fingerprint_is_rejected(self):
        report = self.write_stages()
        report["receive"].pop("target")
        (self.runtime / "stages.json").write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(ConfigError):
            cli.require_stages("binding-a", "wxid_fake_target")

    def test_current_binding_and_target_fingerprint_pass(self):
        self.write_stages()
        self.assertIsNone(cli.require_stages("binding-a", "wxid_fake_target"))
        self.assertEqual(cli.target_fingerprint("wxid_fake_target"), sha256(b"wxid_fake_target").hexdigest())
        self.assertNotEqual(cli.target_fingerprint("wxid_fake_target"), cli.target_fingerprint("wxid_other_target"))

    def test_stage_updates_preserve_other_stages_and_store_only_fingerprint(self):
        cli.record_stage("model", "binding-a", model="deepseek-flash")
        cli.record_stage("receive", "binding-a", target=cli.target_fingerprint("wxid_fake_target"), incoming=1, outgoing=1)
        cli.require_stages("binding-a", "wxid_fake_target")
        content = (self.runtime / "stages.json").read_text(encoding="utf-8")
        self.assertNotIn("wxid_fake_target", content)
        self.assertTrue(json.loads(content)["model"]["passed"])

    def test_importing_bot_in_fresh_process_never_loads_wechat_sdk_or_workers(self):
        script = (
            "import sys,json,threading; import bot; "
            "print(json.dumps({'sdk':any(n=='wechatauto' or n.startswith('wechatauto.') for n in sys.modules),"
            "'gui':'uiautomation' in sys.modules,'openai':'openai' in sys.modules,"
            "'threads':threading.active_count()}))"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), {"sdk": False, "gui": False, "openai": False, "threads": 1})
        self.assertEqual(result.stderr, "")

    def test_unexpected_failure_output_hides_key_and_parent_data(self):
        output = io.StringIO()
        with patch.object(cli, "configure_runtime"), patch.object(cli, "load_config", side_effect=RuntimeError("private-api-key and private-parent-message")), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["check"]), 2)
        self.assertNotIn("private-api-key", output.getvalue())
        self.assertNotIn("private-parent-message", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")

    def test_main_reconfigures_console_to_utf8_without_connecting_wechat(self):
        class FakeStream(io.StringIO):
            def __init__(self):
                super().__init__()
                self.configurations = []

            def reconfigure(self, **kwargs):
                self.configurations.append(kwargs)

        stdout, stderr = FakeStream(), FakeStream()
        with patch.object(cli.sys, "stdout", stdout), patch.object(cli.sys, "stderr", stderr), patch.object(cli, "configure_runtime"), patch.object(cli, "load_config", side_effect=ConfigError("测试配置尚未填写")):
            self.assertEqual(cli.main(["check"]), 2)
        self.assertEqual(stdout.configurations, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(stderr.configurations, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertIn("测试配置尚未填写", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
