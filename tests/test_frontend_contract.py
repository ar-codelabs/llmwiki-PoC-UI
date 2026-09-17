import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

DEMO_UI = Path(__file__).resolve().parents[1]
FRONTEND = DEMO_UI / "frontend"
PROTECTED_PAGES = ("index.html", "live.html", "wiki.html")
PUBLIC_CONFIG_FIELDS = {
    "region",
    "user_pool_id",
    "app_client_id",
    "cognito_domain",
    "callback_path",
}
COGNITO_SUB = "00000000-0000-7000-c000-abcdef123456"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_node(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=commonjs", "-e", script],
        cwd=DEMO_UI.parent,
        capture_output=True,
        text=True,
        check=False,
    )


def test_frontend_manifest_moves_legacy_pages():
    assert FRONTEND.is_dir()
    for name in (*PROTECTED_PAGES, "auth-callback.html"):
        assert (FRONTEND / name).is_file()
    for name in PROTECTED_PAGES:
        assert not (DEMO_UI / name).exists()
    assert (FRONTEND / "auth.js").is_file()
    assert (FRONTEND / "api.js").is_file()


def test_all_pages_load_auth_and_api_before_bootstrap():
    for name in PROTECTED_PAGES:
        html = _read(FRONTEND / name)
        auth_script = '<script src="/auth.js"></script>'
        api_script = '<script src="/api.js"></script>'
        assert auth_script in html
        assert api_script in html
        assert html.index(auth_script) < html.index(api_script)
        assert html.index(api_script) < html.index(
            "DemoAuth.bootstrap()"
        )
        assert re.search(
            r"await\s+(?:window\.)?DemoAuth\.bootstrap\(\)",
            html,
        )
        assert "if (!authSession) return;" in html


def test_pages_use_only_shared_client_for_protected_api():
    combined = ""
    for path in FRONTEND.glob("*.html"):
        text = _read(path)
        combined += text
        assert "fetch(" not in text

    required_routes = (
        "/api/session",
        "/api/snapshot",
        "/api/editable",
        "/api/progress",
        "/api/wiki-pr",
        "/api/wiki-repo",
        "/api/wiki-history",
        "/api/wiki-commit",
        "/api/finalization",
        "/api/actions/commit",
        "/api/actions/approve",
        "/api/actions/refresh",
    )
    for route in required_routes:
        assert route in combined

    for legacy in (
        "/api/mode",
        "/api/commit",
        "/api/approve",
        "/api/refresh",
        "snapshot.json",
    ):
        assert legacy not in combined


def test_mutating_actions_preserve_confirmation_and_optimistic_tokens():
    html = _read(FRONTEND / "live.html")
    assert "소스 PR을 생성하고 main에 merge합니다" in html
    assert "wiki PR을 main에 merge합니다" in html
    assert re.search(
        r"expected_head\s*:\s*file\.head",
        html,
    )
    assert re.search(
        r"RUN\.expected_source_commit\s*="
        r"\s*response\.source_commit",
        html,
    )
    assert re.search(
        r"expected_source_commit\s*:"
        r"\s*RUN\.expected_source_commit",
        html,
    )


def test_live_console_exposes_only_fixed_four_step_shaka_control():
    html = _read(FRONTEND / "live.html")

    assert "shaka-player/lib/net/backoff.js" in html
    assert "baseDelay: 1000," in html
    assert "baseDelay: 1200," in html
    assert html.count('data-control-step="') == 4
    for step in ("1", "2", "3", "4"):
        assert f'data-control-step="{step}"' in html
    assert "<textarea" not in html
    assert "<select" not in html
    assert not re.search(
        r'<input\b[^>]*type=["\']text["\']',
        html,
        re.IGNORECASE,
    )
    assert "/bootstrap" not in html.lower()


