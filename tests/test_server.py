import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from jina_reader_mcp import server


class ValidationTests(unittest.TestCase):
    def test_accepts_public_url(self) -> None:
        self.assertEqual(server.validate_target_url("https://example.com/a"), "https://example.com/a")

    def test_rejects_local_targets(self) -> None:
        for url in ("http://localhost:8080", "http://127.0.0.1:8080", "http://192.168.1.10"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    server.validate_target_url(url)

    def test_headers_keep_output_small(self) -> None:
        headers = server._headers("browser", 6000, "main", None)
        self.assertEqual(headers["X-Max-Tokens"], "6000")
        self.assertEqual(headers["X-Engine"], "browser")
        self.assertEqual(headers["X-Target-Selector"], "main")
        self.assertEqual(headers["X-Retain-Images"], "none")


class FetchTests(unittest.IsolatedAsyncioTestCase):
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

        with patch("jina_reader_mcp.server.httpx.AsyncClient", return_value=ClientContext()):
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


if __name__ == "__main__":
    unittest.main()
