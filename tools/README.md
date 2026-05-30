# 抓取工具

用本地反向代理拦截本机 Claude Code 发往 Anthropic API 的请求,记录请求结构与响应 `usage`,
再汇总成表。无需安装 CA 证书(Claude Code → 本地为明文 HTTP)。

## 步骤

### 1. 启动代理(先在 `ANTHROPIC_BASE_URL` 仍指向默认上游时启动)
```bash
cd tools
python3 proxy.py
```
- 监听 `127.0.0.1:8082`,日志写入 `data/capture.jsonl`。
- 上游自动从 `~/.claude/settings.json` 的 `env.ANTHROPIC_BASE_URL` 读取(默认即
  `https://api.anthropic.com`),并缓存到 `data/upstream.txt`。脚本**只读 base_url,从不读取或打印任何 key**。

### 2. 让 Claude Code 走代理
将 `~/.claude/settings.json` 的 `env.ANTHROPIC_BASE_URL` 改为 `http://127.0.0.1:8082`
(仅改此项,鉴权 token 不动),或使用随附的 `switch_base_url.py proxy` / `restore`。
代理会把流量转发到第 1 步记住的上游。

### 3. 汇总
```bash
python3 summarize.py                 # 打印表格
python3 summarize.py --csv out.csv   # 导出 CSV
```

## 汇总表字段

| 列 | 含义 |
|----|------|
| `#` | 请求序号 |
| 消息数 | 该请求 `messages` 数组长度(每轮工具调用 +2) |
| thinking预算 | 是否开启 extended thinking 及 budget |
| 输入think块 | 回传到输入里的上一轮 thinking 块数 |
| cache断点 | `cache_control` 断点数 |
| 输入tok | `input_tokens` — 未命中缓存部分 |
| cache读 | `cache_read_input_tokens` — 命中前缀,0.1× 计费 |
| cache写 | `cache_creation_input_tokens` — 写缓存,5 分钟 1.25× |
| 输出tok | `output_tokens` |

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `LISTEN_HOST` / `LISTEN_PORT` | 127.0.0.1 / 8082 | 代理监听地址 |
| `LOG_FILE` | data/capture.jsonl | 日志路径 |
| `SETTINGS_FILE` | ~/.claude/settings.json | 从哪读 `env.ANTHROPIC_BASE_URL` |
| `UPSTREAM_BASE_URL` | (空) | 手动指定上游,优先于 settings.json |
| `REDACT` | 1 | 设 0 则不脱敏日志中的鉴权头 |
| `QUIET` | 0 | 设 1 则不在终端实时打印 |
