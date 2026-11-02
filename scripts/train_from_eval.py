#!/usr/bin/env python3
"""把人工抽检结果整理成训练材料：偏好数据 + 失败模式报告。

注意：这不是微调模型，不更新权重。39 条标签不足以做可靠的 fine-tune；
这一轮产出的是：
  - data/eval/preferences.jsonl  未来 DPO/偏好训练用
  - data/eval/training-report.md 失败模式 + 规则建议
  - data/eval/labels.csv         原始标签
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "bot.db"
OUT = ROOT / "data" / "eval"


def rows() -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    data = con.execute(
        "SELECT e.*, datetime(g.ts, 'unixepoch', 'localtime') AS msg_time "
        "FROM eval_cases e JOIN eval_batches b ON b.id=e.batch_id "
        "LEFT JOIN group_log g ON g.id=e.src_id "
        "WHERE e.status='labeled' AND b.status='submitted' ORDER BY g.ts, e.id"
    ).fetchall()
    out = []
    for r in data:
        try:
            ctx = json.loads(r["context"] or "[]")
        except Exception:
            ctx = []
        out.append({"id": r["id"], "gid": r["gid"], "trigger": r["trigger"] or "",
                    "reply": r["reply"] or "", "label": r["label"] or "",
                    "note": r["note"] or "", "context": ctx,
                    "time": r["msg_time"] or ""})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-labels", type=int, default=1)
    args = ap.parse_args()
    data = rows()
    if len(data) < args.min_labels:
        print(f"[skip] 只有 {len(data)} 条标签，少于 {args.min_labels}")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    counts = Counter(d["label"] for d in data)
    times = [d["time"] for d in data if d["time"]]
    span = f"{times[0]} ~ {times[-1]}" if times else "未知"

    # 偏好数据：负标签作为 reject，正标签作为 chosen
    with open(OUT / "preferences.jsonl", "w", encoding="utf-8") as f:
        for d in data:
            f.write(json.dumps({
                "case_id": d["id"],
                "context": d["context"],
                "trigger": d["trigger"],
                "reply": d["reply"],
                "label": d["label"],
                "preference": "reject" if d["label"] in
                              ("hallucinated", "should_not", "cold") else "chosen",
                "note": d["note"],
            }, ensure_ascii=False) + "\n")

    # CSV 原始标签
    with open(OUT / "labels.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "gid", "time", "trigger", "reply", "label", "note"])
        for d in data:
            w.writerow([d["id"], d["gid"], d["time"], d["trigger"],
                        d["reply"], d["label"], d["note"]])

    def examples(label: str, n: int = 4) -> list[str]:
        out = []
        for d in data:
            if d["label"] == label:
                out.append(f"#{d['id']} 触发：{d['trigger'][:70]}\n  她：{d['reply'][:120]}")
            if len(out) >= n:
                break
        return out

    lines = [
        "# 人工评测反馈训练报告",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"样本：{len(data)} 条，时间：{span}",
        "",
        "## 标签分布",
    ]
    for k, v in counts.most_common():
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "## 结论",
        "- 这批标签全部来自 **20:38 群聊节奏闸上线之前**的样本，不能直接当作“她现在的表现”。",
        "- 39 条不足以做可靠的 fine-tune；本轮产出偏好数据，供后续 DPO/规则校准使用。",
        "- 最主要的失败不是“答错事实”，而是 **在没掌握上下文时硬接、替别人补动机**。",
        "",
        "## 不懂装懂（hallucinated）",
        "模式：A/B 在聊一个具体话题，她只看到半句就补一个笃定的判断/动机，",
        "例如“爱笑那性格，宁愿自己慢慢养”“保谁不保谁早就定好了”。",
        "",
    ]
    lines += examples("hallucinated")
    lines += [
        "",
        "## 不该回（should_not）",
        "模式：别人只是笑、感叹、接半句，没有新信息，她偏要追问或拆台。",
        "",
    ]
    lines += examples("should_not")
    lines += [
        "",
        "## 像人（human_like）",
        "这些回复有具体依据、延续话题、没有替别人补内幕，可以作为正向参考。",
        "",
    ]
    lines += examples("human_like")
    lines += [
        "",
        "## 建议规则（待确认后上线）",
        "1. 只看到半句、不知道前因时：先问一句，或者不接；不要替别人补动机/内幕。",
        "2. 别人只回“哈哈哈/笑死/？”这类情绪词，没有新信息时：不接。",
        "3. 在 A/B 的具体讨论里，除非被 @/被引用，否则不主动下结论。",
        "4. 不确定的事直接说“我没看懂/不确定”，不要用笃定语气蒙混。",
        "",
        "## 产物",
        "- `data/eval/preferences.jsonl`：偏好训练数据",
        "- `data/eval/labels.csv`：原始标签",
        "- `data/eval/training-report.md`：本报告",
        "",
        "## 下一步",
        "这批先做规则校准 + 偏好数据；等积累 300~500 条、覆盖新节奏上线后的样本，再考虑 DPO/LoRA。",
    ]
    (OUT / "training-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with sqlite3.connect(DB) as c:
        c.execute("UPDATE eval_batches SET status='trained', trained_at=? WHERE status='submitted'",
                  (int(time.time()),))
        c.commit()
    print(f"[ok] labels={len(data)} report={OUT / 'training-report.md'}")
    print(f"[ok] preferences={OUT / 'preferences.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
