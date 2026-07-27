import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "keyswap.py"
SPEC = importlib.util.spec_from_file_location("keyswap_cli_under_test", MODULE_PATH)
keyswap = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = keyswap
SPEC.loader.exec_module(keyswap)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary_directory.name) / "config.json"
        self.config = {
            "devices": "auto",
            "substitutions": {"C-nk_minus": "="},
            "sequences": {":sig": "Kind regards"},
            "xkb": {"layout": "br"},
        }
        self.write_config()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_config(self):
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_cli(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = keyswap.main(["--config", str(self.config_path), *arguments])
        return result, stdout.getvalue(), stderr.getvalue()

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_list_all_uses_expansion_cli_terminology(self):
        result, stdout, stderr = self.run_cli("list", "all")

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("substitutions:", stdout)
        self.assertIn("C-nk_minus -> '='", stdout)
        self.assertIn("expansions:", stdout)
        self.assertIn("':sig' -> 'Kind regards'", stdout)

    def test_list_can_show_only_substitutions(self):
        result, stdout, _stderr = self.run_cli("list", "substitutions")

        self.assertEqual(result, 0)
        self.assertIn("substitutions:", stdout)
        self.assertNotIn("expansions:", stdout)

    def test_add_substitution(self):
        result, _stdout, stderr = self.run_cli(
            "add", "substitution", "C-nk_delete", "."
        )

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            self.read_config()["substitutions"]["C-nk_delete"],
            ".",
        )

    def test_add_expansion_writes_existing_sequences_format(self):
        result, _stdout, _stderr = self.run_cli(
            "add", "expansion", ":phone", "1234"
        )

        self.assertEqual(result, 0)
        config = self.read_config()
        self.assertEqual(config["sequences"][":phone"], "1234")
        self.assertNotIn("expansions", config)

    def test_add_refuses_duplicate_without_force(self):
        before = self.config_path.read_text(encoding="utf-8")

        result, _stdout, stderr = self.run_cli(
            "add", "substitution", "C-nk_minus", "new"
        )

        self.assertEqual(result, 2)
        self.assertIn("already exists", stderr)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_add_force_replaces_duplicate(self):
        result, _stdout, _stderr = self.run_cli(
            "add", "substitution", "C-nk_minus", "new", "--force"
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            self.read_config()["substitutions"]["C-nk_minus"],
            "new",
        )

    def test_invalid_addition_does_not_modify_config(self):
        before = self.config_path.read_text(encoding="utf-8")

        result, _stdout, stderr = self.run_cli(
            "add", "substitution", "invalid-modifier-x", "value"
        )

        self.assertEqual(result, 2)
        self.assertIn("invalid change", stderr)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_delete_expansion(self):
        result, _stdout, _stderr = self.run_cli(
            "delete", "expansion", ":sig"
        )

        self.assertEqual(result, 0)
        self.assertNotIn(":sig", self.read_config()["sequences"])

    def test_delete_reports_missing_mapping_without_modifying_config(self):
        before = self.config_path.read_text(encoding="utf-8")

        result, _stdout, stderr = self.run_cli(
            "delete", "expansion", ":missing"
        )

        self.assertEqual(result, 2)
        self.assertIn("does not exist", stderr)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_test_command_validates_config(self):
        result, stdout, stderr = self.run_cli("test")

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("config is valid", stdout)

    def test_test_command_reports_malformed_config(self):
        self.config_path.write_text("{", encoding="utf-8")

        result, _stdout, stderr = self.run_cli("test")

        self.assertEqual(result, 2)
        self.assertIn("keyswap: error:", stderr)

    def test_service_command_uses_user_service(self):
        with patch.object(keyswap.subprocess, "run") as run:
            run.return_value.returncode = 0
            result, _stdout, _stderr = self.run_cli("restart")

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            ["systemctl", "--user", "restart", "keyswap.service"],
            check=False,
        )

    def test_status_disables_pager(self):
        with patch.object(keyswap.subprocess, "run") as run:
            run.return_value.returncode = 3
            result, _stdout, _stderr = self.run_cli("status")

        self.assertEqual(result, 3)
        run.assert_called_once_with(
            [
                "systemctl",
                "--user",
                "status",
                "keyswap.service",
                "--no-pager",
            ],
            check=False,
        )

    def test_history_uses_existing_journal(self):
        with patch.object(keyswap.subprocess, "run") as run:
            run.return_value.returncode = 0
            result, _stdout, _stderr = self.run_cli(
                "history", "--lines", "25", "--since", "today", "--bugs"
            )

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            [
                "journalctl",
                "--user",
                "-u",
                "keyswap.service",
                "--no-pager",
                "-n",
                "25",
                "--since",
                "today",
                "--grep",
                "BUG_CONTEXT",
            ],
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
