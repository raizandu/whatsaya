"""Testes do runner sanitizado de Definition of Done da AYA V1."""

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = REPO_ROOT / "deploy" / "scripts" / "aya_v1_regression.py"
SPEC = importlib.util.spec_from_file_location("aya_v1_regression", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def _args(output_dir: Path, **overrides):
    values = {
        "skip_suite": True,
        "staging_results": None,
        "output_dir": output_dir,
        "strict": False,
        "model": "terra",
        "provider": "openrouter",
        "reasoning": "medium",
        "config_subdir": "instance",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class AyaV1RegressionTests(unittest.TestCase):
    def test_catalog_has_exactly_21_unique_valid_cases(self):
        cases = runner.load_catalog(repo_root=REPO_ROOT)

        self.assertEqual(len(cases), 21)
        self.assertEqual([case["id"] for case in cases], [f"{number:02d}" for number in range(1, 22)])
        self.assertTrue(all(case["checks"] for case in cases))

    def test_generates_sanitized_json_and_markdown_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "report"
            exit_code, _, report = runner.execute(_args(output_dir), repo_root=REPO_ROOT)

            self.assertEqual(exit_code, 0)
            self.assertTrue((output_dir / "resultado.json").is_file())
            self.assertTrue((output_dir / "resultado.md").is_file())
            saved = json.loads((output_dir / "resultado.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["summary"]["total"], 21)
            self.assertFalse(saved["approved_for_final_qa"])
            self.assertEqual(saved["candidate"]["model"], "terra")
            self.assertEqual(saved["candidate"]["reasoning"], "medium")
            self.assertNotIn(str(REPO_ROOT), (output_dir / "resultado.md").read_text(encoding="utf-8"))
            self.assertEqual(report["suite"]["state"], "PULADA")

    def test_rejects_free_form_candidate_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(runner.RegressionError):
                runner.execute(
                    _args(Path(temp_dir), model="terra com telefone 5511999999999"),
                    repo_root=REPO_ROOT,
                )

    def test_strict_exits_one_when_any_required_staging_is_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            exit_code, _, report = runner.execute(
                _args(Path(temp_dir), strict=True),
                repo_root=REPO_ROOT,
            )

            self.assertEqual(exit_code, 1)
            self.assertIn("01", report["summary"]["strict_blockers"])
            self.assertIn("07", report["summary"]["strict_blockers"])
            self.assertIn("21", report["summary"]["strict_blockers"])

    def test_all_green_staging_cannot_fake_21_of_21_when_suite_is_skipped(self):
        cases = runner.load_catalog(repo_root=REPO_ROOT)
        staging_payload = {
            "cases": {
                case["id"]: {
                    "checks": {
                        check["id"]: True
                        for check in case["checks"]
                        if check["kind"] == "staging"
                    }
                }
                for case in cases
                if case["staging_required"]
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            staging_path = temp_path / "staging.json"
            staging_path.write_text(json.dumps(staging_payload), encoding="utf-8")
            exit_code, _, report = runner.execute(
                _args(temp_path / "report", staging_results=staging_path),
                repo_root=REPO_ROOT,
            )

            self.assertEqual(exit_code, 0)
            self.assertFalse(report["approved_for_final_qa"])
            self.assertGreater(report["summary"]["counts"]["SEM_COBERTURA"], 0)
            passed = report["summary"]["counts"]["PASSOU"] + report["summary"]["counts"]["PASSOU_AUTOMACAO"]
            self.assertLess(passed, 21)

    def test_strict_treats_automation_only_success_as_pending_when_staging_is_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            exit_code, _, report = runner.execute(
                _args(Path(temp_dir), skip_suite=False, strict=True),
                repo_root=REPO_ROOT,
                suite_runner=lambda _root: runner.SuiteResult("PASSOU", 0, 0.1),
            )

            case_07 = next(result for result in report["cases"] if result["id"] == "07")
            self.assertEqual(exit_code, 1)
            self.assertEqual(case_07["status"], "PASSOU_AUTOMACAO")
            self.assertIn("07", report["summary"]["strict_blockers"])

    def test_complete_staging_and_passing_suite_approve_all_cases(self):
        cases = runner.load_catalog(repo_root=REPO_ROOT)
        staging_payload = {
            "cases": {
                case["id"]: {
                    "checks": {
                        check["id"]: True
                        for check in case["checks"]
                        if check["kind"] == "staging"
                    }
                }
                for case in cases
                if case["staging_required"]
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            staging_path = temp_path / "staging.json"
            staging_path.write_text(json.dumps(staging_payload), encoding="utf-8")
            exit_code, _, report = runner.execute(
                _args(temp_path / "report", skip_suite=False, staging_results=staging_path, strict=True),
                repo_root=REPO_ROOT,
                suite_runner=lambda _root: runner.SuiteResult("PASSOU", 0, 0.1),
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(report["approved_for_final_qa"])
            self.assertEqual(report["summary"]["counts"]["PASSOU"], 21)
            self.assertEqual(report["summary"]["counts"]["PASSOU_AUTOMACAO"], 0)

    def test_suite_invocation_uses_argument_list_and_repository_cwd(self):
        completed = subprocess.CompletedProcess(["npm", "test"], 0)
        with patch.object(runner.subprocess, "run", return_value=completed) as run:
            result = runner._run_suite(REPO_ROOT)

        self.assertEqual(result.state, "PASSOU")
        run.assert_called_once_with(
            ["npm", "test"],
            cwd=REPO_ROOT,
            check=False,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
