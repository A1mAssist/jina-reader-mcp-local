import asyncio
import json
import socket
import threading
import unittest
from dataclasses import replace
from io import BytesIO
from unittest.mock import AsyncMock, patch

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from jina_reader_mcp import server


def sample_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (Hello PDF) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class ValidationTests(unittest.TestCase):
    def test_proxy_falls_back_to_all_proxy(self) -> None:
        with patch("jina_reader_mcp.server.getproxies", return_value={"all": "http://127.0.0.1:7890"}):
            self.assertEqual(server.configured_proxy(), "http://127.0.0.1:7890")

    def test_accepts_public_url(self) -> None:
        self.assertEqual(server.validate_target_url("https://example.com/a"), "https://example.com/a")

    def test_rejects_local_targets(self) -> None:
        for url in ("http://localhost:8080", "http://127.0.0.1:8080", "http://192.168.1.10", "http://224.0.0.1"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    server.validate_target_url(url)

    def test_headers_keep_output_small(self) -> None:
        headers = server._headers("browser", 6000, "main", None)
        self.assertEqual(headers["X-Max-Tokens"], "6000")
        self.assertEqual(headers["X-Engine"], "browser")
        self.assertEqual(headers["X-Target-Selector"], "main")
        self.assertEqual(headers["X-Retain-Images"], "none")

    def test_token_limit_uses_tokenizer(self) -> None:
        content = "你好，web reader. " * 100
        limited = server.limit_tokens(content, 20)
        encoding = server.tiktoken.get_encoding("cl100k_base")
        self.assertLessEqual(len(encoding.encode_ordinary(limited)), 20)
        self.assertTrue(content.startswith(limited))


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_cache_rechecks_navigation_and_validates_every_url(self) -> None:
        checked_hosts = {}
        with patch("jina_reader_mcp.server.validate_public_destination", new=AsyncMock(return_value="ok")) as validate:
            await server.validate_browser_request("https://example.com/page", True, checked_hosts)
            await server.validate_browser_request("https://example.com/script.js", False, checked_hosts)
            self.assertEqual(validate.await_count, 1)
            await server.validate_browser_request("https://example.com/next", True, checked_hosts)
            self.assertEqual(validate.await_count, 2)
            with self.assertRaises(ValueError):
                await server.validate_browser_request("https://user:secret@example.com/script.js", False, checked_hosts)

    async def test_reads_text_pdf_and_caps_download(self) -> None:
        data = sample_pdf()
        client_type = httpx.AsyncClient
        mime = {"content-type": "application/pdf"}
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, content=data, headers=mime,
        ))
        public_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]
        with patch("jina_reader_mcp.server.socket.getaddrinfo", return_value=public_answer), patch(
            "jina_reader_mcp.server.getproxies", return_value={}
        ), patch(
            "jina_reader_mcp.server.httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(transport=transport, **kwargs),
        ):
            result = await server._fetch("https://example.com/document", "auto", 500, 30, None, None)
            self.assertIn("## Page 1\n\nHello PDF", result)
            mime["content-type"] = "application/octet-stream"
            result = await server._fetch("https://example.com/document.pdf", "auto", 500, 30, None, None)
            self.assertIn("Hello PDF", result)
            with patch.object(server, "MAX_PDF_BYTES", 100):
                with self.assertRaisesRegex(RuntimeError, "20 MB limit"):
                    await server._fetch("https://example.com/document.pdf", "auto", 500, 30, None, None)

    async def test_scan_only_pdf_has_clear_error(self) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        output = BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(RuntimeError, "need OCR"):
            server.extract_pdf_text(output.getvalue())

    async def test_rejects_domain_with_private_dns_answer(self) -> None:
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
        with patch("jina_reader_mcp.server.socket.getaddrinfo", return_value=answers):
            with self.assertRaisesRegex(ValueError, "non-public"):
                await server.validate_public_destination("https://example.com")

    async def test_proxy_dns_fallback_rejects_private_answer(self) -> None:
        def dns_response(request):
            if request.url.params["type"] == "A":
                return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 1, "data": "93.184.215.14"}]})
            return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 28, "data": "::1"}]})

        client_type = httpx.AsyncClient
        transport = httpx.MockTransport(dns_response)
        with patch("jina_reader_mcp.server.socket.getaddrinfo", side_effect=socket.gaierror), patch(
            "jina_reader_mcp.server.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ), patch(
            "jina_reader_mcp.server.httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(transport=transport, timeout=kwargs["timeout"]),
        ):
            with self.assertRaisesRegex(ValueError, "non-public"):
                await server.validate_public_destination("https://example.com")

    async def test_proxy_dns_failure_is_closed(self) -> None:
        client_type = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        with patch("jina_reader_mcp.server.socket.getaddrinfo", side_effect=socket.gaierror), patch(
            "jina_reader_mcp.server.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ), patch(
            "jina_reader_mcp.server.httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(transport=transport, timeout=kwargs["timeout"]),
        ):
            with self.assertRaisesRegex(ValueError, "Public DNS lookup failed"):
                await server.validate_public_destination("https://example.com")

    async def test_read_has_one_total_deadline(self) -> None:
        async def slow_fetch(*_args):
            await asyncio.sleep(2)
            return "late"

        with patch("jina_reader_mcp.server._fetch_impl", side_effect=slow_fetch):
            with self.assertRaisesRegex(RuntimeError, "total timeout"):
                await server._fetch("https://example.com", "auto", 8000, 1, None, None)

    async def test_fetches_markdown_from_reader(self) -> None:
        response = httpx.Response(
            200,
            text="# Example",
            headers={"content-type": "text/markdown"},
            request=httpx.Request("POST", "http://127.0.0.1:3000/"),
        )
        fake_client = AsyncMock()
        fake_client.post.return_value = response

        class ClientContext:
            async def __aenter__(self):
                return fake_client

            async def __aexit__(self, *_):
                return None

        with patch.object(server, "settings", replace(server.settings, backend="reader")), patch(
            "jina_reader_mcp.server.httpx.AsyncClient", return_value=ClientContext()
        ):
            result = await server._fetch("https://example.com", "auto", 8000, 30, None, None)

        self.assertEqual(result, "# Example")
        fake_client.post.assert_awaited_once()

    async def test_read_urls_keeps_per_url_results(self) -> None:
        async def fake_fetch(url, *_args):
            if url.endswith("/bad"):
                raise RuntimeError("Reader unavailable")
            return "# Good"

        with patch("jina_reader_mcp.server._fetch", new=AsyncMock(side_effect=fake_fetch)):
            result = json.loads(await server.read_urls(["https://example.com/good", "https://example.com/bad"]))

        self.assertEqual(result["results"][0], {"url": "https://example.com/good", "content": "# Good"})
        self.assertEqual(result["results"][1], {"url": "https://example.com/bad", "error": "Reader unavailable"})

    async def test_search_web_returns_links(self) -> None:
        with patch("jina_reader_mcp.server.getproxies", return_value={"https": "http://127.0.0.1:7890"}), patch(
            "jina_reader_mcp.server.DDGS"
        ) as ddgs:
            ddgs.return_value.text.return_value = [
                {"title": "Example", "href": "https://example.com", "body": "Example result"}
            ]
            result = json.loads(await server.search_web("example", count=1))

        self.assertEqual(result["results"][0]["url"], "https://example.com")
        self.assertEqual(ddgs.return_value.text.call_args.kwargs["backend"], "auto")
        self.assertEqual(ddgs.call_args.kwargs["proxy"], "http://127.0.0.1:7890")

    async def test_search_timeout_keeps_slot_until_thread_finishes(self) -> None:
        started = threading.Event()
        release = threading.Event()
        slot = asyncio.Semaphore(1)

        def slow_search(*_args, **_kwargs):
            started.set()
            release.wait(3)
            return [{"title": "Late", "href": "https://example.com", "body": "Late result"}]

        with patch.object(server, "settings", replace(server.settings, timeout_seconds=1)), patch.object(
            server, "semaphore", slot
        ), patch("jina_reader_mcp.server.DDGS") as ddgs:
            ddgs.return_value.text.side_effect = slow_search
            try:
                with self.assertRaisesRegex(RuntimeError, "total timeout"):
                    await server.search_web("slow", count=1)
                self.assertTrue(started.is_set())
                self.assertTrue(slot.locked())
            finally:
                release.set()
            await asyncio.wait_for(slot.acquire(), timeout=2)
            slot.release()

    async def test_native_reader_rejects_redirect_to_localhost(self) -> None:
        def redirect(request):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

        client_type = httpx.AsyncClient
        transport = httpx.MockTransport(redirect)
        public_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]
        with patch("jina_reader_mcp.server.socket.getaddrinfo", return_value=public_answer), patch(
            "jina_reader_mcp.server.getproxies", return_value={}
        ), patch(
            "jina_reader_mcp.server.httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(transport=transport, **kwargs),
        ):
            with self.assertRaises(ValueError):
                await server._native_fetch("https://example.com", "auto", 30, None, None)


if __name__ == "__main__":
    unittest.main()
