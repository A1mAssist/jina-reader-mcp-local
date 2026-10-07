from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field


Engine = Literal["auto", "curl", "browser"]
MAX_BATCH_SIZE = 2


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
    reader_base_url: str
    api_key: str
    timeout_seconds: int
    max_tokens: int
    max_concurrency: int

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = os.getenv("READER_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("READER_BASE_URL must be an http(s) URL")
        return cls(
            reader_base_url=base_url,
            api_key=os.getenv("READER_API_KEY", "").strip(),
            timeout_seconds=_env_int("READER_TIMEOUT_SECONDS", 30, 1, 180),
            max_tokens=_env_int("READER_MAX_TOKENS", 8000, 500, 50000),
            max_concurrency=_env_int("READER_MAX_CONCURRENCY", 1, 1, 4),
        )


settings = Settings.from_env()
semaphore = asyncio.Semaphore(settings.max_concurrency)
mcp = FastMCP(
    "local-jina-reader",
    instructions=(
        "Read public web pages through the local Jina Reader service. "
        "Use read_url only when web content is needed; prefer a target_selector "
        "for pages with a known content container."
    ),
)


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
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        raise ValueError("private, loopback, link-local, and reserved IPs are not allowed")

    # ponytail: block obvious local targets here and rely on Reader's existing SSRF guard;
    # add DNS pinning only if this adapter ever becomes a network-facing service.
    return url


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

    request_headers = _headers(engine, max_tokens, target_selector, wait_for_selector)
    async with semaphore:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            try:
                response = await client.post(
                    f"{settings.reader_base_url}/",
                    headers=request_headers,
                    json={"url": url, "timeout": timeout},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:500].replace("\n", " ")
                raise RuntimeError(f"Reader returned HTTP {exc.response.status_code}: {detail}") from exc
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Reader request failed: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        if isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, dict) and isinstance(data.get("content"), str):
                return data["content"]
        return str(payload)
    return response.text


@mcp.tool()
async def read_url(
    url: Annotated[str, Field(description="Absolute public http(s) URL to read.")],
    engine: Annotated[Engine, Field(description="Reader engine: auto, curl, or browser.")] = "auto",
    max_tokens: Annotated[int, Field(description="Maximum output size in Reader tokens.", ge=500, le=50000)] = settings.max_tokens,
    timeout: Annotated[int, Field(description="Maximum fetch time in seconds.", ge=1, le=180)] = settings.timeout_seconds,
    target_selector: Annotated[str | None, Field(description="Optional CSS selector for the main content.")] = None,
    wait_for_selector: Annotated[str | None, Field(description="Optional CSS selector to wait for before extraction.")] = None,
) -> str:
    """Fetch one public URL and return clean Markdown."""
    return await _fetch(url, engine, max_tokens, timeout, target_selector, wait_for_selector)


@mcp.tool()
async def read_urls(
    urls: Annotated[list[str], Field(description="One or two absolute public http(s) URLs to read.", min_length=1, max_length=MAX_BATCH_SIZE)],
    engine: Annotated[Engine, Field(description="Reader engine: auto, curl, or browser.")] = "auto",
    max_tokens: Annotated[int, Field(description="Maximum output size per URL in Reader tokens.", ge=500, le=50000)] = settings.max_tokens,
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


if __name__ == "__main__":
    main()
