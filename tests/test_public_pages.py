from unittest.mock import AsyncMock

import httpx
import pytest

from jarvis.public_pages import ArticleText, read_page, resolve


async def test_pinned_ip_preserves_tls_host_and_never_authenticates():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><script>hidden secrets</script><nav>menu</nav><p>Public science article. "
            + "Research evidence. " * 20
            + "</p></html>",
        )

    result = await read_page(
        "https://example.com/article",
        resolver=AsyncMock(return_value="93.184.216.34"),
        transport=httpx.MockTransport(handler),
    )
    assert "Public science" in result["excerpt"] and "hidden secrets" not in result["excerpt"]
    assert calls[0].url.host == "93.184.216.34"
    assert calls[0].headers["host"] == "example.com"
    assert calls[0].extensions["sni_hostname"] == "example.com"
    assert "authorization" not in calls[0].headers and "cookie" not in calls[0].headers


async def test_redirect_private_dns_and_credential_urls_are_blocked():
    resolver = AsyncMock(return_value="93.184.216.34")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1:8100/secrets"})

    with pytest.raises(ValueError):
        await read_page("https://example.com", resolver=resolver, transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    with pytest.raises(ValueError, match="Private"):
        await read_page("https://example.com", resolver=AsyncMock(return_value="10.0.0.1"))
    with pytest.raises(ValueError, match="Credential"):
        await read_page("https://example.com/?access_token=synthetic", resolver=resolver)


async def test_mixed_public_private_dns_rejected(monkeypatch):
    import asyncio

    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(
            return_value=[
                (None, None, None, None, ("93.184.216.34", 443)),
                (None, None, None, None, ("127.0.0.1", 443)),
            ]
        ),
    )
    with pytest.raises(ValueError, match="mixed"):
        await resolve("example.com")


async def test_size_mime_and_challenge_are_not_bypassed():
    for mime, body in [
        ("application/pdf", "x" * 200),
        ("text/plain", "x" * 1_500_001),
        ("text/html", "Verify you are human. " * 40),
    ]:
        with pytest.raises(ValueError):
            await read_page(
                "https://example.com",
                resolver=AsyncMock(return_value="93.184.216.34"),
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, headers={"content-type": mime}, text=body)
                ),
            )
    parser = ArticleText()
    parser.feed("<nav>hidden<script>still hidden</script>hidden</nav><p>Visible</p>")
    assert parser.parts == ["Visible"]
