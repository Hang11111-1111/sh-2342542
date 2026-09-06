#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本地已登录的饿了么 Chrome(CDP 9222)抽取 cookie, 供云端 GitHub Actions 使用。

前提: 你的 Mac 上监控用的 Chrome 正在运行并开启了远程调试(9222 端口), 且已登录饿了么。
用法:
    python extract_cookies.py
会生成 eleme_cookies.json —— 把它的【完整内容】粘进 GitHub Secret: ELEME_COOKIES。

注意:
  - 该脚本只在本地、且 Chrome 9222 在线时可用。
  - 饿了么 cookie 会过期, 过期后云端会抓不到/跳登录; 届时重跑本脚本并更新 Secret 即可。
"""
import json
import re
from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"


def main():
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP, timeout=30000)
        ctx = b.contexts[0]
        cookies = ctx.cookies()
        pat = re.compile(r"ele\.me|taobao|tmall|alimama|alipay|alicdn|\.tao|\.ele|amap")
        ele = [c for c in cookies if pat.search(c.get("domain", ""))]
        with open("eleme_cookies.json", "w", encoding="utf-8") as f:
            json.dump(ele, f, ensure_ascii=False, indent=2)
        print(f"已写出 {len(ele)} 个饿了么相关 cookie -> eleme_cookies.json")
        b.close()


if __name__ == "__main__":
    main()
