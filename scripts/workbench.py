#!/usr/bin/env python3
"""管理人工作台 v0.1：规则 / Canon / 数据管线 / 变更 / 发布 + 评测入口。

启动：
  .venv/bin/python scripts/workbench.py --port 8099
浏览器：
  http://127.0.0.1:8099/
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import eval_ui as ev  # noqa: E402

DB = ROOT / "data" / "bot.db"
POLICY = ROOT / "control" / "policy.json"
RELEASE = ROOT / "control" / "release.json"
UI = ROOT / "workbench_ui"


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init_db() -> None:
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS policy_rules(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT UNIQUE, scope TEXT, priority INTEGER DEFAULT 100,
            text TEXT NOT NULL, status TEXT DEFAULT 'active',
            version INTEGER DEFAULT 0, created_at INTEGER, updated_at INTEGER);
        CREATE TABLE IF NOT EXISTS policy_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER UNIQUE, rules_json TEXT, note TEXT,
            created_at INTEGER, active INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS canon_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT, title TEXT, source TEXT, summary TEXT, tags TEXT,
            status TEXT DEFAULT 'draft', created_at INTEGER, updated_at INTEGER);
        CREATE TABLE IF NOT EXISTS change_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, problem TEXT, hypothesis TEXT, artifact TEXT,
            metric TEXT, rollback TEXT, status TEXT DEFAULT 'draft',
            created_at INTEGER, updated_at INTEGER);
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT, action TEXT, target TEXT, detail TEXT, created_at INTEGER);
        """)
        # 从 control/policy.json 同步初始规则（仅空表时）
        n = c.execute("SELECT COUNT(*) FROM policy_rules").fetchone()[0]
        if n == 0 and POLICY.exists():
            try:
                data = json.loads(POLICY.read_text(encoding="utf-8"))
                for r in data.get("rules", []):
                    c.execute(
                        "INSERT OR IGNORE INTO policy_rules(rule_id, scope, priority, text, "
                        "status, version, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (r.get("id") or f"r{int(time.time()*1000)}", r.get("scope", "group"),
                         int(r.get("priority", 100)), r.get("text", ""),
                         r.get("status", "active"), int(data.get("version", 0)),
                         int(time.time()), int(time.time())))
            except Exception:
                pass


def audit(action: str, target: str, detail: str = "") -> None:
    with conn() as c:
        c.execute("INSERT INTO audit_log(actor, action, target, detail, created_at) "
                  "VALUES('master',?,?,?,?)", (action, target, detail, int(time.time())))


def release_info() -> dict:
    try:
        data = json.loads(RELEASE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return data


def dashboard() -> dict:
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM eval_cases").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM eval_cases WHERE status='pending'").fetchone()[0]
        labels = c.execute(
            "SELECT label, COUNT(*) n FROM eval_cases WHERE status='labeled' "
            "GROUP BY label ORDER BY n DESC").fetchall()
        labeled = sum(r["n"] for r in labels)
        good = sum(r["n"] for r in labels if r["label"] in ("human_like", "ok"))
        bad = sum(r["n"] for r in labels if r["label"] in ("cold", "hallucinated", "should_not"))
        since = int(time.time()) - 7 * 86400
        user_n = c.execute("SELECT COUNT(*) FROM group_log WHERE role='user' AND ts>=?",
                           (since,)).fetchone()[0]
        bot_n = c.execute("SELECT COUNT(*) FROM group_log WHERE role='assistant' AND ts>=?",
                          (since,)).fetchone()[0]
        corpus_n = c.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
        canon_n = c.execute("SELECT COUNT(*) FROM canon_items").fetchone()[0]
        rules_n = c.execute("SELECT COUNT(*) FROM policy_rules WHERE status='active'").fetchone()[0]
        changes_n = c.execute("SELECT COUNT(*) FROM change_requests WHERE status!='released'").fetchone()[0]
        recent = c.execute(
            "SELECT id, label, note FROM eval_cases WHERE status='labeled' "
            "ORDER BY labeled_at DESC LIMIT 5").fetchall()
    return {
        "eval": {"total": total, "pending": pending, "labeled": labeled,
                 "like_rate": round(good / labeled * 100) if labeled else 0,
                 "bad_rate": round(bad / labeled * 100) if labeled else 0,
                 "labels": [{"key": r["label"],
                             "name": ev.LABELS.get(r["label"], (r["label"], ""))[0],
                             "count": r["n"]} for r in labels]},
        "group": {"user_7d": user_n, "bot_7d": bot_n},
        "corpus": corpus_n, "canon": canon_n, "rules": rules_n,
        "changes": changes_n,
        "release": release_info(),
        "recent": [{"id": r["id"], "label": r["label"], "note": r["note"] or ""} for r in recent],
    }


