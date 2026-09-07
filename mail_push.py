#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SMTP 邮件推送模块（纯标准库，无第三方依赖，适合常驻脚本/launchd/GitHub Actions）。

兼容原 pushplus_push 的调用签名:  push(token, title, content, template="txt")
  - token    参数被忽略（邮件不需要 pushplus token）
  - title    -> 邮件主题
  - content  -> 邮件正文

配置来源（优先级：环境变量 > 同目录/.smtp_env 文件 > 默认值）:
  SMTP_HOST   发件 SMTP 主机（默认 smtp.qq.com）
  SMTP_PORT   端口（默认 465，SSL）
  SMTP_USER   发件邮箱（如 1478363@qq.com）
  SMTP_PASS   SMTP 授权码（QQ邮箱需在「设置-账户」开启 SMTP 后生成，不是登录密码）
  MAIL_TO     收件邮箱（默认 1478363@qq.com）
  MAIL_FROM   发件显示地址（默认同 SMTP_USER）

本地用法: 在脚本同目录放一个 .smtp_env (KEY=VALUE 每行一个, 此文件 gitignore),
          运行脚本即自动读取, 无需在命令里暴露密码。
云端用法: 用 GitHub Secrets 注入同名环境变量。
"""
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr


def _load_dotenv():
    """从脚本同目录/ cwd 的 .smtp_env 加载 KEY=VALUE 到 os.environ（仅补充缺失项）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, ".smtp_env"), os.path.join(os.getcwd(), ".smtp_env")):
        if os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


_load_dotenv()


def _config():
    host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASS", "")
    to = os.environ.get("MAIL_TO", "1478363@qq.com")
    frm = os.environ.get("MAIL_FROM", user or "")
    return host, port, user, pwd, to, frm


def push(token=None, title="通知", content="", template="txt", **kwargs):
    """发送一封邮件。返回 (success: bool, detail: str)。"""
    host, port, user, pwd, to, frm = _config()
    if not user or not pwd:
        return False, "缺少 SMTP_USER/SMTP_PASS（邮件发件凭据）"
    if not to:
        return False, "缺少 MAIL_TO（收件邮箱）"
    is_html = (template == "html")
    msg = MIMEText(content, "html" if is_html else "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    try:
        msg["From"] = formataddr((str(Header("水果店监控", "utf-8")), frm)) if frm else frm
    except Exception:
        msg["From"] = frm
    msg["To"] = to
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as s:
            s.login(user, pwd)
            s.sendmail(frm, [to], msg.as_string())
        return True, f"邮件已发送至 {to}"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP 认证失败(授权码错误?): {e}"
    except Exception as e:  # noqa
        return False, f"邮件发送失败: {e}"


def main():
    import sys
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="通知")
    ap.add_argument("--content", default="")
    ap.add_argument("--template", default="txt")
    args = ap.parse_args()
    ok, detail = push(None, args.title, args.content, args.template)
    print(("OK: " if ok else "FAIL: ") + detail)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
