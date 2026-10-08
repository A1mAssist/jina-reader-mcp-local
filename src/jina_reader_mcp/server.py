from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from urllib.request import getproxies

import httpx
import tiktoken
import trafilatura
from ddgs import DDGS
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from pydantic import Field


Engine = Literal["auto", "curl", "browser"]
SearchEngine = Literal["auto", "google"]
MAX_BATCH_SIZE = 2
MAX_HTML_BYTES = 5_000_000
MAX_PDF_BYTES = 20_000_000
MAX_PDF_PAGES = 100


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    backend: str
    reader_base_url: str
    api_key: str
    timeout_seconds: int
    max_tokens: int
    max_concurrency: int
    chrome_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = os.getenv("READER_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("READER_BASE_URL must be an http(s) URL")
        backend = os.getenv("READER_BACKEND", "native").strip().lower()
        if backend not in {"native", "reader"}:
            raise RuntimeError("READER_BACKEND must be native or reader")
        return cls(
            backend=backend,
            reader_base_url=base_url,
            api_key=os.getenv("READER_API_KEY", "").strip(),
            timeout_seconds=_env_int("READER_TIMEOUT_SECONDS", 30, 1, 180),
            max_tokens=_env_int("READER_MAX_TOKENS", 8000, 500, 50000),
            max_concurrency=_env_int("READER_MAX_CONCURRENCY", 1, 1, 4),
            chrome_path=os.getenv("CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        )


settings = Settings.from_env()
semaphore = asyncio.Semaphore(settings.max_concurrency)
mcp = FastMCP(
    "local-web-reader",
    instructions=(
        "Search the web and read public pages from this computer. "
        "Use search_web for discovery and read_url for page content."
    ),
)


def configured_proxy() -> str | None:
    proxies = getproxies()
    return proxies.get("https") or proxies.get("http") or proxies.get("all")


def validate_target_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("url must not contain embedded credentials")

    hostname = parsed.hostname.rstrip(".").lower()
    if (
        hostname in {"localhost", "localhost.localdomain"}
        or hostname.endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError("local and internal hostnames are not allowed")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (not address.is_global or address.is_multicast):
        raise ValueError("non-public IPs are not allowed")

    return url


async def validate_public_destination(url: str) -> str:
    validate_target_url(url)
    parsed = urlparse(url)
    hostname = parsed.hostname
    assert hostname is not None
    try:
        answers = await asyncio.to_thread(
            socket.getaddrinfo, hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        addresses = [entry[4][0] for entry in answers]
    except OSError as exc:
        proxy = configured_proxy()
        if not proxy:
            raise ValueError(f"Could not resolve hostname: {hostname}") from exc
        addresses = await _resolve_public_dns(hostname, proxy)
    if not addresses:
        raise ValueError("host resolves to a non-public IP address")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global or address.is_multicast:
            raise ValueError("host resolves to a non-public IP address")
    return url


async def validate_browser_request(
    url: str, navigation: bool, checked_hosts: dict[str, asyncio.Task[str]],
) -> None:
    validate_target_url(url)
    hostname = urlparse(url).hostname
    assert hostname is not None
    if navigation or hostname not in checked_hosts:
        checked_hosts[hostname] = asyncio.create_task(validate_public_destination(url))
    await checked_hosts[hostname]


async def _resolve_public_dns(hostname: str, proxy: str) -> list[str]:
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=5) as client:
            responses = await asyncio.gather(*(
                client.get(
                    "https://cloudflare-dns.com/dns-query",
                    params={"name": hostname, "type": record_type},
                    headers={"Accept": "application/dns-json"},
                )
                for record_type in ("A", "AAAA")
            ))
            addresses = []
            for response in responses:
                response.raise_for_status()
                payload = response.json()
                if payload.get("Status") != 0:
                    raise ValueError(f"Public DNS could not resolve hostname: {hostname}")
                addresses.extend(
                    answer["data"] for answer in payload.get("Answer", [])
                    if answer.get("type") in (1, 28)
                )
            return addresses
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Public DNS lookup failed for hostname: {hostname}") from exc


def limit_tokens(content: str, max_tokens: int) -> str:
    encoding = tiktoken.get_encoding("cl100k_base")
    tokens = encoding.encode_ordinary(content)
    if len(tokens) <= max_tokens:
        return content
    return encoding.decode_bytes(tokens[:max_tokens]).decode("utf-8", errors="ignore")


def extract_pdf_text(data: bytes) -> str:
    if b"%PDF-" not in data[:1024]:
        raise RuntimeError("Response is not a PDF")
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise RuntimeError("Password-protected PDFs are not supported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise RuntimeError(f"PDF exceeds {MAX_PDF_PAGES} page limit")
        pages = [
            f"## Page {number}\n\n{text.strip()}"
            for number, page in enumerate(reader.pages, 1)
            if (text := page.extract_text()) and text.strip()
        ]
    except PdfReadError as exc:
        raise RuntimeError(f"Could not read PDF: {exc}") from exc
    if not pages:
        raise RuntimeError("PDF has no selectable text; scanned PDFs need OCR")
    return "\n\n".join(pages)


def _headers(
    engine: Engine,
    max_tokens: int,
    target_selector: str | None,
    wait_for_selector: str | None,
) -> dict[str, str]:
    headers = {
        "Accept": "text/markdown",
        "X-Max-Tokens": str(max_tokens),
        "X-Retain-Images": "none",
        "X-Retain-Links": "text",
    }
    if engine != "auto":
        headers["X-Engine"] = engine
    if target_selector:
        headers["X-Target-Selector"] = target_selector
    if wait_for_selector:
        headers["X-Wait-For-Selector"] = wait_for_selector
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    return headers


async def _fetch(
    url: str,
    engine: Engine,
    max_tokens: int,
    timeout: int,
    target_selector: str | None,
    wait_for_selector: str | None,
) -> str:
    validate_target_url(url)
    if not 500 <= max_tokens <= 50000:
        raise ValueError("max_tokens must be between 500 and 50000")
    if not 1 <= timeout <= 180:
        raise ValueError("timeout must be between 1 and 180 seconds")

    try:
        return await asyncio.wait_for(
            _fetch_impl(url, engine, max_tokens, timeout, target_selector, wait_for_selector),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Web read exceeded {timeout} second total timeout") from exc


async def _fetch_impl(
    url: str, engine: Engine, max_tokens: int, timeout: int,
    target_selector: str | None, wait_for_selector: str | None,
) -> str:
    async with semaphore:
        try:
            if settings.backend == "native":
                content = await _native_fetch(url, engine, timeout, target_selector, wait_for_selector)
            else:
                request_headers = _headers(engine, max_tokens, target_selector, wait_for_selector)
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    response = await client.post(
                        f"{settings.reader_base_url}/",
                        headers=request_headers,
                        json={"url": url, "timeout": timeout},
                    )
                    response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    payload = response.json()
                    data = payload.get("data", payload) if isinstance(payload, dict) else payload
                    content = data["content"] if isinstance(data, dict) and isinstance(data.get("content"), str) else str(payload)
                else:
                    content = response.text
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500].replace("\n", " ")
            raise RuntimeError(f"Web server returned HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Web request failed: {exc}") from exc
        return await asyncio.to_thread(limit_tokens, content, max_tokens)


async def _native_fetch(
    url: str, engine: Engine, timeout: int,
    target_selector: str | None, wait_for_selector: str | None,
) -> str:
    await validate_public_destination(url)
    if engine == "browser" or target_selector or wait_for_selector:
        chrome = Path(settings.chrome_path)
        if not chrome.is_file():
            raise RuntimeError(f"Chrome executable not found: {chrome}. Set CHROME_PATH.")
        async with async_playwright() as playwright:
            proxy = configured_proxy()
            browser = await playwright.chromium.launch(
                executable_path=str(chrome), headless=True,
                proxy={"server": proxy} if proxy else None,
            )
            try:
                context = await browser.new_context(service_workers="block")
                page = await context.new_page()
                checked_hosts: dict[str, asyncio.Task[str]] = {}
                async def allow_public(route):
                    try:
                        await validate_browser_request(
                            route.request.url, route.request.is_navigation_request(), checked_hosts,
                        )
                    except ValueError:
                        await route.abort()
                    else:
                        await route.continue_()
                await page.route("**/*", allow_public)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                if response and response.status >= 400:
                    raise RuntimeError(f"Web server returned HTTP {response.status}")
                await validate_public_destination(page.url)
                if wait_for_selector:
                    await page.locator(wait_for_selector).wait_for(timeout=timeout * 1000)
                html = await (page.locator(target_selector).first.inner_html() if target_selector else page.content())
                if len(html.encode("utf-8")) > MAX_HTML_BYTES:
                    raise RuntimeError("HTML page exceeds 5 MB limit")
                final_url = page.url
            finally:
                await browser.close()
    else:
        proxy = configured_proxy()
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, proxy=proxy) as client:
            for _ in range(6):
                async with client.stream("GET", url, headers={"Accept": "text/html,application/xhtml+xml"}) as response:
                    if response.is_redirect:
                        url = str(response.next_request.url)
                        await validate_public_destination(url)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    is_pdf = "application/pdf" in content_type or (
                        urlparse(url).path.lower().endswith(".pdf") and "html" not in content_type
                    )
                    if not is_pdf and "html" not in content_type:
                        raise RuntimeError("Native reader supports HTML and PDF pages only")
                    max_bytes = MAX_PDF_BYTES if is_pdf else MAX_HTML_BYTES
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > max_bytes:
                            raise RuntimeError("PDF exceeds 20 MB limit" if is_pdf else "HTML page exceeds 5 MB limit")
                    if is_pdf:
                        return await asyncio.to_thread(extract_pdf_text, bytes(chunks))
                    html = chunks.decode(response.encoding or "utf-8", errors="replace")
                    final_url = url
                    break
            else:
                raise RuntimeError("Too many redirects")

    content = await asyncio.to_thread(
        trafilatura.extract, html, url=final_url, output_format="markdown", include_links=True,
    )
    if not content:
        raise RuntimeError("No readable content found; try engine='browser'")
    return content


@mcp.tool()
async def search_web(
    query: Annotated[str, Field(description="Web search query, 1-300 characters.", min_length=1, max_length=300)],
    count: Annotated[int, Field(description="Maximum results.", ge=1, le=10)] = 5,
    engine: Annotated[SearchEngine, Field(description="Automatic multi-engine search or Google only.")] = "auto",
) -> str:
    """Search from this computer and return titles, links and snippets."""
    if not query.strip() or len(query) > 300:
        raise ValueError("query must contain 1-300 characters")
    if not 1 <= count <= 10:
        raise ValueError("count must be between 1 and 10")
    if engine not in {"auto", "google"}:
        raise ValueError("engine must be auto or google")

    def run_search() -> list[dict[str, str]]:
        proxy = configured_proxy()
        results = DDGS(proxy=proxy, timeout=settings.timeout_seconds).text(
            query, max_results=count, backend=engine,
        )
        return [
            {"title": item.get("title", ""), "url": item.get("href", ""), "snippet": item.get("body", "")}
            for item in results[:count]
        ]

    async def perform_search() -> list[dict[str, str]]:
        await semaphore.acquire()
        task = asyncio.create_task(asyncio.to_thread(run_search))

        def release_slot(done: asyncio.Task) -> None:
            semaphore.release()
            if not done.cancelled():
                done.exception()

        task.add_done_callback(release_slot)
        return await asyncio.shield(task)

    try:
        results = await asyncio.wait_for(perform_search(), timeout=settings.timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Search exceeded {settings.timeout_seconds} second total timeout") from exc
    except Exception as exc:
        raise RuntimeError(f"{engine} search failed: {exc}") from exc
    if not results:
        raise RuntimeError(f"{engine} returned no results")
    return json.dumps({"results": results}, ensure_ascii=False)


@mcp.tool()
async def read_url(
    url: Annotated[str, Field(description="Absolute public http(s) URL to read.")],
    engine: Annotated[Engine, Field(description="Reader engine: auto or curl for HTML/PDF; browser for HTML only.")] = "auto",
    max_tokens: Annotated[int, Field(description="Maximum output size in cl100k_base tokens.", ge=500, le=50000)] = settings.max_tokens,
    timeout: Annotated[int, Field(description="Maximum fetch time in seconds.", ge=1, le=180)] = settings.timeout_seconds,
    target_selector: Annotated[str | None, Field(description="Optional CSS selector for the main content.")] = None,
    wait_for_selector: Annotated[str | None, Field(description="Optional CSS selector to wait for before extraction.")] = None,
) -> str:
    """Fetch one public URL and return clean Markdown."""
    return await _fetch(url, engine, max_tokens, timeout, target_selector, wait_for_selector)


@mcp.tool()
async def read_urls(
    urls: Annotated[list[str], Field(description="One or two absolute public http(s) URLs to read.", min_length=1, max_length=MAX_BATCH_SIZE)],
    engine: Annotated[Engine, Field(description="Reader engine: auto or curl for HTML/PDF; browser for HTML only.")] = "auto",
    max_tokens: Annotated[int, Field(description="Maximum output size per URL in cl100k_base tokens.", ge=500, le=50000)] = settings.max_tokens,
    timeout: Annotated[int, Field(description="Maximum fetch time per URL in seconds.", ge=1, le=180)] = settings.timeout_seconds,
    target_selector: Annotated[str | None, Field(description="Optional CSS selector for the main content on every URL.")] = None,
    wait_for_selector: Annotated[str | None, Field(description="Optional CSS selector to wait for on every URL.")] = None,
) -> str:
    """Fetch up to two public URLs and return a JSON result for each URL."""
    results = await asyncio.gather(
        *(_fetch(url, engine, max_tokens, timeout, target_selector, wait_for_selector) for url in urls),
        return_exceptions=True,
    )
    payload = []
    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            payload.append({"url": url, "error": str(result)})
        else:
            payload.append({"url": url, "content": result})
    return json.dumps({"results": payload}, ensure_ascii=False)


def main() -> None:
    mcp.run(transport="stdio")


def check() -> None:
    async def run() -> None:
        print(f"Proxy: {'configured' if configured_proxy() else 'direct'}")
        await asyncio.to_thread(tiktoken.get_encoding, "cl100k_base")
        print("Tokenizer: OK")
        if settings.backend == "native" and not Path(settings.chrome_path).is_file():
            raise RuntimeError(f"Chrome not found at {settings.chrome_path}; set CHROME_PATH")
        await read_url("https://example.com", max_tokens=500)
        print("Static read: OK")
        await read_url(
            "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
            max_tokens=500,
        )
        print("PDF read: OK")
        if settings.backend == "native":
            await read_url("https://example.com", engine="browser", max_tokens=500)
            print("Browser read: OK")
        await search_web("example domain", count=1)
        print("Search: OK")

    try:
        asyncio.run(run())
    except Exception as exc:
        raise SystemExit(f"Check failed: {exc}") from exc


if __name__ == "__main__":
    main()
