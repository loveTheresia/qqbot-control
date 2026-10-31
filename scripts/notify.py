#!/usr/bin/env python3
"""独立通知器：掉线时把消息推到微信/邮件/Bark 等，不依赖 bot 进程。

用法：
    python3 scripts/notify.py "消息"
    python3 scripts/notify.py "消息" --dry-run   # 只打印会发什么，不真的发

配置写在 config/.env（不会打印密钥）：
    NOTIFY_CHANNELS=wecom,serverchan,pushplus,bark,email,webhook   # 不写则自动探测
    WECOM_WEBHOOK=...
    SERVERCHAN_KEY=...
    PUSHPLUS_TOKEN=...
    BARK_URL=https://api.day.app/xxxx
    SMTP_HOST=smtp.qq.com  SMTP_PORT=465  SMTP_USER=...  SMTP_PASS=...  SMTP_TO=...
    NOTIFY_WEBHOOK=...
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.environ.get("NOTIFY_ENV_FILE", str(ROOT / "config" / ".env")))
TIMEOUT = 10


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def post_json(url: str, obj: dict) -> str:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(300).decode("utf-8", "ignore")


def post_form(url: str, fields: dict) -> str:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(300).decode("utf-8", "ignore")


def send_email(env: dict[str, str], title: str, msg: str) -> str:
    host = env.get("SMTP_HOST", "")
    user = env.get("SMTP_USER", "")
    password = env.get("SMTP_PASS", "")
    to = [x.strip() for x in env.get("SMTP_TO", "").split(",") if x.strip()]
    sender = env.get("SMTP_FROM", user)
    if not (host and user and password and to):
        raise ValueError("SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO 未配齐")
    port = int(env.get("SMTP_PORT", "465"))
    mail = EmailMessage()
    mail["Subject"] = title
    mail["From"] = sender
    mail["To"] = ", ".join(to)
    mail.set_content(msg)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx) as s:
            s.login(user, password)
            s.send_message(mail)
    else:
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(mail)
    return f"email -> {len(to)} 个收件人"


def build_channels(env: dict[str, str]) -> list[str]:
    raw = env.get("NOTIFY_CHANNELS", "").strip()
    known = ["wecom", "serverchan", "pushplus", "bark", "email", "webhook"]
    if raw:
        return [c.strip() for c in raw.split(",") if c.strip() in known]
    auto = []
    if env.get("WECOM_WEBHOOK"):
        auto.append("wecom")
    if env.get("SERVERCHAN_KEY") or env.get("SERVERCHAN_SENDKEY"):
        auto.append("serverchan")
    if env.get("PUSHPLUS_TOKEN"):
        auto.append("pushplus")
    if env.get("BARK_URL") or env.get("BARK_KEY"):
        auto.append("bark")
    if all(env.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_TO")):
        auto.append("email")
    if env.get("NOTIFY_WEBHOOK"):
        auto.append("webhook")
    return auto


def send_one(channel: str, env: dict[str, str], title: str, msg: str) -> str:
    if channel == "wecom":
        url = env.get("WECOM_WEBHOOK", "")
        if not url:
            raise ValueError("WECOM_WEBHOOK 未配置")
        return "wecom -> " + post_json(url, {"msgtype": "text", "text": {"content": f"{title}\n{msg}"}})

    if channel == "serverchan":
        key = env.get("SERVERCHAN_KEY") or env.get("SERVERCHAN_SENDKEY", "")
        if not key:
            raise ValueError("SERVERCHAN_KEY 未配置")
        url = f"https://sctapi.ftqq.com/{key}.send"
        return "serverchan -> " + post_form(url, {"title": title, "desp": msg})

    if channel == "pushplus":
        token = env.get("PUSHPLUS_TOKEN", "")
        if not token:
            raise ValueError("PUSHPLUS_TOKEN 未配置")
        url = "https://www.pushplus.plus/send"
        return "pushplus -> " + post_json(
            url, {"token": token, "title": title, "content": msg, "template": "txt"}
        )

    if channel == "bark":
        url = env.get("BARK_URL", "")
        key = env.get("BARK_KEY", "")
        if not url and key:
            url = f"https://api.day.app/{key}"
        if not url:
            raise ValueError("BARK_URL 未配置")
        return "bark -> " + post_json(
            url.rstrip("/"), {"title": title, "body": msg, "group": "QQBot"}
        )

    if channel == "email":
        return send_email(env, title, msg)

    if channel == "webhook":
        url = env.get("NOTIFY_WEBHOOK", "")
        if not url:
            raise ValueError("NOTIFY_WEBHOOK 未配置")
        return "webhook -> " + post_json(url, {"event": "qq_kick", "message": msg})

    raise ValueError(f"未知渠道 {channel}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    msg = args[0] if args else "（空消息）"
    env = load_env()
    channels = build_channels(env)
    title = "QQBot 掉线通知"

    if not channels:
        print("未配置通知渠道（config/.env 里没有任何 NOTIFY_* / WECOM_* / SMTP_*）")
        return 2

    if dry:
        print("dry-run，渠道：" + ", ".join(channels))
        print("标题：" + title)
        print("内容：" + msg.replace("\n", " / ")[:300])
        return 0

    ok = 0
    for ch in channels:
        try:
            result = send_one(ch, env, title, msg)
            print(result)
            ok += 1
        except Exception as exc:  # 网络/配置错误都不应打断其它渠道
            print(f"{ch} -> 失败：{exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
