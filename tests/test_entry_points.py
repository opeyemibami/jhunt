"""The installed console scripts exist and are stubs until their week arrives."""

import shutil
import subprocess

import pytest


@pytest.mark.parametrize("script", ["jhunt-worker", "jhunt-api"])
def test_console_script_is_a_not_implemented_stub(script: str) -> None:
    path = shutil.which(script)
    assert path is not None, f"{script} is not installed; run `uv sync`"

    result = subprocess.run([path], capture_output=True, text=True, timeout=30)

    assert result.returncode == 1
    assert result.stderr.strip() == f"{script}: not implemented"
