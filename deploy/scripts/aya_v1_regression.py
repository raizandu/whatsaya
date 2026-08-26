#!/usr/bin/env python3
"""Executa e consolida a regressão AYA V1 sem acessar WhatsApp ou produção."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = REPO_ROOT / "tests" / "fixtures" / "aya_v1_cases.json"
EXPECTED_IDS = {f"{case_id:02d}" for case_id in range(1, 22)}
STRICT_FAILURE_STATUSES = {"PENDENTE_STAGING", "FALHOU", "SEM_COBERTURA"}
_METADATA_VALUE = re.compile(r"^[A-Za-z0-9._:/+-]{1,120}$")


class RegressionError(ValueError):
    """Erro seguro de entrada que pode ser exibido sem dados do contato."""


@dataclass(frozen=True)
class SuiteResult:
    state: str
    exit_code: int | None
    elapsed_seconds: float


def load_catalog(path: Path = DEFAULT_CATALOG, repo_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegressionError("catálogo de regressão inválido ou inacessível") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise RegressionError("catálogo deve conter uma lista 'cases'")
    cases = payload["cases"]
    _validate_catalog(cases, repo_root)
    return sorted(cases, key=lambda item: item["id"])


def _validate_catalog(cases: list[Any], repo_root: Path) -> None:
    if len(cases) != 21:
        raise RegressionError("catálogo deve conter exatamente 21 casos")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(ids) != 21 or set(ids) != EXPECTED_IDS or len(ids) != len(set(ids)):
        raise RegressionError("catálogo deve conter IDs únicos de 01 a 21")

    for case in cases:
        case_id = case["id"]
        if not isinstance(case.get("title"), str) or not case["title"].strip():
            raise RegressionError(f"caso {case_id}: título obrigatório")
        if case.get("criticality") not in {"P0", "P1"}:
            raise RegressionError(f"caso {case_id}: criticidade inválida")
        if case.get("layer") not in {"AUTOMACAO", "STAGING", "HIBRIDA"}:
            raise RegressionError(f"caso {case_id}: camada inválida")
        if not isinstance(case.get("staging_required"), bool):
            raise RegressionError(f"caso {case_id}: staging_required deve ser booleano")
        if case["layer"] == "AUTOMACAO" and case["staging_required"]:
            raise RegressionError(f"caso {case_id}: camada automática não exige staging")
        if case["layer"] in {"STAGING", "HIBRIDA"} and not case["staging_required"]:
            raise RegressionError(f"caso {case_id}: camada conversacional deve exigir staging")
        _validate_evidence(case, repo_root)
        _validate_checks(case)


def _validate_evidence(case: dict[str, Any], repo_root: Path) -> None:
    case_id = case["id"]
    evidence = case.get("automated_evidence")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise RegressionError(f"caso {case_id}: automated_evidence deve ser uma lista")
    if case["layer"] in {"AUTOMACAO", "HIBRIDA"} and not evidence:
        raise RegressionError(f"caso {case_id}: camada automática sem evidência")
    for reference in evidence:
        if "::" not in reference:
            raise RegressionError(f"caso {case_id}: evidência deve usar caminho::seletor")
        relative_path, selector = reference.split("::", 1)
        evidence_path = repo_root / relative_path
        if not evidence_path.is_file():
            raise RegressionError(f"caso {case_id}: arquivo de evidência inexistente")
        if not selector or selector not in evidence_path.read_text(encoding="utf-8"):
            raise RegressionError(f"caso {case_id}: seletor de evidência inexistente")


def _validate_checks(case: dict[str, Any]) -> None:
    case_id = case["id"]
    checks = case.get("checks")
    if not isinstance(checks, list) or not checks:
        raise RegressionError(f"caso {case_id}: checks objetivos obrigatórios")
    check_ids: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            raise RegressionError(f"caso {case_id}: check inválido")
        if check.get("kind") not in {"automation", "staging"}:
            raise RegressionError(f"caso {case_id}: tipo de check inválido")
        if not isinstance(check.get("id"), str) or not check["id"].startswith(f"{case_id}."):
            raise RegressionError(f"caso {case_id}: ID de check inválido")
        if not isinstance(check.get("description"), str) or not check["description"].strip():
            raise RegressionError(f"caso {case_id}: descrição de check obrigatória")
        check_ids.append(check["id"])
    if len(check_ids) != len(set(check_ids)):
        raise RegressionError(f"caso {case_id}: IDs de check duplicados")
    has_staging_check = any(check["kind"] == "staging" for check in checks)
    if has_staging_check != case["staging_required"]:
        raise RegressionError(f"caso {case_id}: checks de staging inconsistentes")


def load_staging_results(path: Path | None, cases: list[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegressionError("resultado de staging inválido ou inacessível") from exc
    raw_cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(raw_cases, dict):
        raise RegressionError("resultado de staging deve conter um objeto 'cases'")
    known_ids = {case["id"] for case in cases}
    if not set(raw_cases).issubset(known_ids):
        raise RegressionError("resultado de staging contém caso desconhecido")

    accepted: dict[str, dict[str, bool]] = {}
    expected_checks = {
        case["id"]: {check["id"] for check in case["checks"] if check["kind"] == "staging"}
        for case in cases
    }
    for case_id, result in raw_cases.items():
        if not isinstance(result, dict) or not isinstance(result.get("checks"), dict):
            raise RegressionError(f"staging {case_id}: objeto checks obrigatório")
        checks = result["checks"]
        if not set(checks).issubset(expected_checks[case_id]):
            raise RegressionError(f"staging {case_id}: check desconhecido")
        if not all(isinstance(value, bool) for value in checks.values()):
            raise RegressionError(f"staging {case_id}: resultados devem ser booleanos")
        accepted[case_id] = dict(checks)
    return accepted


def _run_suite(repo_root: Path) -> SuiteResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["npm", "test"],
            cwd=repo_root,
            check=False,
            text=True,
        )
    except OSError:
        return SuiteResult("FALHOU", 127, round(time.monotonic() - started, 3))
    state = "PASSOU" if completed.returncode == 0 else "FALHOU"
    return SuiteResult(state, completed.returncode, round(time.monotonic() - started, 3))


def _git_commit(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "desconhecido"
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and _METADATA_VALUE.fullmatch(value) else "desconhecido"


def _metadata_value(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return "não_informado"
    if not _METADATA_VALUE.fullmatch(cleaned):
        raise RegressionError(f"metadado {field} contém caracteres inválidos")
    return cleaned


def _case_status(
    case: dict[str, Any],
    suite: SuiteResult,
    staging: dict[str, dict[str, bool]],
) -> tuple[str, dict[str, bool | int]]:
    has_automation = bool(case["automated_evidence"])
    automation_passed = has_automation and suite.state == "PASSOU"
    automation_failed = has_automation and suite.state == "FALHOU"
    required_staging_checks = [
        check["id"] for check in case["checks"] if check["kind"] == "staging"
    ]
    supplied = staging.get(case["id"], {})
    staging_complete = bool(required_staging_checks) and all(
        check_id in supplied for check_id in required_staging_checks
    )
    staging_failed = any(supplied.get(check_id) is False for check_id in required_staging_checks)
    staging_passed = staging_complete and all(supplied[check_id] for check_id in required_staging_checks)

    facts: dict[str, bool | int] = {
        "automation_evidence_count": len(case["automated_evidence"]),
        "automation_executed": suite.state != "PULADA",
        "automation_passed": automation_passed,
        "staging_required": case["staging_required"],
        "staging_checks_expected": len(required_staging_checks),
        "staging_checks_supplied": sum(check_id in supplied for check_id in required_staging_checks),
        "staging_passed": staging_passed,
    }
    if automation_failed or staging_failed:
        return "FALHOU", facts
    if case["staging_required"] and not staging_complete:
        if automation_passed:
            return "PASSOU_AUTOMACAO", facts
        return "PENDENTE_STAGING", facts
    if has_automation and not automation_passed:
        return "SEM_COBERTURA", facts
    if not has_automation and not staging_passed:
        return "SEM_COBERTURA", facts
    if case["staging_required"]:
        return "PASSOU", facts
    return "PASSOU_AUTOMACAO", facts


def _build_report(
    cases: list[dict[str, Any]],
    suite: SuiteResult,
    staging: dict[str, dict[str, bool]],
    strict: bool,
    run_id: str,
    metadata: dict[str, str],
) -> dict[str, Any]:
    results = []
    for case in cases:
        status, facts = _case_status(case, suite, staging)
        results.append(
            {
                "id": case["id"],
                "title": case["title"],
                "criticality": case["criticality"],
                "layer": case["layer"],
                "status": status,
                "facts": facts,
            }
        )
    counts = {
        status: sum(result["status"] == status for result in results)
        for status in ("PASSOU_AUTOMACAO", "PENDENTE_STAGING", "PASSOU", "FALHOU", "SEM_COBERTURA")
    }
    approved = suite.state == "PASSOU" and len(results) == 21 and all(
        result["status"] == "PASSOU"
        if result["facts"]["staging_required"]
        else result["status"] in {"PASSOU", "PASSOU_AUTOMACAO"}
        for result in results
    )
    strict_blockers = [
        result["id"]
        for result in results
        if (
            result["status"] in STRICT_FAILURE_STATUSES
            or (result["status"] == "PASSOU_AUTOMACAO" and result["facts"]["staging_required"])
        )
    ]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "candidate": metadata,
        "approved_for_final_qa": approved,
        "strict": strict,
        "suite": {
            "command": ["npm", "test"],
            "state": suite.state,
            "exit_code": suite.exit_code,
            "elapsed_seconds": suite.elapsed_seconds,
        },
        "summary": {"total": len(results), "counts": counts, "strict_blockers": strict_blockers},
        "cases": results,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    suite = report["suite"]
    approval = "SIM" if report["approved_for_final_qa"] else "NÃO"
    lines = [
        "# Resultado da regressão AYA V1",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Commit: `{report['candidate']['commit']}`",
        f"- Modelo/provider: `{report['candidate']['model']}` / `{report['candidate']['provider']}`",
        f"- Reasoning/config: `{report['candidate']['reasoning']}` / `{report['candidate']['config_subdir']}`",
        f"- Suíte automática: **{suite['state']}** (exit `{suite['exit_code']}`)",
        f"- Liberado para QA Final: **{approval}**",
        "",
        "| ID | Cenário | Criticidade | Camada | Resultado |",
        "|---|---|---|---|---|",
    ]
    for result in report["cases"]:
        lines.append(
            f"| {result['id']} | {result['title']} | {result['criticality']} | "
            f"{result['layer']} | {result['status']} |"
        )
    counts = report["summary"]["counts"]
    lines.extend(
        [
            "",
            "## Resumo",
            "",
            *[f"- {status}: {count}" for status, count in counts.items()],
            "",
            "Um caso que exige staging só recebe `PASSOU` quando todos os checks de staging "
            "foram informados como verdadeiros. A suíte automática isolada nunca produz 21/21.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_artifacts(output_dir: Path, report: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "resultado.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "resultado.md").write_text(_render_markdown(report), encoding="utf-8")


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Regressão consolidada da AYA V1")
    parser.add_argument("--skip-suite", action="store_true", help="não executa npm test")
    parser.add_argument("--staging-results", type=Path, help="JSON com checks objetivos de staging")
    parser.add_argument("--output-dir", type=Path, help="diretório dos artefatos")
    parser.add_argument("--strict", action="store_true", help="falha com qualquer cenário obrigatório pendente")
    parser.add_argument("--model", default=os.getenv("WHATSAPP_CLIENT_MODEL", ""))
    parser.add_argument("--provider", default=os.getenv("WHATSAPP_CLIENT_PROVIDER", ""))
    parser.add_argument("--reasoning", default=os.getenv("WHATSAPP_CLIENT_REASONING_EFFORT", ""))
    parser.add_argument("--config-subdir", default=os.getenv("WHATSAPP_CONFIG_SUBDIR", ""))
    return parser


def execute(
    args: argparse.Namespace,
    repo_root: Path = REPO_ROOT,
    suite_runner: Callable[[Path], SuiteResult] = _run_suite,
) -> tuple[int, Path, dict[str, Any]]:
    cases = load_catalog(repo_root / "tests" / "fixtures" / "aya_v1_cases.json", repo_root)
    staging = load_staging_results(args.staging_results, cases)
    suite = SuiteResult("PULADA", None, 0.0) if args.skip_suite else suite_runner(repo_root)
    run_id = _default_run_id()
    output_dir = args.output_dir or repo_root / "reports" / "aya-v1" / run_id
    metadata = {
        "commit": _git_commit(repo_root),
        "model": _metadata_value(args.model, "model"),
        "provider": _metadata_value(args.provider, "provider"),
        "reasoning": _metadata_value(args.reasoning, "reasoning"),
        "config_subdir": _metadata_value(args.config_subdir, "config_subdir"),
    }
    report = _build_report(cases, suite, staging, args.strict, run_id, metadata)
    _write_artifacts(output_dir, report)

    if suite.state == "FALHOU":
        return 1, output_dir, report
    if args.strict and report["summary"]["strict_blockers"]:
        return 1, output_dir, report
    return 0, output_dir, report


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, output_dir, report = execute(args)
    except RegressionError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    print(f"resultado={output_dir / 'resultado.md'}")
    print(f"liberado_qa_final={str(report['approved_for_final_qa']).lower()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
