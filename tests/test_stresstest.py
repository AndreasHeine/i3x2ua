from __future__ import annotations

import base64
import io
import ssl
import sys
import urllib.error
from email.message import Message
from unittest.mock import Mock, patch

import pytest

import run_stresstest


def make_supervisor(monkeypatch: pytest.MonkeyPatch, *options: str) -> run_stresstest.Supervisor:
    monkeypatch.setattr(sys, "argv", ["run_stresstest.py", *options])
    return run_stresstest.Supervisor(run_stresstest._parse_args())


@pytest.mark.parametrize("worker", ["discovery_worker", "subscription_worker"])
@pytest.mark.parametrize("authenticated", [False, True])
def test_workers_forward_connection_options(monkeypatch: pytest.MonkeyPatch, worker: str, authenticated: bool) -> None:
    options = ["--base-url", "https://127.0.0.1:8443/v1", "--request-timeout", "7"]
    if authenticated:
        options += ["--insecure", "--username", "test-user", "--password", "test-password"]
    supervisor = make_supervisor(monkeypatch, *options)
    process = Mock(stdout=io.StringIO(""), stderr=io.StringIO(""), returncode=0)

    def communicate(timeout: float) -> tuple[None, str]:
        supervisor.stop_event.set()
        return None, ""

    process.communicate.side_effect = communicate
    process.wait.side_effect = supervisor.stop_event.set
    spawn = Mock(return_value=process)
    monkeypatch.setattr(supervisor, "_spawn", spawn)
    getattr(supervisor, worker)(0)

    base_command = (
        run_stresstest.DISCOVERY_COMMAND if worker == "discovery_worker" else run_stresstest.SUBSCRIPTION_COMMAND
    )
    expected = [*base_command, "--base-url", options[1], "--timeout", "7.0"]
    if authenticated:
        expected += ["--insecure", "--username", "test-user", "--password", "test-password"]
    spawn.assert_called_once_with(expected, capture_stdout=worker == "subscription_worker")


@pytest.mark.parametrize("insecure", [False, True])
@pytest.mark.parametrize(
    "credentials", [[], ["--username", "test-user"], ["--username", "test-user", "--password", "pw"]]
)
def test_probe_uses_tls_and_auth_options(
    monkeypatch: pytest.MonkeyPatch, insecure: bool, credentials: list[str]
) -> None:
    supervisor = make_supervisor(
        monkeypatch,
        "--base-url",
        "https://127.0.0.1:8443/v1/",
        *credentials,
        *(["--insecure"] if insecure else []),
    )
    with patch("run_stresstest.urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.side_effect = supervisor.stop_event.set
        supervisor.probe_worker()

    request = urlopen.call_args.args[0]
    assert request.full_url == "https://127.0.0.1:8443/v1/info"
    assert request.get_method() == "GET"
    if credentials:
        password = credentials[3] if len(credentials) > 2 else ""
        encoded = base64.b64encode(f"test-user:{password}".encode()).decode("ascii")
        assert request.get_header("Authorization") == f"Basic {encoded}"
    else:
        assert request.get_header("Authorization") is None
    context = urlopen.call_args.kwargs["context"]
    assert context.check_hostname is not insecure
    assert context.verify_mode == (ssl.CERT_NONE if insecure else ssl.CERT_REQUIRED)
    assert urlopen.call_args.kwargs["timeout"] == supervisor.args.probe_timeout
    assert supervisor.probe.samples == 1
    assert supervisor.probe.errors == 0


def test_probe_records_authentication_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = make_supervisor(monkeypatch, "--username", "test-user", "--password", "wrong")

    def fail_request(*args: object, **kwargs: object) -> None:
        supervisor.stop_event.set()
        raise urllib.error.HTTPError("http://127.0.0.1:8000/v1/info", 401, "Unauthorized", Message(), None)

    with patch("run_stresstest.urllib.request.urlopen", side_effect=fail_request):
        supervisor.probe_worker()
    assert supervisor.probe.samples == 1
    assert supervisor.probe.errors == 1
