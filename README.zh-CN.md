# Local Web Reader MCP

简体中文 | [English](README.md)

这是一个面向 Windows 的本地 stdio MCP 服务，供 Agent 搜索网页、读取公开 HTML 页面和有文本层的 PDF。Agent 调用时才启动，无需 Docker、WSL 或外部搜索 API Key；搜索和网页读取仍需要联网。

默认使用 Windows 原生后端：网页搜索由 [ddgs](https://github.com/deedy5/ddgs) 发起，HTML 正文由 [Trafilatura](https://github.com/adbar/trafilatura) 提取。JavaScript 页面可通过 Playwright 调用本机已安装的 Chrome。项目也保留了连接自部署 Jina Reader HTTP 服务的可选后端。

## Windows 安装

安装 [uv](https://docs.astral.sh/uv/)，然后在 PowerShell 中运行：

```powershell
git clone https://github.com/A1mAssist/jina-reader-mcp-local.git
Set-Location jina-reader-mcp-local
uv sync --locked
uv run jina-reader-check
uv run python -m unittest discover -s tests -v
```

`jina-reader-check` 会预加载分词器词表，检查 Chrome，并通过当前网络和代理测试静态 HTML、文本型 PDF、浏览器读取及搜索。首次运行可能需要下载词表。

## 接入 MCP 客户端

把下面的 `D:/path/to/jina-reader-mcp-local` 改成仓库在本机的**绝对路径**。Windows 路径在示例中使用正斜杠，避免 JSON/TOML 转义问题。

Codex 的 `~/.codex/config.toml` 可添加：

```toml
[mcp_servers.local_web_reader]
type = "stdio"
command = "uv"
args = ["run", "--directory", "D:/path/to/jina-reader-mcp-local", "jina-reader-mcp"]
startup_timeout_sec = 60
tool_timeout_sec = 180
```

其他使用 `mcpServers` JSON 配置的客户端可添加：

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

如果客户端找不到 `uv`，请将 `command` 改为 `uv.exe` 的绝对路径，并重启客户端。

保存配置后，按客户端要求重启或刷新 MCP 服务。服务名 `local_web_reader` 是客户端配置名；实际暴露三个工具：

| 工具 | 用途 |
|---|---|
| `search_web` | 默认自动选择搜索后端，返回 1–10 条标题、链接和摘要；可指定 `engine="google"`，但 Google 反爬页面可能导致没有可解析结果 |
| `read_url` | 把单个公开 HTML 页面或文本型 PDF 提取为 Markdown |
| `read_urls` | 一次读取最多两个 URL，分别返回内容或错误 |

例如，已将服务注册到 `mcporter` 时，可在 PowerShell 中调用：

```powershell
mcporter call local_web_reader.search_web 'query=example domain' count=2
mcporter call local_web_reader.read_url url=https://example.com
```

## 网络、代理与读取方式

原生后端会使用 `HTTPS_PROXY`、`HTTP_PROXY`、`ALL_PROXY` 等环境变量，或 Windows 系统代理。默认采用静态读取；JavaScript 驱动的 HTML 页面可传 `engine="browser"`。浏览器仅在该次请求期间启动，用完关闭。如果 Chrome 不在默认的 `C:\Program Files\Google\Chrome\Application\chrome.exe`，请设置 `CHROME_PATH`。

PDF 请使用默认的 `engine="auto"`。它必须有可选择的文本层；扫描件需要另行 OCR。原生模式限制 HTML 为 5 MB，PDF 为 20 MB、100 页。`max_tokens` 通过 `cl100k_base` 限制输出，其他模型的 token 数可能不同。读取和搜索的超时覆盖排队、跳转及实际请求的总时间。

工具会校验 URL 和 DNS 结果，拒绝访问私有地址。当 Windows DNS 无法解析而已配置代理时，会通过代理查询 Cloudflare DNS。它只适合本机 Agent 使用，并不是面向不可信网络的完整 SSRF 隔离边界：DNS 校验与连接之间仍可能变化，代理和浏览器跳转也会影响这一边界。

## 可选：自部署 Jina Reader

将 `READER_BACKEND=reader`，并把 `READER_BASE_URL` 指向已有的 Jina Reader HTTP 服务后，`read_url` 和 `read_urls` 会走该服务；`search_web` 仍从本机发起。

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `READER_BACKEND` | `native` | 页面读取后端：`native` 或 `reader` |
| `READER_BASE_URL` | `http://127.0.0.1:3000` | 可选 Reader HTTP 地址 |
| `READER_API_KEY` | 空 | Reader 后端可选 Bearer token |
| `READER_TIMEOUT_SECONDS` | `30` | 单次调用总超时，1–180 秒 |
| `READER_MAX_TOKENS` | `8000` | `cl100k_base` 输出上限，500–50000 |
| `READER_MAX_CONCURRENCY` | `1` | 并发请求数，1–4 |
| `CHROME_PATH` | 系统 Chrome 路径 | 浏览器模式的可执行文件 |

## 当前边界

- 搜索依赖外部搜索引擎及其反爬状态；本项目没有独立网页索引。
- MCP 没有内置语义检索或本地重排模型。Agent 可以对已返回的候选结果再次筛选，但无法找回搜索引擎未返回的页面。
- 不保留网页图片；扫描 PDF、登录后页面等场景不在默认能力范围内。

## 许可证

MIT，见 [LICENSE](LICENSE)。
