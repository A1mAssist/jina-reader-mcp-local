# Handoff

更新日期：2026-10-08

## 项目目标与现状

本仓库提供 Windows 原生、按需启动的 stdio MCP 服务，用于本机 Agent 搜索公开网页，以及读取公开 HTML 和文本型 PDF。默认不依赖 Docker、WSL、自部署 Reader 或外部 SERP API Key，但网络请求仍会访问搜索引擎与目标站点。

入口在 `src/jina_reader_mcp/server.py`，由 `pyproject.toml` 中的 `jina-reader-mcp` 脚本启动。三个 MCP 工具是 `search_web`、`read_url`、`read_urls`。默认读取后端为 `native`；设置 `READER_BACKEND=reader` 才会调用自部署 Jina Reader HTTP 服务。搜索始终由本机 `ddgs` 发起。HTML 提取使用 Trafilatura，动态 HTML 使用本机 Chrome/Playwright，文本型 PDF 使用 pypdf。

通用安装与配置示例见 [README.md](README.md) 和 [README.zh-CN.md](README.zh-CN.md)。服务名建议使用 `local_web_reader`。本机 Codex 和 `agent-reach` skill 已各自配置了这个别名，但这些用户级配置不在 Git 仓库内；在另一台电脑上需要重新注册，且绝对路径必须指向那台电脑的仓库副本。

## 验证方式

在仓库根目录用 PowerShell 运行：

```powershell
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run jina-reader-check
```

单元测试覆盖 URL/DNS 防护、代理回退、总超时、搜索结果、文本型 PDF、批量读取和 Reader 后端等。`jina-reader-check` 是需要联网的端到端检查，会依次测试静态 HTML、PDF、浏览器模式和搜索；首次运行可能下载分词器词表。MCP 客户端还应做一次工具发现和实际 `read_url` / `search_web` 调用，不能只凭进程退出码判定成功。

2026-10-08 在 Windows/PowerShell 上验证：`uv sync --locked` 通过，17 个单元测试通过，`jina-reader-check` 的静态读取、PDF、浏览器读取和搜索全部通过，检测到已配置的代理。

## 已知边界

- 搜索结果来自外部搜索引擎；Google 模式可能遇到反爬。没有本地网页索引，也没有内置语义检索或本地重排模型。
- 默认只处理公开 HTML 与有文本层的 PDF。扫描件需 OCR，网页图片不会保留；动态 HTML 需要 Chrome。
- 本服务针对本机 Agent 使用。URL/DNS 检查降低私网访问风险，但 DNS 检查与实际连接之间存在变化窗口，不能作为公网暴露时的完整 SSRF 防护。
- 浏览器模式会按需启动 Chrome；搜索和读取的超时包含排队时间。若客户端也有工具超时，需将它设得高于服务端可能使用的超时。
- Codex 与 `mcporter` 各有自己的 MCP 配置，注册到一处不代表另一处已加载。在 PowerShell 中使用 `mcporter` 时，已验证的工具调用格式是 `mcporter call local_web_reader.read_url url=https://example.com`；函数式调用曾遇到解析错误，而解析错误可能仍返回退出码 0。

## 接手时先检查

1. `git status --short --branch`，保留工作区已有改动。
2. 运行上述单元测试，再按当前代理状态运行端到端检查。
3. 核对客户端的 MCP 启动命令、仓库绝对路径和服务名；客户端配置不随仓库同步。
4. 修改工具契约时，同步英文/中文 README、测试和 MCP 客户端调用示例。
