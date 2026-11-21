"""Web research and install-method comparison (Build 12, module 7).
The network is replaced by a fake resolver and transport."""

from typing import Any

import pytest

from agent.registry import ToolRegistry
from hostplatform import webfetch
from hostplatform.webfetch import FetchError, fetch_page, html_to_text, validate_url
from tests.test_adapters import FakeHost, apt
from tools.research import (
    COMPARE_INSTALL_METHODS,
    FETCH_WEB_PAGE,
    register_research_tools,
)

ALLOWED = ("docs.python.org", "example.org")
PUBLIC = "93.184.216.34"


def resolver(table: dict[str, list[str]] | None = None) -> Any:
    table = table or {}
    return lambda host: table.get(host, [PUBLIC])


class Transport:
    def __init__(self, *responses: tuple[int, dict[str, str], bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, str]] = []

    def __call__(
        self, host: str, ip: str, path: str, timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        self.calls.append((host, ip, path))
        return self.responses.pop(0)


PAGE = (
    b"<html><head><title>t</title><script>evil()</script></head>"
    b"<body><h1>Install</h1><p>Run pip install x</p></body></html>"
)
HTML = (200, {"content-type": "text/html; charset=utf-8"}, PAGE)


# -- URL validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.python.org/3/",
        "ftp://docs.python.org/",
        "https://user:pw@docs.python.org/",
        "https://docs.python.org@evil.com/",
        "https://docs.python.org:8443/",
        "https://evil.com/",
        "https://docs.python.org.evil.com/",
        "https://93.184.216.34/",
        "https://[::1]/",
        "https://docs.python.org/a b",
        "https://docs.python.org/\x00",
        "https://docs.python.org/" + "a" * 400,
        "file:///etc/passwd",
        "",
    ],
)
def test_bad_urls_refused(url: str) -> None:
    with pytest.raises(FetchError):
        validate_url(url, ALLOWED)


def test_subdomains_of_allowed_hosts_accepted() -> None:
    assert validate_url("https://sub.example.org/x?q=1", ALLOWED) == (
        "sub.example.org",
        "/x?q=1",
    )


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "fd00::1",
        "0.0.0.0",
    ],
)
def test_non_public_resolution_refused(addr: str) -> None:
    t = Transport(HTML)
    with pytest.raises(FetchError, match="non-public"):
        fetch_page(
            "https://docs.python.org/",
            ALLOWED,
            resolver({"docs.python.org": [addr]}),
            t,
        )
    assert t.calls == []


def test_any_private_address_among_many_is_refused() -> None:
    with pytest.raises(FetchError):
        fetch_page("https://docs.python.org/", ALLOWED,
                   resolver({"docs.python.org": [PUBLIC, "10.1.1.1"]}), Transport(HTML))  # fmt: skip


def test_connection_is_pinned_to_checked_address() -> None:
    t = Transport(HTML)
    fetch_page("https://docs.python.org/3/", ALLOWED, resolver(), t)
    assert t.calls == [("docs.python.org", PUBLIC, "/3/")]


# -- fetching ------------------------------------------------------------------------


def test_html_page_becomes_text_without_scripts() -> None:
    data = fetch_page("https://docs.python.org/", ALLOWED, resolver(), Transport(HTML))
    assert data["status"] == "ok" and "Run pip install x" in data["text"]
    assert "evil()" not in data["text"] and "untrusted" in data["note"]


def test_redirect_to_allowed_host_followed_and_revalidated() -> None:
    t = Transport((301, {"location": "https://example.org/new"}, b""), HTML)
    data = fetch_page("https://docs.python.org/old", ALLOWED, resolver(), t)
    assert data["status"] == "ok" and data["final_url"] == "https://example.org/new"


def test_redirect_to_disallowed_host_refused() -> None:
    t = Transport((302, {"location": "https://evil.com/"}, b""))
    with pytest.raises(FetchError, match="not on the allowed"):
        fetch_page("https://docs.python.org/", ALLOWED, resolver(), t)


