import pytest

from src import run_context, sandbox
from src.agent_tools import tool_execute_python, tool_generate_pdf


def test_container_is_isolated(tmp_path):
    opts = sandbox.container_options(tmp_path)
    assert opts["network_mode"] == "none"
    assert opts["cap_drop"] == ["ALL"]
    assert "no-new-privileges" in opts["security_opt"]
    assert opts["read_only"] is True
    assert not opts["user"].startswith("0:")
    assert opts["pids_limit"] > 0 and opts["mem_limit"] and opts["nano_cpus"] > 0
    mounts = list(opts["volumes"])
    assert mounts == [str(tmp_path.resolve())]  # only the session workspace
    assert not any("docker.sock" in m for m in mounts)
    assert "privileged" not in opts


def test_python_runs_in_session_workspace(docker, session):
    out = tool_execute_python("```python\nprint(sum(i*i for i in range(1, 101)))\n```")
    assert out.startswith("SUKCES") and "338350" in out
    run = docker.runs[-1]
    assert run["network_mode"] == "none"
    writable = [path for path, m in run["volumes"].items() if m["mode"] == "rw"]
    assert writable == [str(run_context.workspace_dir().resolve())]  # only the session workspace is writable


def test_timeout_kills_container(docker, session):
    docker.hang = True
    out = tool_execute_python("print('x')")
    assert out.startswith("BŁĄD WYKONANIA") and "limit czasu" in out


def test_missing_image_is_reported_not_pulled(session):
    from fakes import FakeDocker

    fake = FakeDocker(image_available=False)
    sandbox.set_docker_client(fake)
    try:
        out = tool_execute_python("print(1)")
    finally:
        sandbox.set_docker_client(None)
    assert out.startswith("BŁĄD") and "setup" in out
    assert fake.images.pulled == [] and fake.runs == []


def test_latex_error_is_explained(docker, session):
    docker.latex_fails = True
    out = tool_generate_pdf("\\documentclass{article}\\begin{document}x\\end{document}", "broken.tex")
    assert out.startswith("BŁĄD KOMPILACJI") and "LaTeX Error" in out


def test_not_latex_is_rejected(docker, session):
    assert tool_generate_pdf("just text", "x.tex").startswith("BŁĄD")
    assert docker.runs == []


@pytest.mark.parametrize("bad", ["../escape.tex", "sub/dir.tex", "/etc/passwd"])
def test_workspace_file_rejects_paths(session, bad):
    with pytest.raises(ValueError):
        run_context.workspace_file(bad)


def test_company_documents_are_mounted_read_only(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    opts = sandbox.container_options(tmp_path / "ws", data_dir=docs)
    assert opts["volumes"][str(docs.resolve())] == {"bind": "/data", "mode": "ro"}
    assert opts["volumes"][str((tmp_path / "ws").resolve())] == {"bind": "/work", "mode": "rw"}
    assert opts["network_mode"] == "none"
