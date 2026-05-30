#!/usr/bin/env python3
"""
Claude Code 请求体抓取反向代理。

把 Claude Code 指向本代理后,所有对模型的调用都会先打到这里。本代理:
  1. 把请求体(JSON)逐条记录到 JSONL 日志;
  2. 转发到「真正的上游」——自动从 ~/.claude/settings.json 的
     env.ANTHROPIC_BASE_URL 读取(含 SSE 流式响应);
  3. 把响应原样回传给 Claude Code,同时解析其中的 usage(计费)。

鉴权:Claude Code 会把 settings.json 里的 token 作为请求头(Authorization /
x-api-key)自带过来,代理原样转发即可。**本脚本只读取 env.ANTHROPIC_BASE_URL
一个字段,从不读取也从不打印任何 key。**

不需要任何 CA 证书:Claude Code -> 本地是明文 HTTP,本地 -> 上游才是 HTTPS。

用法:
    python3 proxy.py                          # 上游自动从 settings.json 解析
    UPSTREAM_BASE_URL=https://x/y python3 proxy.py   # 手动指定上游(优先)
    SETTINGS_FILE=/path/settings.json python3 proxy.py
"""
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8082"))
SETTINGS_FILE = os.path.expanduser(os.environ.get("SETTINGS_FILE", "~/.claude/settings.json"))
UPSTREAM_OVERRIDE = os.environ.get("UPSTREAM_BASE_URL", "").strip()
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_SCRIPT_DIR, "data")
_CACHE_FILE = os.path.join(_DATA_DIR, "upstream.txt")
LOG_FILE = os.environ.get("LOG_FILE", os.path.join(_DATA_DIR, "capture.jsonl"))
# 解析出的真实上游(在 main() 里填充)
UP_SCHEME = "https"
UP_HOST = ""
UP_PORT = 443
UP_BASE = ""
# 默认脱敏鉴权头;想原样记录就设 REDACT=0
REDACT = os.environ.get("REDACT", "1") != "0"
# 默认在终端实时打印抓到的内容;设 QUIET=1 关闭
QUIET = os.environ.get("QUIET", "0") == "1"

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}
SECRET_HEADERS = {"authorization", "x-api-key", "proxy-authorization"}

_log_lock = threading.Lock()
_seq_lock = threading.Lock()
_seq = [0]


def next_seq():
    with _seq_lock:
        _seq[0] += 1
        return _seq[0]


def _loose_json(raw):
    """容忍 // 注释、/* */ 注释、尾随逗号的 JSON 解析。"""
    try:
        return json.loads(raw)
    except Exception:
        pass
    s = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    s = re.sub(r"(^|\s)//[^\n]*", r"\1", s)
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return json.loads(s)


def _read_settings_base_url():
    """仅读取 settings.json 里的 env.ANTHROPIC_BASE_URL,绝不读取/返回任何 key。"""
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        data = _loose_json(f.read())
    if not isinstance(data, dict):
        return None
    env = data.get("env") or {}
    if isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL"):
        return str(env["ANTHROPIC_BASE_URL"]).strip()
    return None


def _parse_base(url):
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme or "https"
    host = u.hostname or ""
    port = u.port or (443 if scheme == "https" else 80)
    base_path = (u.path or "").rstrip("/")
    return scheme, host, port, base_path


def _is_self(host, port):
    return host in {LISTEN_HOST, "127.0.0.1", "localhost", "0.0.0.0"} and int(port) == int(LISTEN_PORT)


def resolve_upstream():
    """确定真实上游。优先级:UPSTREAM_BASE_URL 环境变量 > settings.json > 缓存。
    若 settings.json 已被指向本代理(回环),自动回退到缓存。"""
    url, src = None, None
    if UPSTREAM_OVERRIDE:
        url, src = UPSTREAM_OVERRIDE, "UPSTREAM_BASE_URL 环境变量"
    else:
        try:
            u = _read_settings_base_url()
            if u:
                sc, h, p, _ = _parse_base(u)
                if _is_self(h, p):
                    sys.stderr.write("[proxy] settings.json 的 ANTHROPIC_BASE_URL 已指向本代理,改用缓存的真实上游\n")
                else:
                    url, src = u, f"{SETTINGS_FILE}"
        except FileNotFoundError:
            sys.stderr.write(f"[proxy] 未找到 {SETTINGS_FILE}\n")
        except Exception as e:
            sys.stderr.write(f"[proxy] 解析 settings.json 失败: {e}\n")
    if not url and os.path.exists(_CACHE_FILE):
        with open(_CACHE_FILE, encoding="utf-8") as f:
            url = f.read().strip()
        src = f"{_CACHE_FILE}(缓存)"
    if not url:
        sys.exit(
            "无法确定上游。请任选其一:\n"
            "  1) 先在 settings.json 仍指向默认上游(api.anthropic.com)时启动本代理(会自动读取并缓存);\n"
            "  2) 启动时显式指定:UPSTREAM_BASE_URL=https://api.anthropic.com python3 proxy.py"
        )
    sc, h, p, bp = _parse_base(url)
    if not _is_self(h, p):
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(f"{sc}://{h}:{p}{bp}")
        except Exception:
            pass
    return sc, h, p, bp, src


