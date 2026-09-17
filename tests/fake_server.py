from __future__ import annotations

import argparse
import json
from copy import deepcopy
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
TEST_SUBJECT = "00000000-0000-7000-c000-abcdef123456"
REFRESH_OPERATION_ID = "collector-refresh:accepted"

STATIC_ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/live.html": "live.html",
    "/wiki.html": "wiki.html",
    "/auth.js": "auth.js",
    "/api.js": "api.js",
    "/auth/callback": "auth-callback.html",
    "/auth-callback.html": "auth-callback.html",
}

PUBLIC_CONFIG = {
    "region": "ap-northeast-2",
    "user_pool_id": "ap-northeast-2_demo",
    "app_client_id": "demo-client",
    "cognito_domain": (
        "https://demo.auth.ap-northeast-2.amazoncognito.com"
    ),
    "callback_path": "/auth/callback",
}

SESSION = {
    "subject": TEST_SUBJECT,
    "display_label": "Demo SA",
    "can_execute": True,
}

EDITABLE = {
    "files": [
        {
            "key": "shaka-player/lib/net/backoff.js",
            "repo": "shaka-player",
            "path": "lib/net/backoff.js",
            "content": (
                "const retry = {\n"
                "  baseDelay: 1000,\n"
                "};\n"
            ),
            "lines": 3,
            "head": "source-head-001",
        }
    ]
}

PROGRESS = {
    "done": True,
    "wiki_pull_request_id": "91",
    "runtime_events": [
        {"event": "stage_plan"},
        {"event": "converse_started"},
        {"event": "converse_finished"},
        {"event": "validation_passed"},
        {"event": "publish_state_transition"},
    ],
    "payload": {
        "agent_invoked": True,
        "trace": [
            "1단: changed source matched the inverse index",
            "2단: semantic change requires documentation",
        ],
        "diff_sha256": "fake-diff-sha256",
        "scope": ["networking/shaka-networking.md"],
        "runtime": {
            "usage": {
                "turns": 1,
                "input_tokens": 120,
                "output_tokens": 48,
            },
            "skills_loaded": ["api-reference"],
        },
    },
}

WIKI_PULL_REQUEST = {
    "pull_request_id": "91",
    "status": "OPEN",
    "branch": "refs/heads/wikiagent/update-shaka-networking",
    "source_commit": "wiki-source-commit-001",
    "review_complete": True,
    "all_files": [
        "networking/shaka-networking.md",
        "networking/shaka-networking.json",
        "CURSOR.json",
    ],
    "files": [
        {
            "path": "networking/shaka-networking.md",
            "truncated": False,
            "unified": [
                "--- a/networking/shaka-networking.md",
                "+++ b/networking/shaka-networking.md",
                "@@ -1,2 +1,3 @@",
                " # Shaka Networking",
                "+Updated from browser smoke data.",
            ],
        },
        {
            "path": "networking/shaka-networking.json",
            "truncated": False,
            "unified": [
                "--- a/networking/shaka-networking.json",
                "+++ b/networking/shaka-networking.json",
                "@@ -1 +1 @@",
                '-{"status":"unverified"}',
                '+{"status":"current"}',
            ],
        },
        {
            "path": "CURSOR.json",
            "truncated": False,
            "unified": [
                "--- a/CURSOR.json",
                "+++ b/CURSOR.json",
                "@@ -1 +1 @@",
                '-{"shaka-player":"old"}',
                '+{"shaka-player":"source-head-001"}',
            ],
        },
    ],
}

WIKI_REPOSITORY = {
    "kind": "CodeCommit",
    "repo": "apps-knowledge",
    "default_branch": "main",
    "clone_url": "https://example.invalid/v1/repos/apps-knowledge",
    "branch_count": 4,
    "last_modified": "2026-09-13T09:00:00+00:00",
    "tree": [
        {"depth": 0, "kind": "dir", "path": "guides"},
        {"depth": 1, "kind": "dir", "path": "networking"},
        {
            "depth": 2,
            "kind": "file",
            "path": "networking/shaka-networking.md",
        },
        {
            "depth": 2,
            "kind": "file",
            "path": "networking/shaka-networking.json",
        },
    ],
}

WIKI_HISTORY = {
    "commits": [
        {
            "commit": "wiki-commit-001",
            "subject": "Shaka Networking 문서 갱신",
            "author": "Wiki Agent",
            "date": "2026-09-13T09:00:00+00:00",
            "kind": "게시",
            "meta": {
                "source": "shaka-player",
                "source_pull_request": "17",
                "diff_sha256": "fake-diff-sha256",
                "pr_description": "Browser smoke fixture",
            },
        }
    ]
}

WIKI_COMMIT = {
    "commit": "wiki-commit-001",
    "author": "Wiki Agent",
    "date": "2026-09-13T09:00:00+00:00",
    "parent": "wiki-parent-001",
    "message": "Shaka Networking 문서 갱신\n\nsource_pull_request: 17",
    "files": [
        {
            "path": "networking/shaka-networking.md",
            "change": "M",
            "unified": [
                "--- a/networking/shaka-networking.md",
                "+++ b/networking/shaka-networking.md",
                "@@ -1,2 +1,3 @@",
                " # Shaka Networking",
                "+Updated from browser smoke data.",
            ],
            "before": "# Shaka Networking\n",
            "after": (
                "# Shaka Networking\n\n"
                "Updated from browser smoke data.\n"
            ),
        }
    ],
}

