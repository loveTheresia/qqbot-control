#!/usr/bin/env python3
"""每晚 00:00 跑一次：从群日志里学习群级语气/用词/梗，沉淀成 style_items。

用法：
  .venv/bin/python scripts/nightly_style.py                # 处理昨天（Asia/Shanghai）
  .venv/bin/python scripts/nightly_style.py --date 2026-10-30
  .venv/bin/python scripts/nightly_style.py --dry-run      # 只打印候选，不写库/不调模型
  .venv/bin/python scripts/nightly_style.py --min-msgs 10
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("qqbot", ROOT / "bot" / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

STOPWORDS = {
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "为什么", "就是", "不是",
    "真的", "可以", "没有", "现在", "今天", "明天", "昨天", "一个", "感觉", "有点",
    "厉害", "觉得", "知道", "喜欢", "不错", "牛逼", "牛", "东西",
    "知道", "觉得", "还是", "已经", "然后", "所以", "但是", "因为", "如果", "这样",
    "那样", "时候", "东西", "事情", "问题", "哈哈哈", "哈哈哈哈", "哈哈", "笑死",
    "真的吗", "不是吧", "不会吧", "怎么说", "有没有", "是不是", "好不好",
}

PROMPT = """你是群聊风格分析器。根据一个群一天的聊天候选词/统计，提炼这个群稳定存在的说话风格。
只输出 JSON，不要解释：
{"summary":"不超过80字，概括句长、语气、常用词、玩梗方式","items":[
  {"text":"词或梗","kind":"slang|meme|particle|pattern","trigger":"什么话题/上下文用","meaning":"一句话解释","safe":true,"score":0.0}
]}
要求：
- 只保留确实像群内固定用法、黑话、口头禅、梗；一次性玩笑不要；
- 不收录脏话、攻击、隐私、人名、链接、纯数字；
- trigger 写清楚适用场景，没有就写“通用”；
- items 最多 8 个；宁可少，不要编。
"""


LOOKUP_PROMPT = """你在核实一个群聊词/梗的真实含义。根据搜索结果，用一句话解释它、说明使用场景、判断是否适合机器人学。
只输出 JSON：{"meaning":"不超过50字","trigger":"什么情况下用","safe":true,"confidence":0.0}
搜索结果不足或互相矛盾就降低 confidence，不要编；不确定 safe 就填 false。
"""


def clean(text: str) -> str:
    text = re.sub(r"@\d+", " ", text or "")
    text = re.sub(r"https?://\S+", " ", text)
    return text.strip()


def mine(messages: list[dict]) -> tuple[list[dict], dict]:
    counts: dict[str, int] = {}
    users: dict[str, set] = {}
    text_all = []
    particles = {p: 0 for p in "啊吧呢嘛哈呀哦嗯"}
    for m in messages:
        raw = clean(m["content"])
        if not raw:
            continue
        text_all.append(raw)
        for p in particles:
            particles[p] += raw.count(p)
        for seg in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", raw):
            for n in range(2, min(6, len(seg)) + 1):
                for i in range(len(seg) - n + 1):
                    g = seg[i:i + n]
                    if g in STOPWORDS:
                        continue
                    if bot.has_profanity(g) or not re.search(r"[\u4e00-\u9fffA-Za-z]", g):
                        continue
                    counts[g] = counts.get(g, 0) + 1
                    users.setdefault(g, set()).add(m["uid"])
    cands = []
    for g, c in counts.items():
        u = len(users.get(g, ()))
        if c >= 3 and u >= 2:
            cands.append({"text": g, "count": c, "users": u, "score": c + 2 * u})
    cands.sort(key=lambda x: x["score"], reverse=True)
    # 去掉被更长候选包含且次数相同的子串
    top = []
    for c in cands:
        if any(c["text"] != o["text"] and c["text"] in o["text"] and c["count"] <= o["count"] for o in top):
            continue
        top.append(c)
        if len(top) >= 30:
            break
    stats = {
        "messages": len(text_all),
        "avg_len": round(sum(len(t) for t in text_all) / max(1, len(text_all)), 1),
        "question_ratio": round(sum(1 for t in text_all if bot.is_question(t)) / max(1, len(text_all)), 2),
        "particles": {k: v for k, v in particles.items() if v >= 3},
    }
    return top, stats


def learn_group(gid: int, day: str, min_msgs: int = 10, dry_run: bool = False,
                force: bool = False) -> tuple[int, int]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bot.TZ)
    end = start + timedelta(days=1)
    if not dry_run and bot.STORE.has_style_run(gid, day) and not force:
        print(f"[skip] {gid} {day} 已经学过")
        return 0, 0
    rows = bot.STORE.db.execute(
        "SELECT uid, content FROM group_log WHERE gid=? AND role='user' AND ts>=? AND ts<?",
        (gid, int(start.timestamp()), int(end.timestamp())),
    ).fetchall()
    msgs = [{"uid": uid, "content": content} for uid, content in rows]
    if len(msgs) < min_msgs:
        print(f"[skip] {gid} {day} 只有 {len(msgs)} 条，少于 {min_msgs} 条")
        return 0, 0
    cands, stats = mine(msgs)
    if dry_run:
        print(f"[dry] {gid} {day} msgs={stats['messages']} stats={stats}")
        for c in cands[:20]:
            print("  ", c)
        return len(cands), 0
    if not cands:
        print(f"[skip] {gid} {day} 没有稳定候选")
        return 0, 0
    payload = "\n".join(f"- {c['text']}（{c['count']}次/{c['users']}人）" for c in cands)
    user = (f"【群 {gid} / {day} 统计】\n{json.dumps(stats, ensure_ascii=False)}\n\n"
            f"【候选词】\n{payload}")
    raw = bot.chat([{"role": "system", "content": PROMPT},
                    {"role": "user", "content": user}], 0.2, 700)
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        print(f"[fail] {gid} {day} 模型没返回 JSON：{(raw or '')[:120]}")
        return 0, 0
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        print(f"[fail] {gid} {day} JSON 解析失败：{e}")
        return 0, 0
    summary = str(data.get("summary", "")).strip()
    if summary:
        bot.STORE.set_group_style(gid, summary)
    all_text = "\n".join(clean(x["content"]) for x in msgs)
    n = 0
    lookups = 0
    for it in data.get("items", [])[:8]:
        text = str(it.get("text", "")).strip()
        if not text or text not in all_text or not it.get("safe", False):
            continue
        kind = str(it.get("kind", "slang"))[:20]
        trigger = str(it.get("trigger", "通用"))[:80]
        meaning = str(it.get("meaning", "")).strip()[:200]
        score = max(0.0, min(1.0, float(it.get("score", 0.5) or 0.5)))
        count = next((c["count"] for c in cands if c["text"] == text), 1)
        verified, source = 0, ""
        need_lookup = kind in ("meme", "slang") or not meaning
        if need_lookup and lookups < 5:
            lookups += 1
            try:
                results = bot.web_search(f"{text} 什么意思 梗", 4)
            except Exception as e:
                results = []
                print(f"[lookup] {text} 搜索失败 {e}")
            if results:
                ctx = "\n".join(f"- {r['title']}：{r.get('snippet', '')[:180]}" for r in results[:4])
                try:
                    raw2 = bot.chat([
                        {"role": "system", "content": LOOKUP_PROMPT},
                        {"role": "user", "content": f"【词/梗】{text}\n【搜索结果】\n{ctx}"},
                    ], 0.2, 300)
                    mm = re.search(r"\{.*\}", raw2 or "", re.S)
                    info = json.loads(mm.group(0)) if mm else {}
                    m2 = str(info.get("meaning", "")).strip()
                    t2 = str(info.get("trigger", "")).strip()
                    if m2:
                        meaning = m2[:200]
                        trigger = (t2 or trigger)[:80]
                        if info.get("safe", True) and float(info.get("confidence", 0) or 0) >= 0.4:
                            verified = 1
                            source = "；".join(r.get("url", "")[:120] for r in results[:2] if r.get("url"))
                except Exception as e:
                    print(f"[lookup] {text} 解释失败 {e}")
        elif kind in ("particle", "pattern") and meaning:
            verified = 1
        if kind == "meme" and re.search(r"(选手|战队|主播|人名|ID|选手ID)", meaning):
            continue
        status = "active" if (score >= 0.6 and verified) else "candidate"
        bot.STORE.upsert_style_item(
            gid, text, kind, trigger, meaning, score, count,
            status=status, verified=verified, source=source,
        )
        if verified:
            n += 1
        print(f"[item] {text} verified={verified} meaning={meaning[:40]}")
    bot.STORE.record_style_run(gid, day, len(msgs), n)
    print(f"[ok] {gid} {day} msgs={len(msgs)} items={n} summary={summary[:80]}")
    return len(msgs), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，默认昨天")
    ap.add_argument("--gid", type=int, help="只处理某个群")
    ap.add_argument("--min-msgs", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.date:
        day = args.date
    else:
        day = (datetime.now(bot.TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    if args.gid:
        gids = [args.gid]
    else:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bot.TZ)
        end = start + timedelta(days=1)
        gids = [r[0] for r in bot.STORE.db.execute(
            "SELECT DISTINCT gid FROM group_log WHERE role='user' AND ts>=? AND ts<?",
            (int(start.timestamp()), int(end.timestamp()))).fetchall()]
    print(f"[run] day={day} groups={gids} dry={args.dry_run}")
    total_msgs = total_items = 0
    for gid in gids:
        m, n = learn_group(gid, day, args.min_msgs, args.dry_run, args.force)
        total_msgs += m
        total_items += n
    if not args.dry_run:
        bot.STORE.decay_styles()
    print(f"[done] groups={len(gids)} msgs={total_msgs} items={total_items}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
