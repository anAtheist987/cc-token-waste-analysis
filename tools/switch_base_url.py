#!/usr/bin/env python3
"""
切换 ~/.claude/settings.json 里的 ANTHROPIC_BASE_URL,让 Claude Code 走/不走代理。

只针对 ANTHROPIC_BASE_URL 这一个值做就地正则替换,**不解析整份 JSON、不重排格式、
不读取也不打印任何 key**。首次运行会把原文件完整备份到 <settings>.capbak。

用法:
    python3 switch_base_url.py proxy     # 指向代理 http://127.0.0.1:8082
    python3 switch_base_url.py restore    # 从备份恢复原始文件
    python3 switch_base_url.py show       # 只打印当前 ANTHROPIC_BASE_URL 的值(非密钥)

可用环境变量:SETTINGS_FILE(默认 ~/.claude/settings.json)、
            PROXY_URL(默认 http://127.0.0.1:8082)
"""
import os
import re
import shutil
import sys

SETTINGS_FILE = os.path.expanduser(os.environ.get("SETTINGS_FILE", "~/.claude/settings.json"))
PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:8082")
BAK = SETTINGS_FILE + ".capbak"

PAT = re.compile(r'("ANTHROPIC_BASE_URL"\s*:\s*")([^"]*)(")')


def read():
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        return f.read()


def write(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        f.write(s)


def current_value(raw):
    m = PAT.search(raw)
    return m.group(2) if m else None


def set_value(new_url):
    raw = read()
    if not PAT.search(raw):
        sys.exit("没找到 ANTHROPIC_BASE_URL 字段,未做修改。")
    if not os.path.exists(BAK):
        shutil.copy2(SETTINGS_FILE, BAK)
        print(f"已备份原文件 -> {BAK}")
    new_raw, n = PAT.subn(lambda m: m.group(1) + new_url + m.group(3), raw)
    write(new_raw)
    print(f"ANTHROPIC_BASE_URL 已设为: {new_url}  (替换 {n} 处)")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "show"
    if action == "proxy":
        set_value(PROXY_URL)
        print("现在新开一个 session 运行 claude,流量会经过代理。")
    elif action == "restore":
        if not os.path.exists(BAK):
            sys.exit(f"没有备份 {BAK},无法恢复。")
        shutil.copy2(BAK, SETTINGS_FILE)
        print(f"已从备份恢复: {BAK} -> {SETTINGS_FILE}")
    elif action == "show":
        print("当前 ANTHROPIC_BASE_URL =", repr(current_value(read())))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
