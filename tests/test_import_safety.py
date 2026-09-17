import os
import subprocess
import sys
from pathlib import Path

import pytest

# ⚠️ 저장소 배치를 전제하지 않는다. 단독 번들에서도 이 파일 위치가 기준이다.
CONSOLE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONSOLE_ROOT))

from backend import clients as clients_module  # noqa: E402
from backend.settings import Settings  # noqa: E402

MODULES = (
    "config",
    "live",
    "collect",
    "server",
    "verify",
    "backend.clients",
)


def _audit_code(
    module_name: str,
    import_paths: tuple[str, ...],
) -> str:
    return f"""
import argparse
import base64
import boto3
import builtins
import concurrent.futures
import contextlib
import dataclasses
import datetime
import difflib
import functools
import http.server
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time
import urllib.request

sys.path[:0] = {list(import_paths)!r}

recorded_calls = []

class ImportSideEffect(BaseException):
    pass

def record(name):
    def fail(*args, **kwargs):
        recorded_calls.append(name)
        raise ImportSideEffect(name)
    return fail

boto3.Session = record("boto3.Session")
boto3.client = record("boto3.client")
urllib.request.urlopen = record("urllib.request.urlopen")
subprocess.run = record("subprocess.run")
subprocess.Popen = record("subprocess.Popen")
os.system = record("os.system")
argparse.ArgumentParser.parse_args = record(
    "argparse.ArgumentParser.parse_args"
)
argparse.ArgumentParser.parse_known_args = record(
    "argparse.ArgumentParser.parse_known_args"
)
sys.exit = record("sys.exit")
builtins.open = record("builtins.open")
pathlib.Path.open = record("Path.open")
pathlib.Path.read_text = record("Path.read_text")
pathlib.Path.read_bytes = record("Path.read_bytes")
pathlib.Path.write_text = record("Path.write_text")
pathlib.Path.write_bytes = record("Path.write_bytes")
pathlib.Path.touch = record("Path.touch")
pathlib.Path.exists = record("Path.exists")

try:
    __import__({module_name!r})
except ImportSideEffect:
    pass
finally:
    if recorded_calls:
        unique_calls = list(dict.fromkeys(recorded_calls))
        raise AssertionError(
            "import side effects recorded: " + ", ".join(unique_calls)
        )
"""


def _run_import_audit(
    module_name: str,
    *extra_import_paths: Path,
) -> subprocess.CompletedProcess[str]:
    import_paths = tuple(
        str(path)
        for path in (
            *extra_import_paths,
            CONSOLE_ROOT,
        )
    )
    code = _audit_code(module_name, import_paths)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=CONSOLE_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": os.pathsep.join(import_paths),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    return result


@pytest.mark.parametrize("module_name", MODULES)
def test_import_has_no_aws_network_or_cli_side_effect(module_name):
    result = _run_import_audit(module_name)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_import_audit_canary_detects_file_apis_and_swallowed_session(
    tmp_path,
):
    module_name = "import_safety_canary"
    (tmp_path / "canary-target.txt").write_text(
        "canary",
        encoding="utf-8",
    )
    (tmp_path / f"{module_name}.py").write_text(
        """
import boto3
from pathlib import Path

target = Path(__file__).with_name("canary-target.txt")

def swallow(operation):
    try:
        operation()
    except BaseException:
        pass

for mode in (
    "r", "rb", "rt", "r+", "r+b",
    "w", "wb", "wt", "w+", "w+b",
    "a", "ab", "at", "a+", "a+b",
    "x", "xb", "xt", "x+", "x+b",
):
    swallow(lambda mode=mode: open(target, mode))
    swallow(lambda mode=mode: target.open(mode))

swallow(lambda: target.read_text(encoding="utf-8"))
swallow(target.read_bytes)
swallow(lambda: target.write_text("changed", encoding="utf-8"))
swallow(lambda: target.write_bytes(b"changed"))
swallow(target.touch)
swallow(target.exists)
swallow(boto3.Session)
""",
        encoding="utf-8",
    )
    result = _run_import_audit(module_name, tmp_path)
    assert result.returncode != 0, (
        "import audit falsely accepted recorded file APIs and a "
        "swallowed boto3.Session call"
    )
    assert result.stdout == ""
    for expected_call in (
        "builtins.open",
        "Path.open",
        "Path.read_text",
        "Path.read_bytes",
        "Path.write_text",
        "Path.write_bytes",
        "Path.touch",
        "Path.exists",
        "boto3.Session",
    ):
        assert expected_call in result.stderr


def test_create_clients_uses_region_only_and_maps_all_clients(
    monkeypatch,
):
    settings = Settings(
        region="test-region-1",
        wiki_repo="wiki",
        repo_prefix="repo-",
        lambda_log_group="lambda-log",
        runtime_log_group="runtime-log",
        skills_bucket="skills",
        snapshot_bucket="snapshots",
        collector_function_name="collector",
        redaction_secret_arn="secret",
        user_pool_id="pool",
        app_client_id="client",
        cognito_domain="domain",
        callback_path="/callback",
    )
    created = {
        name: object()
        for name in (
            "codecommit",
            "logs",
            "s3",
            "lambda",
            "secretsmanager",
        )
    }
    session_calls = []
    client_calls = []

    class FakeSession:
        def __init__(self, *args, **kwargs):
            session_calls.append((args, kwargs))

        def client(self, name):
            client_calls.append(name)
            return created[name]

    monkeypatch.setattr(
        clients_module.boto3,
        "Session",
        FakeSession,
    )

    clients = clients_module.create_clients(settings)

    assert session_calls == [
        ((), {"region_name": settings.region})
    ]
    assert client_calls == [
        "codecommit",
        "logs",
        "s3",
        "lambda",
        "secretsmanager",
    ]
    assert clients == clients_module.AwsClients(
        codecommit=created["codecommit"],
        logs=created["logs"],
        s3=created["s3"],
        lambda_client=created["lambda"],
        secretsmanager=created["secretsmanager"],
    )