def test_live_query_failures_keep_mutating_controls_disabled():
    html = _read(FRONTEND / "live.html")
    mutation_match = re.search(
        r"function fixedMutation\(content\)\{.*?\n\}"
        r"\n\nfunction drawTL",
        html,
        re.DOTALL,
    )
    load_match = re.search(
        r"async function loadFiles\(\)\{.*?\n\}"
        r"\nfunction pick",
        html,
        re.DOTALL,
    )
    pick_match = re.search(
        r"function pick\(index\)\{.*?\n\}"
        r"\n\n\$\('btnCommit'",
        html,
        re.DOTALL,
    )
    review_match = re.search(
        r"async function showWikiPR\(wpr,payload\)\{.*?\n\}"
        r"\n\n\$\('btnApprove'",
        html,
        re.DOTALL,
    )
    assert mutation_match is not None
    assert load_match is not None
    assert pick_match is not None
    assert review_match is not None
    fixed_mutation = mutation_match.group(0).removesuffix(
        "\n\nfunction drawTL"
    )
    load_files = load_match.group(0).removesuffix(
        "\nfunction pick"
    )
    pick = pick_match.group(0).removesuffix(
        "\n\n$('btnCommit'"
    )
    show_wiki_pr = review_match.group(0).removesuffix(
        "\n\n$('btnApprove'"
    )
    script = f"""
const assert = require("node:assert/strict");
const elements = new Map();
function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      disabled: false,
      innerHTML: "",
      textContent: "",
      value: "",
      classList: {{add() {{}}, remove() {{}}}},
      scrollIntoView() {{}},
    }});
  }}
  return elements.get(id);
}}
const $ = id => element(id);
const esc = value => String(value ?? "");
const scrollBehavior = "auto";
let FILES = [{{key: "stale"}}];
let NEXT_CONTENT = null;
let READ_ONLY = false;
const FIXED_KEY = "shaka-player/lib/net/backoff.js";
const FIXED_REPOSITORY = "shaka-player";
const FIXED_PATH = "lib/net/backoff.js";
let RUN = {{
  wiki_pr: "stale-pr",
  expected_source_commit: "stale-head",
  doc_paths: [],
}};
global.window = {{
  DemoApi: {{get: async () => {{ throw new Error("request failed"); }}}},
}};
{fixed_mutation}
{load_files}
{pick}
{show_wiki_pr}

(async () => {{
  await loadFiles();
  assert.equal(FILES.length, 0);
  assert.equal($("btnCommit").disabled, true);

  FILES = [{{key: "stale"}}];
  $("btnCommit").disabled = false;
  window.DemoApi.get = async () => ({{
    files: [{{key: "incomplete", content: "body"}}],
  }});
  await loadFiles();
  assert.equal(FILES.length, 0);
  assert.equal($("btnCommit").disabled, true);

  $("btnCommit").disabled = false;
  window.DemoApi.get = async () => ({{
    files: [{{
      key: FIXED_KEY,
      repo: FIXED_REPOSITORY,
      path: FIXED_PATH,
      content: "const retry = {{\\n  baseDelay: 1500,\\n}};\\n",
      head: "unsupported-head",
    }}],
  }});
  await loadFiles();
  assert.equal(FILES.length, 1);
  assert.equal(NEXT_CONTENT, null);
  assert.equal($("btnCommit").disabled, true);

  window.DemoApi.get = async () => ({{
    pull_request_id: "91",
    status: "OPEN",
    branch: "refs/heads/wikiagent/update",
    all_files: ["guides/page.md"],
    files: [],
  }});
  await showWikiPR("91", {{scope: []}});
  assert.equal($("btnApprove").disabled, true);
  assert.equal(RUN.wiki_pr, null);
  assert.equal(RUN.expected_source_commit, null);
}})().catch((error) => {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_malformed_token_usage_cannot_create_executable_markup():
    html = _read(FRONTEND / "live.html")
    num_match = re.search(
        r"const num=.*?;\n",
        html,
    )
    tick_match = re.search(
        r"async function tick\(pullRequestId\)\{.*?\n\}"
        r"\n\nasync function showWikiPR",
        html,
        re.DOTALL,
    )
    assert num_match is not None
    assert tick_match is not None
    tick = tick_match.group(0).removesuffix(
        "\n\nasync function showWikiPR"
    )
    script = f"""
const elements = new Map();
function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      disabled: false,
      innerHTML: "",
      textContent: "",
      classList: {{add() {{}}, remove() {{}}}},
    }});
  }}
  return elements.get(id);
}}
const $ = id => element(id);
const esc = value => String(value ?? "").replace(
  /[&<>]/g,
  character => ({{"&": "&amp;", "<": "&lt;", ">": "&gt;"}}[character]),
);
{num_match.group(0)}
let POLL = null;
let STATE = {{}};
let SINCE = 0;
let READ_ONLY = false;
function drawTL() {{}}
function setPhase() {{}}
async function showWikiPR() {{}}
global.clearInterval = () => {{}};
global.window = {{
  DemoApi: {{
    get: async () => ({{
      done: true,
      wiki_pull_request_id: null,
      runtime_events: [],
      payload: {{
        agent_invoked: true,
        trace: [],
        diff_sha256: "safe",
        scope: [],
        runtime: {{
          usage: {{
            input_tokens: '<img src=x onerror="globalThis.pwned=1">',
            output_tokens: '<svg onload="globalThis.pwned=2">',
          }},
        }},
      }},
    }}),
  }},
}};
{tick}

