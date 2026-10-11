"""Read public HTML over pinned, globally routable HTTPS addresses only."""

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import httpx


class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer", "header", "noscript", "svg"}:
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden = self.hidden[: self.hidden.index(tag)]

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


async def resolve(host):
    values = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(row[4][0] for row in values))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("Private or mixed DNS addresses are not allowed")
    return addresses[0]


async def read_page(url, *, resolver=resolve, transport=None):
    from .public_research import public_url

    async with asyncio.timeout(12):
        for _ in range(3):
            if not public_url(url):
                raise ValueError("A public HTTPS URL is required")
            parsed = urlparse(url)
            if any(
                key.lower()
                in {"token", "access_token", "code", "key", "auth", "password", "sig", "signature"}
                for key in parse_qs(parsed.query)
            ):
                raise ValueError("Credential-bearing URLs are not accepted")
            ip = await resolver(parsed.hostname)
            if not ipaddress.ip_address(ip).is_global:
                raise ValueError("Private address rejected")
            target = httpx.URL(url).copy_with(host=ip, fragment=None)
            # No proxy, cookie jar, bearer header, browser state or DNS re-resolution.
            # A new transport per hop keeps TLS host checks separate even on shared CDN IPs.
            async with httpx.AsyncClient(
                timeout=8, trust_env=False, follow_redirects=False, transport=transport
            ) as client:
                async with client.stream(
                    "GET",
                    target,
                    headers={
                        "Host": parsed.hostname,
                        "User-Agent": "JarvisPublicResearch/1.0",
                        "Accept": "text/html, text/plain",
                    },
                    extensions={"sni_hostname": parsed.hostname},
                ) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "").lower()
                    if not mime.startswith(("text/html", "text/plain")):
                        raise ValueError("Only public text and HTML are supported")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1_500_000:
                            raise ValueError("Page exceeds size limit")
                    content = data.decode("utf-8", errors="replace")
                    if mime.startswith("text/html"):
                        parser = ArticleText()
                        parser.feed(content)
                        content = " ".join(parser.parts)
                    if len(content.strip()) < 120 or any(
                        phrase in content.lower()[:1500]
                        for phrase in (
                            "verify you are human",
                            "enable javascript and cookies",
                            "unusual traffic",
                            "checking your browser",
                        )
                    ):
                        raise ValueError("Page requires interactive access or contains insufficient text")
                    return {"url": url, "excerpt": content[:7000], "coverage": "public_page_excerpt"}
        raise ValueError("Too many public redirects")
