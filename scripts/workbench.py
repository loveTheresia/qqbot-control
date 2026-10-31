#!/usr/bin/env python3
"""孤独的鱼：规则 / Canon / 数据管线 / 变更 / 发布 + 评测入口。

启动：
  .venv/bin/python scripts/workbench.py --port 8099
浏览器：
  http://127.0.0.1:8099/
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import eval_ui as ev  # noqa: E402

_spec = importlib.util.spec_from_file_location("qqbot_workbench", ROOT / "bot" / "bot.py")
BOT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(BOT)

DB = ROOT / "data" / "bot.db"
POLICY = ROOT / "control" / "policy.json"
CANON_FILE = ROOT / "control" / "canon.json"
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


def load_env() -> dict:
    cfg = {}
    p = ROOT / "config" / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def get_state(k: str) -> str:
    with conn() as c:
        row = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return row["v"] if row else ""


def set_state(k: str, v: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (k, v))


def gh_request(method: str, path: str, data: dict | None = None) -> dict:
    cfg = load_env()
    repo = cfg.get("GH_REPO", "")
    token = cfg.get("GH_TOKEN", "")
    if not repo or not token:
        raise RuntimeError("GH_REPO / GH_TOKEN 未配置")
    url = f"https://api.github.com/repos/{repo}{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "qqbot-workbench",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="ignore")
        try:
            msg = json.loads(raw).get("message", raw)
        except Exception:
            msg = raw
        raise RuntimeError(f"GitHub {e.code}: {msg[:300]}")


def github_status() -> dict:
    cfg = load_env()
    out = {"configured": bool(cfg.get("GH_REPO") and cfg.get("GH_TOKEN")),
           "repo": cfg.get("GH_REPO", ""), "branch": cfg.get("GH_BRANCH", "main"),
           "pr": None, "checks": [], "head": "", "message": ""}
    if not out["configured"]:
        out["message"] = "请在 config/.env 配置 GH_REPO / GH_TOKEN"
        return out
    try:
        ref = gh_request("GET", f"/git/ref/heads/{out['branch']}")
        out["head"] = ref.get("object", {}).get("sha", "")
        pr_raw = get_state("gh_pr")
        pr = json.loads(pr_raw) if pr_raw else None
        if pr:
            try:
                live = gh_request("GET", f"/pulls/{pr['number']}")
                pr.update({"state": live.get("state"), "mergeable": live.get("mergeable"),
                           "url": live.get("html_url", pr.get("url", "")),
                           "head": live.get("head", {}).get("sha", pr.get("head", ""))})
                out["pr"] = pr
            except Exception:
                out["pr"] = pr
        check_sha = pr.get("head") if pr else out["head"]
        checks = gh_request("GET", f"/commits/{check_sha}/check-runs")
        out["checks"] = [{"name": c.get("name"), "status": c.get("status"),
                          "conclusion": c.get("conclusion"),
                          "url": c.get("details_url")} for c in checks.get("check_runs", [])]
    except Exception as e:
        out["message"] = str(e)
    return out


def next_control_version() -> int:
    try:
        refs = gh_request("GET", "/git/matching-refs/tags/control-v")
        n = 0
        for r in refs:
            m = re.search(r"/control-v(\d+)$", str(r.get("ref", "")))
            if m:
                n = max(n, int(m.group(1)))
        return n + 1
    except Exception:
        try:
            return int(release_info().get("control_version", 0)) + 1
        except Exception:
            return 1


def github_create_pr() -> dict:
    cfg = load_env()
    repo = cfg.get("GH_REPO", "")
    branch = cfg.get("GH_BRANCH", "main")
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    ver = int(policy.get("version", 0))
    ctrl_ver = next_control_version()
    existing = get_state("gh_pr")
    if existing:
        pr = json.loads(existing)
        if pr.get("state", "open") == "open":
            return {"ok": True, "pr": pr, "reused": True}
    head = gh_request("GET", f"/git/ref/heads/{branch}")["object"]["sha"]
    base_commit = gh_request("GET", f"/git/commits/{head}")
    tree_sha = base_commit["tree"]["sha"]
    cfile = canon_file()
    files = {
        "control/policy.json": policy,
        "control/release.json": {"control_version": ctrl_ver,
                                 "policy_version": ver,
                                 "canon_version": cfile["version"],
                                 "updated_at": int(time.time()),
                                 "note": policy.get("note", "")},
        "control/canon.json": cfile,
    }
    blobs = []
    for path, obj in files.items():
        content = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
        blob = gh_request("POST", "/git/blobs",
                          {"content": base64.b64encode(content.encode()).decode(),
                           "encoding": "base64"})
        blobs.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = gh_request("POST", "/git/trees", {"base_tree": tree_sha, "tree": blobs})
    author = {"name": cfg.get("GH_AUTHOR_NAME", "workbench"),
              "email": cfg.get("GH_AUTHOR_EMAIL", "workbench@users.noreply.github.com")}
    commit = gh_request("POST", "/git/commits", {
        "message": f"control: v{ctrl_ver}", "tree": tree["sha"],
        "parents": [head], "author": author, "committer": author})
    new_branch = f"control/v{ctrl_ver}-{int(time.time())}"
    gh_request("POST", "/git/refs",
               {"ref": f"refs/heads/{new_branch}", "sha": commit["sha"]})
    body = (f"自动提交 control v{ver}。\n\n"
            f"- 规则数：{len(policy.get('rules', []))}\n"
            f"- 备注：{policy.get('note', '')}\n")
    pr = gh_request("POST", "/pulls", {
        "title": f"control: v{ctrl_ver}", "head": new_branch, "base": branch, "body": body})
    pr_state = {"number": pr["number"], "url": pr["html_url"], "branch": new_branch,
                "head": commit["sha"], "version": ver,
                "control_version": ctrl_ver, "state": "open"}
    set_state("gh_pr", json.dumps(pr_state, ensure_ascii=False))
    audit("github_pr", str(pr["number"]), new_branch)
    return {"ok": True, "pr": pr_state}


def github_pull() -> dict:
    cfg = load_env()
    branch = cfg.get("GH_BRANCH", "main")
    content = gh_request("GET", f"/contents/control/policy.json?ref={branch}")
    remote = json.loads(base64.b64decode(content["content"]).decode())
    if not isinstance(remote.get("version"), int) or not isinstance(remote.get("rules"), list):
        raise RuntimeError("远端 policy 结构不合法，拒绝覆盖")
    POLICY.write_text(json.dumps(remote, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        ccontent = gh_request("GET", f"/contents/control/canon.json?ref={branch}")
        remote_canon = json.loads(base64.b64decode(ccontent["content"]).decode())
        CANON_FILE.write_text(json.dumps(remote_canon, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
        sync_canon_db(remote_canon)
    except Exception as e:
        print(f"[github] canon pull: {e}")
    rel = release_info()
    rel.update({"policy_version": remote["version"], "updated_at": int(time.time()),
                "note": f"github pull {branch}"})
    RELEASE.write_text(json.dumps(rel, ensure_ascii=False, indent=2), encoding="utf-8")
    audit("github_pull", branch, f"v{remote['version']}")
    return {"ok": True, "policy_version": remote["version"], "rules": len(remote["rules"])}


def github_merge() -> dict:
    raw = get_state("gh_pr")
    if not raw:
        raise RuntimeError("当前没有待合并的 PR")
    pr = json.loads(raw)
    res = gh_request("PUT", f"/pulls/{pr['number']}/merge",
                     {"merge_method": "squash"})
    sha = res.get("sha", "")
    tag = f"control-v{pr.get('control_version', pr.get('version', 0))}"
    try:
        gh_request("POST", "/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
    except Exception as e:
        # tag 已存在不阻塞合并
        print(f"[github] tag {tag}: {e}")
    # 以 GitHub 合并后的 policy 为准，写回本地
    cfg = load_env()
    repo = cfg.get("GH_REPO", "")
    content = gh_request("GET", f"/contents/control/policy.json?ref={sha}")
    remote = json.loads(base64.b64decode(content["content"]).decode())
    POLICY.write_text(json.dumps(remote, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        ccontent = gh_request("GET", f"/contents/control/canon.json?ref={sha}")
        remote_canon = json.loads(base64.b64decode(ccontent["content"]).decode())
        CANON_FILE.write_text(json.dumps(remote_canon, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
        sync_canon_db(remote_canon)
    except Exception as e:
        print(f"[github] canon sync: {e}")
    rel = release_info()
    rel.update({"control_version": pr.get("control_version", 0),
                "policy_version": remote.get("version", pr.get("version", 0)),
                "updated_at": int(time.time()), "note": f"github {tag}"})
    RELEASE.write_text(json.dumps(rel, ensure_ascii=False, indent=2), encoding="utf-8")
    set_state("gh_pr", "")
    set_state("gh_last_release", json.dumps({"tag": tag, "sha": sha,
                                             "version": remote.get("version", 0)}))
    audit("github_merge", tag, sha)
    return {"ok": True, "tag": tag, "sha": sha, "policy_version": remote.get("version", 0)}


def builtin_rules() -> dict:
    return {"rules": BOT.RULES, "group": BOT.GROUP_RULES}


def params_info() -> dict:
    BOT.reload_params()
    groups = {
        "回复节奏": [
            ("REPLY_DELAY_MIN", "收到消息后的基础反应延迟下限（秒）"),
            ("REPLY_DELAY_MAX", "收到消息后的基础反应延迟上限（秒）"),
            ("READ_PER_CHAR", "每字阅读时间（秒）"),
            ("READ_MAX", "阅读时间上限（秒）"),
            ("DELAY_MIN", "每句之间停顿下限（秒）"),
            ("DELAY_MAX", "每句之间停顿上限（秒）"),
            ("TYPE_PER_CHAR_MIN", "每字打字时间下限（秒）"),
            ("TYPE_PER_CHAR_MAX", "每字打字时间上限（秒）"),
            ("TYPE_DELAY_MAX", "单句打字时间上限（秒）"),
            ("BATCH_PAUSE_MIN", "连发一批后的停顿下限（秒）"),
            ("BATCH_PAUSE_MAX", "连发一批后的停顿上限（秒）"),
            ("JITTER_MIN", "随机抖动下限（秒）"),
            ("JITTER_MAX", "随机抖动上限（秒）"),
        ],
        "群聊频率": [
            ("FOLLOWUP_WINDOW", "她发言后多久内继续判断接话（秒）"),
            ("GROUP_REPLY_COOLDOWN", "她说完后至少隔多久才接非直接消息（秒）"),
            ("GROUP_MIN_OTHERS_MSGS", "她说完后至少几条别人的消息才接"),
            ("GROUP_MAX_REPLIES_10MIN", "10 分钟内最多几条非直接回复"),
            ("AMBIENT_MODE", "主动插话模式：off / conservative"),
            ("LULL_SEC", "群聊空档多少秒才考虑主动插话"),
            ("AMBIENT_THRESHOLD", "主动插话置信度阈值"),
            ("AMBIENT_MAX_10MIN", "10 分钟最多主动插话次数"),
            ("AMBIENT_COOLDOWN", "两次主动插话最短间隔（秒）"),
        ],
        "热情/随机": [
            ("REPLY_PROB", "私聊随机回复概率（0~1）"),
            ("AMBIENT_PROB", "旧 social 模式的随机插话概率；conservative 下不用"),
            ("MUTE_PROB", "随机装没看见的概率"),
        ],
    }
    data = {}
    for group, keys in groups.items():
        rows = []
        for key, desc in keys:
            try:
                val = BOT.pget(key) if key in BOT.PARAM_TYPES else getattr(BOT, key)
            except Exception:
                val = None
            rows.append({"key": key, "desc": desc, "value": val})
        data[group] = rows
    return {"groups": data,
            "hot": sorted(BOT.PARAM_TYPES.keys()),
            "note": "带 /参数 的键可热调；其余改 config/.env 后需重启。没有单一“热情值”，热情=回复概率+主动插话+提问补全共同作用。"}


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


def canon_file() -> dict:
    with conn() as c:
        rows = c.execute("SELECT * FROM canon_items ORDER BY id").fetchall()
    items = [{"id": r["id"], "type": r["type"], "title": r["title"],
              "source": r["source"], "summary": r["summary"], "tags": r["tags"],
              "status": r["status"]} for r in rows]
    return {"version": int(time.time()), "updated_at": int(time.time()), "items": items}


def sync_canon_db(data: dict) -> None:
    with conn() as c:
        for it in data.get("items", []):
            if not isinstance(it, dict) or not it.get("title"):
                continue
            title = str(it["title"])[:120]
            row = c.execute("SELECT id FROM canon_items WHERE title=?", (title,)).fetchone()
            if row:
                c.execute("UPDATE canon_items SET type=?, source=?, summary=?, tags=?, "
                          "status=?, updated_at=? WHERE id=?",
                          (str(it.get("type", "work"))[:20], str(it.get("source", ""))[:120],
                           str(it.get("summary", ""))[:1000], str(it.get("tags", ""))[:200],
                           str(it.get("status", "draft"))[:20], int(time.time()), row["id"]))
            else:
                c.execute("INSERT INTO canon_items(type, title, source, summary, tags, status, "
                          "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                          (str(it.get("type", "work"))[:20], title,
                           str(it.get("source", ""))[:120], str(it.get("summary", ""))[:1000],
                           str(it.get("tags", ""))[:200], str(it.get("status", "draft"))[:20],
                           int(time.time()), int(time.time())))


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
        c.execute("UPDATE policy_rules SET version=? WHERE status='active'", (ver,))
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
        elif p == "/api/workbench/builtin":
            self._json(builtin_rules())
        elif p == "/api/workbench/params":
            self._json(params_info())
        elif p == "/api/workbench/github":
            self._json(github_status())
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
                cur_ver = int(release_info().get("policy_version", 0))
                with conn() as c:
                    draft = c.execute(
                        "SELECT COUNT(*) FROM policy_rules WHERE status='active' AND version!=?",
                        (cur_ver,)).fetchone()[0]
                if draft >= 1:
                    self._json({"error": "已有一条未发布规则。先发布并观察，再改下一条。"}, 409)
                    return
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
                cur_ver = int(release_info().get("policy_version", 0))
                with conn() as c:
                    draft = c.execute(
                        "SELECT COUNT(*) FROM policy_rules WHERE status='active' AND version!=? AND id!=?",
                        (cur_ver, rid)).fetchone()[0]
                if status == "active" and draft >= 1:
                    self._json({"error": "已有一条未发布规则。先发布并观察，再启用下一条。"}, 409)
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
            elif p == "/api/workbench/canon/status":
                cid = int(data.get("id", 0))
                status = str(data.get("status", "draft"))
                if status not in ("draft", "active", "approved", "retired"):
                    self._json({"error": "bad status"}, 400)
                    return
                with conn() as c:
                    c.execute("UPDATE canon_items SET status=?, updated_at=? WHERE id=?",
                              (status, int(time.time()), cid))
                audit("canon_status", str(cid), status)
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
            elif p == "/api/workbench/github/pr":
                self._json(github_create_pr())
            elif p == "/api/workbench/github/pull":
                self._json(github_pull())
            elif p == "/api/workbench/github/merge":
                self._json(github_merge())
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
    print(f"[孤独的鱼] http://127.0.0.1:{args.port}/", flush=True)
    HTTPD.serve_forever()


HTTPD = None
if __name__ == "__main__":
    main()