(async () => {{
  await tick("17");
  process.stdout.write($("judgePre").innerHTML);
}})().catch(error => {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""
    result = _run_node(script)

    assert result.returncode == 0, result.stderr
    fragment = result.stdout.lower()
    for forbidden in (
        "<img",
        "<svg",
        "onerror",
        "onload",
        "globalthis.pwned",
    ):
        assert forbidden not in fragment


def test_wiki_dates_cannot_create_markup_or_event_attributes():
    html = _read(FRONTEND / "wiki.html")

    def function_source(
        pattern: str,
        suffix: str,
    ) -> str:
        match = re.search(pattern, html, re.DOTALL)
        assert match is not None
        return match.group(0).removesuffix(suffix)

    load_repo = function_source(
        r"async function loadRepo\(\)\{.*?\n\}"
        r"\n\nfunction matches",
        "\n\nfunction matches",
    )
    matches = function_source(
        r"function matches\(c\)\{.*?\n\}"
        r"\n\nfunction drawList",
        "\n\nfunction drawList",
    )
    draw_list = function_source(
        r"function drawList\(\)\{.*?\n\}"
        r"\n\nasync function show",
        "\n\nasync function show",
    )
    show = function_source(
        r"async function show\(id\)\{.*?\n\}"
        r"\n\n\$\('csel'",
        "\n\n$('csel'",
    )

    script = f"""
const elements = new Map();
function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      innerHTML: "",
      textContent: "",
      value: "",
      querySelectorAll() {{ return []; }},
    }});
  }}
  return elements.get(id);
}}
global.document = {{getElementById: element}};
const $ = id => element(id);
const esc = s => String(s ?? "").replace(
  /[&<>]/g,
  c => ({{"&": "&amp;", "<": "&lt;", ">": "&gt;"}}[c]),
);
let HIST = [], SEL = null, Q = "";
function short(s) {{ return String(s || "").slice(0, 12); }}
function when(d) {{
  return String(d || "").replace("T", " ").slice(0, 19);
}}
{load_repo}
{matches}
{draw_list}
{show}

const mutation = "<svg/onload=open()>";
global.window = {{
  DemoApi: {{
    get: async path => path === "/api/wiki-repo"
      ? {{
          kind: "git",
          repo: "wiki",
          default_branch: "main",
          clone_url: "https://example.invalid/wiki",
          branch_count: 1,
          last_modified: mutation,
          tree: [],
        }}
      : {{
          commit: "commit-id",
          author: "author",
          date: mutation,
          parent: "",
          message: "message",
          files: [],
        }},
  }},
}};

(async () => {{
  HIST = [{{
    commit: "commit-id",
    subject: "subject",
    author: "author",
    date: mutation,
    kind: "기타",
    meta: {{}},
  }}];
  await loadRepo();
  drawList();
  await show("commit-id");
  process.stdout.write(JSON.stringify({{
    last_modified: $("repokv").innerHTML,
    history_date: $("clist").innerHTML,
    detail_date: $("cdetail").innerHTML,
  }}));
}})().catch(error => {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    fragments = json.loads(result.stdout)

    class ExecutableMarkupAudit(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.dangerous = []

        def handle_starttag(self, tag, attrs):
            if tag.lower() == "svg" or any(
                name.lower().startswith("on")
                for name, _ in attrs
            ):
                self.dangerous.append((tag, attrs))

        handle_startendtag = handle_starttag

    for sink, fragment in fragments.items():
        audit = ExecutableMarkupAudit()
        audit.feed(fragment)
        assert audit.dangerous == [], (
            f"{sink} created executable markup: "
            f"{audit.dangerous}"
        )


def test_pages_require_backend_session_before_opening_shell():
    canonical_subject = COGNITO_SUB
    for name in PROTECTED_PAGES:
        html = _read(FRONTEND / name)
        match = re.search(
            r"async function bootstrapPage\(\)\{.*?\n\}"
            r"\n\nbootstrapPage\(\);",
            html,
            re.DOTALL,
        )
        assert match is not None
        bootstrap_page = match.group(0).removesuffix(
            "\n\nbootstrapPage();"
        )
        script = f"""
const assert = require("node:assert/strict");
const elements = new Map();
function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      hidden: id === "appShell",
      textContent: "",
      disabled: false,
    }});
  }}
  return elements.get(id);
}}
function resetElements() {{
  elements.clear();
  element("appShell").hidden = true;
  element("authGate").hidden = false;
}}
global.document = {{getElementById: element}};
const $ = id => element(id);
const subjectLabel = value => {{
  const text = String(value || "unknown");
  return text.length > 18
    ? `${{text.slice(0, 8)}}…${{text.slice(-4)}}`
    : text;
}};
let HIST = [];
let scenario = "success";
let sessionCalls = 0;
let logoutCalls = 0;
let dataCalls = 0;
const canonicalSubject = {json.dumps(canonical_subject)};

