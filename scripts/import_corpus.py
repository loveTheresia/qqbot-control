#!/usr/bin/env python3
"""导入风格语料并生成“语料卡”：原句 + 意思 + 用法 + 例句 + 标签。

用法：
  .venv/bin/python scripts/import_corpus.py --file data/corpus/xxx.jsonl --source 作品A
  .venv/bin/python scripts/import_corpus.py --file data/corpus/xxx.txt --kind style
  ... --dry-run       只打印卡片，不写库
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("qqbot", ROOT / "bot" / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

PROMPT = """你在把一句群聊/作品里的话整理成“语料卡”。只根据原句和必要常识，不要编造。
只输出 JSON：
{"meaning":"一句话意思","trigger":"什么场景/话题下用","examples":["改写例句1","改写例句2"],"tags":["标签"],"safe":true,"confidence":0.0}
要求：
- meaning 必须具体，不要只说“一种表达”；
- trigger 写适用语境，没有就写“通用”；
- examples 是 1~2 句自然改写，不要照抄原句；
- tags 最多 5 个：游戏/LPL/吐槽/安慰/日常等；
- 涉及脏话、攻击、隐私、政治敏感、纯人名/选手名 → safe=false；
- 不确定就降低 confidence，不要硬编。
"""

LOOKUP_PROMPT = """根据搜索结果，核实这句话/梗的真实含义和使用场景，整理成语料卡。
只输出 JSON：{"meaning":"不超过60字","trigger":"什么场景用","examples":["例句"],"tags":["标签"],"safe":true,"confidence":0.0}
搜索不足或矛盾就降低 confidence；不确定 safe 填 false；不要编。
"""


def read_entries(path: Path, default_source: str, default_kind: str) -> list[dict]:
    out = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            text = str(d.get("text", "")).strip()
            if not text:
                continue
            out.append({
                "text": text,
                "source": str(d.get("source", default_source)),
                "kind": str(d.get("kind", default_kind)),
                "meaning": str(d.get("meaning", "")),
                "trigger": str(d.get("trigger", "")),
                "examples": d.get("examples", []),
                "tags": d.get("tags", []),
                "verified": int(d.get("verified", 0) or 0),
                "score": float(d.get("score", 0.5) or 0.5),
            })
        else:
            out.append({"text": line, "source": default_source, "kind": default_kind,
                        "meaning": "", "trigger": "", "examples": [], "tags": [],
                        "verified": 0, "score": 0.5})
    return out


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def enrich(entry: dict, no_search: bool = False, min_conf: float = 0.4) -> dict | None:
    text = entry["text"]
    if not text or len(text) < 2 or len(text) > 200:
        return None
    if bot.has_profanity(text) or re.search(r"https?://|@\d{5,}", text):
        return None
    base = f"【原句】{text}\n【来源】{entry.get('source', 'manual')}\n【类型】{entry.get('kind', 'style')}"
    raw = bot.chat([{"role": "system", "content": PROMPT},
                    {"role": "user", "content": base}], 0.2, 400)
    info = parse_json(raw)
    if not info:
        return None
    meaning = str(info.get("meaning", "")).strip()
    trigger = str(info.get("trigger", "")).strip()
    examples = [str(x).strip()[:100] for x in info.get("examples", []) if str(x).strip()][:2]
    tags = [str(x).strip()[:20] for x in info.get("tags", []) if str(x).strip()][:5]
    safe = bool(info.get("safe", False))
    conf = max(0.0, min(1.0, float(info.get("confidence", 0) or 0)))

    if safe and (conf < 0.6 or not meaning) and not no_search:
        try:
            results = bot.web_search(f"{text} 什么意思 梗 出处", 4)
        except Exception:
            results = []
        if results:
            ctx = "\n".join(f"- {r['title']}：{r.get('snippet', '')[:180]}" for r in results[:4])
            raw2 = bot.chat([
                {"role": "system", "content": LOOKUP_PROMPT},
                {"role": "user", "content": f"{base}\n【搜索结果】\n{ctx}"},
            ], 0.2, 400)
            info2 = parse_json(raw2)
            if info2:
                meaning = str(info2.get("meaning", "")).strip() or meaning
                trigger = str(info2.get("trigger", "")).strip() or trigger
                examples = [str(x).strip()[:100] for x in info2.get("examples", [])
                            if str(x).strip()][:2] or examples
                tags = [str(x).strip()[:20] for x in info2.get("tags", [])
                        if str(x).strip()][:5] or tags
                safe = bool(info2.get("safe", safe))
                conf = max(conf, float(info2.get("confidence", 0) or 0))
    if not safe or not meaning or conf < min_conf:
        return None
    return {
        "text": text,
        "meaning": meaning[:300],
        "trigger": (trigger or "通用")[:160],
        "examples": json.dumps(examples, ensure_ascii=False),
        "tags": ",".join(tags)[:200],
        "source": entry.get("source", "manual"),
        "kind": entry.get("kind", "style"),
        "verified": 1,
        "score": round(conf, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--source", default="manual")
    ap.add_argument("--kind", default="style")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--min-confidence", type=float, default=0.4)
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"[fail] 文件不存在：{path}")
        return 1
    entries = read_entries(path, args.source, args.kind)
    print(f"[run] {path} entries={len(entries)} dry={args.dry_run}")
    added = skipped = 0
    for e in entries[:args.limit]:
        card = enrich(e, args.no_search, args.min_confidence)
        if not card:
            skipped += 1
            print(f"[skip] {e['text'][:40]}")
            continue
        print(f"[card] {card['text']} -> {card['meaning'][:50]} | {card['trigger']}")
        if args.dry_run:
            added += 1
            continue
        cid = bot.STORE.add_corpus(
            card["text"], card["source"], card["kind"], card["tags"],
            card["meaning"], card["trigger"], card["examples"],
            1, card["score"],
        )
        if cid:
            added += 1
        else:
            skipped += 1
    print(f"[done] added={added} skipped={skipped} dry={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
