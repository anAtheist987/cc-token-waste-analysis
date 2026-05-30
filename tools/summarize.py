#!/usr/bin/env python3
"""
把 proxy.py 抓到的 capture.jsonl 汇总成一张表。

用法:
    python3 summarize.py                 # 读 ./capture.jsonl,打印表格
    python3 summarize.py /tmp/x.jsonl    # 指定日志
    python3 summarize.py --csv out.csv   # 同时导出 CSV
    python3 summarize.py --full          # 多打印一列「最后一条 user 消息预览」
"""
import csv
import json
import os
import sys


def text_len(content):
    """messages / system 的 content 可能是 str 或 [{type,text,...}]。返回字符数。"""
    if isinstance(content, str):
        return len(content)
    total = 0
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    total += len(b["text"])
                elif isinstance(b.get("content"), (str, list)):
                    total += text_len(b["content"])
    return total


def count_cache_control(body):
    """统计 cache_control 断点数量(system + tools + messages)。"""
    n = 0

    def scan_blocks(blocks):
        c = 0
        if isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and "cache_control" in b:
                    c += 1
        return c

    n += scan_blocks(body.get("system"))
    n += scan_blocks(body.get("tools"))
    for m in body.get("messages", []) or []:
        n += scan_blocks(m.get("content"))
    return n


def count_input_thinking(body):
    """统计被回传到输入里的 thinking 块数量(上一轮 reasoning)。"""
    n = 0
    for m in body.get("messages", []) or []:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"):
                    n += 1
    return n


def last_user_preview(messages, n=60):
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            s = c if isinstance(c, str) else ""
            if not s and isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        s = b["text"]
                        break
            s = " ".join(s.split())
            return (s[:n] + "…") if len(s) > n else s
    return ""


def num(v):
    return v if isinstance(v, (int, float)) else 0


def summarize(rec):
    body = rec.get("body")
    if not isinstance(body, dict):
        return None
    # 只关心 messages 类请求(带计费),跳过 count_tokens 等
    if "/v1/messages" not in rec.get("path", "") or rec.get("path", "").endswith("count_tokens"):
        # 仍保留,但标记
        pass
    msgs = body.get("messages", []) or []
    tools = body.get("tools", []) or []
    think = body.get("thinking")
    think_budget = ""
    if isinstance(think, dict) and think.get("type") == "enabled":
        think_budget = think.get("budget_tokens", "Y")
    resp = rec.get("response") or {}
    return {
        "seq": rec.get("seq", ""),
        "time": rec.get("time", ""),
        "path": rec.get("path", ""),
        "model": body.get("model", ""),
        "msgs": len(msgs),
        "tools": len(tools),
        "think": think_budget,
        "in_think": count_input_thinking(body),
        "cc": count_cache_control(body),
        "in_tok": resp.get("input_tokens", ""),
        "c_read": resp.get("cache_read_input_tokens", ""),
        "c_write": resp.get("cache_creation_input_tokens", ""),
        "out_tok": resp.get("output_tokens", ""),
        "status": rec.get("status", ""),
        "_preview": last_user_preview(msgs),
        "_resp": resp,
    }


def main():
    args = sys.argv[1:]
    csv_out = None
    full = False
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--csv":
            csv_out = args[i + 1]
            i += 2
        elif a == "--full":
            full = True
            i += 1
        else:
            path = a
            i += 1
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "capture.jsonl")

    if not os.path.exists(path):
        sys.exit(f"找不到日志文件: {path}(先跑 proxy.py 抓一些请求)")

    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            s = summarize(rec)
            if s:
                rows.append(s)

    if not rows:
        sys.exit("日志里没有可解析的 JSON 请求体。")

    cols = [
        ("seq", "#"), ("time", "时间"), ("model", "模型"),
        ("msgs", "消息数"), ("tools", "工具数"),
        ("think", "thinking预算"), ("in_think", "输入think块"),
        ("cc", "cache断点"),
        ("in_tok", "输入tok(全价)"), ("c_read", "cache读(10%)"),
        ("c_write", "cache写(125%)"), ("out_tok", "输出tok"),
    ]
    if full:
        cols.append(("_preview", "最后user消息"))

    def cell(v):
        return "" if v is None else str(v)

    # 列宽(按显示宽度,中文算 2)
    def disp_w(s):
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)

    widths = {}
    for key, title in cols:
        w = disp_w(title)
        for r in rows:
            w = max(w, disp_w(cell(r.get(key))))
        widths[key] = w

    def pad(s, w):
        return s + " " * (w - disp_w(s))

    header = " | ".join(pad(title, widths[key]) for key, title in cols)
    sep = "-+-".join("-" * widths[key] for key, _ in cols)
    print(header)
    print(sep)
    for r in rows:
        print(" | ".join(pad(cell(r.get(key)), widths[key]) for key, _ in cols))

    # 汇总行 + 计费结论
    print(sep)
    tot_in = sum(num(r["in_tok"]) for r in rows)
    tot_read = sum(num(r["c_read"]) for r in rows)
    tot_write = sum(num(r["c_write"]) for r in rows)
    tot_out = sum(num(r["out_tok"]) for r in rows)
    print(f"共 {len(rows)} 条请求")
    print(f"  输入 tok(全价)   合计 {tot_in:>10,}")
    print(f"  cache 读(10%价)  合计 {tot_read:>10,}   <- 重复前缀按这里计费,不是全价")
    print(f"  cache 写(125%价) 合计 {tot_write:>10,}")
    print(f"  输出 tok         合计 {tot_out:>10,}   <- 每轮 reasoning 在生成它的那一次按输出计费一次")
    print()
    print("解读:")
    print("  · 若同一轮里出现多条请求(seq 连续、消息数递增)= 每个 tool call 都是一次新请求。")
    print("  · 这些后续请求的 cache读 很大、输入tok(全价) 很小 = 重复前缀走 cache 读(10%),不是全价重读。")
    print("  · 某轮的 thinking 只在生成它的那条请求计入 输出tok;之后作为 输入think块 回传,")
    print("    若被 cache断点 覆盖则计入 cache读(10%),不会再次按输出计费。")

    if csv_out:
        with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            keys = [k for k, _ in cols]
            w.writerow([t for _, t in cols])
            for r in rows:
                w.writerow([cell(r.get(k)) for k in keys])
        print(f"\nCSV 已导出: {csv_out}")


if __name__ == "__main__":
    main()
