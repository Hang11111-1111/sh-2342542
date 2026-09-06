#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pushplus 微信推送模块（纯标准库，无第三方依赖，适合常驻脚本/launchd）。

用法（作为模块）:
    from pushplus_push import push
    ok, msg = push(token="xxxx", title="标题", content="内容")

用法（命令行）:
    python3 pushplus_push.py --token TOKEN --title 标题 --content 内容 [--template txt|html]
"""
import sys
import json
import urllib.request
import urllib.error

PUSHPLUS_URL = "https://www.pushplus.plus/send"


def push(token, title, content, template="txt", topic="", channel="wechat"):
    """发送一条 pushplus 微信消息。

    返回 (success: bool, detail: str)
    """
    if not token:
        return False, "缺少 pushplus token"
    payload = {
        "token": token,
        "title": title or "通知",
        "content": content or "",
        "template": template,       # txt / html
        "channel": channel,         # wechat / 企业微信等
    }
    if topic:
        payload["topic"] = topic
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_URL,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            obj = json.loads(body)
        except Exception:
            return False, f"非 JSON 响应: {body[:200]}"
        # pushplus 成功时 code == 200
        if obj.get("code") == 200:
            return True, obj.get("msg", "ok")
        return False, f"code={obj.get('code')} msg={obj.get('msg')}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, f"网络错误: {e.reason}"
    except Exception as e:  # noqa
        return False, f"异常: {e}"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--title", default="通知")
    ap.add_argument("--content", default="")
    ap.add_argument("--template", default="txt")
    ap.add_argument("--topic", default="")
    args = ap.parse_args()
    ok, detail = push(args.token, args.title, args.content, args.template, args.topic)
    print(("OK: " if ok else "FAIL: ") + detail)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