global.window = {{
  DemoAuth: {{
    bootstrap: async () => ({{subject: "forged-local-subject"}}),
    logout: async () => {{ logoutCalls += 1; }},
    validSubject: value => (
      scenario !== "invalid-subject" &&
      typeof value === "string" &&
      /^[A-Za-z0-9._:@-]{{1,128}}$/.test(value)
    ),
  }},
  DemoApi: {{
    get: async path => {{
      if (path === "/api/session") {{
        sessionCalls += 1;
        assert.equal(element("appShell").hidden, true);
        if (scenario === "unauthorized") {{
          const error = new Error("unauthorized");
          error.status = 401;
          throw error;
        }}
        return {{
          subject: canonicalSubject,
          can_execute: true,
        }};
      }}
      if (scenario === "data-failure") {{
        const error = new Error("data failure");
        error.status = 500;
        throw error;
      }}
      return {{commits: []}};
    }},
  }},
}};

async function load() {{
  dataCalls += 1;
  if (scenario === "data-failure") {{
    const error = new Error("data failure");
    error.status = 500;
    throw error;
  }}
}}
function drawTL() {{}}
function applySession() {{}}
async function loadFiles() {{
  dataCalls += 1;
  if (scenario === "data-failure") {{
    const error = new Error("data failure");
    error.status = 500;
    throw error;
  }}
}}
async function loadRepo() {{
  dataCalls += 1;
  if (scenario === "data-failure") {{
    const error = new Error("data failure");
    error.status = 500;
    throw error;
  }}
}}
function drawList() {{}}
async function show() {{}}
{bootstrap_page}

(async () => {{
  resetElements();
  await bootstrapPage();
  assert.equal(sessionCalls, 1);
  assert.equal(element("appShell").hidden, false);
  assert.equal(element("authGate").hidden, true);
  assert.equal(
    element("authSubject").textContent,
    subjectLabel(canonicalSubject),
  );

  scenario = "invalid-subject";
  sessionCalls = 0;
  logoutCalls = 0;
  resetElements();
  await bootstrapPage();
  assert.equal(sessionCalls, 1);
  assert.equal(element("appShell").hidden, true);
  assert.equal(element("authGate").hidden, false);
  assert.equal(logoutCalls, 1);

  scenario = "unauthorized";
  sessionCalls = 0;
  logoutCalls = 0;
  resetElements();
  await bootstrapPage();
  assert.equal(sessionCalls, 1);
  assert.equal(element("appShell").hidden, true);
  assert.equal(element("authGate").hidden, false);
  assert.equal(logoutCalls, 1);

  scenario = "data-failure";
  sessionCalls = 0;
  logoutCalls = 0;
  dataCalls = 0;
  resetElements();
  await bootstrapPage();
  assert.equal(sessionCalls, 1);
  assert.equal(dataCalls > 0, true);
  assert.equal(element("appShell").hidden, true);
  assert.equal(element("authGate").hidden, false);
  assert.equal(logoutCalls, 0);
}})().catch(error => {{
  process.stderr.write(
    {json.dumps(name)} + ": " + String(error.stack || error),
  );
  process.exitCode = 1;
}});
"""
        result = _run_node(script)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


def test_auth_ui_is_safe_dense_and_accessible():
    for name in PROTECTED_PAGES:
        html = _read(FRONTEND / name)
        assert 'id="appShell" hidden' in html
        assert 'id="authStatus"' in html
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
        assert 'id="logoutButton"' in html
        assert 'aria-label="로그아웃"' in html
        assert ":focus-visible" in html
        assert "prefers-reduced-motion" in html
        assert "subjectLabel" in html


def test_protected_pages_keep_unauthenticated_auth_gate_grid_layout():
    auth_gate_grid = re.compile(
        r"(?:^|})\s*\.auth-gate\s*\{"
        r"[^{}]*\bdisplay\s*:\s*grid\b[^{}]*\}",
        re.DOTALL,
    )
    for name in PROTECTED_PAGES:
        html = _read(FRONTEND / name)
        style = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
        assert style is not None
        assert auth_gate_grid.search(style.group(1)) is not None, name


def test_protected_pages_hide_auth_gate_when_hidden():
    hidden_auth_gate = re.compile(
        r"(?:^|})[^{}]*"
        r"\.auth-gate\s*\[\s*hidden\s*\][^{}]*\{"
        r"[^{}]*\bdisplay\s*:\s*none\b"
        r"(?:\s*!important)?[^{}]*\}",
        re.DOTALL,
    )
    missing = []
    for name in PROTECTED_PAGES:
        html = _read(FRONTEND / name)
        style = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
        assert style is not None
        if hidden_auth_gate.search(style.group(1)) is None:
            missing.append(name)
    assert missing == [], (
        "hidden auth gate CSS contract missing from "
        + ", ".join(missing)
    )


def test_frontend_has_no_external_asset_dependency():
    external_asset = re.compile(
        r"""(?:src|href)\s*=\s*["']https?://""",
        re.IGNORECASE,
    )
    for path in FRONTEND.glob("*.html"):
        assert external_asset.search(_read(path)) is None


