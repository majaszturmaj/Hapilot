"""The agent image's grep wrapper (sandbox/agent/grep): a call without a search pattern gets a message the model can
act on; every other call goes to the real grep unchanged. Found in the v0.4.6 dev-set check (2026-11-23): cut-off
`grep -ril ` calls, grep's usage text, the same call again, the SDK's loop stop."""

import shutil
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[2] / "sandbox" / "agent" / "grep"
pytestmark = pytest.mark.skipif(not Path("/usr/bin/grep").exists() or shutil.which("sh") is None,
                                reason="needs sh and /usr/bin/grep")


def grep(*args, cwd):
    return subprocess.run(["sh", str(WRAPPER), *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=10)


@pytest.mark.parametrize("args", [("-ril",), ("-ril", "-e"), (), ("-r", "--")])
def test_no_pattern_gets_a_message_with_the_right_form(tmp_path, args):
    done = grep(*args, cwd=tmp_path)
    assert done.returncode == 2
    assert "bez wzorca" in done.stderr and "grep -ril -e" in done.stderr


@pytest.mark.parametrize("args", [("-ril", "-e", "kilometr", "."), ("-ril", "kilometr", "."), ("-rilE", "kilo|xyz", "."),
                                  ("-ril", "--regexp=kilometr", "."), ("-ri", "--", "kilometr", ".")])
def test_patterns_go_to_the_real_grep(tmp_path, args):
    (tmp_path / "a.txt").write_text("Kilometrówka 0,89 zł\n", encoding="utf-8")
    done = grep(*args, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "a.txt" in done.stdout or "Kilometrówka" in done.stdout