def test_redirect_to_http_refused() -> None:
    t = Transport((302, {"location": "http://docs.python.org/"}, b""))
    with pytest.raises(FetchError, match="https"):
        fetch_page("https://docs.python.org/", ALLOWED, resolver(), t)


def test_redirect_loop_is_bounded() -> None:
    loop = (302, {"location": "/again"}, b"")
    data = fetch_page(
        "https://docs.python.org/", ALLOWED, resolver(), Transport(*[loop] * 10)
    )
    assert data["status"] == "too_many_redirects"


def test_binary_content_refused() -> None:
    t = Transport((200, {"content-type": "application/octet-stream"}, b"\x7fELF"))
    assert (
        fetch_page("https://docs.python.org/x", ALLOWED, resolver(), t)["status"]
        == "unsupported_content"
    )


def test_http_error_reported() -> None:
    t = Transport((404, {}, b""))
    assert (
        fetch_page("https://docs.python.org/x", ALLOWED, resolver(), t)["http_status"]
        == 404
    )


def test_large_page_is_truncated() -> None:
    big = (200, {"content-type": "text/plain"}, b"a" * (webfetch.MAX_BYTES + 100))
    data = fetch_page("https://docs.python.org/x", ALLOWED, resolver(), Transport(big))
    assert data["truncated"] and len(data["text"]) <= webfetch.MAX_TEXT


def test_secrets_in_pages_are_redacted() -> None:
    page = (200, {"content-type": "text/plain"}, b"token: ghp_" + b"a" * 36)
    data = fetch_page("https://docs.python.org/x", ALLOWED, resolver(), Transport(page))
    assert "ghp_" + "a" * 36 not in data["text"]


def test_transport_errors_reported() -> None:
    def boom(*_a: Any) -> Any:
        raise ConnectionResetError

    assert (
        fetch_page("https://docs.python.org/", ALLOWED, resolver(), boom)["status"]
        == "error"
    )


def test_resolution_failure_refused() -> None:
    def nores(_h: str) -> list[str]:
        raise OSError

    with pytest.raises(FetchError, match="resolve"):
        fetch_page("https://docs.python.org/", ALLOWED, nores, Transport(HTML))


def test_malformed_html_does_not_crash() -> None:
    assert isinstance(html_to_text("<p>unclosed <b><<<&#xZZ;"), str)


# -- tools ----------------------------------------------------------------------------


def make(**kw: Any) -> ToolRegistry:
    reg = ToolRegistry()
    register_research_tools(reg, allowed=ALLOWED, resolve=resolver(), **kw)
    return reg


def test_tools_are_read_only() -> None:
    reg = make(transport=Transport(HTML))
    assert set(reg.names()) == {FETCH_WEB_PAGE, COMPARE_INSTALL_METHODS}
    assert not any(reg.is_operation(n) for n in reg.names())


def test_fetch_tool_reports_refusal_as_data() -> None:
    data = make(transport=Transport()).call(
        FETCH_WEB_PAGE, {"url": "https://evil.com/"}
    )
    assert data["status"] == "refused"


def test_compare_methods_with_apt_versions() -> None:
    host = FakeHost()
    host.available["ripgrep"] = "14.1"
    reg = make(
        packages=apt(host),
        which=lambda n: "/usr/bin/" + n if n in ("apt-get", "snap") else None,
    )
    data = reg.call(COMPARE_INSTALL_METHODS, {"tool": "ripgrep"})
    rows = {r["method"]: r for r in data["methods"]}
    assert rows["apt"]["candidate_version"] == "14.1" and rows["apt"]["in_repository"]
    assert rows["snap"]["manager_present"] and not rows["pipx"]["manager_present"]
    assert rows["apt"]["supported_by_agent"] and not rows["snap"]["supported_by_agent"]


def test_compare_rejects_bad_name() -> None:
    assert "error" in make().call(COMPARE_INSTALL_METHODS, {"tool": "x; rm -rf /"})


def test_compare_without_apt() -> None:
    data = make(which=lambda _n: None).call(
        COMPARE_INSTALL_METHODS, {"tool": "ripgrep"}
    )
    assert "candidate_version" not in data["methods"][0]