FINALIZATION = {
    "wiki_head": "wiki-head-001",
    "wiki_head_author": "Wiki Reviewer",
    "wiki_head_message": "Merge wiki pull request 91",
    "cursor": "source-head-001",
    "docs": [
        {
            "path": "networking/shaka-networking.md",
            "status": "current",
            "stale_after": "2026-12-12",
            "verified_count": 1,
            "last_verified_pr": "91",
        }
    ],
}

SNAPSHOT = {
    "generated_at": "2026-09-13T09:00:00+00:00",
    "region": "ap-northeast-2",
    "runs": [
        {
            "repo": "shaka-player",
            "source": "lib/net/backoff.js",
            "_at": "2026-09-13T08:58:00+00:00",
            "base_sha": "source-base-001",
            "head_sha": "source-head-001",
            "scope": ["networking/shaka-networking.md"],
            "agent_invoked": True,
            "trace": [
                "1단: changed source matched the inverse index",
                "2단: semantic change requires documentation",
            ],
            "diff_sha256": "fake-diff-sha256",
            "pull_request_description": "Browser smoke fixture",
            "cursor_commit": "source-head-001",
            "runtime": {
                "usage": {
                    "turns": 1,
                    "input_tokens": 120,
                    "output_tokens": 48,
                },
                "wiki": {
                    "pull_request_id": "91",
                    "branch": (
                        "refs/heads/wikiagent/update-shaka-networking"
                    ),
                    "files": [
                        "networking/shaka-networking.md",
                        "networking/shaka-networking.json",
                    ],
                },
                "tool_calls": [
                    {
                        "tool": "read_skill",
                        "arg": "api-reference",
                        "chars": 320,
                    }
                ],
                "skills_loaded": ["api-reference"],
            },
            "diffs": {
                "source": [
                    {
                        "path": "lib/net/backoff.js",
                        "unified": [
                            "@@ -1,3 +1,3 @@",
                            " const retry = {",
                            "-  baseDelay: 1000,",
                            "+  baseDelay: 1200,",
                            " };",
                        ],
                        "before": (
                            "const retry = {\n"
                            "  baseDelay: 1000,\n"
                            "};\n"
                        ),
                        "after": (
                            "const retry = {\n"
                            "  baseDelay: 1200,\n"
                            "};\n"
                        ),
                        "before_truncated": False,
                        "after_truncated": False,
                    }
                ],
                "wiki": [
                    {
                        "path": "networking/shaka-networking.md",
                        "unified": [
                            "@@ -1,2 +1,3 @@",
                            " # Shaka Networking",
                            "+Updated from browser smoke data.",
                        ],
                        "before": "# Shaka Networking\n",
                        "after": (
                            "# Shaka Networking\n\n"
                            "Updated from browser smoke data.\n"
                        ),
                        "before_truncated": False,
                        "after_truncated": False,
                    }
                ],
            },
        }
    ],
    "wiki": {
        "total_md": 1,
        "pair_ok": 1,
        "by_status": {
            "current": 1,
            "unverified": 0,
            "stale": 0,
        },
        "by_top": {
            "networking": {
                "total": 1,
                "current": 1,
                "unverified": 0,
                "stale": 0,
            }
        },
        "head": "wiki-head-001",
        "head_author": "Wiki Reviewer",
        "head_message": "Merge wiki pull request 91",
        "has_index_md": True,
        "docs_truncated": False,
        "docs": [
            {
                "path": "networking/shaka-networking.md",
                "type": "api-reference",
                "status": "current",
                "stale_after": "2026-12-12",
                "verified_count": 1,
                "sources": ["shaka-player/lib/net/backoff.js"],
                "has_json_pair": True,
            }
        ],
    },
    "bootstrap": {
        "branches": [
            "refs/heads/wikiagent/bootstrap-shaka-player"
        ],
        "pull_requests": [
            {
                "pull_request_id": "3",
                "repo": "shaka-player",
                "status": "CLOSED",
                "merged": True,
                "merge_option": "SQUASH_MERGE",
            }
        ],
    },
    "skills": ["api-reference"],
}

QUERY_RESPONSES = {
    "/api/config": PUBLIC_CONFIG,
    "/api/session": SESSION,
    "/api/snapshot": SNAPSHOT,
    "/api/editable": EDITABLE,
    "/api/progress": PROGRESS,
    "/api/wiki-pr": WIKI_PULL_REQUEST,
    "/api/wiki-repo": WIKI_REPOSITORY,
    "/api/wiki-history": WIKI_HISTORY,
    "/api/wiki-commit": WIKI_COMMIT,
    "/api/finalization": FINALIZATION,
}

