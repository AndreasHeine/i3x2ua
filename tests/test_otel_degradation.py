"""The gateway must own the signal that OpenTelemetry is advertised but cannot export.

Registry images ship the ``otel`` extra (see ``.github/workflows/docker.yml``), so a deployment
that sets ``I3X_OTEL_ENABLED=true`` really gets traces. When the SDK/exporter is missing, FastAPI
raises ``FastAPIError`` inside its lifespan guard and swallows it into the ``fastapi`` logger, so
tracing vanishes without a gateway-visible reason: the gateway has to detect the missing modules,
say so at startup, and must not ask FastAPI to auto-configure an export pipeline it cannot build.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

from i3x_server import telemetry
from i3x_server.application.state import GatewayState, install_gateway_state
from i3x_server.bootstrap import app_factory, openapi_config, runtime_setup
from i3x_server.bootstrap.app_factory import create_app
from i3x_server.config.settings import settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The modules FastAPI's automatic OTLP export imports at startup
# (``fastapi/telemetry/_runtime.py``). Their absence is the degradation.
_MISSING = ("opentelemetry.sdk.trace", "opentelemetry.exporter.otlp.proto.http.trace_exporter")
_LOGGER = "i3x_server.bootstrap.runtime_setup"


def test_missing_otel_export_modules_reports_absent_sdk_and_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing SDK or exporter package is reported by module name."""
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter", None)

    assert telemetry.missing_otel_export_modules() == _MISSING


def test_missing_otel_export_modules_is_empty_when_modules_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the extra installed nothing is reported, so startup stays quiet."""
    monkeypatch.setattr(telemetry, "find_spec", lambda name: object())

    assert telemetry.missing_otel_export_modules() == ()


def test_missing_otel_export_modules_survives_an_unloaded_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """``find_spec`` raises for a dotted module whose parent package is not loaded."""
    monkeypatch.setattr(telemetry, "find_spec", lambda name: (_ for _ in ()).throw(ValueError("not loaded")))

    assert telemetry.missing_otel_export_modules() == _MISSING


def test_missing_otel_export_modules_reports_a_module_with_no_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolvable package that simply has no module is reported as missing."""
    monkeypatch.setattr(telemetry, "find_spec", lambda name: None)

    assert telemetry.missing_otel_export_modules() == _MISSING


def test_diagnose_otel_capability_names_the_degradation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enabled tracing with no export pipeline must be stated at the gateway's own logger."""
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_otlp_endpoint", "http://jaeger:4318")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "missing_otel_export_modules", lambda: _MISSING)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        runtime_setup.diagnose_otel_capability()

    record = next(item for item in caplog.records if item.name == _LOGGER)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "OpenTelemetry enabled" in message
    assert "opentelemetry.sdk.trace" in message
    assert "opentelemetry.exporter.otlp.proto.http.trace_exporter" in message
    assert "http://jaeger:4318" in message
    assert "I3X_OTEL_ENABLED=false" in message


def test_diagnose_otel_capability_reports_the_configured_export_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deployment-supplied standard variable wins over the gateway setting, like the exporter does."""
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_otlp_endpoint", None)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setattr(telemetry, "missing_otel_export_modules", lambda: _MISSING)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        runtime_setup.diagnose_otel_capability()

    assert "endpoint=http://collector:4318" in caplog.text


def test_diagnose_otel_capability_reports_an_unset_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without any endpoint the message says so instead of leaving the field empty."""
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_otlp_endpoint", None)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "missing_otel_export_modules", lambda: _MISSING)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        runtime_setup.diagnose_otel_capability()

    assert "endpoint=unset" in caplog.text


@pytest.mark.parametrize("enabled", [False, True])
def test_diagnose_otel_capability_stays_silent_without_a_degradation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    enabled: bool,
) -> None:
    """Nothing is logged when tracing is off or when export can run."""
    monkeypatch.setattr(settings, "otel_enabled", enabled)
    monkeypatch.setattr(telemetry, "missing_otel_export_modules", lambda: ())

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        runtime_setup.diagnose_otel_capability()

    assert not [item for item in caplog.records if item.name == _LOGGER]


def test_telemetry_config_disables_auto_configure_when_export_cannot_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """FastAPI must not be asked to build an export pipeline the image cannot provide."""
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(telemetry, "missing_otel_export_modules", lambda: _MISSING)

    config = openapi_config.telemetry_config()

    assert config["auto_configure"] is False
    # Native instrumentation stays enabled: providers can be configured by another component.
    assert config["tracing"] is True
    assert config["metrics"] is True


def test_telemetry_config_does_not_probe_export_when_tracing_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled tracing must not resolve the optional packages, which the probe imports."""
    probes: list[str] = []

    def _probe() -> tuple[str, ...]:
        probes.append("probe")
        return ()

    monkeypatch.setattr(settings, "otel_enabled", False)
    monkeypatch.setattr(telemetry, "missing_otel_export_modules", _probe)

    config = openapi_config.telemetry_config()

    assert config["auto_configure"] is False
    assert probes == []


def test_create_app_auto_configures_when_export_can_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the extra installed the auto-configure switch follows the setting."""
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(telemetry, "find_spec", lambda name: object())

    assert create_app()._telemetry["auto_configure"] is True


@pytest.mark.asyncio
async def test_lifespan_reports_otel_capability_before_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """The diagnostic runs during startup, after logging is configured, before instruments exist."""
    monkeypatch.setenv("I3X_SKIP_OPCUA_CONNECT", "1")
    monkeypatch.setenv("I3X_ENABLE_MCP", "0")
    calls: list[str] = []
    monkeypatch.setattr(runtime_setup, "diagnose_otel_capability", lambda: calls.append("diagnose"))
    monkeypatch.setattr(runtime_setup, "configure_otel_metrics", lambda: calls.append("metrics"))

    app = FastAPI()
    install_gateway_state(app, GatewayState())

    async with app_factory.lifespan(app):
        pass

    assert calls == ["diagnose", "metrics"]


def _build_steps(workflow: str) -> list[str]:
    """The bodies of the steps that run ``docker/build-push-action``."""
    return [body for body in workflow.split("- name:") if "docker/build-push-action" in body]


def test_registry_image_builds_install_the_otel_extra() -> None:
    """The scan build and the pushed build must both install the OTel extra.

    ``docker-compose.yml`` advertises ``I3X_OTEL_ENABLED: "true"`` and an OTLP endpoint, so the
    published image has to be able to export. The scanned image must also match the pushed one,
    otherwise the Trivy CRITICAL gate would not cover the shipped OTel dependencies.
    """
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
    steps = _build_steps(workflow)

    assert len(steps) == 2, "the workflow must build the scan image and the pushed image"
    for step in steps:
        assert "INSTALL_OTEL=true" in step


def test_compose_build_installs_the_otel_extra_it_advertises() -> None:
    """Regression guard: the local build keeps tracing truthful for compose."""
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'I3X_OTEL_ENABLED: "true"' in compose
    assert 'INSTALL_OTEL: "true"' in compose
