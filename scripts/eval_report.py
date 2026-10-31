#!/usr/bin/env python3
"""生成一份轻量群聊评测报表：人工抽检结果 + 线上群聊指标。

用法：
  .venv/bin/python scripts/eval_report.py
  .venv/bin/python scripts/eval_report.py --days 7 --out data/eval/report.md
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("qqbot", ROOT / "bot" / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    now = int(time.time())
    since = now - args.days * 86400
    db = bot.STORE.db
    stats = bot.STORE.eval_stats()
    labels = stats["labels"]
    labeled = sum(labels.values())
    good = labels.get("human_like", 0)
    bad = labels.get("cold", 0) + labels.get("hallucinated", 0) + labels.get("should_not", 0)
    rate_good = (good / labeled * 100) if labeled else 0
    rate_bad = (bad / labeled * 100) if labeled else 0

    user_n = db.execute(
        "SELECT COUNT(*) FROM group_log WHERE role='user' AND ts>=?", (since,)
    ).fetchone()[0]
    bot_n = db.execute(
        "SELECT COUNT(*) FROM group_log WHERE role='assistant' AND ts>=?", (since,)
    ).fetchone()[0]
    turn_rows = db.execute(
        "SELECT role FROM group_log WHERE ts>=? ORDER BY id", (since,)
    ).fetchall()
    turns = 0
    prev = None
    for (role,) in turn_rows:
        if role == "assistant" and prev != "assistant":
            turns += 1
        prev = role
    groups = db.execute(
        "SELECT COUNT(DISTINCT gid) FROM group_log WHERE ts>=?", (since,)
    ).fetchone()[0]
    accusations = db.execute(
        "SELECT COUNT(*) FROM group_log WHERE role='user' AND ts>=? AND "
        "(content LIKE '%你是机器人%' OR content LIKE '%你是AI%' OR "
        "content LIKE '%像机器人%' OR content LIKE '%是不是AI%')", (since,)
    ).fetchone()[0]
    corrections = db.execute(
        "SELECT COUNT(*) FROM group_log WHERE role='user' AND ts>=? AND "
        "(content LIKE '%你刚才%' OR content LIKE '%不是说%' OR "
        "content LIKE '%你之前说%' OR content LIKE '%说错%')", (since,)
    ).fetchone()[0]
    top_bad = db.execute(
        "SELECT id, trigger, reply, label, note FROM eval_cases "
        "WHERE status='labeled' AND label IN ('cold','hallucinated','should_not') "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()

    lines = [
        f"# 群聊评测报表（最近 {args.days} 天）",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 人工抽检",
        f"- 总用例：{stats['total']}，待评：{stats['pending']}，已评：{labeled}",
        f"- 像人：{good}（{rate_good:.0f}%）",
        f"- 明显问题（冷淡/不懂装懂/不该回）：{bad}（{rate_bad:.0f}%）",
    ]
    if labels:
        lines.append("- 标签分布：" + "、".join(f"{k} {v}" for k, v in labels.items()))
    lines += [
        "",
        "## 线上群聊",
        f"- 群数：{groups}",
        f"- 群员消息：{user_n} 条",
        f"- 她发言：{bot_n} 句 / {turns} 轮（平均每 "
        f"{max(1, round(user_n / max(1, turns)))} 条群消息回 1 次）",
        f"- 被质疑像 AI/机器人：{accusations} 次",
        f"- 被指出前后不一致/说错：{corrections} 次",
    ]
    if top_bad:
        lines += ["", "## 最近的问题用例"]
        for cid, trigger, reply, label, note in top_bad:
            lines.append(f"- #{cid} [{label}] 触发：{trigger[:60]} ｜ 她：{reply[:80]}"
                         + (f" ｜ 备注：{note[:40]}" if note else ""))
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"[saved] {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