def test_auth_source_contains_required_fail_closed_boundaries():
    source = _read(FRONTEND / "auth.js")
    for required in (
        'fetch("/api/config"',
        "new Uint8Array(32)",
        "/oauth2/authorize",
        "/oauth2/token",
        "code_challenge_method",
        "sessionStorage",
        "__DEMO_TEST_AUTH__",
        "access_token",
        "state",
        "nonce",
        "response.url",
        "window.location.origin",
    ):
        assert required in source
    assert re.search(r'digest\(\s*"SHA-256"', source)
    assert "console." not in source
    assert "innerHTML" not in source
    assert "textContent" not in source


def test_auth_exposes_safe_subject_validator():
    auth_path = json.dumps(str(FRONTEND / "auth.js"))
    subject = json.dumps(COGNITO_SUB)
    script = f"""
const fs = require("node:fs");
const assert = require("node:assert/strict");

global.window = {{}};
global.sessionStorage = {{
  getItem() {{ return null; }},
  setItem() {{}},
  removeItem() {{}},
}};
eval(fs.readFileSync({auth_path}, "utf8"));

assert.equal(typeof window.DemoAuth.validSubject, "function");
for (const value of [
  {subject},
  "A._:@-z9",
  "a".repeat(128),
]) {{
  assert.equal(window.DemoAuth.validSubject(value), true, value);
}}
for (const value of [
  null,
  "",
  " ",
  "subject\\tname",
  "subject\\nname",
  "subject/name",
  "a".repeat(129),
]) {{
  assert.equal(window.DemoAuth.validSubject(value), false, String(value));
}}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_auth_test_injection_bypasses_network():
    auth_path = json.dumps(str(FRONTEND / "auth.js"))
    subject = json.dumps(COGNITO_SUB)
    script = f"""
const fs = require("node:fs");
const assert = require("node:assert/strict");

global.window = {{
  __DEMO_TEST_AUTH__: {{
    accessToken: "test-token",
    subject: {subject},
  }},
  location: {{
    origin: "https://demo.example.test",
    pathname: "/live.html",
    search: "",
    hash: "",
  }},
}};
global.sessionStorage = {{
  getItem() {{ return null; }},
  setItem() {{ throw new Error("storage must not be used"); }},
  removeItem() {{}},
}};
global.fetch = async () => {{
  throw new Error("network must not be used");
}};
eval(fs.readFileSync({auth_path}, "utf8"));

