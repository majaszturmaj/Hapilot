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
    assert settings.default_profile == "best-local"
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


def test_ports_and_added_documents_folder_are_configurable(tmp_path):
    defaults = load_settings({})
    assert (defaults.ui_port, defaults.modelswap_port, defaults.backend_port) == (8601, 8611, 8612)
    assert defaults.added_docs_dir == defaults.data_dir / "added_docs"
    custom = load_settings({"HAPILOT_UI_PORT": "9601", "HAPILOT_MODELSWAP_PORT": "9611", "HAPILOT_BACKEND_PORT": "9612",
                            "HAPILOT_ADDED_DOCS_DIR": str(tmp_path / "dodane")})
    assert (custom.ui_port, custom.modelswap_port, custom.backend_port) == (9601, 9611, 9612)
    assert custom.added_docs_dir == tmp_path / "dodane"


@pytest.mark.parametrize("value", ["80", "70000", "osiem"])
def test_bad_ports_are_rejected(value):
    with pytest.raises(ConfigError, match="HAPILOT_UI_PORT"):
        load_settings({"HAPILOT_UI_PORT": value})


def test_launcher_ports_follow_dotenv(tmp_path):
    """Ports set only in the project's .env reach the launcher (it used to read os.environ before .env was loaded)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    (tmp_path / ".env").write_text("HAPILOT_UI_PORT=9701\nHAPILOT_MODELSWAP_PORT=9711\n")
    code = (f"import sys; from pathlib import Path; import src.config as c; c.PROJECT_ROOT = Path({str(tmp_path)!r}); "
            "sys.path.insert(0, 'scripts'); import hapilotctl; "
            "print(hapilotctl.UI_PORT, hapilotctl.PROXY_PORT, hapilotctl.BACKEND_PORT)")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HAPILOT_")}
    env.update(HAPILOT_PROFILES_FILE=str(root / "config" / "profiles.yaml"), HAPILOT_DATA_DIR=str(tmp_path / "var"))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout.split()[-3:] == ["9701", "9711", "8612"], out.stderr[-500:]
