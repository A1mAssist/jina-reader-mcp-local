# Local Web Reader MCP

A Windows-native stdio MCP server for searching the web and reading public HTML pages. It runs only when an Agent starts it. No Docker, WSL, or external search API key is needed.

The original self-hosted Jina Reader HTTP adapter is still available with `READER_BACKEND=reader`. The native backend uses [ddgs](https://github.com/deedy5/ddgs) for search and [Trafilatura](https://github.com/adbar/trafilatura) for HTML extraction. Browser mode uses an installed Chrome through Playwright.

## Windows setup

Install [uv](https://docs.astral.sh/uv/) and run in PowerShell:

```powershell
uv sync
uv run python -m unittest discover -s tests -v
```

Configure your Agent to launch this checkout as a stdio MCP server:

```json
{
  "mcpServers": {
    "local-web-reader": {
      "command": "uv",
      "args": ["run", "--directory", "D:/Workspaces/PROJECTLESS_CODEX_TASKS/2026-10-08/jina", "jina-reader-mcp"]
    }
  }
}
```

Tools:

- `search_web`: automatic multi-engine search by default, 1-10 results. Google can be requested with `engine="google"`, but may return no parseable results due to its anti-bot page.
- `read_url`: extract Markdown from one public HTML page.
- `read_urls`: read up to two public HTML pages, reporting failures separately.

The native backend uses Windows proxy environment variables (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`) when present, or the Windows system proxy setting. Static extraction is the default; use `engine="browser"` for JavaScript-driven pages. Set `CHROME_PATH` if Chrome is installed outside `C:\Program Files\Google\Chrome\Application\chrome.exe`. Chrome starts for a browser-mode request and closes afterward.

Native mode supports HTML, with a 5 MB page limit. `max_tokens` is an approximate output cap in native mode. It blocks obvious local and private targets, including redirects, but is intended for a local Agent rather than a network-facing service.

## Optional Jina Reader backend

Set `READER_BACKEND=reader` and `READER_BASE_URL` to use an existing Jina Reader HTTP server for `read_url` and `read_urls`. `search_web` still runs natively on Windows.

| Variable | Default | Purpose |
|---|---|---|
| `READER_BACKEND` | `native` | `native` or `reader` for page reading |
| `READER_BASE_URL` | `http://127.0.0.1:3000` | Optional Jina Reader HTTP endpoint |
| `READER_API_KEY` | empty | Optional Bearer token for Reader backend |
| `READER_TIMEOUT_SECONDS` | `30` | Request timeout, 1-180 seconds |
| `READER_MAX_TOKENS` | `8000` | Output cap, 500-50000 |
| `READER_MAX_CONCURRENCY` | `1` | Concurrent requests, 1-4 |
| `CHROME_PATH` | system Chrome path | Browser-mode executable |

## License

MIT
