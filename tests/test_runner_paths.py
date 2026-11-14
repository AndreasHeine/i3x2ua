from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import scripts.run_benchmark as benchmark
import scripts.run_conformance_headless as conformance_headless
import scripts.run_conformancetest as conformance
import scripts.run_stresstest as stress
import scripts.run_tests as test_runner

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runner_paths_are_anchored_at_repository_root() -> None:
    assert benchmark.REPO_ROOT == REPO_ROOT
    assert conformance.REPO_ROOT == REPO_ROOT
    assert conformance.CONFORMANCE_TESTS_DIR == REPO_ROOT / "i3X" / "conformance-tests"
    assert conformance_headless._load_orchestrator() is conformance
    assert stress.REPO_ROOT == REPO_ROOT
    assert stress.DISCOVERY_COMMAND[0] == str(REPO_ROOT / "samples" / "client.py")
    assert stress.SUBSCRIPTION_COMMAND[0] == str(REPO_ROOT / "samples" / "subscription_client.py")
    assert test_runner.REPO_ROOT == REPO_ROOT


def test_benchmark_help_works_when_invoked_outside_repository(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "run_benchmark.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Benchmark sample-client request latency" in result.stdout


def test_test_runner_executes_checks_from_repository_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    run = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=0))
    monkeypatch.setattr("scripts.run_tests.subprocess.run", run)

    assert test_runner.main() == 0
    capsys.readouterr()

    assert run.call_count == len(test_runner.COMMANDS)
    assert all(call.kwargs["cwd"] == REPO_ROOT for call in run.call_args_list)
