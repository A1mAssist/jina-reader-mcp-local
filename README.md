# Local Jina Reader MCP

A small stdio MCP adapter for a self-hosted [Jina Reader](https://github.com/jina-ai/reader) instance.

The Reader service does the browser work and Markdown extraction. This project only exposes one safe, local MCP tool so an Agent can call it when web content is needed.

## Architecture

```text
Agent --stdio MCP--> jina-reader-mcp --HTTP--> Jina Reader OSS
```

The adapter defaults to `http://127.0.0.1:3000`, uses one in-flight fetch, limits the output to 8,000 Reader tokens, and rejects obvious local or private targets.

## Run the Reader backend

Docker is required for the upstream Reader image:

```powershell
docker run --rm -p 127.0.0.1:3000:8081 ghcr.io/jina-ai/reader:oss
```

Smoke-test it:

```powershell
curl.exe -X POST http://127.0.0.1:3000/ `
  -H "Accept: text/markdown" `
  -H "Content-Type: application/json" `
  -d '{"url":"https://example.com"}'
```

## Run the MCP adapter

```powershell
uv run --with . jina-reader-mcp
```

Or install it into a virtual environment:

```powershell
uv sync
uv run jina-reader-mcp
```

The MCP server uses stdio, so configure your Agent to launch `jina-reader-mcp` from this checkout. The tool is named `read_url`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `READER_BASE_URL` | `http://127.0.0.1:3000` | Local Reader HTTP base URL |
| `READER_API_KEY` | empty | Optional Bearer token for a protected Reader endpoint |
| `READER_TIMEOUT_SECONDS` | `30` | Default request timeout, 1-180 |
| `READER_MAX_TOKENS` | `8000` | Default Reader output cap, 500-50000 |
| `READER_MAX_CONCURRENCY` | `1` | Maximum concurrent fetches, 1-4 |

The first version intentionally exposes only `read_url`. Search, parallel fetches, caching, and arbitrary cookies or headers can be added after the single-page path is verified.

## Development

```powershell
uv sync
uv run python -m unittest discover -s tests -v
```

## License

MIT
