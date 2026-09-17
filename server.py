"""데모 UI 로컬 서버. 인증 frontend + 로컬 PoC 실행 API.

⚠️ **127.0.0.1 에만 바인딩한다.** Local test auth는 Browser에서 주입되며 이 server
자체는 JWT를 검증하지 않는다. 쓰기 모드의 action endpoint는 CodeCommit PR을 만들고
merge하므로 외부에 노출하지 않는다.

쓰기 대상은 `backend.action_service.EDITABLE` 로 좁혀 두었다. 그 목록 밖의 경로는
거부한다. ⚠️ 예전에는 `live.EDITABLE` 재노출을 거쳐 읽었는데, 그 재노출은 `live.py`
안에서 쓰이지 않아 lint 가 미사용 import 로 지운다. allowlist 를 소유한 모듈에서 직접
읽는다.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from collections.abc import Sequence
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import live
from backend.action_service import EDITABLE

HERE = Path(__file__).parent
FRONTEND = HERE / "frontend"
SNAPSHOT = HERE / "snapshot.json"
HOST = "127.0.0.1"
LOCAL_SUBJECT = "00000000-0000-4000-8000-000000000001"
LOCAL_PUBLIC_CONFIG = {
    "region": "ap-northeast-2",
    "user_pool_id": "ap-northeast-2_localdemo",
    "app_client_id": "localdemoclient",
    "cognito_domain": (
        "https://local-demo.auth.ap-northeast-2."
        "amazoncognito.com"
    ),
    "callback_path": "/auth/callback",
}


# ⚠️ **로컬 무인증 모드에서 심는 shim.** frontend 의 `DemoAuth.bootstrap()` 은
# `window.__DEMO_TEST_AUTH__` 가 있으면 Cognito 왕복을 건너뛴다. 그 값은 **문서 로드
# 시점**에 있어야 하므로 브라우저 콘솔로는 늦다. 그래서 서버가 HTML 응답에 직접 넣는다.
#
# 이 값은 AWS 권한을 주지 않는다. 권한은 서버 프로세스의 AWS 자격증명에서만 온다.
# 로컬 서버는 원래부터 JWT 를 검증하지 않는다(모듈 docstring). 따라서 이 모드는 권한
# 경계를 바꾸지 않고 **화면 진입 게이트만** 없앤다. `127.0.0.1` 밖으로 내보내지 않는다.
_NO_AUTH_SHIM = (
    b"<script>window.__DEMO_TEST_AUTH__="
    b"{accessToken:'local-no-auth',subject:'local-demo-user'};</script>"
)


class Handler(SimpleHTTPRequestHandler):
    read_only = False
    no_auth = False

    def _no_auth_html_target(self) -> Path | None:
        """`--no-auth` 에서 shim 을 넣을 HTML 파일. 아니면 `None`.

        ⚠️ **요청 경로의 확장자로 판정하지 않는다.** `/` 는 `.html` 로 끝나지 않지만
        `index.html` 을 돌려준다. 확장자만 보면 대시보드에 shim 이 들어가지 않고, 그
        화면만 Cognito 로 튄다(실측 2026-09-17). 디렉터리 요청을 index 파일로 해석한
        뒤에 판정한다.
        """
        if not self.no_auth:
            return None
        target = Path(self.translate_path(self.path))
        if target.is_dir():
            for name in ("index.html", "index.htm"):
                candidate = target / name
                if candidate.is_file():
                    target = candidate
                    break
            else:
                return None
        if target.suffix.lower() not in (".html", ".htm") or not target.is_file():
            return None
        return target

    def send_head(self):  # noqa: ANN201  (표준 라이브러리 시그니처를 따른다)
        """`--no-auth` 일 때 HTML 응답 앞에 세션 shim 을 넣는다.

        `SimpleHTTPRequestHandler` 는 헤더를 먼저 쓰고 본문을 파일 객체로 돌려준다.
        본문 길이가 바뀌므로 `Content-Length` 를 다시 계산해야 하고, 그래서 헤더를
        직접 쓴다. 삽입 대상은 `<head>` 직후이며 없으면 문서 앞에 붙인다.
        """
        target = self._no_auth_html_target()
        if target is None:
            return super().send_head()
        try:
            raw = target.read_bytes()
        except OSError:
            return super().send_head()
        marker = b"<head>"
        index = raw.find(marker)
        body = (
            raw[: index + len(marker)] + _NO_AUTH_SHIM + raw[index + len(marker):]
            if index >= 0
            else _NO_AUTH_SHIM + raw
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return io.BytesIO(body)

    # -- 공통 ---------------------------------------------------------------
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 1_000_000:
            raise ValueError("요청 body 크기가 올바르지 않다")
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("요청 body는 JSON object여야 한다")
        return body

    def _require_action_headers(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if (
            content_type.split(";", 1)[0].strip().lower()
            != "application/json"
            or self.headers.get("X-Demo-Request") != "1"
        ):
            raise ValueError("필수 action header가 없다")

    def _guard(self, fn):
        try:
            self._json(fn())
        except ValueError as e:
            self._json(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": str(e),
                    },
                },
                400,
            )
        except Exception:
            self._json(
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "요청을 처리하지 못했습니다.",
                    },
                },
                500,
            )

    @staticmethod
    def _pull_request_result(
        result: dict,
        operation_prefix: str,
    ) -> dict:
        pull_request_id = result.get("pull_request_id")
        if (
            not isinstance(pull_request_id, str)
            or not pull_request_id.isdigit()
        ):
            raise RuntimeError("invalid local action result")
        return {
            "ok": True,
            "operation_id": (
                f"{operation_prefix}:{pull_request_id}"
            ),
            "pull_request_id": pull_request_id,
        }

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/auth/callback":
            self.path = "/auth-callback.html"
            return super().do_GET()
        if u.path == "/api/config":
            return self._json(dict(LOCAL_PUBLIC_CONFIG))
        if u.path == "/api/session":
            return self._json(
                {
                    "subject": LOCAL_SUBJECT,
                    "display_label": "Local demo user",
                    "can_execute": not self.read_only,
                }
            )
        if u.path == "/api/snapshot":
            return self._guard(
                lambda: json.loads(
                    SNAPSHOT.read_text(encoding="utf-8")
                )
            )
        if u.path == "/api/editable":
            return self._guard(live.list_editable)
        if u.path == "/api/progress":
            pr = (q.get("pr") or [""])[0]
            since = q.get("since")
            return self._guard(lambda: live.progress(
                pr, int(since[0]) if since else None))
        if u.path == "/api/wiki-pr":
            return self._guard(lambda: live.wiki_pr_detail((q.get("pr") or [""])[0]))
        if u.path == "/api/wiki-repo":
            return self._guard(live.wiki_repo_info)
        if u.path == "/api/wiki-history":
            n = int((q.get("limit") or ["40"])[0])
            return self._guard(lambda: live.wiki_history(n))
        if u.path == "/api/wiki-commit":
            return self._guard(lambda: live.wiki_commit((q.get("id") or [""])[0]))
        if u.path == "/api/finalization":
            repo = (q.get("repo") or [""])[0]
            paths = [p for p in (q.get("paths") or [""])[0].split(",") if p]
            return self._guard(lambda: live.finalization(repo, paths))
        super().do_GET()

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path == "/api/actions/refresh":
            try:
                self._require_action_headers()
                if self._body():
                    raise ValueError(
                        "refresh body는 비어 있어야 한다"
                    )
            except ValueError as error:
                return self._json(
                    {
                        "ok": False,
                        "error": {
                            "code": "INVALID_REQUEST",
                            "message": str(error),
                        },
                    },
                    400,
                )
            r = subprocess.run([sys.executable, str(HERE / "collect.py")],
                               capture_output=True, text=True, cwd=HERE)
            ok = r.returncode == 0
            if ok:
                return self._json(
                    {
                        "ok": True,
                        "operation_id": (
                            f"collector-refresh:{LOCAL_SUBJECT}"
                        ),
                        "accepted": True,
                    },
                    202,
                )
            return self._json(
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "snapshot 수집에 실패했습니다.",
                    },
                },
                500,
            )
        if u.path in (
            "/api/actions/commit",
            "/api/actions/approve",
        ):
            if self.read_only:
                return self._json(
                    {
                        "ok": False,
                        "error": {
                            "code": "FORBIDDEN",
                            "message": (
                                "읽기 전용 모드에서는 action을 "
                                "실행할 수 없습니다."
                            ),
                        },
                    },
                    403,
                )
            try:
                self._require_action_headers()
                body = self._body()
            except ValueError as error:
                return self._json(
                    {
                        "ok": False,
                        "error": {
                            "code": "INVALID_REQUEST",
                            "message": str(error),
                        },
                    },
                    400,
                )
            if u.path == "/api/actions/commit":
                return self._guard(
                    lambda: self._pull_request_result(
                        live.commit_and_merge(
                            body.get("key", ""),
                            body.get("content", ""),
                            body.get("title", ""),
                            body.get("description", ""),
                            body.get("expected_head"),
                        ),
                        "source-pr",
                    )
                )
            return self._guard(
                lambda: self._pull_request_result(
                    live.approve(
                        body.get("wiki_pr", ""),
                        body.get("expected_source_commit"),
                    ),
                    "wiki-pr",
                )
            )
        self.send_error(404)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if "/api/" in str(line):
            sys.stderr.write(f"  {line}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="127.0.0.1 전용 demo-ui 서버를 실행한다.",
    )
    parser.add_argument(
        "port",
        nargs="?",
        type=int,
        default=8899,
        help="로컬 listen port (기본값: 8899)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help=(
            "/api/actions/commit과 /api/actions/approve를 "
            "403으로 차단한다."
        ),
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Cognito 로그인 없이 화면에 들어간다. HTML 응답에 로컬 세션을 "
            "심는다. AWS 권한은 바뀌지 않는다. 127.0.0.1 전용."
        ),
    )
    args = parser.parse_args(argv)
    Handler.read_only = args.read_only
    Handler.no_auth = args.no_auth

    srv = ThreadingHTTPServer(
        (HOST, args.port),
        partial(Handler, directory=str(FRONTEND)),
    )
    print(f"데모 UI  http://{HOST}:{args.port}/")
    if args.read_only:
        print(
            "  모드: 읽기 전용 — source commit · wiki approve "
            "action은 403 이다"
        )
    else:
        print("  모드: 쓰기 가능 ⚠️  화면을 공유하거나 터널을 열 때는 --read-only 로 띄운다")
        print(f"  쓰기 허용 소스 {len(EDITABLE)}개: {', '.join(EDITABLE)}")
    if args.no_auth:
        print("  인증: 없음 ⚠️  로컬 세션을 HTML 에 심는다. 127.0.0.1 전용")
    print("  Ctrl+C 로 종료")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