def rules() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM policy_rules ORDER BY status='active' DESC, priority, id").fetchall()
    return [dict(r) for r in rows]


def canon() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM canon_items ORDER BY id DESC LIMIT 200").fetchall()
    return [dict(r) for r in rows]


def changes() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM change_requests ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


def pipeline() -> dict:
    with conn() as c:
        raw = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        glean = c.execute("SELECT COUNT(*) FROM group_log").fetchone()[0]
        cases = c.execute("SELECT COUNT(*) FROM eval_cases").fetchone()[0]
        labeled = c.execute("SELECT COUNT(*) FROM eval_cases WHERE status='labeled'").fetchone()[0]
        pref = 0
        p = ROOT / "data" / "eval" / "preferences.jsonl"
        if p.exists():
            pref = sum(1 for _ in p.open(encoding="utf-8"))
    return {"raw_messages": raw, "group_log": glean, "eval_cases": cases,
            "labeled": labeled, "preferences": pref}


def publish(note: str = "") -> dict:
    with conn() as c:
        active = c.execute("SELECT * FROM policy_rules WHERE status='active' ORDER BY priority, id").fetchall()
        rules_json = json.dumps([{"id": r["rule_id"], "scope": r["scope"],
                                  "priority": r["priority"], "text": r["text"]}
                                 for r in active], ensure_ascii=False)
        ver = (c.execute("SELECT MAX(version) FROM policy_versions").fetchone()[0] or 0) + 1
        c.execute("INSERT INTO policy_versions(version, rules_json, note, created_at, active) "
                  "VALUES(?,?,?,?,0)", (ver, rules_json, note[:200], int(time.time())))
        c.execute("UPDATE policy_versions SET active=0")
        c.execute("UPDATE policy_versions SET active=1 WHERE version=?", (ver,))
        POLICY.parent.mkdir(exist_ok=True)
        POLICY.write_text(json.dumps({"version": ver, "updated_at": int(time.time()),
                                      "note": note[:200],
                                      "rules": json.loads(rules_json)},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            rel = release_info()
            rel.update({"policy_version": ver, "updated_at": int(time.time()),
                        "note": note[:200]})
            RELEASE.write_text(json.dumps(rel, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    audit("publish_policy", f"v{ver}", note)
    return {"version": ver, "rules": len(active)}


class Handler(ev.Handler):
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            self._file(UI / "index.html", "text/html; charset=utf-8")
        elif p == "/workbench.css":
            self._file(UI / "style.css", "text/css; charset=utf-8")
        elif p == "/workbench.js":
            self._file(UI / "app.js", "application/javascript; charset=utf-8")
        elif p == "/eval":
            self._file(ev.UI / "index.html", "text/html; charset=utf-8")
        elif p == "/api/workbench/dashboard":
            self._json(dashboard())
        elif p == "/api/workbench/rules":
            self._json({"rules": rules(), "release": release_info()})
        elif p == "/api/workbench/canon":
            self._json({"canon": canon()})
        elif p == "/api/workbench/changes":
            self._json({"changes": changes()})
        elif p == "/api/workbench/pipeline":
            self._json(pipeline())
        elif p == "/api/workbench/release":
            with conn() as c:
                vs = c.execute("SELECT version, note, created_at, active FROM policy_versions "
                               "ORDER BY version DESC LIMIT 20").fetchall()
            self._json({"release": release_info(),
                        "versions": [dict(v) for v in vs]})
        else:
            super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json({"error": "bad json"}, 400)
            return
        try:
            if p == "/api/workbench/rules":
                text = str(data.get("text", "")).strip()
                if not text:
                    self._json({"error": "text required"}, 400)
                    return
                scope = str(data.get("scope", "group"))[:20]
                pr = int(data.get("priority", 100))
                rid = f"r{int(time.time()*1000)}"
                with conn() as c:
                    c.execute("INSERT INTO policy_rules(rule_id, scope, priority, text, status, "
                              "version, created_at, updated_at) VALUES(?,?,?,?, 'active', 0, ?, ?)",
                              (rid, scope, pr, text[:500], int(time.time()), int(time.time())))
                audit("add_rule", rid, text[:80])
                self._json({"ok": True, "rules": rules()})
            elif p == "/api/workbench/rules/toggle":
                rid = int(data.get("id", 0))
                status = str(data.get("status", "active"))
                if status not in ("active", "disabled"):
                    self._json({"error": "bad status"}, 400)
                    return
                with conn() as c:
                    c.execute("UPDATE policy_rules SET status=?, updated_at=? WHERE id=?",
                              (status, int(time.time()), rid))
                audit("toggle_rule", str(rid), status)
                self._json({"ok": True, "rules": rules()})
            elif p == "/api/workbench/publish":
                res = publish(str(data.get("note", "")))
                self._json({"ok": True, **res, "release": release_info()})
            elif p == "/api/workbench/canon":
                title = str(data.get("title", "")).strip()
                if not title:
                    self._json({"error": "title required"}, 400)
                    return
                with conn() as c:
                    c.execute("INSERT INTO canon_items(type, title, source, summary, tags, status, "
                              "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                              (str(data.get("type", "work"))[:20], title[:120],
                               str(data.get("source", ""))[:120], str(data.get("summary", ""))[:1000],
                               str(data.get("tags", ""))[:200], "draft",
                               int(time.time()), int(time.time())))
                audit("add_canon", title)
                self._json({"ok": True, "canon": canon()})
            elif p == "/api/workbench/changes":
                title = str(data.get("title", "")).strip()
                if not title:
                    self._json({"error": "title required"}, 400)
                    return
                with conn() as c:
                    c.execute("INSERT INTO change_requests(title, problem, hypothesis, artifact, "
                              "metric, rollback, status, created_at, updated_at) "
                              "VALUES(?,?,?,?,?,?, 'draft', ?, ?)",
                              (title[:120], str(data.get("problem", ""))[:1000],
                               str(data.get("hypothesis", ""))[:1000],
                               str(data.get("artifact", ""))[:200],
                               str(data.get("metric", ""))[:300],
                               str(data.get("rollback", ""))[:500],
                               int(time.time()), int(time.time())))
                audit("add_change", title)
                self._json({"ok": True, "changes": changes()})
            elif p == "/api/workbench/changes/status":
                cid = int(data.get("id", 0))
                status = str(data.get("status", "draft"))
                if status not in ("draft", "approved", "canary", "released", "rolled_back"):
                    self._json({"error": "bad status"}, 400)
                    return
                with conn() as c:
                    c.execute("UPDATE change_requests SET status=?, updated_at=? WHERE id=?",
                              (status, int(time.time()), cid))
                audit("change_status", str(cid), status)
                self._json({"ok": True, "changes": changes()})
            else:
                super().do_POST()
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    init_db()
    from http.server import ThreadingHTTPServer
    global HTTPD
    HTTPD = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    ev.HTTPD = HTTPD
    print(f"[workbench] http://127.0.0.1:{args.port}/", flush=True)
    HTTPD.serve_forever()


HTTPD = None
if __name__ == "__main__":
    main()