def extract_usage(raw):
    """从响应字节里抽取 usage(计费关键)。支持 SSE 流式与普通 JSON。"""
    out = {}
    if not raw:
        return out
    text = raw.decode("utf-8", "replace")

    def take(usage, final=False):
        if not isinstance(usage, dict):
            return
        for k in ("input_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens"):
            if usage.get(k) is not None:
                out[k] = usage[k]
        # output_tokens 以最后出现(message_delta)的为准
        if usage.get("output_tokens") is not None:
            out["output_tokens"] = usage["output_tokens"]

    # SSE:逐个 data: 行解析
    found_sse = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        found_sse = True
        t = ev.get("type")
        if t == "message_start":
            take(ev.get("message", {}).get("usage", {}))
        elif t == "message_delta":
            take(ev.get("usage", {}), final=True)

    # 普通 JSON 响应(如非流式 messages)
    if not found_sse:
        try:
            obj = json.loads(text)
            take(obj.get("usage", {}))
        except Exception:
            pass
    return out


def _body_brief(body):
    msgs = body.get("messages", []) or []
    tools = body.get("tools", []) or []
    think = body.get("thinking")
    tb = ""
    if isinstance(think, dict) and think.get("type") == "enabled":
        tb = f" think={think.get('budget_tokens', 'on')}"
    return f"model={body.get('model', '?')} msgs={len(msgs)} tools={len(tools)}{tb}"


def console_request(rec):
    if QUIET:
        return
    body = rec.get("body")
    brief = _body_brief(body) if isinstance(body, dict) else "(non-JSON body)"
    with _log_lock:
        print(f"\n[#{rec['seq']:>3}] → {rec['method']} {rec['path']}  {brief}", flush=True)


def console_response(rec):
    if QUIET:
        return
    u = rec.get("response") or {}
    st = rec.get("status", "?")
    parts = [
        f"in={u.get('input_tokens', '-')}",
        f"cache_read={u.get('cache_read_input_tokens', '-')}",
        f"cache_write={u.get('cache_creation_input_tokens', '-')}",
        f"out={u.get('output_tokens', '-')}",
    ]
    with _log_lock:
        print(f"[#{rec['seq']:>3}] ← {st}  usage: " + "  ".join(parts), flush=True)


def log_record(rec):
    line = json.dumps(rec, ensure_ascii=False)
    with _log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音默认访问日志
        pass

    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        # ---- 记录请求 ----
        headers_logged = {}
        for k, v in self.headers.items():
            if REDACT and k.lower() in SECRET_HEADERS:
                headers_logged[k] = "***REDACTED***"
            else:
                headers_logged[k] = v
        rec = {
            "seq": next_seq(),
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "method": self.command,
            "path": self.path,
            "headers": headers_logged,
        }
        try:
            rec["body"] = json.loads(body.decode("utf-8"))
        except Exception:
            rec["body_text"] = body.decode("utf-8", "replace") if body else ""

        console_request(rec)

        # ---- 转发(剥掉 accept-encoding,保证响应不压缩,便于解析 usage)----
        # 鉴权头由 Claude Code 自带,这里原样转发,不注入任何 key。
        fwd_headers = []
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in HOP_BY_HOP or kl == "accept-encoding":
                continue
            fwd_headers.append((k, v))
        fwd_headers.append(("Host", UP_HOST))

        # 把上游的 base path 前缀拼到 Claude Code 发来的路径前
        fwd_path = UP_BASE + self.path

        try:
            if UP_SCHEME == "https":
                conn = http.client.HTTPSConnection(
                    UP_HOST, UP_PORT, timeout=600,
                    context=ssl.create_default_context(),
                )
            else:
                conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=600)
            conn.putrequest(self.command, fwd_path, skip_host=True, skip_accept_encoding=True)
            for k, v in fwd_headers:
                conn.putheader(k, v)
            conn.endheaders(body if body else None)
            resp = conn.getresponse()
        except Exception as e:
            sys.stderr.write(f"[proxy] upstream error: {e}\n")
            rec["error"] = str(e)
            self._safe_log(rec)
            self.send_error(502, f"upstream error: {e}")
            return

        # ---- 回传(支持 SSE 流式),同时缓存响应文本以解析 usage ----
        up_headers = resp.getheaders()
        has_len = any(k.lower() == "content-length" for k, _ in up_headers)
        rec["status"] = resp.status
        self.send_response(resp.status, resp.reason)
        for k, v in up_headers:
            kl = k.lower()
            if kl in ("transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        if not has_len:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        captured = bytearray()
        CAP = 16 * 1024 * 1024  # 最多缓存 16MB 用于解析,超过不再累积
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                if len(captured) < CAP:
                    captured += chunk
                if has_len:
                    self.wfile.write(chunk)
                else:
                    self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            if not has_len:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

        rec["response"] = extract_usage(bytes(captured))
        console_response(rec)
        self._safe_log(rec)

    def _safe_log(self, rec):
        try:
            log_record(rec)
        except Exception as e:
            sys.stderr.write(f"[proxy] log error: {e}\n")

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy


def main():
    global UP_SCHEME, UP_HOST, UP_PORT, UP_BASE
    os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)
    UP_SCHEME, UP_HOST, UP_PORT, UP_BASE, src = resolve_upstream()

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"[proxy] listening  http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"[proxy] upstream   {UP_SCHEME}://{UP_HOST}:{UP_PORT}{UP_BASE}")
    print(f"[proxy] upstream 来源: {src}")
    print(f"[proxy] log file   {LOG_FILE}")
    print(f"[proxy] 鉴权头脱敏: {'开' if REDACT else '关'}(只读 base_url,从不读 key)")
    print(f"\n让 Claude Code 走代理:把 {SETTINGS_FILE} 里 env.ANTHROPIC_BASE_URL")
    print(f"改为  http://{LISTEN_HOST}:{LISTEN_PORT}  然后新开一个 session 跑 claude。")
    print(f"(本代理已在内存里记住真实上游,改完不影响转发)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[proxy] 停止")


if __name__ == "__main__":
    main()
