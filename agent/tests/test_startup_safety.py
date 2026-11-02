from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("entrypoint,code", [
    ("import app.main", "AGENT_STARTUP_FAILED"),
    ("from app.worker import main; main()", "AGENT_WORKER_FAILED"),
])
def test_invalid_settings_never_print_environment_values(entrypoint, code):
    sentinel = "startup-secret-must-not-leak"
    env = {**os.environ, "AGENT_MAX_NODES": sentinel, "MODEL_API_KEY": sentinel,
           "AGENT_MYSQL_DSN": "mysql://user:" + sentinel + "@localhost/test"}
    result = subprocess.run([sys.executable, "-c", entrypoint], env=env,
                            capture_output=True, text=True, timeout=20)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert code in output
    assert sentinel not in output
