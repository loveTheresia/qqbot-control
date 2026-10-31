#!/usr/bin/env python3
"""从现有真实聊天记录里蒸馏“人格草稿”——不更新模型权重，不覆盖人格核心。

产出：
  persona/distill-<date>.json   原始分析
  persona/card.draft.md         人可读的角色卡草稿
  persona/examples.draft.jsonl  从真实对话里选出的示例对话

用法：
  .venv/bin/python scripts/distill_persona.py --limit 30
  .venv/bin/python scripts/distill_persona.py --limit 20 --dry-run
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("qqbot", ROOT / "bot" / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

REAL_PRIVATE = os.getenv("PERSONA_PRIVATE_CONV", "")
REAL_GID = int(os.getenv("PERSONA_GID", "0") or 0)
OUT_DIR = ROOT / "persona"

BAD_REPLY = re.compile(
    r"(作为(一个)?(AI|人工智能)|我是(AI|程序|机器人)|抱歉|无法(回答|提供)|"
    r"建议你|总的来说|综上所述|希望(这些|我的)|主人|/帮助|/参数|"
    r"搜出来|没查到|查不到|没法直接|系统真执行|发了，群里|发到群里|群名字)"
)
BAD_USER = re.compile(r"^(/|帮助|参数|状态|记忆|清空记忆|人格|评测|语料|查|搜|发到|在群里|发群里|群名字|插话|没叫你|安静点|会上网查|人格|复述|语气词|拉进群|活泼点)")

PROMPT = """你是人格蒸馏器。根据给出的真实聊天样本，提炼“她”的说话方式和稳定特征。

规则：
- 只根据样本，不要编造；样本里没有的，不要写。
- 稳定特征至少出现 2 次；一次性的口头禅单独标成“偶发”。
- 区分“她怎么说话”（voice）和“她持什么观点”（stances）。
- 不要提炼身份事实（年龄/职业等），身份以人格核心为准。
- 不要美化，也不要把她写成完美的人。
- example_ids 只从样本编号里选 6~8 条最能代表她语气的，不要自造对话。

