from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest
from fake_server import create_fake_server
from playwright.sync_api import BrowserContext, Page, expect, sync_playwright

TEST_SUBJECT = "00000000-0000-7000-c000-abcdef123456"
TEST_SUBJECT_LABEL = (
    f"{TEST_SUBJECT[:8]}…{TEST_SUBJECT[-4:]}"
)
TEST_AUTH_SCRIPT = (
    "window.__DEMO_TEST_AUTH__="
    f"{{accessToken:'test-token',subject:"
    f"{json.dumps(TEST_SUBJECT)}}}"
)
DEMO_UI = Path(__file__).resolve().parents[1]
FIXED_CURRENT_CONTENT = (
    "const retry = {\n"
    "  baseDelay: 1000,\n"
    "};\n"
)
FIXED_NEXT_CONTENT = (
    "const retry = {\n"
    "  baseDelay: 1200,\n"
    "};\n"
)
FIXED_TITLE = "demo: toggle Shaka retry base delay"
FIXED_DESCRIPTION = (
    "Fixed Pipeline Control Maintenance scenario"
)


@dataclass
class ObservedPage:
    page: Page
    console_errors: list[str]
    page_errors: list[str]
    requested_urls: list[str]
    unexpected_urls: list[str]
    request_failures: list[str]
    http_errors: list[str]
    cognito_authorize_urls: list[str]


@pytest.fixture
def fake_server():
    server = create_fake_server()
    thread = Thread(
        target=server.serve_forever,
        name="demo-ui-fake-server",
        daemon=True,
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def fake_server_url(fake_server) -> str:
    host, port = fake_server.server_address[:2]
    return f"http://{host}:{port}"


@pytest.fixture
def browser_context() -> Iterator[BrowserContext]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        try:
            yield context
        finally:
            context.close()
            browser.close()


@contextmanager
def observed_page(
    context: BrowserContext,
    *,
    authenticated: bool,
    loopback_origin: str,
    cognito_login_html: str | None = None,
) -> Iterator[ObservedPage]:
    page = context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    requested_urls: list[str] = []
    unexpected_urls: list[str] = []
    request_failures: list[str] = []
    http_errors: list[str] = []
    cognito_authorize_urls: list[str] = []
    allowed_origin = urlsplit(loopback_origin)

    def guard_network(route) -> None:
        url = route.request.url
        target = urlsplit(url)
        if (
            target.scheme == allowed_origin.scheme
            and target.netloc == allowed_origin.netloc
        ):
            route.continue_()
            return
        if (
            cognito_login_html is not None
            and target.scheme == "https"
            and target.hostname
            == "demo.auth.ap-northeast-2.amazoncognito.com"
            and target.path == "/oauth2/authorize"
        ):
            cognito_authorize_urls.append(url)
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
                body=cognito_login_html,
            )
            return
        unexpected_urls.append(url)
        route.abort("blockedbyclient")

    page.on(
        "console",
        lambda message: (
            console_errors.append(message.text)
            if message.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("request", lambda request: requested_urls.append(request.url))
    page.on(
        "requestfailed",
        lambda request: request_failures.append(
            f"{request.url}: {request.failure or 'request failed'}"
        ),
    )
    page.on(
        "response",
        lambda response: (
            http_errors.append(f"{response.status} {response.url}")
            if response.status >= 400
            else None
        ),
    )
    page.route("**/*", guard_network)
    if authenticated:
        page.add_init_script(TEST_AUTH_SCRIPT)
    try:
        yield ObservedPage(
            page=page,
            console_errors=console_errors,
            page_errors=page_errors,
            requested_urls=requested_urls,
            unexpected_urls=unexpected_urls,
            request_failures=request_failures,
            http_errors=http_errors,
            cognito_authorize_urls=cognito_authorize_urls,
        )
    finally:
        page.close()


def assert_browser_clean(observed: ObservedPage) -> None:
    assert observed.console_errors == []
    assert observed.page_errors == []
    assert observed.unexpected_urls == []
    assert observed.request_failures == []
    assert observed.http_errors == []


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
) -> tuple[int, dict[str, str], dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        base_url + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
            "X-Demo-Request": "1",
        },
    )
    with urlopen(request, timeout=5) as response:
        return (
            response.status,
            {key.lower(): value for key, value in response.headers.items()},
            json.load(response),
        )