ACTION_RESPONSES = {
    "/api/actions/commit": (
        HTTPStatus.OK,
        {
            "ok": True,
            "operation_id": "source-pr:17",
            "pull_request_id": "17",
        },
    ),
    "/api/actions/approve": (
        HTTPStatus.OK,
        {
            "ok": True,
            "operation_id": "wiki-pr:91",
            "pull_request_id": "91",
        },
    ),
    "/api/actions/refresh": (
        HTTPStatus.ACCEPTED,
        {
            "ok": True,
            "operation_id": REFRESH_OPERATION_ID,
            "accepted": True,
        },
    ),
}


@dataclass(frozen=True)
class RecordedPost:
    sequence: int
    path: str
    headers: dict[str, str]
    body: dict[str, object]


class FakeDemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, FakeDemoRequestHandler)
        self._record_lock = Lock()
        self._record_sequence = 0
        self._recorded_posts: list[RecordedPost] = []

    def record_post(
        self,
        path: str,
        headers: dict[str, str],
        body: dict[str, object],
    ) -> RecordedPost:
        with self._record_lock:
            self._record_sequence += 1
            record = RecordedPost(
                sequence=self._record_sequence,
                path=path,
                headers=deepcopy(headers),
                body=deepcopy(body),
            )
            self._recorded_posts.append(record)
            return deepcopy(record)

    def recorded_posts(self) -> tuple[RecordedPost, ...]:
        with self._record_lock:
            return tuple(deepcopy(self._recorded_posts))


class FakeDemoRequestHandler(BaseHTTPRequestHandler):
    server: FakeDemoServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(
        self,
        status: HTTPStatus,
        body: dict,
    ) -> None:
        payload = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(
            status,
            payload,
            "application/json; charset=utf-8",
        )

    def _send_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
    ) -> None:
        self._send_json(
            status,
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                },
                "request_id": "fake-request-id",
            },
        )

    def _authorized(self) -> bool:
        return (
            self.headers.get("Authorization")
            == "Bearer test-token"
        )

    def _serve_static(self, path: str) -> bool:
        filename = STATIC_ROUTES.get(path)
        if filename is None:
            return False
        content_type = (
            "text/javascript; charset=utf-8"
            if filename.endswith(".js")
            else "text/html; charset=utf-8"
        )
        self._send_bytes(
            HTTPStatus.OK,
            (FRONTEND / filename).read_bytes(),
            content_type,
        )
        return True

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if self._serve_static(path):
            return
        if path not in QUERY_RESPONSES:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                "NOT_FOUND",
                "요청한 경로를 찾을 수 없습니다.",
            )
            return
        if path != "/api/config" and not self._authorized():
            self._send_error(
                HTTPStatus.UNAUTHORIZED,
                "UNAUTHORIZED",
                "인증이 필요합니다.",
            )
            return
        self._send_json(
            HTTPStatus.OK,
            QUERY_RESPONSES[path],
        )

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            content_length = int(
                self.headers.get("Content-Length", "0")
            )
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > 1_000_000:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_REQUEST",
                "요청 형식이 올바르지 않습니다.",
            )
            return
        raw_body = self.rfile.read(content_length)
        normalized_headers = {
            key.lower(): value
            for key, value in self.headers.items()
        }

        if path not in ACTION_RESPONSES:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                "NOT_FOUND",
                "요청한 경로를 찾을 수 없습니다.",
            )
            return
        if not self._authorized():
            self._send_error(
                HTTPStatus.UNAUTHORIZED,
                "UNAUTHORIZED",
                "인증이 필요합니다.",
            )
            return
        content_type = normalized_headers.get("content-type", "")
        if (
            not content_type.lower().startswith("application/json")
            or normalized_headers.get("x-demo-request") != "1"
        ):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_REQUEST",
                "요청 형식이 올바르지 않습니다.",
            )
            return
        try:
            body = (
                {}
                if path == "/api/actions/refresh" and not raw_body
                else json.loads(raw_body.decode("utf-8"))
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_REQUEST",
                "요청 형식이 올바르지 않습니다.",
            )
            return
        if not isinstance(body, dict) or not self._valid_action_body(
            path,
            body,
        ):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_REQUEST",
                "요청 형식이 올바르지 않습니다.",
            )
            return

        self.server.record_post(path, normalized_headers, body)
        status, response = ACTION_RESPONSES[path]
        self._send_json(status, response)

    @staticmethod
    def _valid_action_body(
        path: str,
        body: dict[str, object],
    ) -> bool:
        if path == "/api/actions/refresh":
            return body == {}
        required = (
            (
                "key",
                "content",
                "title",
                "description",
                "expected_head",
            )
            if path == "/api/actions/commit"
            else ("wiki_pr", "expected_source_commit")
        )
        return all(
            isinstance(body.get(field), str)
            and bool(body[field])
            for field in required
        )


def create_fake_server(
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    return FakeDemoServer((host, port))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Serve the Demo UI with deterministic AWS-free APIs.",
    )
    parser.add_argument(
        "port",
        type=int,
        nargs="?",
        default=8899,
    )
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = create_fake_server(args.host, args.port)
    host, port = server.server_address[:2]
    print(f"fake demo server listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
