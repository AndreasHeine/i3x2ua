from __future__ import annotations

from pytest import MonkeyPatch

from i3x_server.config.settings import Settings


def test_settings_trim_whitespace_from_env_values(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("I3X_MODEL_PRELOAD_ON_STARTUP", " true ")
    monkeypatch.setenv("I3X_MODEL_PRELOAD_BLOCKING", " false ")
    monkeypatch.setenv("I3X_SUBSCRIPTIONS_INITIAL_VALUES", " 1 ")

    settings = Settings()

    assert settings.model_preload_on_startup is True
    assert settings.model_preload_blocking is False
    assert settings.subscriptions_initial_values is True
