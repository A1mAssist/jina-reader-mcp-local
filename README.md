# Local Web Reader MCP

[简体中文](README.zh-CN.md) | English

A Windows-native stdio MCP server for searching the web and reading public HTML pages and text PDFs. It runs only when an Agent starts it. No Docker, WSL, or external search API key is needed.

The original self-hosted Jina Reader HTTP adapter is still available with `READER_BACKEND=reader`. The native backend uses [ddgs](https://github.com/deedy5/ddgs) for search and [Trafilatura](https://github.com/adbar/trafilatura) for HTML extraction. Browser mode uses an installed Chrome through Playwright.

## Windows setup

Install [uv](https://docs.astral.sh/uv/) and run in PowerShell:

```powershell
uv sync --locked
uv run jina-reader-check
uv run python -m unittest discover -s tests -v
```

`jina-reader-check` preloads the tokenizer vocabulary, checks Chrome, and tests static HTML reading, PDF reading, browser reading, and search through the current proxy. It needs network access; the vocabulary may be downloaded on its first run.

Configure your Agent to launch this checkout as a stdio MCP server. Replace the example path with the absolute path to your checkout:

```json
{
  "mcpServers": {
    "local_web_reader": {
      "command": "uv",
      "args": ["run", "--directory", "D:/path/to/jina-reader-mcp-local", "jina-reader-mcp"]
    }
  }
}
```

If your Agent cannot find `uv`, set `command` to the absolute path of `uv.exe` and restart the client.

Tools:

- `search_web`: automatic multi-engine search by default, 1-10 results. Google can be requested with `engine="google"`, but may return no parseable results due to its anti-bot page.
- `read_url`: extract Markdown from one public HTML page or text PDF.
- `read_urls`: read up to two public HTML pages or text PDFs, reporting failures separately.

The native backend uses Windows proxy environment variables (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`) when present, or the Windows system proxy setting. Static extraction is the default; use `engine="browser"` for JavaScript-driven HTML pages. For PDFs, leave `engine="auto"`; they require a selectable text layer, and scanned PDFs need OCR. Set `CHROME_PATH` if Chrome is installed outside `C:\Program Files\Google\Chrome\Application\chrome.exe`. Chrome starts for a browser-mode request and closes afterward.

Native mode limits HTML to 5 MB and PDFs to 20 MB and 100 pages. `max_tokens` limits output using the `cl100k_base` tokenizer (other models may count tokens differently). The timeout is a total deadline for each read or search call, including queue time and redirects. URLs and their DNS answers are checked for private addresses; browser subresources reuse a per-page host check, while navigations are checked again. If Windows DNS cannot resolve a host while a proxy is configured, the server queries Cloudflare DNS through that proxy. This is a local Agent tool, not a network-facing SSRF sandbox: DNS can change between the check and the connection, especially through a proxy, and Chromium may follow a redirect before the next browser route check.

## Optional Jina Reader backend

Set `READER_BACKEND=reader` and `READER_BASE_URL` to use an existing Jina Reader HTTP server for `read_url` and `read_urls`. `search_web` still runs natively on Windows.

| Variable | Default | Purpose |
|---|---|---|
| `READER_BACKEND` | `native` | `native` or `reader` for page reading |
| `READER_BASE_URL` | `http://127.0.0.1:3000` | Optional Jina Reader HTTP endpoint |
| `READER_API_KEY` | empty | Optional Bearer token for Reader backend |
| `READER_TIMEOUT_SECONDS` | `30` | Total call timeout, 1-180 seconds |
| `READER_MAX_TOKENS` | `8000` | `cl100k_base` output cap, 500-50000 |
| `READER_MAX_CONCURRENCY` | `1` | Concurrent requests, 1-4 |
| `CHROME_PATH` | system Chrome path | Browser-mode executable |

## License

MIT