(async () => {{
  const session = await window.DemoAuth.bootstrap();
  assert.equal(session.accessToken, "test-token");
  assert.equal(session.subject, {subject});
  assert.equal(window.DemoAuth.accessToken(), "test-token");
  assert.equal(window.DemoAuth.subject(), {subject});
}})().catch((error) => {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_auth_login_generates_pkce_and_same_origin_callback():
    auth_path = json.dumps(str(FRONTEND / "auth.js"))
    script = f"""
const fs = require("node:fs");
const assert = require("node:assert/strict");
const webcrypto = require("node:crypto").webcrypto;

const values = new Map();
const storage = {{
  getItem(key) {{ return values.has(key) ? values.get(key) : null; }},
  setItem(key, value) {{ values.set(key, String(value)); }},
  removeItem(key) {{ values.delete(key); }},
}};
let seed = 0;
const randomSizes = [];
let assigned = null;
global.window = {{
  location: {{
    origin: "https://demo.example.test",
    pathname: "/live.html",
    search: "?view=active",
    hash: "#source",
    href: "https://demo.example.test/live.html?view=active#source",
    assign(value) {{ assigned = value; }},
  }},
  crypto: {{
    getRandomValues(array) {{
      randomSizes.push(array.byteLength);
      for (let i = 0; i < array.length; i += 1) {{
        array[i] = (seed + i + 1) % 256;
      }}
      seed += array.length;
      return array;
    }},
    subtle: webcrypto.subtle,
  }},
}};
global.sessionStorage = storage;
global.fetch = async (path, options) => {{
  assert.equal(path, "/api/config");
  assert.equal(options.cache, "no-store");
  return {{
    ok: true,
    status: 200,
    url: "https://demo.example.test/api/config",
    headers: {{get: () => "application/json; charset=utf-8"}},
    json: async () => ({{
      region: "ap-northeast-2",
      user_pool_id: "ap-northeast-2_local",
      app_client_id: "client-id",
      cognito_domain:
        "https://demo.auth.ap-northeast-2.amazoncognito.com",
      callback_path: "/auth/callback",
    }}),
  }};
}};
eval(fs.readFileSync({auth_path}, "utf8"));

(async () => {{
  await window.DemoAuth.login();
  assert.deepEqual(randomSizes, [32, 32, 32]);
  const target = new URL(assigned);
  assert.equal(
    target.origin,
    "https://demo.auth.ap-northeast-2.amazoncognito.com",
  );
  assert.equal(target.pathname, "/oauth2/authorize");
  assert.equal(target.searchParams.get("response_type"), "code");
  assert.equal(target.searchParams.get("client_id"), "client-id");
  assert.equal(
    target.searchParams.get("redirect_uri"),
    "https://demo.example.test/auth/callback",
  );
  assert.equal(target.searchParams.get("code_challenge_method"), "S256");
  assert.match(
    target.searchParams.get("code_challenge"),
    /^[A-Za-z0-9_-]{{43}}$/,
  );
  assert.equal(
    target.searchParams.get("state"),
    values.get("demo.auth.oauth_state"),
  );
  assert.equal(
    target.searchParams.get("nonce"),
    values.get("demo.auth.nonce"),
  );
  assert.equal(
    values.get("demo.auth.return_to"),
    "/live.html?view=active#source",
  );
  assert.match(
    values.get("demo.auth.pkce_verifier"),
    /^[A-Za-z0-9_-]{{43}}$/,
  );
}})().catch((error) => {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_auth_callback_validates_state_token_origin_and_claims():
    auth_path = json.dumps(str(FRONTEND / "auth.js"))
    script = f"""
const fs = require("node:fs");
const assert = require("node:assert/strict");

function base64url(value) {{
  return Buffer.from(JSON.stringify(value))
    .toString("base64")
    .replace(/=/g, "")
    .replace(/\\+/g, "-")
    .replace(/\\//g, "_");
}}
function jwt(payload) {{
  return `${{base64url({{alg: "none"}})}}.${{base64url(payload)}}.signature`;
}}

const now = Math.floor(Date.now() / 1000);
const subject = {json.dumps(COGNITO_SUB)};
const accessToken = jwt({{
  sub: subject,
  exp: now + 3600,
  token_use: "access",
  client_id: "client-id",
}});
const idToken = jwt({{
  sub: subject,
  exp: now + 3600,
  token_use: "id",
  aud: "client-id",
  nonce: "stored-nonce",
}});
const values = new Map([
  ["demo.auth.oauth_state", "stored-state"],
  ["demo.auth.pkce_verifier", "v".repeat(43)],
  ["demo.auth.nonce", "stored-nonce"],
  ["demo.auth.return_to", "/wiki.html"],
]);
const storage = {{
  getItem(key) {{ return values.has(key) ? values.get(key) : null; }},
  setItem(key, value) {{ values.set(key, String(value)); }},
  removeItem(key) {{ values.delete(key); }},
}};
let replaced = null;
let tokenRequests = 0;
global.window = {{
  location: {{
    origin: "https://demo.example.test",
    pathname: "/auth/callback",
    search: "?code=auth-code&state=stored-state",
    hash: "",
    href:
      "https://demo.example.test/auth/callback" +
      "?code=auth-code&state=stored-state",
    replace(value) {{ replaced = value; }},
  }},
  history: {{replaceState() {{}}}},
}};
global.sessionStorage = storage;
global.fetch = async (path, options) => {{
  if (path === "/api/config") {{
    return {{
      ok: true,
      status: 200,
      url: "https://demo.example.test/api/config",
      headers: {{get: () => "application/json"}},
      json: async () => ({{
        region: "ap-northeast-2",
        user_pool_id: "ap-northeast-2_local",
        app_client_id: "client-id",
        cognito_domain:
          "https://demo.auth.ap-northeast-2.amazoncognito.com",
        callback_path: "/auth/callback",
      }}),
    }};
  }}
  tokenRequests += 1;
  assert.equal(
    path,
    "https://demo.auth.ap-northeast-2.amazoncognito.com/oauth2/token",
  );
  assert.equal(options.method, "POST");
  assert.match(options.body, /code_verifier=/);
  return {{
    ok: true,
    status: 200,
    url:
      "https://demo.auth.ap-northeast-2.amazoncognito.com/oauth2/token",
    headers: {{get: () => "application/json"}},
    json: async () => ({{
      access_token: accessToken,
      id_token: idToken,
      token_type: "Bearer",
      expires_in: 3600,
    }}),
  }};
}};
eval(fs.readFileSync({auth_path}, "utf8"));

(async () => {{
  await window.DemoAuth.callback();
  assert.equal(tokenRequests, 1);
  assert.equal(window.DemoAuth.accessToken(), accessToken);
  assert.equal(window.DemoAuth.subject(), subject);
  assert.equal(replaced, "/wiki.html");
  assert.equal(values.has("demo.auth.oauth_state"), false);
  assert.equal(values.has("demo.auth.pkce_verifier"), false);
  assert.equal(values.has("demo.auth.nonce"), false);
}})().catch((error) => {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_auth_callback_rejects_state_mismatch_before_token_exchange():
    auth_path = json.dumps(str(FRONTEND / "auth.js"))
    script = f"""
const fs = require("node:fs");
const assert = require("node:assert/strict");

const values = new Map([
  ["demo.auth.oauth_state", "stored-state"],
  ["demo.auth.pkce_verifier", "v".repeat(43)],
  ["demo.auth.nonce", "stored-nonce"],
  ["demo.auth.return_to", "/"],
]);
global.sessionStorage = {{
  getItem(key) {{ return values.has(key) ? values.get(key) : null; }},
  setItem(key, value) {{ values.set(key, String(value)); }},
  removeItem(key) {{ values.delete(key); }},
}};
global.window = {{
  location: {{
    origin: "https://demo.example.test",
    pathname: "/auth/callback",
    search: "?code=auth-code&state=attacker-state",
    hash: "",
    href:
      "https://demo.example.test/auth/callback" +
      "?code=auth-code&state=attacker-state",
  }},
  history: {{replaceState() {{}}}},
}};
let tokenRequests = 0;
global.fetch = async (path) => {{
  if (path !== "/api/config") tokenRequests += 1;
  return {{
    ok: true,
    status: 200,
    url: "https://demo.example.test/api/config",
    headers: {{get: () => "application/json"}},
    json: async () => ({{
      region: "ap-northeast-2",
      user_pool_id: "ap-northeast-2_local",
      app_client_id: "client-id",
      cognito_domain:
        "https://demo.auth.ap-northeast-2.amazoncognito.com",
      callback_path: "/auth/callback",
    }}),
  }};
}};
eval(fs.readFileSync({auth_path}, "utf8"));

(async () => {{
  await assert.rejects(() => window.DemoAuth.callback());
  assert.equal(tokenRequests, 0);
  assert.equal(window.DemoAuth.accessToken(), null);
  assert.equal(values.has("demo.auth.oauth_state"), false);
  assert.equal(values.has("demo.auth.pkce_verifier"), false);
}})().catch((error) => {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_api_client_enforces_same_origin_headers_and_generic_errors():
    api_path = json.dumps(str(FRONTEND / "api.js"))
    script = f"""
const fs = require("node:fs");
const assert = require("node:assert/strict");

global.window = {{
  location: {{origin: "https://demo.example.test"}},
  DemoAuth: {{accessToken: () => "test-token"}},
}};
global.location = window.location;
const calls = [];
global.fetch = async (path, options) => {{
  calls.push([path, options]);
  return {{
    ok: true,
    status: 200,
    url: "https://demo.example.test" + path,
    headers: {{get: () => "application/json; charset=utf-8"}},
    json: async () => ({{ok: true}}),
  }};
}};
eval(fs.readFileSync({api_path}, "utf8"));

(async () => {{
  await window.DemoApi.get("/api/wiki-repo?view=tree");
  await window.DemoApi.post("/api/actions/refresh", {{}});
  assert.equal(calls.length, 2);
  assert.equal(calls[0][0], "/api/wiki-repo?view=tree");
  assert.equal(calls[0][1].method, "GET");
  assert.equal(calls[1][1].method, "POST");
  assert.equal(calls[1][1].body, "{{}}");
  for (const [, options] of calls) {{
    const headers = Object.fromEntries(options.headers.entries());
    assert.equal(headers.authorization, "Bearer test-token");
    assert.equal(headers["content-type"], "application/json");
    assert.equal(headers["x-demo-request"], "1");
    assert.equal(options.cache, "no-store");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.redirect, "error");
  }}

  const beforeReject = calls.length;
  await assert.rejects(
    () => window.DemoApi.get("https://evil.example/api/snapshot"),
  );
  await assert.rejects(
    () => window.DemoApi.get("//evil.example/api/snapshot"),
  );
  await assert.rejects(() => window.DemoApi.get("/not-api"));
  assert.equal(calls.length, beforeReject);

  global.fetch = async () => ({{
    ok: false,
    status: 502,
    url: "https://demo.example.test/api/snapshot",
    headers: {{get: () => "text/html"}},
    json: async () => {{
      throw new Error("secret upstream body");
    }},
  }});
  await assert.rejects(
    () => window.DemoApi.get("/api/snapshot"),
    (error) => {{
      assert.match(error.message, /HTTP 502/);
      assert.equal(error.status, 502);
      assert.doesNotMatch(error.message, /secret|upstream|body/i);
      return true;
    }},
  );

  window.DemoAuth.accessToken = () => null;
  await assert.rejects(
    () => window.DemoApi.get("/api/snapshot"),
    (error) => {{
      assert.equal(error.status, 401);
      return true;
    }},
  );
}})().catch((error) => {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_local_server_uses_frontend_root_and_public_only_config():
    source = _read(DEMO_UI / "server.py")
    assert 'HOST = "127.0.0.1"' in source
    assert 'FRONTEND = HERE / "frontend"' in source
    assert "directory=str(FRONTEND)" in source
    assert '"/api/config"' in source
    assert '"/auth/callback"' in source
    assert '"/auth-callback.html"' in source
    assert '"/api/actions/commit"' in source
    assert '"/api/actions/approve"' in source
    assert '"/api/actions/refresh"' in source

    namespace = {}
    config_match = re.search(
        r"LOCAL_PUBLIC_CONFIG\s*=\s*(\{.*?\n\})",
        source,
        re.DOTALL,
    )
    assert config_match is not None
    exec(
        "LOCAL_PUBLIC_CONFIG = " + config_match.group(1),
        {},
        namespace,
    )
    config = namespace["LOCAL_PUBLIC_CONFIG"]
    assert set(config) == PUBLIC_CONFIG_FIELDS
    rendered = json.dumps(config).lower()
    for forbidden in (
        "secret",
        "credential",
        "password",
        "account_id",
        "role_arn",
        "access_key",
    ):
        assert forbidden not in rendered


def test_local_server_forwards_tokens_on_task_6_action_paths(
    monkeypatch,
):
    sys.path.insert(0, str(DEMO_UI))
    import live
    import server

    calls = []

    def commit(*args):
        calls.append(("commit", args))
        return {"pull_request_id": "17"}

    def approve(*args):
        calls.append(("approve", args))
        return {"pull_request_id": "91"}

    monkeypatch.setattr(live, "commit_and_merge", commit)
    monkeypatch.setattr(live, "approve", approve)

    handler = object.__new__(server.Handler)
    handler.read_only = False
    handler._guard = lambda operation: operation()
    handler._require_action_headers = lambda: None

    handler.path = "/api/actions/commit"
    handler._body = lambda: {
        "key": "shaka-player/lib/net/backoff.js",
        "content": "baseDelay: 1200,\n",
        "title": "docs: update source",
        "description": "demo change",
        "expected_head": "reviewed-head",
    }
    handler.do_POST()

    handler.path = "/api/actions/approve"
    handler._body = lambda: {
        "wiki_pr": "91",
        "expected_source_commit": "reviewed-wiki-head",
    }
    handler.do_POST()

    assert calls == [
        (
            "commit",
            (
                "shaka-player/lib/net/backoff.js",
                "baseDelay: 1200,\n",
                "docs: update source",
                "demo change",
                "reviewed-head",
            ),
        ),
        (
            "approve",
            ("91", "reviewed-wiki-head"),
        ),
    ]
