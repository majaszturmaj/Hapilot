import textwrap

import pytest

from src.config import ConfigError, NotLocalEndpoint, is_local_url, load_profiles, load_settings


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:8611/v1", True),
    ("http://localhost:1234/v1", True),
    ("http://gpu-host.local:8000/v1", True),
    ("http://192.168.1.10:8000/v1", True),
    ("http://10.0.0.5/v1", True),
    ("http://[::1]:8000/v1", True),
    ("https://api.openai.com/v1", False),
    ("http://8.8.8.8:80/v1", False),
    ("http://example.com/v1", False),
])
def test_is_local_url(url, expected):
    assert is_local_url(url) is expected


def _write(tmp_path, body):
    path = tmp_path / "profiles.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_repository_profiles_load():
    settings = load_settings({})
    assert {"reference", "best-local"} <= set(settings.profiles)
    ref = settings.profile("reference")
    assert ref.model_for("coder").model != ref.model_for("writer").model  # two models
    best = settings.profile("best-local")
    assert best.model_for("coder").model == best.model_for("writer").model  # one model for all roles
    assert best.model_for("coder").temperature == 0.0  # role override


def test_non_local_endpoint_is_rejected(tmp_path):
    path = _write(tmp_path, """
        profiles:
          cloud:
            models: {m: {model: x, base_url: "https://api.openai.com/v1"}}
            roles: {default: m}
    """)
    with pytest.raises(NotLocalEndpoint):
        load_profiles(path, {})


def test_env_expansion_and_default_endpoint(tmp_path):
    path = _write(tmp_path, """
        endpoint: ${HAPILOT_MODEL_URL:-http://127.0.0.1:9000/v1}
        profiles:
          p:
            models: {m: {model: x}}
            roles: {default: m}
    """)
    profiles, default = load_profiles(path, {})
    assert default == "p" and profiles["p"].default_model().base_url == "http://127.0.0.1:9000/v1"
    profiles, _ = load_profiles(path, {"HAPILOT_MODEL_URL": "http://192.168.0.2:8000/v1"})
    assert profiles["p"].default_model().base_url == "http://192.168.0.2:8000/v1"


def test_profile_needs_default_role(tmp_path):
    path = _write(tmp_path, """
        profiles:
          p:
            models: {m: {model: x, base_url: "http://127.0.0.1:1/v1"}}
            roles: {coder: m}
    """)
    with pytest.raises(ConfigError):
        load_profiles(path, {})


def test_unknown_profile_in_env_fails():
    with pytest.raises(ConfigError):
        load_settings({"HAPILOT_PROFILE": "does-not-exist"})


def test_invalid_approval_policy_fails():
    with pytest.raises(ConfigError):
        load_settings({"HAPILOT_APPROVAL_POLICY": "sometimes"})


def test_n8n_webhook_must_be_local():
    with pytest.raises(NotLocalEndpoint):
        load_settings({"HAPILOT_N8N_WEBHOOK_BASE": "https://hooks.example.com/webhook"})