def request_raw(
    base_url: str,
    path: str,
    *,
    body: bytes,
    headers: dict[str, str],
) -> tuple[int, dict]:
    request = Request(
        base_url + path,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    try:
        return response.status, json.load(response)
    finally:
        response.close()


def test_fake_server_exposes_every_task_6_route(fake_server, fake_server_url):
    get_routes = (
        "/api/config",
        "/api/session",
        "/api/snapshot",
        "/api/editable",
        "/api/progress?pr=17&since=0",
        "/api/wiki-pr?pr=91",
        "/api/wiki-repo",
        "/api/wiki-history?limit=40",
        "/api/wiki-commit?id=wiki-commit-001",
        (
            "/api/finalization?repo=shaka-player&"
            "paths=networking%2Fshaka-networking.md"
        ),
    )
    for path in get_routes:
        status, headers, body = request_json(fake_server_url, path)
        assert status == 200, path
        assert headers["cache-control"] == "no-store"
        assert headers["content-type"].startswith("application/json")
        assert isinstance(body, dict)

    post_routes = (
        (
            "/api/actions/commit",
            {
                "key": "shaka-player/lib/net/backoff.js",
                "content": FIXED_NEXT_CONTENT,
                "title": FIXED_TITLE,
                "description": FIXED_DESCRIPTION,
                "expected_head": "source-head-001",
            },
            200,
        ),
        (
            "/api/actions/approve",
            {
                "wiki_pr": "91",
                "expected_source_commit": "wiki-source-commit-001",
            },
            200,
        ),
        ("/api/actions/refresh", {}, 202),
    )
    for path, request_body, expected_status in post_routes:
        status, headers, body = request_json(
            fake_server_url,
            path,
            method="POST",
            body=request_body,
        )
        assert status == expected_status
        assert headers["cache-control"] == "no-store"
        assert body["ok"] is True

    recorded = fake_server.recorded_posts()
    assert [request.sequence for request in recorded] == [1, 2, 3]
    assert [request.path for request in recorded] == [
        route[0] for route in post_routes
    ]
    assert [request.body for request in recorded] == [
        route[1] for route in post_routes
    ]


def test_fake_server_records_only_successful_valid_actions(
    fake_server,
    fake_server_url,
):
    valid_headers = {
        "Accept": "application/json",
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
        "X-Demo-Request": "1",
    }
    invalid_requests = (
        (
            "/api/actions/unknown",
            b"{}",
            valid_headers,
            404,
        ),
        (
            "/api/actions/commit",
            b"{}",
            {
                key: value
                for key, value in valid_headers.items()
                if key != "Authorization"
            },
            401,
        ),
        (
            "/api/actions/commit",
            b"{}",
            {
                key: value
                for key, value in valid_headers.items()
                if key != "X-Demo-Request"
            },
            400,
        ),
        (
            "/api/actions/commit",
            b"{not-json",
            valid_headers,
            400,
        ),
    )
    for path, body, headers, expected_status in invalid_requests:
        status, response = request_raw(
            fake_server_url,
            path,
            body=body,
            headers=headers,
        )
        assert status == expected_status
        assert response["ok"] is False
        assert fake_server.recorded_posts() == ()

    valid_body = {
        "key": "shaka-player/lib/net/backoff.js",
        "content": FIXED_NEXT_CONTENT,
        "title": FIXED_TITLE,
        "description": FIXED_DESCRIPTION,
        "expected_head": "source-head-001",
    }
    status, response = request_raw(
        fake_server_url,
        "/api/actions/commit",
        body=json.dumps(valid_body).encode("utf-8"),
        headers=valid_headers,
    )
    assert status == 200
    assert response["ok"] is True
    recorded = fake_server.recorded_posts()
    assert len(recorded) == 1
    assert recorded[0].body == valid_body


def test_fake_server_returns_defensive_record_copies(
    fake_server,
    fake_server_url,
):
    request_body = {
        "wiki_pr": "91",
        "expected_source_commit": "wiki-source-commit-001",
    }
    status, _, response = request_json(
        fake_server_url,
        "/api/actions/approve",
        method="POST",
        body=request_body,
    )
    assert status == 200
    assert response["ok"] is True

    exposed = fake_server.recorded_posts()
    exposed[0].headers["authorization"] = "Bearer mutated"
    exposed[0].body["expected_source_commit"] = "mutated"

    fresh = fake_server.recorded_posts()
    assert fresh[0].headers["authorization"] == "Bearer test-token"
    assert fresh[0].body == request_body


def test_unauthenticated_page_uses_intercepted_cognito_login(
    browser_context,
    fake_server,
    fake_server_url,
):
    login_html = """<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"><title>Local login</title></head>
<body>
  <main>
    <h1>로컬 결정론 로그인</h1>
    <p>실제 Cognito 또는 internet 요청 없이 표시했습니다.</p>
  </main>
</body>
</html>
"""

    with observed_page(
        browser_context,
        authenticated=False,
        loopback_origin=fake_server_url,
        cognito_login_html=login_html,
    ) as observed:
        page = observed.page
        page.goto(
            fake_server_url + "/live.html",
            wait_until="domcontentloaded",
        )
        page.wait_for_load_state("networkidle")

        expect(
            page.get_by_role(
                "heading",
                name="로컬 결정론 로그인",
                exact=True,
            )
        ).to_be_visible()
        assert len(observed.cognito_authorize_urls) == 1
        target = urlsplit(observed.cognito_authorize_urls[0])
        assert target.path == "/oauth2/authorize"
        assert target.hostname == (
            "demo.auth.ap-northeast-2.amazoncognito.com"
        )
        assert fake_server.recorded_posts() == ()
        assert_browser_clean(observed)


def test_network_guard_aborts_non_fake_loopback_origin(
    browser_context,
    fake_server_url,
):
    unexpected_url = "http://127.0.0.1:9/unexpected"
    with observed_page(
        browser_context,
        authenticated=True,
        loopback_origin=fake_server_url,
    ) as observed:
        page = observed.page
        page.goto(fake_server_url + "/")
        page.wait_for_load_state("networkidle")

        result = page.evaluate(
            """async url => {
              try {
                await fetch(url);
                return "resolved";
              } catch (error) {
                return "rejected";
              }
            }""",
            unexpected_url,
        )

        assert result == "rejected"
        assert observed.unexpected_urls == [unexpected_url]
        assert len(observed.request_failures) == 1
        assert unexpected_url in observed.request_failures[0]
        assert observed.http_errors == []
        assert len(observed.console_errors) == 1
        assert "ERR_BLOCKED_BY_CLIENT" in observed.console_errors[0]
        assert observed.page_errors == []


@pytest.mark.parametrize("path", ("/", "/live.html", "/wiki.html"))
def test_authenticated_page_visibility_contract(
    browser_context,
    fake_server,
    fake_server_url,
    path,
):
    with observed_page(
        browser_context,
        authenticated=True,
        loopback_origin=fake_server_url,
    ) as observed:
        page = observed.page
        page.goto(fake_server_url + path)
        page.wait_for_load_state("networkidle")

        auth_gate = page.locator("#authGate")
        app_shell = page.locator("#appShell")
        expect(auth_gate).to_have_js_property("hidden", True)
        expect(auth_gate).to_be_hidden()
        expect(app_shell).to_have_js_property("hidden", False)
        expect(app_shell).to_be_visible()

        assert_browser_clean(observed)


def test_authenticated_dashboard_and_wiki_render_fake_data(
    browser_context,
    fake_server,
    fake_server_url,
):
    with observed_page(
        browser_context,
        authenticated=True,
        loopback_origin=fake_server_url,
    ) as observed:
        page = observed.page
        page.goto(fake_server_url + "/")
        page.wait_for_load_state("networkidle")

        expect(
            page.get_by_role(
                "heading",
                name="조직 LLM Wiki Pipeline Control",
                exact=True,
            )
        ).to_be_visible()
        expect(page.locator("#authSubject")).to_have_text(
            TEST_SUBJECT_LABEL
        )
        expect(page.locator("#runs")).to_contain_text("shaka-player")
        expect(page.locator("#detail")).to_contain_text(
            "networking/shaka-networking.md"
        )

        page.goto(fake_server_url + "/wiki.html")
        page.wait_for_load_state("networkidle")

        expect(
            page.get_by_role(
                "heading",
                name="apps-knowledge 저장 위치와 커밋 이력",
                exact=True,
            )
        ).to_be_visible()
        expect(page.locator("#authSubject")).to_have_text(
            TEST_SUBJECT_LABEL
        )
        expect(page.locator("#repokv")).to_contain_text("apps-knowledge")
        expect(page.locator("#clist")).to_contain_text(
            "Shaka Networking 문서 갱신"
        )
        expect(page.locator("#cdetail")).to_contain_text(
            "networking/shaka-networking.md"
        )

        assert fake_server.recorded_posts() == ()
        assert_browser_clean(observed)


def test_live_confirmations_and_action_contracts(
    browser_context,
    fake_server,
    fake_server_url,
):
    with observed_page(
        browser_context,
        authenticated=True,
        loopback_origin=fake_server_url,
    ) as observed:
        page = observed.page
        page.goto(fake_server_url + "/live.html")
        page.wait_for_load_state("networkidle")

        expect(
            page.get_by_role(
                "heading",
                name="Pipeline Control Console",
                exact=True,
            )
        ).to_be_visible()
        expect(page.locator("#authSubject")).to_have_text(
            TEST_SUBJECT_LABEL
        )
        commit_button = page.locator("#btnCommit")
        approve_button = page.locator("#btnApprove")
        expect(commit_button).to_have_text(
            "고정 Source 변경 실행"
        )
        expect(commit_button).to_be_enabled()
        expect(approve_button).to_have_text("wiki 승인 및 merge")
        expect(page.locator("textarea")).to_have_count(0)
        expect(page.locator("select")).to_have_count(0)
        expect(page.locator('input[type="text"]')).to_have_count(0)
        expect(page.locator("#currentSource")).to_have_text(
            FIXED_CURRENT_CONTENT
        )
        expect(page.locator("#nextSource")).to_have_text(
            FIXED_NEXT_CONTENT
        )
        expect(page.locator("[data-control-step]")).to_have_count(4)

        page.once("dialog", lambda dialog: dialog.dismiss())
        commit_button.click()
        assert fake_server.recorded_posts() == ()

        page.once("dialog", lambda dialog: dialog.accept())
        commit_button.click()
        expect(page.locator("#opStatus")).to_have_text(
            "wiki 검토 대기",
            timeout=5_000,
        )
        expect(approve_button).to_be_enabled()
        expect(page.locator("#diffs")).to_contain_text(
            "networking/shaka-networking.md"
        )
        expect(page.locator("#diffs")).to_contain_text(
            "networking/shaka-networking.json"
        )
        expect(page.locator("#diffs")).to_contain_text(
            "CURSOR.json"
        )

        recorded = fake_server.recorded_posts()
        assert len(recorded) == 1
        source_request = recorded[0]
        assert source_request.sequence == 1
        assert source_request.path == "/api/actions/commit"
        assert source_request.headers["authorization"] == (
            "Bearer test-token"
        )
        assert source_request.headers["x-demo-request"] == "1"
        assert source_request.headers["content-type"].startswith(
            "application/json"
        )
        assert source_request.body == {
            "key": "shaka-player/lib/net/backoff.js",
            "content": FIXED_NEXT_CONTENT,
            "title": FIXED_TITLE,
            "description": FIXED_DESCRIPTION,
            "expected_head": "source-head-001",
        }

        page.once("dialog", lambda dialog: dialog.dismiss())
        approve_button.click()
        assert len(fake_server.recorded_posts()) == 1

        page.once("dialog", lambda dialog: dialog.accept())
        approve_button.click()
        expect(page.locator("#opStatus")).to_have_text(
            "finalization 대기",
            timeout=5_000,
        )

        recorded = fake_server.recorded_posts()
        assert len(recorded) == 2
        wiki_request = recorded[1]
        assert wiki_request.sequence == 2
        assert wiki_request.path == "/api/actions/approve"
        assert wiki_request.headers["authorization"] == (
            "Bearer test-token"
        )
        assert wiki_request.headers["x-demo-request"] == "1"
        assert wiki_request.headers["content-type"].startswith(
            "application/json"
        )
        assert wiki_request.body == {
            "wiki_pr": "91",
            "expected_source_commit": "wiki-source-commit-001",
        }

        page.evaluate("waitFinal(0)")
        expect(page.locator("#finPanel")).to_be_visible()
        expect(page.locator("#opStatus")).to_have_text(
            "freshness 확정 완료"
        )
        expect(page.locator("#finkv")).to_contain_text(
            "source-head-001"
        )
        expect(page.locator("#fintab")).to_contain_text(
            "networking/shaka-networking.md"
        )
        expect(page.locator("#fintab")).to_contain_text("current")
        expect(page.locator("#finnote")).to_contain_text(
            "verified history"
        )
        finalization_urls = [
            url
            for url in observed.requested_urls
            if urlsplit(url).path == "/api/finalization"
        ]
        assert len(finalization_urls) == 1

        assert_browser_clean(observed)


def load_selftest_module():
    path = DEMO_UI / "selftest.py"
    spec = importlib.util.spec_from_file_location(
        "demo_ui_selftest",
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selftest_runs_local_suites_then_diff_check(monkeypatch):
    module = load_selftest_module()
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def run(
        command,
        *,
        cwd,
        env,
        check,
        capture_output=False,
        text=False,
    ):
        calls.append((list(command), Path(cwd), dict(env)))
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="true\n",
                stderr="",
            )
        if command[:4] == [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
        ]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="demo-ui/new-test.py\n",
                stderr="",
            )
        if command[:4] == [
            "git",
            "diff",
            "--no-index",
            "--check",
        ]:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="" if capture_output else None,
            stderr="" if capture_output else None,
        )

    monkeypatch.setattr(module.subprocess, "run", run)

    assert module.main() == 0
    tests_dir = DEMO_UI / "tests"
    smoke = tests_dir / "test_browser_smoke.py"
    assert [call[0] for call in calls] == [
        [
            sys.executable,
            "-m",
            "pytest",
            str(tests_dir),
            f"--ignore={smoke}",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        [
            sys.executable,
            "-m",
            "pytest",
            str(smoke),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        ["git", "rev-parse", "--is-inside-work-tree"],
        ["git", "diff", "--check"],
        ["git", "diff", "--cached", "--check"],
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            ".",
        ],
        [
            "git",
            "diff",
            "--no-index",
            "--check",
            "--",
            "/dev/null",
            "demo-ui/new-test.py",
        ],
    ]
    assert all(call[1] == DEMO_UI for call in calls)
    assert all(
        call[2]["PYTHONDONTWRITEBYTECODE"] == "1"
        for call in calls
    )


def test_selftest_returns_first_nonzero_without_later_commands(
    monkeypatch,
):
    module = load_selftest_module()
    # pytest 2건 · git 저장소 판정 · working tree · index 순서로 부르고 4번째에서 멈춘다.
    return_codes = iter((0, 0, 0, 0, 7))
    calls: list[list[str]] = []

    def run(
        command,
        *,
        cwd,
        env,
        check,
        capture_output=False,
        text=False,
    ):
        calls.append(list(command))
        code = next(return_codes)
        stdout = "" if capture_output else None
        if command[:2] == ["git", "rev-parse"]:
            stdout = "true\n"
        return subprocess.CompletedProcess(
            command,
            code,
            stdout=stdout,
            stderr="" if capture_output else None,
        )

    monkeypatch.setattr(module.subprocess, "run", run)

    tests_dir = DEMO_UI / "tests"
    smoke = tests_dir / "test_browser_smoke.py"
    assert module.main() == 7
    assert calls == [
        [
            sys.executable,
            "-m",
            "pytest",
            str(tests_dir),
            f"--ignore={smoke}",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        [
            sys.executable,
            "-m",
            "pytest",
            str(smoke),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        ["git", "rev-parse", "--is-inside-work-tree"],
        ["git", "diff", "--check"],
        ["git", "diff", "--cached", "--check"],
    ]


def initialize_git_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Task 8 Test"],
        cwd=path,
        check=True,
    )
    demo_ui = path / "demo-ui"
    demo_ui.mkdir()
    tracked = demo_ui / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "demo-ui/tracked.txt"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"],
        cwd=path,
        check=True,
    )


def test_selftest_rejects_untracked_demo_ui_trailing_whitespace(
    tmp_path,
):
    module = load_selftest_module()
    repository = tmp_path / "repository"
    initialize_git_repository(repository)
    (repository / "demo-ui" / "untracked.py").write_text(
        "value = 1  \n",
        encoding="utf-8",
    )

    assert module.check_git_whitespace(
        repository,
        os.environ.copy(),
    ) != 0


def test_selftest_rejects_staged_only_trailing_whitespace(tmp_path):
    module = load_selftest_module()
    repository = tmp_path / "repository"
    initialize_git_repository(repository)
    tracked = repository / "demo-ui" / "tracked.txt"
    tracked.write_text("staged trailing whitespace  \n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "demo-ui/tracked.txt"],
        cwd=repository,
        check=True,
    )
    tracked.write_text("clean\n", encoding="utf-8")

    working_tree = subprocess.run(
        ["git", "diff", "--check"],
        cwd=repository,
        check=False,
    )
    assert working_tree.returncode == 0
    assert module.check_git_whitespace(
        repository,
        os.environ.copy(),
    ) != 0
