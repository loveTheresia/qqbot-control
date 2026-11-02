#!/usr/bin/env python3
"""本地人工抽检界面。

启动：
  .venv/bin/python scripts/eval_ui.py --port 8099
然后浏览器打开 http://127.0.0.1:8099/
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "bot.db"
UI = ROOT / "eval_ui"
HTTPD = None
FINISHING = False

LABELS = {
    "human_like": ("像人", "yes"),
    "ok": ("还行", "yes"),
    "cold": ("冷淡", "yes"),
    "hallucinated": ("不懂装懂", "yes"),
    "should_not": ("不该回", "no"),
    "skip": ("跳过", "skip"),
}


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def stats() -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT status, label, COUNT(*) n FROM eval_cases GROUP BY status, label"
        ).fetchall()
        total = c.execute("SELECT COUNT(*) FROM eval_cases").fetchone()[0]
        pending = c.execute(
            "SELECT COUNT(*) FROM eval_cases WHERE status='pending'").fetchone()[0]
        open_batches = [dict(r) for r in c.execute(
            "SELECT id, day, total, labeled, status FROM eval_batches "
            "WHERE status='open' AND total>0 ORDER BY id DESC").fetchall()]
        submitted = [dict(r) for r in c.execute(
            "SELECT id, day, total, labeled, status FROM eval_batches "
            "WHERE status='submitted' ORDER BY id DESC").fetchall()]
        labels = [{"key": r["label"], "name": LABELS.get(r["label"], (r["label"], ""))[0],
                   "count": r["n"]} for r in rows if r["status"] == "labeled" and r["label"]]
    return {"total": total, "pending": pending, "done": total - pending, "labels": labels,
            "open_batches": open_batches,
            "open_batch": open_batches[0] if open_batches else None,
            "submitted_batches": submitted,
            "completed": pending == 0 and not open_batches and not submitted}


def next_case() -> dict | None:
    with conn() as c:
        r = c.execute(
            "SELECT e.id, e.gid, e.uid, e.nickname, e.case_type, e.context, e.trigger, e.reply "
            "FROM eval_cases e JOIN eval_batches b ON b.id=e.batch_id "
            "WHERE e.status='pending' AND b.status='open' ORDER BY e.id LIMIT 1"
        ).fetchone()
    if not r:
        return None
    try:
        ctx = json.loads(r["context"] or "[]")
    except Exception:
        ctx = []
    return {"id": r["id"], "gid": r["gid"], "uid": r["uid"],
            "nickname": r["nickname"], "case_type": r["case_type"],
            "context": ctx, "trigger": r["trigger"], "reply": r["reply"]}


def recent(limit: int = 6) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT id, label, note, labeled_at FROM eval_cases "
            "WHERE status='labeled' ORDER BY labeled_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"id": r["id"], "label": r["label"],
             "name": LABELS.get(r["label"], (r["label"], ""))[0],
             "note": r["note"] or ""} for r in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "EvalUI/1.0"

    def log_message(self, fmt, *args):
        print(f"[ui] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _file(self, path: Path, ctype: str):
        if not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, path.read_bytes(), ctype)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/next":
            self._json({"case": next_case(), "stats": stats()})
        elif p == "/api/stats":
            self._json(stats())
        elif p == "/api/recent":
            self._json({"recent": recent(), "stats": stats()})
        elif p == "/api/export.csv":
            with conn() as c:
                rows = c.execute(
                    "SELECT id, gid, uid, nickname, case_type, trigger, reply, status, "
                    "label, should_reply, note, created_at FROM eval_cases ORDER BY id"
                ).fetchall()
            s = io.StringIO()
            w = csv.writer(s)
            w.writerow(["id", "gid", "uid", "nickname", "case_type", "trigger",
                        "reply", "status", "label", "should_reply", "note", "created_at"])
            w.writerows([tuple(r) for r in rows])
            self._send(200, ("\ufeff" + s.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="labels.csv"'})
        elif p in ("/", "/index.html"):
            self._file(UI / "index.html", "text/html; charset=utf-8")
        elif p == "/style.css":
            self._file(UI / "style.css", "text/css; charset=utf-8")
        elif p == "/app.js":
            self._file(UI / "app.js", "application/javascript; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json({"error": "bad json"}, 400)
            return
        if p == "/api/submit":
            with conn() as c:
                open_batches = c.execute(
                    "SELECT id, day FROM eval_batches WHERE status='open' AND total>0 "
                    "ORDER BY id DESC").fetchall()
                if not open_batches:
                    self._json({"error": "当前没有待提交的评测批次"}, 400)
                    return
                for b in open_batches:
                    n = c.execute("SELECT COUNT(*) FROM eval_cases "
                                  "WHERE batch_id=? AND status='pending'", (b["id"],)).fetchone()[0]
                    if n:
                        self._json({"error": f"{b['day']} 还有 {n} 条未评，不能提交"}, 400)
                        return
                now_ts = int(time.time())
                for b in open_batches:
                    c.execute("UPDATE eval_batches SET status='submitted', submitted_at=? "
                              "WHERE id=?", (now_ts, b["id"]))
            log = open(ROOT / "logs" / "eval_train.log", "a", encoding="utf-8")
            subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / "train_from_eval.py")],
                cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._json({"ok": True, "message": "已提交，训练材料生成中",
                        "stats": stats()})
            return
        if p == "/api/finish":
            global FINISHING, HTTPD
            if FINISHING:
                self._json({"error": "already finishing"}, 409)
                return
            FINISHING = True
            self._json({"ok": True})
            threading.Timer(1.0, HTTPD.shutdown).start()
            return
        if p != "/api/label":
            self._json({"error": "not found"}, 404)
            return
        try:
            cid = int(data.get("id"))
            key = str(data.get("label", ""))
            if key not in LABELS:
                self._json({"error": "bad label"}, 400)
                return
            note = str(data.get("note", ""))[:300]
            status = "skipped" if key == "skip" else "labeled"
            label = None if key == "skip" else key
            should = LABELS[key][1]
            with conn() as c:
                c.execute(
                    "UPDATE eval_cases SET status=?, label=?, should_reply=?, note=?, "
                    "labeled_at=? WHERE id=?", (status, label, should, note, int(time.time()), cid)
                )
                c.execute(
                    "UPDATE eval_batches SET labeled=(SELECT COUNT(*) FROM eval_cases "
                    "WHERE batch_id=eval_batches.id AND status IN ('labeled','skipped')) "
                    "WHERE id=(SELECT batch_id FROM eval_cases WHERE id=?)", (cid,)
                )
            self._json({"ok": True, "stats": stats(), "next": next_case()})
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    if not (UI / "index.html").exists():
        raise SystemExit(f"缺少界面文件：{UI / 'index.html'}")
    global HTTPD
    HTTPD = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv = HTTPD
    print(f"[eval-ui] http://127.0.0.1:{args.port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
