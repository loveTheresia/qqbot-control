#!/usr/bin/env python3
"""校验公开仓库里的控制文件。无第三方依赖，供 GitHub Actions 使用。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_PATTERNS = [
    r"github_pat_[A-Za-z0-9_]+",
    r"ghp_[A-Za-z0-9]{20,}",
    r"sk-[A-Za-z0-9]{20,}",
    r"-----BEGIN (RSA |OPENSSH )?PRIVATE KEY-----",
]


def fail(msg: str) -> None:
    print(f"[fail] {msg}")
    raise SystemExit(1)


def check_policy() -> None:
    p = ROOT / "control" / "policy.json"
    if not p.exists():
        fail("control/policy.json 不存在")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data.get("version"), int):
        fail("policy.version 必须是整数")
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        fail("policy.rules 必须是数组")
    ids = set()
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            fail(f"rules[{i}] 不是对象")
        rid = str(r.get("id", "")).strip()
        text = str(r.get("text", "")).strip()
        if not rid or not text:
            fail(f"rules[{i}] 缺少 id/text")
        if rid in ids:
            fail(f"重复规则 id: {rid}")
        ids.add(rid)
        if r.get("status", "active") not in ("active", "disabled"):
            fail(f"规则 {rid} status 非法")
        if not isinstance(r.get("priority", 100), int):
            fail(f"规则 {rid} priority 必须是整数")


def check_release() -> None:
    p = ROOT / "control" / "release.json"
    if not p.exists():
        fail("control/release.json 不存在")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data.get("policy_version"), int):
        fail("release.policy_version 必须是整数")


def scan_secrets() -> None:
    for p in ROOT.rglob("*"):
        if not p.is_file() or ".git" in p.parts:
            continue
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".zip"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat in SECRET_PATTERNS:
            if re.search(pat, text) and p.name not in (".env.example",):
                fail(f"疑似密钥：{p.relative_to(ROOT)}")


def main() -> int:
    check_policy()
    check_release()
    scan_secrets()
    print("[ok] control files valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