只输出 JSON：
{
  "voice": {
    "length": "句长习惯",
    "particles": ["语气词"],
    "tone": "整体语气",
    "humor": "幽默/吐槽方式",
    "habits": ["稳定出现的说话习惯"]
  },
  "stable_traits": [{"trait": "行为特征，不要形容词堆砌", "evidence": "样本里的表现"}],
  "stances": [{"topic": "话题", "stance": "她的稳定看法", "evidence": "样本依据"}],
  "interests": ["从对话里能确认的兴趣"],
  "example_ids": [1, 2, 3],
  "boundaries": ["她明显不接/回避的东西"]
}
"""


def real_rows() -> list[dict]:
    if not REAL_PRIVATE or not REAL_GID:
        raise SystemExit("请设置 PERSONA_PRIVATE_CONV 和 PERSONA_GID")
    con = bot.STORE.db
    rows = []
    for r in con.execute(
        "SELECT id, role, '对方' AS nickname, content, ts, 'private' AS src FROM messages "
        "WHERE conv=? ORDER BY id", (REAL_PRIVATE,)
    ):
        rows.append({"id": r[0], "role": r[1], "nick": r[2], "text": r[3],
                     "ts": r[4], "src": r[5], "gid": 0})
    for r in con.execute(
        "SELECT id, role, nickname, content, ts, 'group' AS src FROM group_log "
        "WHERE gid=? ORDER BY id", (REAL_GID,)
    ):
        rows.append({"id": r[0], "role": r[1], "nick": r[2], "text": r[3],
                     "ts": r[4], "src": r[5], "gid": REAL_GID})
    return rows


def build_samples(limit: int) -> list[dict]:
    rows = real_rows()
    samples = []
    seen = set()
    for i, r in enumerate(rows):
        if r["role"] != "assistant":
            continue
        text = (r["text"] or "").strip()
        if not (12 <= len(text) <= 220):
            continue
        if text.startswith("/") or BAD_REPLY.search(text):
            continue
        if text in seen:
            continue
        # 前一条用户消息
        prev = next((x for x in reversed(rows[max(0, i - 4):i]) if x["role"] == "user"), None)
        if not prev or not (prev["text"] or "").strip():
            continue
        ptext = (prev["text"] or "").strip()
        if len(ptext) < 6 or not re.search(r"[\u4e00-\u9fffA-Za-z]", ptext):
            continue
        if re.match(r"^[?？!！。.…]", ptext):
            continue
        if re.fullmatch(r"[?？!！。.…]+", ptext):
            continue
        if re.search(r"(插话|没叫你|安静点|会上网查|群里没有|群名字|复述|人格|拉进群|语气词|活泼点)", ptext):
            continue
        if BAD_USER.search(ptext):
            continue
        # 后续 10 分钟内有人接话 = 参与度加分
        follow = any(x["role"] == "user" and 0 <= x["ts"] - r["ts"] <= 600
                     for x in rows[i + 1:i + 5])
        score = 0
        if 15 <= len(text) <= 60:
            score += 2
        elif len(text) <= 120:
            score += 1
        if follow:
            score += 2
        if r["src"] == "group":
            score += 1
        if "？" in prev["text"] or "?" in prev["text"]:
            score += 1
        ctx = [x for x in rows[max(0, i - 5):i] if x["role"] == "user"][-3:]
        samples.append({
            "score": score, "src": r["src"], "reply": text,
            "trigger": (prev["text"] or "").strip()[:160],
            "context": [{"who": x["nick"] or "群友", "text": (x["text"] or "")[:120]}
                        for x in ctx],
            "ts": r["ts"],
        })
        seen.add(text)
    samples.sort(key=lambda x: (x["score"], len(x["reply"])), reverse=True)
    # 群聊和私聊都留一些
    group = [x for x in samples if x["src"] == "group"][:max(5, limit * 2 // 3)]
    priv = [x for x in samples if x["src"] == "private"][:max(3, limit - len(group))]
    out = group + priv
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


def call_llm(samples: list[dict]) -> dict:
    blocks = []
    for n, s in enumerate(samples, 1):
        ctx = "\n".join(f"{c['who']}：{c['text']}" for c in s["context"])
        blocks.append(f"【样本 {n}｜{s['src']}】\n{ctx}\n触发：{s['trigger']}\n她：{s['reply']}")
    user = "\n\n".join(blocks)
    raw = bot.chat([{"role": "system", "content": PROMPT},
                    {"role": "user", "content": user}], 0.2, 1600)
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise RuntimeError(f"模型没有返回 JSON：{(raw or '')[:200]}")
    return json.loads(m.group(0))


def write_drafts(samples: list[dict], data: dict) -> tuple[Path, Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d-%H%M")
    raw_path = OUT_DIR / f"distill-{day}.json"
    card_path = OUT_DIR / "card.draft.md"
    ex_path = OUT_DIR / "examples.draft.jsonl"

    raw_path.write_text(json.dumps({"samples": samples, "analysis": data},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    ids = [int(x) for x in data.get("example_ids", []) if str(x).isdigit()]
    chosen = [samples[i - 1] for i in ids if 1 <= i <= len(samples)][:8]
    if not chosen:
        chosen = samples[:6]
    with open(ex_path, "w", encoding="utf-8") as f:
        for s in chosen:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    voice = data.get("voice") or {}
    lines = [
        "# 人格蒸馏草稿（未生效）",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"样本：{len(samples)} 条（真实群聊 + 主人私聊，已过滤命令/AI 自曝/测试会话）",
        "",
        "## 声音",
        f"- 句长：{voice.get('length', '')}",
        f"- 语气：{voice.get('tone', '')}",
        f"- 幽默：{voice.get('humor', '')}",
        f"- 语气词：{'、'.join(voice.get('particles', []))}",
    ]
    for h in voice.get("habits", []):
        lines.append(f"- 习惯：{h}")
    lines += ["", "## 稳定行为"]
    for t in data.get("stable_traits", []):
        lines.append(f"- {t.get('trait', '')}（依据：{t.get('evidence', '')[:80]}）")
    lines += ["", "## 稳定立场"]
    for s in data.get("stances", []):
        lines.append(f"- {s.get('topic', '')}：{s.get('stance', '')}")
    lines += ["", "## 兴趣"]
    lines.append("- " + "、".join(data.get("interests", []) or ["（样本不足）"]))
    lines += ["", "## 示例对话（从真实记录中选）"]
    for s in chosen:
        lines.append(f"- 触发：{s['trigger'][:80]}")
        lines.append(f"  她：{s['reply'][:160]}")
    lines += ["", "## 边界"]
    for b in data.get("boundaries", []):
        lines.append(f"- {b}")
    lines += [
        "",
        "## 说明",
        "- 这是蒸馏草稿，**没有覆盖 `config/system_prompt.txt`，也没有写进线上人格**。",
        "- 现有聊天记录主要能学“她在这个群怎么说话、持什么观点”，不能学“知书达理”；读书/经典需要单独建 canon。",
        "- 样本里有大量 8 字以内短回复，已过滤；但旧记录仍包含“接话过频”的坏习惯，示例只选有上下文的回复，不代表鼓励那种频率。",
    ]
    card_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return raw_path, card_path, ex_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    samples = build_samples(max(10, min(60, args.limit)))
    print(f"[data] samples={len(samples)} group={sum(1 for s in samples if s['src']=='group')} "
          f"private={sum(1 for s in samples if s['src']=='private')}")
    if args.dry_run:
        for i, s in enumerate(samples, 1):
            print(f"{i}. [{s['src']}] {s['trigger'][:50]} -> {s['reply'][:80]}")
        return 0
    data = call_llm(samples)
    raw, card, ex = write_drafts(samples, data)
    print(f"[ok] {raw}\n[ok] {card}\n[ok] {ex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
