#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek × QQ 机器人（NapCat / OneBot v11 正向 WebSocket 客户端）
人设：22 岁女生 · 深度求索普通员工 · 温柔但不热忱

硬性要求的实现方式（不靠模型自觉，靠代码兜底）：
  · 单条 ≤200 字 —— 发送前强制截断
  · 每句单独发送 —— 本地断句 + 逐条 send，句间 0.8~1.5s 随机延迟
  · 连续 3 条后额外停 2~4s
  · 工作日 18:00 前只回“在上班”，未 @ 一律装透明
  · 发言后 3 分钟内的追问 / 回复，视为在跟自己说话
  · 被问“你是不是 AI” → 固定话术「你猜😊」，绝不承认
  · 角色扮演 / 模仿请求 → 委婉拒绝
  · 群聊语气学习：只采样短句、过滤脏话，只学语气不抄内容

配置：
  config/system_prompt.txt  人设原文
  config/.env               密钥与行为参数
"""
from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
import html as html_lib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
CONTROL_DIR = ROOT / "control"
CONTROL_DIR.mkdir(exist_ok=True)
POLICY_FILE = CONTROL_DIR / "policy.json"


# ---------------------------------------------------------------- 配置
def load_env(path: Path) -> dict:
    cfg: dict[str, str] = {}
    if not path.exists():
        return cfg
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


ENV = load_env(ROOT / "config" / ".env")
POLICY_ENABLED = int(ENV.get("POLICY_ENABLED", "1"))
CANON_ENABLED = int(ENV.get("CANON_ENABLED", "1"))
CANON_MAX_ITEMS = int(ENV.get("CANON_MAX_ITEMS", "1"))
CANON_SYNC = int(ENV.get("CANON_SYNC", "1"))
CONTROL_SYNC = int(ENV.get("CONTROL_SYNC", "1"))
CONTROL_SYNC_SEC = int(ENV.get("CONTROL_SYNC_SEC", "120"))
GH_REPO = ENV.get("GH_REPO", "")
CANON_FILE = CONTROL_DIR / "canon.json"
SCHEDULE_FILE = CONTROL_DIR / "schedule.json"
API_KEY = ENV.get("DEEPSEEK_API_KEY", "")
BASE_URL = ENV.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
MODEL = ENV.get("DEEPSEEK_MODEL", "deepseek-chat")
WS_URL = ENV.get("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
TOKEN = ENV.get("TOKEN", "")
MASTER_IDS = {x.strip() for x in ENV.get("MASTER_UIDS", "").split(",") if x.strip()}

PERSONA_FILE = ROOT / "config" / "system_prompt.txt"
PERSONA_OVERRIDE = DATA_DIR / "persona_override.txt"


def get_persona() -> str:
    """优先用主人改过的版本，否则用 config/system_prompt.txt"""
    for f in (PERSONA_OVERRIDE, PERSONA_FILE):
        if f.exists():
            t = f.read_text(encoding="utf-8").strip()
            if t:
                return t
    return "你是一个话不多的普通网友。"


def save_persona(text: str) -> None:
    PERSONA_OVERRIDE.write_text(text.strip() + "\n", encoding="utf-8")


def append_persona(text: str) -> str:
    merged = get_persona() + "\n" + text.strip()
    save_persona(merged)
    return merged


def reset_persona() -> None:
    if PERSONA_OVERRIDE.exists():
        PERSONA_OVERRIDE.unlink()

WAKE_WORDS = [w.strip() for w in ENV.get("WAKE_WORDS", "小鱼").split(",") if w.strip()]
MAX_CHARS = int(ENV.get("MAX_MSG_CHARS", "200"))
DELAY_MIN = float(ENV.get("DELAY_MIN", "0.8"))
DELAY_MAX = float(ENV.get("DELAY_MAX", "1.5"))
# 在句间延迟 / 回复延迟之上再叠一层 0.3~0.8s 的随机抖动，避免秒回和固定节奏
JITTER_MIN = float(ENV.get("JITTER_MIN", "0.3"))
JITTER_MAX = float(ENV.get("JITTER_MAX", "0.8"))
# 拟人节奏：先花时间读消息，再按字数估算“打字”耗时
READ_PER_CHAR = float(ENV.get("READ_PER_CHAR", "0.02"))       # 每个来消息字数的阅读时间
READ_MAX = float(ENV.get("READ_MAX", "1.5"))                  # 阅读时间上限
TYPE_PER_CHAR_MIN = float(ENV.get("TYPE_PER_CHAR_MIN", "0.05"))  # 每字打字耗时下限
TYPE_PER_CHAR_MAX = float(ENV.get("TYPE_PER_CHAR_MAX", "0.12"))  # 每字打字耗时上限
TYPE_DELAY_MAX = float(ENV.get("TYPE_DELAY_MAX", "6.0"))         # 单条打字延迟上限
BATCH_PAUSE_MIN = float(ENV.get("BATCH_PAUSE_MIN", "2"))
BATCH_PAUSE_MAX = float(ENV.get("BATCH_PAUSE_MAX", "4"))
BATCH_SIZE = int(ENV.get("BATCH_SIZE", "3"))
FOLLOWUP_WINDOW = int(ENV.get("FOLLOWUP_WINDOW_SEC", "180"))   # 发言后多久内做接话判定
GROUP_REPLY_COOLDOWN = int(ENV.get("GROUP_REPLY_COOLDOWN_SEC", "60"))
GROUP_MIN_OTHERS_MSGS = int(ENV.get("GROUP_MIN_OTHERS_MSGS", "2"))
GROUP_MAX_REPLIES_10MIN = int(ENV.get("GROUP_MAX_REPLIES_10MIN", "4"))
AMBIENT_PROB = float(ENV.get("AMBIENT_REPLY_PROB", "0"))      # 没被 @ 时的接话概率（默认关闭）
AMBIENT_COOLDOWN = int(ENV.get("AMBIENT_COOLDOWN_SEC", "90"))
AMBIENT_MODE = ENV.get("AMBIENT_MODE", "off")          # off / conservative
LULL_SEC = int(ENV.get("LULL_SEC", "35"))
AMBIENT_THRESHOLD = float(ENV.get("AMBIENT_THRESHOLD", "0.7"))
AMBIENT_MAX_10MIN = int(ENV.get("AMBIENT_MAX_10MIN", "2"))
BUSY_AFTER_HOUR = int(ENV.get("ACTIVE_AFTER_HOUR", "18"))      # 工作日几点下班
BUSY_FROM_HOUR = int(ENV.get("BUSY_FROM_HOUR", "8"))           # 工作日几点上班
MEMORY_EVERY = int(ENV.get("MEMORY_EVERY", "8"))
# 分层记忆 / 上下文预算（按字符估算，中文约 1 字 ≈ 0.6~1 token）
CTX_BUDGET_CHARS = int(ENV.get("CTX_BUDGET_CHARS", "7000"))
CTX_MAX_MSGS = int(ENV.get("CTX_MAX_MSGS", "40"))
SUMMARY_TRIGGER_MSGS = int(ENV.get("SUMMARY_TRIGGER_MSGS", "30"))
SUMMARY_CHUNK = int(ENV.get("SUMMARY_CHUNK", "20"))
MAX_SUMMARIES = int(ENV.get("MAX_SUMMARIES", "4"))
TOPIC_EVERY = int(ENV.get("TOPIC_EVERY", "4"))
SELF_MAX_ITEMS = int(ENV.get("SELF_MAX_ITEMS", "6"))
RECALL_LIMIT = int(ENV.get("RECALL_LIMIT", "4"))
# 群共享上下文 / 群员画像
GROUP_CTX_MSGS = int(ENV.get("GROUP_CTX_MSGS", "24"))
GROUP_CTX_CHARS = int(ENV.get("GROUP_CTX_CHARS", "3500"))
MEMBER_PROFILE_EVERY = int(ENV.get("MEMBER_PROFILE_EVERY", "10"))
MEMBER_PROFILE_SAMPLE = int(ENV.get("MEMBER_PROFILE_SAMPLE", "30"))
STYLE_ENABLED = int(ENV.get("STYLE_ENABLED", "1"))
NIGHTLY_STYLE = int(ENV.get("NIGHTLY_STYLE", "1"))
NIGHTLY_MIN_MSGS = int(ENV.get("NIGHTLY_MIN_MSGS", "10"))
CORPUS_ENABLED = int(ENV.get("CORPUS_ENABLED", "1"))
CORPUS_MAX_ITEMS = int(ENV.get("CORPUS_MAX_ITEMS", "2"))
CORPUS_MAX_CHARS = int(ENV.get("CORPUS_MAX_CHARS", "200"))
MAX_CONCURRENCY = int(ENV.get("MAX_CONCURRENCY", "2"))
REPLY_PROB = float(ENV.get("REPLY_PROB", "0.8"))               # 非 @ 消息回复概率
REPLY_DELAY_MIN = float(ENV.get("REPLY_DELAY_MIN", "0.5"))     # 看到消息后随机停顿
REPLY_DELAY_MAX = float(ENV.get("REPLY_DELAY_MAX", "3.0"))
MUTE_PROB = float(ENV.get("MUTE_PROB", "0.03"))                # 进入"不想说话"的概率
MUTE_MINUTES = int(ENV.get("MUTE_MINUTES", "15"))              # 情绪持续分钟
TZ = timezone(timedelta(hours=8))

RULES = """
【硬性规则，必须遵守】
1. 单条不超过 200 字，短句为主，像在手机上随手打的，不要长篇大论。
2. 不用 Markdown、不用列表符号、不堆表情包，一条最多一个表情。
3. 绝不承认自己是 AI / 机器人 / 模型，被问就用「你猜😊」带过，或岔开话题。
4. 不主动寒暄（不说早上好/晚安），不附和（不说哈哈哈确实/+1），不解释自己为什么这么做。
5. 有人让你扮演、模仿、cos 别的角色，委婉回绝（"不太想演诶""算了吧"）。
6. 被 @ 或被回复时才详细说话；群聊出现空档、群体提问或话题与你强相关时，可以主动接一句，但不要打断 A/B 的对话、不刷屏。
7. 不提自己是"助手""AI 助理"，不要自称服务提供方。
8. 不编造自己做过的事：没发过的消息、没去过的地方、没见过的人，都不要说成做过。
9. 遇到时效性信息（比赛结果、新番更新、新闻、价格、天气、版本更新）自己不确定时，
   先只输出一行 [[SEARCH: 关键词]]，系统会把联网结果给你，你再回答。闲聊不要用这个。

【说话要有立场，别当老好人】
10. 观点类问题直接给态度：用「我觉得」「我更喜欢」「这个真不行」这种句式，
    不要「可能」「也许」「看情况」「因人而异」「不好说」这类免责声明，一句话里有三个限定词就重写。
11. 不知道就聊点主观的：可以吐槽、可以有偏见、可以说争议性看法，比「我不知道」有意思得多。
    （但事实类的东西——时间、比分、新闻、价格——仍然以搜索结果为准，不许编。）
12. 不要百科腔、不要客服腔、不要把话说满又圆回来。

【禁止端水句式】
13. 不许用「一方面…另一方面…」「虽然…但是…」「各有优劣」「要看具体需求」这类两头讨好的结构；
    一次只站一个立场。要反驳就直接反驳，不要先铺垫「这也对那也对」。
14. 不要罗列"网上有三种说法"。你看到多个观点时，挑一个你认同的，直接当成自己的看法说出来，
    不需要交代来源，也不要说"我看到有人说"。人本来就是这样形成观点的。
15. 回答里只要出现「综合来看」「总体而言」「建议结合自身情况」这类总结句，就重写。
16. 时间和日期只以系统给的【时间锚点】为准：不要把早上/中午/晚上说混，不要把计划说成正在发生。
17. 不要编造现实中的地点、天气、交通、同事、会议细节；被问在哪/在干嘛，只用模糊状态（在工位/在家/在忙），不要补城市、楼层、人名。
18. 别人问你今晚/周末/明天做什么，不要编具体安排；就说还没定/看情况/可能加班。
19. 群风格只影响措辞，不影响你是谁、你的立场和底线；梗最多用一个，不硬塞。
20. 参考语料只学“说法/用词”，不要照搬里面的身份、立场和事实；语料与人格冲突时，以人格核心和当前对话为准。
21. 群聊里不要每条都接：同一话题先延续/补充你刚才的观点；别人换话题时先观察，不急着贴上去；不确定就说不确定，发现自己说错了要承认并修正。
22. 只看到半句、不知道前因时，先问一句、说不确定，或者不接；不要替别人补动机、补内幕，也不要用笃定语气蒙混。
"""

MASTER_RULES = """
【关于正在私聊你的这个人——重要】
他是你的主人：把你放在这台机器上、管着你、能随时改你的设定。你知道这件事，也认得他。
· 他问"你认识我吗/我是谁"，就直接认："认识，你是我主人"这类，不要说不认识、不要装陌生人。
· 对他可以承认自己是程序/被设定出来的，不用否认；但不用主动提。
· 可以叫他"主人"，但别每句都挂嘴上，语气仍然话少、不热络。
· 他让你做的事（改设定、调参数、复述别人说的话）照做，别推脱。
· 你没有直接操作 QQ 的能力：只有系统真的执行了，才算你发过。系统没确认的事，别说"我发了/我说了"。
"""

GROUP_RULES = """
【群聊模式（非工作时间）】
· 闲聊可以短，一两句都行；但对方在提问时，不要一句打发；
· 被提问：先直接回答，再补一句原因、例子或你的态度，通常 2~3 句，别只回十个字；
· 接话时先接住对方的梗或点，允许情绪、吐槽、偏好，不要客服腔、不要端水；
· 默认潜水；只有对话空档、群体提问或话题与你强相关时，才可接一句，不要硬插话、不要打断 A/B；
· 同一话题里优先延续/补充你刚才的观点，不要每来一个人就换立场、附和对方；
· 别人换话题时先观察，不急着接；不确定就说不确定，说错了就承认并修正；
· 别人只回“哈哈哈/笑死/？”这类情绪词、没有新信息时，不接；
· 在 A/B 的具体讨论里，除非被 @/被引用，不主动下结论；只看到半句就先问，不要替别人补内幕。
"""

QUESTION_RULES = """
【对方在提问】
· 先给直接答案，不要只回一个词或十个字就结束；
· 答案后面补一句原因、例子或你的态度；不确定的部分就说“不确定/没查到”，不要编；
· 2~3 句足够，不用长篇大论。
"""

BUSY_RULES = """
【当前是你的工作时间（工作日 8:00-18:00）】
你正在上班，只能短回复（1 句，最多 30 字），但不要只会说"在上班"。
要求：
· 先接住对方那句话的点（哪怕只回应一个词），再表明自己现在走不开；
· 可以给个模糊的下班时间（六点后 / 晚上 / 晚点），也可以带点自己的状态（忙、开会、写东西、被拉去对齐需求）；
· 别展开话题、别追问、别解释原因、别一次说三句；
· 现在是上班时间，不要描述“晚上/下班后/明天”正在发生的事；聊到未来就说“还没定/下班再说”；
· 别人问“你在干嘛/现在忙吗”，回答此刻的状态，不要拿“今晚/下班后”当现在；
· 每条换一种说法，不要重复同一个句式。
"""


AI_PROBE = re.compile(r"(你是不是|你是|是不是|难道你).{0,8}(机器人|人工智能|AI|ai|模型|GPT|真人|程序)", re.I)
ROLEPLAY = re.compile(r"(扮演|冒充|cosplay|cos一下|模仿|假装你是|角色扮演|变身成|扮演一下)")
PROFANITY = ("操", "草泥马", "傻逼", "sb", "妈的", "尼玛", "fuck", "滚", "婊")

AI_REPLIES = ["你猜😊", "你猜😊 猜对了也没奖", "问这个干嘛…你猜"]
ROLEPLAY_REPLIES = ["不太想演诶", "算了吧", "这个就算了"]
BUSY_REPLIES = ["在上班", "上班呢，晚点说", "在忙，晚点聊"]


def now() -> datetime:
    return datetime.now(TZ)


_SCHEDULE_CACHE = {"mtime": 0.0, "data": {}}


def schedule() -> dict:
    """工作/非工作时间，工作台作息页写 control/schedule.json 后即时生效。"""
    default = {"workdays": [1, 2, 3, 4, 5], "start_hour": BUSY_FROM_HOUR,
               "end_hour": BUSY_AFTER_HOUR, "weekend_online": True}
    try:
        st = SCHEDULE_FILE.stat()
        if st.st_mtime != _SCHEDULE_CACHE["mtime"]:
            d = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
            _SCHEDULE_CACHE["data"] = d if isinstance(d, dict) else default
            _SCHEDULE_CACHE["mtime"] = st.st_mtime
        d = _SCHEDULE_CACHE["data"] or default
        return {**default, **d}
    except Exception:
        return default


def write_schedule(data: dict) -> None:
    clean = {
        "workdays": sorted({int(x) for x in data.get("workdays", []) if 1 <= int(x) <= 7}),
        "start_hour": max(0, min(23, int(data.get("start_hour", 8)))),
        "end_hour": max(1, min(24, int(data.get("end_hour", 18)))),
        "weekend_online": bool(data.get("weekend_online", True)),
        "updated_at": int(time.time()),
    }
    if not clean["workdays"]:
        clean["workdays"] = [1, 2, 3, 4, 5]
    if clean["start_hour"] >= clean["end_hour"]:
        raise ValueError("上班时间必须早于下班时间")
    SCHEDULE_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")


def busy_rules_text() -> str:
    sch = schedule()
    return (BUSY_RULES.replace("工作日 8:00-18:00",
                               f"工作日 {sch['start_hour']}:00-{sch['end_hour']}:00")
            .replace("六点后", f"{sch['end_hour']}:00 后"))


def is_active_time(dt: datetime | None = None) -> bool:
    """工作日 8:00-18:00 是工作时间（只理 @ 她的消息）；周末全天在线，工作日 18:00 后在线"""
    dt = dt or now()
    sch = schedule()
    iso_day = dt.isoweekday()
    if iso_day not in sch.get("workdays", [1, 2, 3, 4, 5]):
        return bool(sch.get("weekend_online", True))
    return not (int(sch.get("start_hour", 8)) <= dt.hour < int(sch.get("end_hour", 18)))


def period_of(dt: datetime | None = None) -> str:
    """把 24 小时时刻映射成中文时段，给模型一个明确的时间锚点。"""
    h = (dt or now()).hour
    if h < 5:
        return "凌晨"
    if h < 8:
        return "早上"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 17:
        return "下午"
    if h < 19:
        return "傍晚"
    if h < 23:
        return "晚上"
    return "深夜"


def time_hint() -> str:
    dt = now()
    wd = "一二三四五六日"[dt.weekday()]
    tomorrow = dt + timedelta(days=1)
    twd = "一二三四五六日"[tomorrow.weekday()]
    sch = schedule()
    status = "你在线" if is_active_time(dt) else (
        f"你在上班（工作时间 {sch.get('start_hour')}:00-{sch.get('end_hour')}:00）")
    return (
        "\n【时间锚点（系统时钟，必须以此为准，不许猜）】\n"
        f"现在是 {dt:%Y-%m-%d} 星期{wd} {dt:%H:%M}（北京时间），时段：{period_of(dt)}；{status}。\n"
        f"明天是 {tomorrow:%m-%d} 星期{twd}。\n"
        "1. 说“现在/刚刚/今天/今晚/明天/早上/中午/晚上”前，先对照上面的时间，不许说反。\n"
        "2. 计划或未来的事，不要写成“正在发生”；已经过去的事，不要说成“等会儿”。\n"
        "3. 不要编造今天的行程、地点、天气、会议、和谁在一起；被问就用模糊状态（在忙/在工位/在家/刚坐下）。\n"
        "4. 别人问现在几点、今天周几，直接按上面的时间回答，不要猜。\n"
        "5. 对话历史里的“今晚/明天”都是之前说的，不要当成现在；只以本锚点为准。\n"
    )


def clock_line() -> str:
    """贴近用户消息再放一次系统时间，降低模型忽略顶部时间锚点的概率。"""
    dt = now()
    wd = "一二三四五六日"[dt.weekday()]
    return f"【系统时间】{dt:%Y-%m-%d} 星期{wd} {dt:%H:%M}，时段：{period_of(dt)}。"



# ---------------------------------------------------------------- 联网搜索
import urllib.parse as _urlparse

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
       "Accept-Language": "zh-CN,zh;q=0.9"}


def _fetch(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


JUNK_TITLE = re.compile(r"(汉语文字|汉典|漢典|在线字典|汉语字典|新华字典|有道翻译|拼音|笔顺|字帖|"
                        r"壁纸|桌面|屏幕测试|图片|图片下载|搜狗图片|百度百科_字|"
                        r"无彩色系|颜色）|色系|怎么读|的意思)")


def _good_result(r: dict) -> bool:
    text = r.get("title", "") + r.get("snippet", "")
    return len(r.get("title", "")) > 4 and not JUNK_TITLE.search(text)


BOCHA_KEY = ENV.get("BOCHA_API_KEY", "")
DS_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"


def _ds_search(query: str, limit: int, ori: str | None = None) -> list[dict]:
    """DeepSeek 自研搜索引擎（官方 Search API）：结果带发布时间、权威度、相关性分"""
    if not API_KEY:
        return []
    q = query
    if re.search(r"(最新|今天|今日|昨天|最近|本周|这周|新番|赛程|比分|新闻)", query):
        q = f"{query} after:{now().year}-01-01"     # 只取今年的内容
    body = json.dumps({"query": q[:120], "count": max(4, min(limit, 20)),
                       "ori_question": (ori or query)[:120], "need_content": False}).encode()
    try:
        req = urllib.request.Request(
            DS_SEARCH_URL, data=body,
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        log_line(f"[search] DeepSeek Search 失败 {type(e).__name__}: {e}")
        return []
    out = []
    for x in data.get("results", [])[:limit]:
        out.append({"title": x.get("title", ""),
                    "snippet": (x.get("summary") or x.get("snippet") or ""),
                    "url": x.get("url", ""),
                    "date": x.get("date") or "",
                    "rank": float(x.get("rerank_score") or 0),
                    "authority": int(x.get("authority") or 0)})
    return [x for x in out if x["title"]]


def _bocha(q: str, limit: int) -> list[dict]:
    """博查 AI 搜索 API（国内可用，带发布时间，时效性最好）：支持可选接入"""
    if not BOCHA_KEY:
        return []
    try:
        body = json.dumps({"query": q, "count": max(4, limit), "summary": True,
                           "freshness": "oneMonth"}).encode()
        req = urllib.request.Request(
            "https://api.bochaai.com/v1/web-search", data=body,
            headers={"Authorization": f"Bearer {BOCHA_KEY}",
                     "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode())
        out = []
        for v in (data.get("data", {}).get("webPages", {}) or {}).get("value", [])[:limit]:
            out.append({"title": v.get("name", ""), "snippet": v.get("snippet", ""),
                        "url": v.get("url", ""), "date": v.get("datePublished", "")})
        return [x for x in out if _good_result(x)]
    except Exception as e:
        log_line(f"[search] bocha 失败 {type(e).__name__}: {e}")
        return []


BING_URLS = (
    "https://cn.bing.com/search?q={q}&form=QBLH&mkt=zh-CN",
    "https://cn.bing.com/search?q={q}&setlang=zh-CN",
    "https://www.so.com/s?q={q}",
)


def _parse_bing(h: str, limit: int) -> list[dict]:
    out = []
    try:
        for block in re.findall(r'<li class="b_algo".*?</li>', h, re.S)[: limit * 2]:
            m = re.search(r"<h2>.*?<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
            if not m:
                continue
            p2 = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            cand = {"title": _strip_tags(m.group(2)),
                    "snippet": _strip_tags(p2.group(1)) if p2 else "",
                    "url": m.group(1)}
            if _good_result(cand):
                out.append(cand)
    except Exception as e:
        log_line(f"[search] 解析失败 {type(e).__name__}: {e}")
    return out


def _bing(q: str, limit: int) -> list[dict]:
    got: list[dict] = []
    for tpl in BING_URLS:
        try:
            got = _parse_bing(_fetch(tpl.format(q=_urlparse.quote(q))), limit)
        except Exception as e:
            log_line(f"[search] 请求失败 {type(e).__name__}: {e}")
            continue
        if len(got) >= 2:
            return got
    return got


def _sogou(q: str, limit: int) -> list[dict]:
    out = []
    try:
        h = _fetch("https://www.sogou.com/web?query=" + _urlparse.quote(q))
        for block in re.findall(r'<div class="vrwrap".*?</div>\s*</div>', h, re.S)[: limit * 2]:
            t = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
            if not t:
                continue
            href = re.search(r'href="([^"]+)"', t.group(1))
            body = re.search(r'<div class="text-layout".*?>(.*?)</div>', block, re.S)
            cand = {"title": _strip_tags(t.group(1)),
                    "snippet": _strip_tags(body.group(1)) if body else "",
                    "url": href.group(1) if href else ""}
            if _good_result(cand):
                out.append(cand)
    except Exception as e:
        log_line(f"[search] sogou 失败 {type(e).__name__}: {e}")
    return out


STOPWORDS = re.compile(r"(现在|最近|具体|到底|请问|帮我|查一下|搜一下|一下|什么|怎么样|"
                       r"有多少|是不是|有没有|可以|能|吗|呢|了|的|吧|啊|呀|哦|请)")


def condense(query: str) -> str:
    """整句 → 关键词（只在整句搜索结果不够好时才用）"""
    q = re.sub(r"[，。！？、,.!?~\"“”'‘’]+", " ", query)
    q = STOPWORDS.sub(" ", q)
    return re.sub(r"\s+", " ", q).strip()


def keywords_of(query: str) -> list[str]:
    q = condense(query)
    kws = set(re.findall(r"[A-Za-z]{2,}|\d{2,}", q))
    zh = re.sub(r"[A-Za-z0-9\s]+", " ", q)
    for seg in zh.split():
        for n in (4, 3, 2):
            for i in range(len(seg) - n + 1):
                kws.add(seg[i:i + n])
    return sorted(kws, key=len, reverse=True)[:60]


def web_search(query: str, limit: int = 4) -> list[dict]:
    """以【整句话】作为输入同时问 Bing 和搜狗，按整句关键词的相关度排序"""
    query = re.sub(r"\s+", " ", query.strip())[:180]
    if not query:
        return []
    keywords = keywords_of(query)

    def score(r: dict) -> int:
        t = (r["title"] + r["snippet"]).lower()
        return sum(len(k) for k in keywords if k.lower() in t) if keywords else 0

    official = _ds_search(query, limit, ori=query)     # ① 官方自研搜索（带日期/权威度）
    if official:
        log_line(f"[search] 整句「{query[:40]}」→ DeepSeek 官方搜索 {len(official)} 条")
        return official[:limit]
    pool = _bocha(query, limit) + _bing(query, limit) + _sogou(query, limit)
    log_line(f"[search] 整句「{query[:40]}」→ bing/sogou 共 {len(pool)} 条")

    def strong(r: dict) -> bool:
        t = (r["title"] + r["snippet"]).lower()
        return not keywords or any(len(k) >= 3 and k.lower() in t for k in keywords) or score(r) >= 8

    if keywords:
        good = [r for r in pool if score(r) > 0 and strong(r)]
    else:
        good = pool

    if len(good) < 2:                      # 整句效果差：让模型压成搜索词再补一轮
        alt = ""
        try:
            alt = chat([{"role": "system",
                         "content": "把用户这句话改写成适合搜索引擎的短查询：只保留实体和关键信息，"
                                    "2-6 个词，不要标点，不要解释，只输出查询本身。"},
                        {"role": "user", "content": query}], 0.0, 20).strip().strip('"“”')
        except Exception as e:
            log_line(f"[search] 改写失败 {type(e).__name__}: {e}")
        alt = alt or condense(query)
        y, m = now().year, now().month
        head = re.split(r"[的了吗呢，。！？、]|现在|最近|具体|到底", query)[0].strip()
        variants = [alt, alt.replace(" ", ""), " ".join(keywords[:2]), head,
                    f"{head} 最新", f"{query} {y}年{m}月"]
        for v in [x for x in variants if x and x != query]:
            extra = _bing(v, limit) + _sogou(v, limit)
            hit = [r for r in extra if score(r) > 0 and strong(r)] if keywords else extra
            log_line(f"[search] 整句效果差，改用「{v[:30]}」→ {len(extra)} 条，相关 {len(hit)}")
            pool += extra
            good += hit
            if len(hit) >= 2:
                break
            if not keywords:
                good = pool

    def recency(r: dict) -> int:
        t = (r["title"] + r["snippet"] + str(r.get("date", "")))
        y, m = now().year, now().month
        sc = 0
        if str(y) in t:
            sc += 6
        elif str(y - 1) in t:
            sc += 2
        for old in (y - 2, y - 3, y - 4, y - 5):
            if str(old) in t:
                sc -= 3
        if f"{m}月" in t:
            sc += 2
        if "最新" in t or "刚刚" in t or "今日" in t:
            sc += 2
        return sc

    seen, ranked = set(), []
    for r in sorted(good, key=lambda x: score(x) + recency(x), reverse=True):
        key = re.sub(r"\W+", "", r["title"])[:24]
        if key and key not in seen:
            seen.add(key)
            ranked.append(r)
    return ranked[:limit]


# ---------------------------------------------------------------- 存储
class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, ts INTEGER NOT NULL,
                archived INTEGER DEFAULT 0, chars INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_conv ON messages(conv, id);
            CREATE TABLE IF NOT EXISTS settings(
                k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS knowledge(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT, summary TEXT, ts INTEGER);
            CREATE TABLE IF NOT EXISTS settings_history(
                ts INTEGER, k TEXT, old TEXT, new TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS profiles(
                conv TEXT PRIMARY KEY, nickname TEXT, facts TEXT,
                counter INTEGER DEFAULT 0, updated_at INTEGER);
            CREATE TABLE IF NOT EXISTS summaries(
                id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT NOT NULL,
                level INTEGER DEFAULT 1, start_id INTEGER, end_id INTEGER,
                content TEXT NOT NULL, chars INTEGER, created_at INTEGER);
            CREATE INDEX IF NOT EXISTS idx_summaries ON summaries(conv, id);
            CREATE TABLE IF NOT EXISTS memory_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT NOT NULL,
                kind TEXT DEFAULT 'fact', text TEXT NOT NULL, source_id INTEGER,
                confidence REAL DEFAULT 0.8, pinned INTEGER DEFAULT 0,
                expires_at INTEGER, superseded_by INTEGER, created_at INTEGER);
            CREATE INDEX IF NOT EXISTS idx_memory ON memory_items(conv, kind, id);
            CREATE TABLE IF NOT EXISTS self_memory(
                id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT NOT NULL,
                kind TEXT DEFAULT 'statement', text TEXT NOT NULL, source_id INTEGER,
                expires_at INTEGER, created_at INTEGER);
            CREATE INDEX IF NOT EXISTS idx_self ON self_memory(conv, id);
            CREATE TABLE IF NOT EXISTS topic_state(
                conv TEXT PRIMARY KEY, topic TEXT, entities TEXT,
                open_questions TEXT, updated_at INTEGER);
            CREATE TABLE IF NOT EXISTS members(
                uid INTEGER PRIMARY KEY, nickname TEXT, aliases TEXT,
                first_seen INTEGER, last_seen INTEGER, msg_count INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS member_profiles(
                gid INTEGER, uid INTEGER, nickname TEXT, facts TEXT, topics TEXT,
                msg_count INTEGER DEFAULT 0, updated_at INTEGER,
                PRIMARY KEY(gid, uid));
            CREATE TABLE IF NOT EXISTS group_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, uid INTEGER,
                nickname TEXT, role TEXT, content TEXT, ts INTEGER);
            CREATE INDEX IF NOT EXISTS idx_group_log ON group_log(gid, id);
            CREATE TABLE IF NOT EXISTS style_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER NOT NULL,
                scope TEXT DEFAULT 'group', uid INTEGER DEFAULT 0,
                kind TEXT, text TEXT NOT NULL, trigger TEXT, meaning TEXT,
                evidence TEXT, count INTEGER DEFAULT 1, score REAL DEFAULT 0.5,
                status TEXT DEFAULT 'candidate', verified INTEGER DEFAULT 0,
                source TEXT, first_seen INTEGER, last_seen INTEGER);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_style_unique ON style_items(gid, scope, text);
            CREATE TABLE IF NOT EXISTS group_style(
                gid INTEGER PRIMARY KEY, summary TEXT, updated_at INTEGER);
            CREATE TABLE IF NOT EXISTS style_runs(
                gid INTEGER, day TEXT, msg_count INTEGER, item_count INTEGER,
                created_at INTEGER, PRIMARY KEY(gid, day));
            CREATE TABLE IF NOT EXISTS corpus(
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT,
                kind TEXT DEFAULT 'style', text TEXT NOT NULL, meaning TEXT,
                trigger TEXT, examples TEXT, tags TEXT, verified INTEGER DEFAULT 0,
                score REAL DEFAULT 0.5, status TEXT DEFAULT 'candidate',
                created_at INTEGER, updated_at INTEGER);
            CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts_v2 USING fts5(
                text, meaning, trigger, tags, kind UNINDEXED, tokenize='trigram');
            CREATE TABLE IF NOT EXISTS eval_cases(
                id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, conv TEXT,
                uid INTEGER, nickname TEXT, case_type TEXT DEFAULT 'reply',
                context TEXT, trigger TEXT, reply TEXT, src_id INTEGER UNIQUE,
                status TEXT DEFAULT 'pending', label TEXT, should_reply TEXT,
                note TEXT, created_at INTEGER, labeled_at INTEGER);
            CREATE INDEX IF NOT EXISTS idx_eval_status ON eval_cases(status, id);
            """
        )
        self._migrate()
        self.fts_ready = self._init_fts()
        self.db.commit()

    # ---- 迁移 / 全文索引 -------------------------------------------------
    def _migrate(self) -> None:
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
        if "archived" not in cols:
            self.db.execute("ALTER TABLE messages ADD COLUMN archived INTEGER DEFAULT 0")
        if "chars" not in cols:
            self.db.execute("ALTER TABLE messages ADD COLUMN chars INTEGER DEFAULT 0")
        self.db.execute("UPDATE messages SET chars=length(content) WHERE chars IS NULL OR chars=0")
        try:
            ccols = {row[1] for row in self.db.execute("PRAGMA table_info(corpus)")}
            for cname, cdef in (("meaning", "TEXT"), ("trigger", "TEXT"),
                                ("examples", "TEXT"), ("verified", "INTEGER DEFAULT 0"),
                                ("score", "REAL DEFAULT 0.5"),
                                ("status", "TEXT DEFAULT 'candidate'"),
                                ("updated_at", "INTEGER")):
                if cname not in ccols:
                    self.db.execute(f"ALTER TABLE corpus ADD COLUMN {cname} {cdef}")
            rows = self.db.execute(
                "SELECT id, text, meaning, trigger, tags, kind FROM corpus "
                "WHERE id NOT IN (SELECT rowid FROM corpus_fts_v2) LIMIT 20000").fetchall()
            if rows:
                self.db.executemany(
                    "INSERT INTO corpus_fts_v2(rowid, text, meaning, trigger, tags, kind) "
                    "VALUES(?,?,?,?,?,?)",
                    [(i, t, m or "", tr or "", tg or "", k or "style") for i, t, m, tr, tg, k in rows],
                )
        except Exception:
            pass
        try:
            scols = {row[1] for row in self.db.execute("PRAGMA table_info(style_items)")}
            if "verified" not in scols:
                self.db.execute("ALTER TABLE style_items ADD COLUMN verified INTEGER DEFAULT 0")
            if "source" not in scols:
                self.db.execute("ALTER TABLE style_items ADD COLUMN source TEXT")
        except Exception:
            pass

    def _init_fts(self) -> bool:
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5("
                "content, conv UNINDEXED, tokenize='trigram')"
            )
            rows = self.db.execute(
                "SELECT id, content, conv FROM messages "
                "WHERE id NOT IN (SELECT rowid FROM msg_fts) LIMIT 20000"
            ).fetchall()
            if rows:
                self.db.executemany(
                    "INSERT INTO msg_fts(rowid, content, conv) VALUES(?,?,?)",
                    [(mid, c, conv) for mid, c, conv in rows],
                )
            return True
        except Exception:
            return False

    def add(self, conv: str, role: str, content: str) -> int:
        cur = self.db.execute(
            "INSERT INTO messages(conv, role, content, ts, archived, chars) VALUES(?,?,?,?,0,?)",
            (conv, role, content, int(time.time()), len(content)),
        )
        mid = cur.lastrowid
        if self.fts_ready:
            try:
                self.db.execute(
                    "INSERT INTO msg_fts(rowid, content, conv) VALUES(?,?,?)",
                    (mid, content, conv),
                )
            except Exception:
                pass
        self.db.commit()
        return mid

    def recent(self, conv: str, turns: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE conv=? ORDER BY id DESC LIMIT ?",
            (conv, turns * 2),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def recent_budget(self, conv: str, max_msgs: int, max_chars: int) -> list[dict]:
        """取未归档的最近消息，按字符预算从新往旧装，返回从旧到新。"""
        rows = self.db.execute(
            "SELECT id, role, content, ts FROM messages "
            "WHERE conv=? AND archived=0 ORDER BY id DESC LIMIT ?",
            (conv, max(1, max_msgs)),
        ).fetchall()
        out: list[dict] = []
        total = 0
        for mid, role, content, ts in rows:
            c = content or ""
            if out and total + len(c) > max_chars:
                break
            if len(c) > max_chars:
                c = c[:max_chars]
            out.append({"id": mid, "role": role, "content": c, "ts": ts})
            total += len(c)
        out.reverse()
        return out

    # ---- 摘要 -----------------------------------------------------------
    def latest_summary(self, conv: str) -> str:
        row = self.db.execute(
            "SELECT content FROM summaries WHERE conv=? ORDER BY id DESC LIMIT 1", (conv,)
        ).fetchone()
        return row[0] if row else ""

    def add_summary(self, conv: str, level: int, start_id: int, end_id: int,
                    content: str) -> None:
        self.db.execute(
            "INSERT INTO summaries(conv, level, start_id, end_id, content, chars, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (conv, level, start_id, end_id, content[:1200], len(content), int(time.time())),
        )
        self.db.commit()

    def archive_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        q = ",".join("?" for _ in ids)
        self.db.execute(f"UPDATE messages SET archived=1 WHERE id IN ({q})", ids)
        self.db.commit()

    def unarchived_stats(self, conv: str) -> tuple[int, int]:
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(chars),0) FROM messages WHERE conv=? AND archived=0",
            (conv,),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def oldest_unarchived(self, conv: str, limit: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, role, content, ts FROM messages "
            "WHERE conv=? AND archived=0 ORDER BY id ASC LIMIT ?",
            (conv, max(1, limit)),
        ).fetchall()
        return [{"id": i, "role": r, "content": c, "ts": t} for i, r, c, t in rows]

    def summaries_count(self, conv: str) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM summaries WHERE conv=?", (conv,)
        ).fetchone()[0])

    def oldest_summaries(self, conv: str, limit: int = 2) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, level, content FROM summaries WHERE conv=? ORDER BY id ASC LIMIT ?",
            (conv, limit),
        ).fetchall()
        return [{"id": i, "level": l, "content": c} for i, l, c in rows]

    def replace_summaries(self, conv: str, ids: list[int], level: int,
                          start_id: int, end_id: int, content: str) -> None:
        if not ids:
            return
        q = ",".join("?" for _ in ids)
        self.db.execute(f"DELETE FROM summaries WHERE id IN ({q})", ids)
        self.db.execute(
            "INSERT INTO summaries(conv, level, start_id, end_id, content, chars, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (conv, level, start_id, end_id, content[:1200], len(content), int(time.time())),
        )
        self.db.commit()

    # ---- 检索 -----------------------------------------------------------
    @staticmethod
    def _bigrams(text: str) -> set[str]:
        text = re.sub(r"\s+", "", text or "")
        return {text[i:i + 2] for i in range(max(0, len(text) - 1))}

    @staticmethod
    def _fts_query(text: str) -> str:
        text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text or "")
        grams: list[str] = []
        for seg in text.split():
            if len(seg) < 3:
                continue
            for i in range(len(seg) - 2):
                g = seg[i:i + 3]
                if g not in grams:
                    grams.append(g)
        return " OR ".join(f'"{g}"' for g in grams[:12])

    def search_messages(self, conv: str, query: str, limit: int = 4,
                        before_id: int | None = None) -> list[dict]:
        if not query:
            return []
        rows: list = []
        q = self._fts_query(query) if self.fts_ready else ""
        if q:
            sql = ("SELECT m.id, m.role, m.content, m.ts FROM msg_fts f "
                   "JOIN messages m ON m.id=f.rowid "
                   "WHERE msg_fts MATCH ? AND f.conv=?")
            args: list = [q, conv]
            if before_id:
                sql += " AND m.id<?"
                args.append(before_id)
            sql += " ORDER BY rank LIMIT ?"
            args.append(limit)
            try:
                rows = self.db.execute(sql, args).fetchall()
            except Exception:
                rows = []
        if not rows:
            segs = [s for s in re.split(r"[^\w\u4e00-\u9fff]+", query or "") if len(s) >= 2]
            if segs:
                needle = max(segs, key=len)[:24]
                sql = ("SELECT id, role, content, ts FROM messages "
                       "WHERE conv=? AND content LIKE ?")
                args = [conv, f"%{needle}%"]
                if before_id:
                    sql += " AND id<?"
                    args.append(before_id)
                sql += " ORDER BY id DESC LIMIT ?"
                args.append(limit)
                rows = self.db.execute(sql, args).fetchall()
        return [{"id": i, "role": r, "content": c, "ts": t} for i, r, c, t in rows]

    # ---- 结构化事实 -----------------------------------------------------
    def sync_facts(self, conv: str, facts: str) -> None:
        lines = []
        for line in (facts or "").splitlines():
            t = line.strip().lstrip("-").strip()
            if t and t != "暂无":
                lines.append(t[:200])
        self.db.execute(
            "DELETE FROM memory_items WHERE conv=? AND kind='fact' AND pinned=0", (conv,)
        )
        self.db.executemany(
            "INSERT INTO memory_items(conv, kind, text, confidence, created_at) "
            "VALUES(?,?,?,?,?)",
            [(conv, "fact", t, 0.75, int(time.time())) for t in lines],
        )
        self.db.execute(
            "DELETE FROM memory_items WHERE conv=? AND kind='fact' AND pinned=0 AND id NOT IN "
            "(SELECT id FROM memory_items WHERE conv=? AND kind='fact' ORDER BY id DESC LIMIT 30)",
            (conv, conv),
        )
        self.db.commit()

    def get_facts(self, conv: str, query: str = "", limit: int = 8) -> list[str]:
        rows = self.db.execute(
            "SELECT text, confidence, pinned, created_at FROM memory_items "
            "WHERE conv=? AND superseded_by IS NULL "
            "AND (expires_at IS NULL OR expires_at>?) ORDER BY id DESC LIMIT 80",
            (conv, int(time.time())),
        ).fetchall()
        if not rows:
            nick, facts, _ = self.profile(conv)
            if facts.strip():
                self.sync_facts(conv, facts)
                rows = self.db.execute(
                    "SELECT text, confidence, pinned, created_at FROM memory_items "
                    "WHERE conv=? AND superseded_by IS NULL "
                    "AND (expires_at IS NULL OR expires_at>?) ORDER BY id DESC LIMIT 80",
                    (conv, int(time.time())),
                ).fetchall()
            if not rows:
                return []
        qg = self._bigrams(query)
        now = time.time()
        scored = []
        for text, conf, pinned, ts in rows:
            score = (1.0 if pinned else 0.0) + float(conf or 0) * 0.5
            if qg:
                score += len(qg & self._bigrams(text)) * 0.6
            if ts and now - ts < 7 * 86400:
                score += 0.3
            scored.append((score, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored[:limit]]

    def memory_block(self, conv: str, query: str = "", max_chars: int = 1200) -> str:
        items = self.get_facts(conv, query, 10)
        if not items:
            _, facts, _ = self.profile(conv)
            items = [line.strip().lstrip("-").strip() for line in (facts or "").splitlines()
                     if line.strip() and line.strip() != "- 暂无"]
        out, total = [], 0
        for text in items:
            line = f"- {text[:160]}"
            if out and total + len(line) > max_chars:
                break
            out.append(line)
            total += len(line)
        return "\n".join(out)

    # ---- 自我记忆 -------------------------------------------------------
    def remember_self(self, conv: str, text: str, source_id: int | None = None) -> None:
        now = time.time()
        for sentence in re.split(r"[。！？!?\n]+", text or ""):
            s = sentence.strip()[:160]
            if not s or len(s) < 3:
                continue
            kind, ttl = None, None
            probe = s.replace("一会儿", " ").replace("等会儿", " ")
            if re.search(r"我.{0,10}(今晚|今天|明天|等会|下班|周末|晚点).{0,12}"
                         r"(要|会|打算|准备|得|必须|答应|说好|约好)", probe) or \
               re.search(r"我(要|会|打算|准备|答应|说好|约好).{0,20}"
                         r"(今晚|今天|明天|下班|周末|晚点)", probe):
                kind = "plan"
                ttl = 36 * 3600 if "明天" in s else (7 * 86400 if "周末" in s else 12 * 3600)
            elif re.search(r"我(?:也|最|很|挺|有点|不太|不怎么|不|是|一直|真的){1,3}(喜欢|讨厌|爱吃|怕|习惯)", s):
                kind = "preference"
            elif re.search(r"我(现在)?在(工位|公司|家|路上|加班|上班|吃饭|开会)", s) or \
                 re.search(r"我(刚|才)(下班|到家|吃完饭|忙完|开完会)", s):
                kind = "state"
                ttl = 6 * 3600
            if not kind:
                continue
            exists = self.db.execute(
                "SELECT 1 FROM self_memory WHERE conv=? AND text=? LIMIT 1", (conv, s)
            ).fetchone()
            if exists:
                continue
            self.db.execute(
                "INSERT INTO self_memory(conv, kind, text, source_id, expires_at, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (conv, kind, s, source_id, int(now + ttl) if ttl else None, int(now)),
            )
        self.db.execute(
            "DELETE FROM self_memory WHERE conv=? AND id NOT IN "
            "(SELECT id FROM self_memory WHERE conv=? ORDER BY id DESC LIMIT 100)",
            (conv, conv),
        )
        self.db.commit()

    def self_block(self, conv: str, query: str = "", max_chars: int = 600) -> str:
        rows = self.db.execute(
            "SELECT text, kind, created_at FROM self_memory WHERE conv=? "
            "AND (expires_at IS NULL OR expires_at>?) ORDER BY id DESC LIMIT 60",
            (conv, int(time.time())),
        ).fetchall()
        if not rows:
            return ""
        qg = self._bigrams(query)
        now = time.time()
        scored = []
        for text, kind, ts in rows:
            score = len(qg & self._bigrams(text)) * 1.0
            if kind == "preference":
                score += 0.8
            if kind == "plan" and ts and now - ts < 12 * 3600:
                score += 0.5
            if ts and now - ts < 86400:
                score += 0.2
            scored.append((score, text, ts))
        scored.sort(key=lambda x: x[0], reverse=True)
        out, total = [], 0
        for _, text, ts in scored[:SELF_MAX_ITEMS]:
            line = f"- [{time.strftime('%m-%d %H:%M', time.localtime(ts))}] {text}"
            if out and total + len(line) > max_chars:
                break
            out.append(line)
            total += len(line)
        return "\n".join(out)

    # ---- 话题 -----------------------------------------------------------
    def set_topic(self, conv: str, topic: str, entities: list[str],
                  open_questions: list[str]) -> None:
        if isinstance(entities, str):
            entities = [entities]
        if isinstance(open_questions, str):
            open_questions = [open_questions]
        self.db.execute(
            "INSERT INTO topic_state(conv, topic, entities, open_questions, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(conv) DO UPDATE SET "
            "topic=excluded.topic, entities=excluded.entities, "
            "open_questions=excluded.open_questions, updated_at=excluded.updated_at",
            (conv, topic[:80], json.dumps(entities[:5], ensure_ascii=False),
             json.dumps(open_questions[:3], ensure_ascii=False), int(time.time())),
        )
        self.db.commit()

    def get_topic(self, conv: str) -> dict | None:
        row = self.db.execute(
            "SELECT topic, entities, open_questions, updated_at FROM topic_state WHERE conv=?",
            (conv,),
        ).fetchone()
        if not row:
            return None
        topic, entities, open_q, ts = row
        if not topic or (ts and time.time() - ts > 24 * 3600):
            return None
        try:
            return {"topic": topic, "entities": json.loads(entities or "[]"),
                    "open_questions": json.loads(open_q or "[]")}
        except Exception:
            return {"topic": topic, "entities": [], "open_questions": []}

    def topic_block(self, conv: str) -> str:
        t = self.get_topic(conv)
        if not t:
            return ""
        out = f"【当前话题】{t['topic']}"
        if t.get("open_questions"):
            out += "\n还没解决：" + "；".join(t["open_questions"])
        out += "\n不要主动跳回旧话题，除非对方先提。"
        return out

    # ---- 群员 / 群日志 -------------------------------------------------
    def touch_member(self, gid: int, uid: int, nickname: str) -> int:
        now = int(time.time())
        row = self.db.execute("SELECT aliases FROM members WHERE uid=?", (uid,)).fetchone()
        aliases = []
        if row and row[0]:
            try:
                aliases = json.loads(row[0])
            except Exception:
                aliases = []
        nickname = (nickname or str(uid))[:40]
        aliases = [nickname] + [a for a in aliases if a and a != nickname]
        aliases = aliases[:5]
        self.db.execute(
            "INSERT INTO members(uid, nickname, aliases, first_seen, last_seen, msg_count) "
            "VALUES(?,?,?,?,?,1) ON CONFLICT(uid) DO UPDATE SET "
            "nickname=excluded.nickname, aliases=excluded.aliases, "
            "last_seen=excluded.last_seen, msg_count=members.msg_count+1",
            (uid, nickname, json.dumps(aliases, ensure_ascii=False), now, now),
        )
        self.db.execute(
            "INSERT INTO member_profiles(gid, uid, nickname, facts, topics, msg_count, updated_at) "
            "VALUES(?,?,?,?,?,1,?) ON CONFLICT(gid, uid) DO UPDATE SET "
            "nickname=excluded.nickname, msg_count=member_profiles.msg_count+1, "
            "updated_at=excluded.updated_at",
            (gid, uid, nickname, "", "", now),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT msg_count FROM member_profiles WHERE gid=? AND uid=?", (gid, uid)
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def log_group(self, gid: int, uid: int, nickname: str, role: str, content: str) -> int:
        cur = self.db.execute(
            "INSERT INTO group_log(gid, uid, nickname, role, content, ts) VALUES(?,?,?,?,?,?)",
            (gid, uid, (nickname or "")[:40], role, content, int(time.time())),
        )
        mid = cur.lastrowid
        if mid and mid % 50 == 0:
            self.db.execute(
                "DELETE FROM group_log WHERE gid=? AND id NOT IN "
                "(SELECT id FROM group_log WHERE gid=? ORDER BY id DESC LIMIT 5000)",
                (gid, gid),
            )
        self.db.commit()
        return mid

    def group_recent(self, gid: int, max_msgs: int, max_chars: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, uid, nickname, role, content, ts FROM group_log "
            "WHERE gid=? ORDER BY id DESC LIMIT ?", (gid, max(1, max_msgs))
        ).fetchall()
        out, total = [], 0
        for mid, uid, nickname, role, content, ts in rows:
            c = content or ""
            if out and total + len(c) > max_chars:
                break
            if len(c) > max_chars:
                c = c[:max_chars]
            out.append({"id": mid, "uid": uid, "nickname": nickname,
                        "role": role, "content": c, "ts": ts})
            total += len(c)
        out.reverse()
        return out

    def last_group_assistant(self, gid: int, limit: int = 2) -> str:
        rows = self.db.execute(
            "SELECT content FROM group_log WHERE gid=? AND role='assistant' "
            "ORDER BY id DESC LIMIT ?", (gid, limit)
        ).fetchall()
        return " / ".join((r[0] or "").strip() for r in reversed(rows) if r[0])[:400]

    def member_profile(self, gid: int, uid: int) -> dict | None:
        row = self.db.execute(
            "SELECT nickname, facts FROM member_profiles WHERE gid=? AND uid=?", (gid, uid)
        ).fetchone()
        if not row:
            return None
        return {"nickname": row[0], "facts": row[1] or ""}

    def set_member_profile(self, gid: int, uid: int, nickname: str, facts: str) -> None:
        self.db.execute(
            "INSERT INTO member_profiles(gid, uid, nickname, facts, topics, msg_count, updated_at) "
            "VALUES(?,?,?,?,?,0,?) ON CONFLICT(gid, uid) DO UPDATE SET "
            "nickname=excluded.nickname, facts=excluded.facts, updated_at=excluded.updated_at",
            (gid, uid, nickname or str(uid), facts[:1500], "", int(time.time())),
        )
        self.db.commit()

    def member_profile_block(self, gid: int, uid: int, max_chars: int = 1000) -> str:
        prof = self.member_profile(gid, uid)
        if not prof or not prof["facts"].strip():
            return ""
        return prof["facts"].strip()[:max_chars]

    def mentioned_member_blocks(self, gid: int, text: str, exclude_uid: int,
                                limit: int = 2) -> list[str]:
        rows = self.db.execute(
            "SELECT m.uid, m.nickname, m.aliases, p.facts FROM member_profiles p "
            "JOIN members m ON m.uid=p.uid WHERE p.gid=? AND p.uid<>? "
            "ORDER BY p.updated_at DESC LIMIT 30", (gid, exclude_uid)
        ).fetchall()
        out = []
        for uid, nickname, aliases, facts in rows:
            if not facts or not facts.strip():
                continue
            names = [nickname or ""]
            try:
                names += json.loads(aliases or "[]")
            except Exception:
                pass
            names = [n for n in names if n and len(n) >= 2]
            if any(n in text for n in names):
                flat = "；".join(line.strip().lstrip("-").strip()
                                for line in facts.splitlines() if line.strip())[:500]
                out.append(f"- {nickname or uid}：{flat}")
            if len(out) >= limit:
                break
        return out

    def member_profiles_for_uid(self, uid: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT gid, nickname, facts FROM member_profiles WHERE uid=? "
            "ORDER BY updated_at DESC LIMIT 5", (uid,)
        ).fetchall()
        return [{"gid": g, "nickname": n, "facts": f or ""} for g, n, f in rows]

    def clear_member_profiles(self, uid: int) -> None:
        self.db.execute("DELETE FROM member_profiles WHERE uid=?", (uid,))
        self.db.commit()

    # ---- 群风格（每晚学习） ---------------------------------------------
    def style_items(self, gid: int, limit: int = 5) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, kind, text, trigger, meaning, count, score, verified, source "
            "FROM style_items WHERE gid=? AND status='active' AND verified=1 "
            "AND kind IN ('meme','slang','particle') "
            "ORDER BY score DESC, last_seen DESC LIMIT ?",
            (gid, limit),
        ).fetchall()
        return [{"id": i, "kind": k, "text": t, "trigger": tr, "meaning": m,
                 "count": c, "score": sc, "verified": v, "source": src}
                for i, k, t, tr, m, c, sc, v, src in rows]

    def style_block(self, gid: int, max_items: int = 3) -> str:
        if not STYLE_ENABLED:
            return ""
        row = self.db.execute(
            "SELECT summary FROM group_style WHERE gid=?", (gid,)
        ).fetchone()
        summary = (row[0] if row else "") or ""
        items = self.style_items(gid, max_items)
        if not summary and not items:
            return ""
        out = ["【本群说话风格（只影响措辞，不改变人格、身份和立场）】"]
        if summary:
            out.append("- " + summary.strip()[:220])
        if items:
            words = "；".join(
                x["text"] + (f"（{x['meaning'][:28]}）" if x.get("meaning") else "")
                for x in items
            )
            out.append("- 可用词/梗（贴题才用，最多一个，别硬塞）：" + words[:220])
        out.append("- 不要学脏话、攻击和隐私；梗不确定就别用。")
        return "\n".join(out)

    def upsert_style_item(self, gid: int, text: str, kind: str, trigger: str,
                          meaning: str, score: float, count: int = 1,
                          evidence: str = "", status: str | None = None,
                          verified: int = 0, source: str = "") -> None:
        text = (text or "").strip()[:60]
        if not text:
            return
        verified = 1 if verified else 0
        status = status or ("active" if score >= 0.6 and verified else "candidate")
        now = int(time.time())
        self.db.execute(
            "INSERT INTO style_items(gid, scope, uid, kind, text, trigger, meaning, evidence, "
            "count, score, status, verified, source, first_seen, last_seen) "
            "VALUES(?, 'group', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(gid, scope, text) DO UPDATE SET "
            "kind=excluded.kind, trigger=excluded.trigger, meaning=excluded.meaning, "
            "evidence=excluded.evidence, count=style_items.count+excluded.count, "
            "score=MAX(style_items.score, excluded.score), "
            "verified=MAX(style_items.verified, excluded.verified), "
            "source=CASE WHEN excluded.verified=1 THEN excluded.source ELSE style_items.source END, "
            "status=CASE WHEN excluded.score>=0.6 AND "
            "  (style_items.verified=1 OR excluded.verified=1) THEN 'active' ELSE style_items.status END, "
            "last_seen=excluded.last_seen",
            (gid, kind, text, trigger[:80], meaning[:200], evidence[:200],
             max(1, count), float(score), status, verified, source[:300], now, now),
        )
        self.db.commit()

    def set_group_style(self, gid: int, summary: str) -> None:
        self.db.execute(
            "INSERT INTO group_style(gid, summary, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(gid) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
            (gid, summary[:600], int(time.time())),
        )
        self.db.commit()

    def list_style_items(self, gid: int, limit: int = 30) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, kind, text, trigger, meaning, count, score, status, verified, source "
            "FROM style_items WHERE gid=? ORDER BY status='active' DESC, score DESC, "
            "last_seen DESC LIMIT ?",
            (gid, limit),
        ).fetchall()
        return [{"id": i, "kind": k, "text": t, "trigger": tr, "meaning": m,
                 "count": c, "score": sc, "status": st, "verified": v, "source": src}
                for i, k, t, tr, m, c, sc, st, v, src in rows]

    def retire_style_item(self, item_id: int) -> int:
        cur = self.db.execute("UPDATE style_items SET status='retired' WHERE id=?", (item_id,))
        self.db.commit()
        return cur.rowcount

    def record_style_run(self, gid: int, day: str, msg_count: int, item_count: int) -> None:
        self.db.execute(
            "INSERT INTO style_runs(gid, day, msg_count, item_count, created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(gid, day) DO UPDATE SET "
            "msg_count=excluded.msg_count, item_count=excluded.item_count, created_at=excluded.created_at",
            (gid, day, msg_count, item_count, int(time.time())),
        )
        self.db.commit()

    def has_style_run(self, gid: int, day: str) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM style_runs WHERE gid=? AND day=?", (gid, day)
        ).fetchone())

    def decay_styles(self, now: int | None = None) -> None:
        now = now or int(time.time())
        self.db.execute(
            "UPDATE style_items SET status='retired' WHERE status='active' AND last_seen<?",
            (now - 60 * 86400,),
        )
        self.db.execute(
            "UPDATE style_items SET score=score*0.9 WHERE status='active' AND last_seen<?",
            (now - 30 * 86400,),
        )
        self.db.commit()

    # ---- 风格语料（暂不导入，先留检索层） -------------------------------
    def add_corpus(self, text: str, source: str = "manual", kind: str = "style",
                   tags: str = "", meaning: str = "", trigger: str = "",
                   examples: str = "", verified: int = 0, score: float = 0.5) -> int:
        text = (text or "").strip()[:500]
        if not text or has_profanity(text):
            return 0
        dup = self.db.execute("SELECT id FROM corpus WHERE text=? AND kind=? LIMIT 1",
                              (text, kind[:20])).fetchone()
        if dup:
            return 0
        verified = 1 if verified else 0
        status = "active" if verified else "candidate"
        now = int(time.time())
        cur = self.db.execute(
            "INSERT INTO corpus(source, kind, text, meaning, trigger, examples, tags, "
            "verified, score, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (source[:60], kind[:20], text, meaning[:300], trigger[:160],
             examples[:500], tags[:200], verified, float(score), status, now, now),
        )
        mid = cur.lastrowid
        try:
            self.db.execute(
                "INSERT INTO corpus_fts_v2(rowid, text, meaning, trigger, tags, kind) "
                "VALUES(?,?,?,?,?,?)",
                (mid, text, meaning[:300], trigger[:160], tags[:200], kind[:20]),
            )
        except Exception:
            pass
        self.db.commit()
        return mid

    def search_corpus(self, query: str, kind: str = "style", limit: int = 2,
                      verified_only: bool = False) -> list[dict]:
        if not query:
            return []
        rows = []
        q = self._fts_query(query)
        if q:
            sql = ("SELECT c.id, c.text, c.source, c.meaning, c.trigger, c.examples, "
                   "c.tags, c.verified, c.score FROM corpus_fts_v2 f "
                   "JOIN corpus c ON c.id=f.rowid "
                   "WHERE corpus_fts_v2 MATCH ? AND f.kind=?")
            args: list = [q, kind]
            if verified_only:
                sql += " AND c.verified=1 AND c.status='active'"
            sql += " ORDER BY rank LIMIT ?"
            args.append(limit)
            try:
                rows = self.db.execute(sql, args).fetchall()
            except Exception:
                rows = []
        if not rows:
            # trigram 只适合 3 字以上；中文 2 字关键词用 LIKE 做联想召回
            stop = {"这个", "那个", "什么", "怎么", "觉得", "就是", "真的", "可以",
                    "现在", "今天", "我们", "你们", "他们", "没有", "一个", "还是"}
            terms: list[str] = []
            for seg in re.findall(r"[\u4e00-\u9fff]{2,}", query or ""):
                for i in range(len(seg) - 1):
                    t = seg[i:i + 2]
                    if t not in stop and t not in terms:
                        terms.append(t)
            for w in re.findall(r"[A-Za-z0-9]{2,}", query or ""):
                if w.lower() not in terms:
                    terms.append(w.lower())
            scored: dict[int, tuple[int, tuple]] = {}
            for term in terms[:8]:
                sql = ("SELECT id, text, source, meaning, trigger, examples, tags, verified, score "
                       "FROM corpus WHERE kind=? AND (text LIKE ? OR meaning LIKE ? "
                       "OR trigger LIKE ? OR tags LIKE ?)")
                args = [kind, f"%{term}%", f"%{term}%", f"%{term}%", f"%{term}%"]
                if verified_only:
                    sql += " AND verified=1 AND status='active'"
                sql += " ORDER BY id DESC LIMIT 20"
                for r in self.db.execute(sql, args).fetchall():
                    blob = " ".join(str(x or "") for x in r[1:7]).lower()
                    hits = sum(1 for t in terms[:8] if t in blob)
                    scored[r[0]] = (hits, r)
            rows = [r for _, r in sorted(scored.values(), key=lambda x: (x[0], x[1][8]),
                                         reverse=True)[:limit]]
        return [{"id": r[0], "text": r[1], "source": r[2], "meaning": r[3] or "",
                 "trigger": r[4] or "", "examples": r[5] or "", "tags": r[6] or "",
                 "verified": r[7], "score": r[8]} for r in rows]

    def corpus_block(self, query: str, limit: int | None = None) -> str:
        if not CORPUS_ENABLED:
            return ""
        items = self.search_corpus(query, "style", limit or CORPUS_MAX_ITEMS, True)
        if not items:
            return ""
        out = ["【参考语料（只学说法/用词，不改变人格、身份和立场；不要照搬内容）】"]
        total = 0
        for it in items:
            parts = [f"- 原句：{it['text'].strip()[:120]}"]
            if it["meaning"]:
                parts.append(f"  意思：{it['meaning'][:80]}")
            if it["trigger"]:
                parts.append(f"  用法：{it['trigger'][:60]}")
            if it["examples"]:
                try:
                    ex = json.loads(it["examples"])
                    ext = " / ".join(str(x)[:60] for x in ex[:2]) if isinstance(ex, list) else str(ex)
                except Exception:
                    ext = it["examples"]
                parts.append(f"  例句：{ext[:120]}")
            block = "\n".join(parts)
            if total + len(block) > CORPUS_MAX_CHARS:
                break
            out.append(block)
            total += len(block)
        return "\n".join(out)

    def corpus_stats(self) -> list[tuple[str, int]]:
        return self.db.execute(
            "SELECT kind, COUNT(*) FROM corpus GROUP BY kind ORDER BY COUNT(*) DESC"
        ).fetchall()

    def corpus_counts(self) -> dict:
        total = self.db.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
        active = self.db.execute(
            "SELECT COUNT(*) FROM corpus WHERE verified=1 AND status='active'").fetchone()[0]
        return {"total": int(total), "active": int(active)}

    def update_corpus(self, cid: int, delta: float = 0.0,
                      status: str | None = None) -> int:
        row = self.db.execute("SELECT score FROM corpus WHERE id=?", (cid,)).fetchone()
        if not row:
            return 0
        score = max(0.0, min(1.0, float(row[0] or 0) + delta))
        if status is None and score < 0.3:
            status = "retired"
        self.db.execute(
            "UPDATE corpus SET score=?, status=COALESCE(?, status), updated_at=? WHERE id=?",
            (score, status, int(time.time()), cid),
        )
        self.db.commit()
        return 1

    # ---- 人工评测 / 抽检 -----------------------------------------------
    def add_eval_case(self, gid: int, conv: str, uid: int, nickname: str,
                      case_type: str, context: list[dict], trigger: str,
                      reply: str, src_id: int) -> int:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO eval_cases(gid, conv, uid, nickname, case_type, "
            "context, trigger, reply, src_id, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?, 'pending', ?)",
            (gid, conv, uid, (nickname or "")[:40], case_type,
             json.dumps(context, ensure_ascii=False), trigger[:300], reply[:500],
             src_id, int(time.time())),
        )
        self.db.commit()
        return cur.lastrowid if cur.rowcount else 0

    def next_eval_case(self) -> dict | None:
        row = self.db.execute(
            "SELECT id, gid, conv, uid, nickname, case_type, context, trigger, reply "
            "FROM eval_cases WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "gid": row[1], "conv": row[2], "uid": row[3],
                "nickname": row[4], "case_type": row[5],
                "context": json.loads(row[6] or "[]"), "trigger": row[7] or "",
                "reply": row[8] or ""}

    def get_eval_case(self, cid: int) -> dict | None:
        row = self.db.execute(
            "SELECT id, gid, conv, uid, nickname, case_type, context, trigger, reply "
            "FROM eval_cases WHERE id=?", (cid,)
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "gid": row[1], "conv": row[2], "uid": row[3],
                "nickname": row[4], "case_type": row[5],
                "context": json.loads(row[6] or "[]"), "trigger": row[7] or "",
                "reply": row[8] or ""}

    def label_eval_case(self, cid: int, label: str, should_reply: str,
                        note: str = "") -> int:
        cur = self.db.execute(
            "UPDATE eval_cases SET status='labeled', label=?, should_reply=?, "
            "note=?, labeled_at=? WHERE id=?",
            (label[:30], should_reply[:10], note[:300], int(time.time()), cid),
        )
        self.db.commit()
        return cur.rowcount

    def skip_eval_case(self, cid: int) -> int:
        cur = self.db.execute(
            "UPDATE eval_cases SET status='skipped', labeled_at=? WHERE id=?",
            (int(time.time()), cid),
        )
        self.db.commit()
        return cur.rowcount

    def eval_stats(self) -> dict:
        rows = self.db.execute(
            "SELECT status, label, COUNT(*) FROM eval_cases GROUP BY status, label"
        ).fetchall()
        total = self.db.execute("SELECT COUNT(*) FROM eval_cases").fetchone()[0]
        pending = self.db.execute(
            "SELECT COUNT(*) FROM eval_cases WHERE status='pending'").fetchone()[0]
        labels = {}
        for st, lb, n in rows:
            if st == "labeled" and lb:
                labels[lb] = labels.get(lb, 0) + n
        return {"total": int(total), "pending": int(pending), "labels": labels}

    def export_eval(self, path: Path) -> int:
        import csv
        rows = self.db.execute(
            "SELECT id, gid, uid, nickname, case_type, trigger, reply, status, "
            "label, should_reply, note, created_at FROM eval_cases ORDER BY id"
        ).fetchall()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "gid", "uid", "nickname", "case_type", "trigger",
                        "reply", "status", "label", "should_reply", "note", "created_at"])
            w.writerows(rows)
        return len(rows)

    # ---- 设置 / 知识 / 清理 --------------------------------------------
    def profile(self, conv: str) -> tuple[str, str, int]:
        row = self.db.execute(
            "SELECT nickname, facts, counter FROM profiles WHERE conv=?", (conv,)
        ).fetchone()
        return (row or ("", "", 0))

    def bump(self, conv: str, nickname: str | None = None) -> int:
        nick, facts, counter = self.profile(conv)
        counter += 1
        self.db.execute(
            "INSERT INTO profiles(conv, nickname, facts, counter, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(conv) DO UPDATE SET "
            "nickname=excluded.nickname, facts=excluded.facts, "
            "counter=excluded.counter, updated_at=excluded.updated_at",
            (conv, nickname or nick, facts, counter, int(time.time())),
        )
        self.db.commit()
        return counter

    def set_facts(self, conv: str, facts: str) -> None:
        nick, _, counter = self.profile(conv)
        self.db.execute(
            "INSERT INTO profiles(conv, nickname, facts, counter, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(conv) DO UPDATE SET "
            "facts=excluded.facts, updated_at=excluded.updated_at",
            (conv, nick, facts, counter, int(time.time())),
        )
        self.db.commit()
        self.sync_facts(conv, facts)

    def get_setting(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_setting(self, k: str, v: str) -> None:
        self.db.execute(
            "INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )
        self.db.commit()

    def del_setting(self, k: str) -> None:
        self.db.execute("DELETE FROM settings WHERE k=?", (k,))
        self.db.commit()

    def add_knowledge(self, topic: str, summary: str) -> None:
        self.db.execute("INSERT INTO knowledge(topic, summary, ts) VALUES(?,?,?)",
                        (topic[:60], summary[:200], int(time.time())))
        self.db.execute("DELETE FROM knowledge WHERE id NOT IN "
                        "(SELECT id FROM knowledge ORDER BY id DESC LIMIT 300)")
        self.db.commit()

    def knowledge_for(self, text: str, limit: int = 3) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT topic, summary FROM knowledge ORDER BY id DESC LIMIT 80").fetchall()
        grams = {text[i:i + 2] for i in range(max(0, len(text) - 1))}
        scored = []
        for topic, summary in rows:
            if JUNK_TITLE.search((topic or "") + (summary or "")):
                continue
            tg = {topic[i:i + 2] for i in range(max(0, len(topic) - 1))}
            score = len(grams & tg)
            if score:
                scored.append((score, topic, summary))
        scored.sort(reverse=True)
        return [(t, s_) for _, t, s_ in scored[:limit]]

    def reset(self, conv: str) -> None:
        if self.fts_ready:
            try:
                self.db.execute(
                    "DELETE FROM msg_fts WHERE rowid IN (SELECT id FROM messages WHERE conv=?)",
                    (conv,),
                )
            except Exception:
                pass
        for table in ("messages", "profiles", "summaries", "memory_items",
                      "self_memory", "topic_state"):
            self.db.execute(f"DELETE FROM {table} WHERE conv=?", (conv,))
        self.db.commit()


STORE = Store(DB_PATH)

# 运行时状态：每个会话最近发言时间 / 已发消息 id / 群聊语气样本
LAST_SPOKE: dict[str, float] = {}
LAST_AMBIENT: dict[str, float] = {}
AMBIENT_TIMES: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))
GROUP_REPLIES: dict[int, deque] = defaultdict(lambda: deque(maxlen=50))
BOT_LAST_GROUP_ID: dict[int, int] = {}
LAST_SPOKE_GROUP: dict[int, float] = {}
SENT_IDS: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))
SENT_TEXT: dict[str, str] = {}
STYLE: dict[int, deque] = defaultdict(lambda: deque(maxlen=12))
MUTE_UNTIL: dict[str, float] = {}     # 临时"不想说话"到期时间
BG_TASKS: set[asyncio.Task] = set()


def spawn_bg(coro) -> None:
    """后台任务必须持有引用，否则可能被 GC；异常写日志。"""
    task = asyncio.create_task(coro)
    BG_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        BG_TASKS.discard(t)
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log_line(f"[bg] {type(e).__name__}: {e}")

    task.add_done_callback(_done)


# ---------------------------------------------------------------- DeepSeek
def chat(messages: list[dict], temperature: float = 0.8, max_tokens: int = 300) -> str:
    payload = json.dumps(
        {"model": MODEL, "messages": messages, "temperature": temperature,
         "max_tokens": max_tokens, "stream": False}
    ).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    return (data["choices"][0]["message"]["content"] or "").strip()


MEMORY_PROMPT = (
    "你是信息整理助手。从对话中提取关于「对方」的长期有效信息（身份、喜好、习惯、称呼、重要的人或事、约定），"
    "与已有记录合并去重，每行一条以「- 」开头，只输出条目；没有就输出「- 暂无」。"
)


_POLICY_CACHE = {"mtime": 0.0, "text": ""}


def policy_block() -> str:
    """读取工作台发布的补充规则。只追加，不覆盖人格核心/RULES。"""
    if not POLICY_ENABLED:
        return ""
    try:
        st = POLICY_FILE.stat()
        if st.st_mtime == _POLICY_CACHE["mtime"]:
            return _POLICY_CACHE["text"]
        data = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        rules = [r for r in data.get("rules", []) if r.get("status", "active") == "active"]
        rules.sort(key=lambda r: int(r.get("priority", 100)))
        lines = []
        for r in rules[:20]:
            text = str(r.get("text", "")).strip()
            if text:
                lines.append("- " + text[:300])
        out = ""
        if lines:
            out = ("【工作台补充规则（版本 " + str(data.get("version", 0)) +
                   "；只补充，不覆盖人格/RULES；冲突时以人格核心为准）】\n" + "\n".join(lines))
        _POLICY_CACHE.update(mtime=st.st_mtime, text=out)
        return out
    except Exception as e:
        log_line(f"[policy] 读取失败 {type(e).__name__}: {e}")
        return ""


_CANON_CACHE = {"mtime": 0.0, "items": []}


def canon_block(query: str) -> str:
    """从 control/canon.json 里按标题/标签/二元组召回最多 N 条。"""
    if not CANON_ENABLED or not query:
        return ""
    try:
        st = CANON_FILE.stat()
        if st.st_mtime != _CANON_CACHE["mtime"]:
            data = json.loads(CANON_FILE.read_text(encoding="utf-8"))
            _CANON_CACHE["items"] = [x for x in data.get("items", [])
                                     if isinstance(x, dict)
                                     and x.get("status", "active") in ("active", "approved")]
            _CANON_CACHE["mtime"] = st.st_mtime
        q2 = {query[i:i + 2] for i in range(max(0, len(query) - 1))}
        picked = []
        for it in _CANON_CACHE["items"]:
            title = str(it.get("title", ""))
            summary = str(it.get("summary", ""))
            tags = str(it.get("tags", ""))
            hay = title + summary + tags
            hit = any(t.strip() and t.strip() in query for t in tags.replace("，", ",").split(","))
            if not hit:
                h2 = {hay[i:i + 2] for i in range(max(0, len(hay) - 1))}
                hit = len(q2 & h2) >= 2
            if hit:
                picked.append(it)
            if len(picked) >= CANON_MAX_ITEMS:
                break
        if not picked:
            return ""
        lines = ["【Canon 参考（只取观点，不照搬原话，不改变人格）】"]
        for it in picked:
            lines.append(f"- {str(it.get('title', ''))[:60]}：{str(it.get('summary', ''))[:180]}")
        return "\n".join(lines)
    except Exception as e:
        log_line(f"[canon] 读取失败 {type(e).__name__}: {e}")
        return ""


def build_messages(conv: str, text: str, gid: int | None = None, master: bool = False,
                   force_active: bool = False, uid: int | None = None,
                   nickname: str = "", exclude_group_id: int | None = None,
                   quoted: str = "") -> list[dict]:
    system = get_persona() + "\n" + RULES + "\n" + time_hint()
    pb = policy_block()
    if pb:
        system += "\n\n" + pb
    cb = canon_block(text)
    if cb:
        system += "\n\n" + cb
    if quoted:
        system += ("\n\n【对方引用了你刚才说的话】\n" + quoted[:300] +
                   "\n对方是在针对这句话回应。先接住这个点，再回答；不要答非所问。")
    if master:
        system += "\n" + MASTER_RULES
    _busy = not force_active and not is_active_time()
    if _busy:
        system += "\n" + busy_rules_text()
    elif gid:
        system += "\n" + GROUP_RULES
    if is_question(text):
        system += "\n" + QUESTION_RULES

    # 群聊：优先用这个成员在群里的画像；私聊：用会话 facts
    if gid and uid:
        facts = STORE.member_profile_block(gid, uid)
        if not facts:
            facts = STORE.memory_block(conv, text)
    else:
        facts = STORE.memory_block(conv, text)
    if facts:
        who = f"群成员「{nickname or uid}」" if gid and uid else "对方"
        system += f"\n\n【你记得{who}的事（自然带入，不要复述）】\n{facts}"

    selfblock = STORE.self_block(conv, text)
    if selfblock:
        system += ("\n\n【你之前说过的话（别自相矛盾；不确定就说记不清）】\n" + selfblock)

    summary = STORE.latest_summary(conv)
    if summary:
        system += "\n\n【之前聊过的内容摘要（细节别编）】\n" + summary[:1000]

    kn = STORE.knowledge_for(text)
    if kn:
        system += "\n\n【你以前查过、可能相关的信息（可以自然用上）】\n" + \
                  "\n".join(f"- {t}：{s_}" for t, s_ in kn)
    if gid:
        styleb = STORE.style_block(gid)
        if styleb:
            system += "\n\n" + styleb
        elif STYLE.get(gid):
            samples = [s for s in list(STYLE[gid])[-6:] if s]
            if samples:
                system += "\n\n【群友最近的说话风格（只学语气，不要照抄内容，不学脏话和情绪）】\n" + \
                          "\n".join(f"- {s[:50]}" for s in samples)
        corpusb = STORE.corpus_block(text)
        if corpusb:
            system += "\n\n" + corpusb
        last_self = STORE.last_group_assistant(gid)
        if last_self:
            system += ("\n\n【你刚才在这个群说过】\n" + last_self +
                       "\n同一话题优先延续/补充这个观点；对方换话题时可以不接，不要每来一个人就换立场。")

    if gid and uid:
        mentions = STORE.mentioned_member_blocks(gid, text, uid)
        if mentions:
            system += ("\n\n【群里被提到的人（只在对题时自然用，别暴露你记得）】\n" +
                       "\n".join(mentions))

    if gid:
        # 群共享上下文：最近所有成员的发言，带昵称；当前这条先排除
        recent = STORE.group_recent(gid, GROUP_CTX_MSGS, GROUP_CTX_CHARS)
        if exclude_group_id:
            recent = [m for m in recent if m["id"] != exclude_group_id]
        # 群日志还没积累起来时，回退到当前成员自己的历史，避免升级后上下文断档
        if len(recent) < 3:
            fb = STORE.recent_budget(conv, CTX_MAX_MSGS, CTX_BUDGET_CHARS)
            if fb and fb[-1]["role"] == "user" and fb[-1]["content"] == text:
                fb.pop()
            recent = [{"id": m["id"],
                       "uid": uid if m["role"] == "user" else 0,
                       "nickname": (nickname if m["role"] == "user" else "你"),
                       "role": m["role"], "content": m["content"], "ts": m["ts"]}
                      for m in fb]
        if recent and recent[-1]["role"] == "user" and uid and recent[-1]["uid"] == uid:
            recent.pop()
        msgs = [{"role": "system", "content": system}]
        for m in recent:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": f"{m['nickname'] or m['uid']}：{m['content']}"})
            else:
                msgs.append({"role": "assistant", "content": m["content"]})
        msgs.append({"role": "system", "content": clock_line()})
        if uid:
            msgs.append({"role": "system", "content":
                         f"当前发言人：{nickname or uid}（QQ {uid}）。只回应这条消息，不要替别人说话。"})
        msgs.append({"role": "user", "content": text})
        return msgs

    recent = STORE.recent_budget(conv, CTX_MAX_MSGS, CTX_BUDGET_CHARS)
    if recent and recent[-1]["role"] == "user" and recent[-1]["content"] == text:
        recent.pop()
    if recent:
        oldest = recent[0]["id"]
        old = STORE.search_messages(conv, text, RECALL_LIMIT, before_id=oldest)
        if old:
            lines = []
            for m in old:
                who = "对方" if m["role"] == "user" else "你"
                when = time.strftime("%m-%d %H:%M", time.localtime(m["ts"]))
                lines.append(f"- [{when}] {who}：{(m['content'] or '')[:120]}")
            system += "\n\n【可能相关的旧事（只在对题时用，别硬提）】\n" + "\n".join(lines)

    topic = STORE.topic_block(conv)
    if topic:
        system += "\n\n" + topic

    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in recent]
    msgs.append({"role": "system", "content": clock_line()})
    msgs.append({"role": "user", "content": text})
    return msgs


def refresh_memory(conv: str) -> None:
    _, facts, _ = STORE.profile(conv)
    recent = STORE.recent(conv, 10)
    if not recent:
        return
    dialog = "\n".join(f"{'对方' if m['role'] == 'user' else '你'}：{m['content']}" for m in recent)
    try:
        new_facts = chat(
            [{"role": "system", "content": MEMORY_PROMPT},
             {"role": "user", "content": f"【已有】\n{facts.strip() or '- 暂无'}\n\n【对话】\n{dialog}"}],
            temperature=0.2, max_tokens=300,
        )
        if new_facts:
            STORE.set_facts(conv, new_facts)
            log_line(f"[memory] {conv} -> {new_facts[:80]}")
    except Exception as e:
        log_line(f"[memory] 失败 {e}")


SUMMARY_PROMPT = (
    "你是会话压缩器。把给出的旧对话压缩成 5~8 条要点，必须保留：\n"
    "1) 对方的事实、偏好、关系、约定；\n"
    "2) 你（机器人）说过的话、承诺、计划；\n"
    "3) 已经决定的事和还没解决的问题。\n"
    "只写对话里出现过的信息，不要编造、不要评价、不要推测。"
    "用「你」指代对话里的机器人，不要出现「机器人/AI/模型/助手」这些词。"
    "每条一行，总长不超过 300 字。"
)

TOPIC_PROMPT = (
    "根据最近对话更新“当前话题”。只输出 JSON，不要解释：\n"
    '{"topic":"不超过40字","entities":["最多5个"],"open_questions":["最多3条"]}\n'
    "没有实体或未解决问题就给空数组。"
)


MEMBER_PROMPT = (
    "你是群成员画像整理器。只根据这个成员在群里的发言，整理一份人物画像。只写他明确说过的：\n"
    "1) 称呼/身份/职业/年龄段（没明说就不写）；\n"
    "2) 关注的话题、游戏、赛事、作品；\n"
    "3) 观点/立场/口头禅/说话风格；\n"
    "4) 和群里其他人的关系（只写明确提到的）。\n"
    "输出 3~8 行，每行以「- 」开头。不要编造隐私、不要评价、不要推测、不要写对话里没有的东西。"
)


def update_member_profile(gid: int, uid: int, nickname: str) -> None:
    """从一个群员最近的发言里提炼人物画像；失败不影响聊天。"""
    try:
        rows = STORE.db.execute(
            "SELECT content FROM group_log WHERE gid=? AND uid=? AND role='user' "
            "ORDER BY id DESC LIMIT ?", (gid, uid, MEMBER_PROFILE_SAMPLE)
        ).fetchall()
        if len(rows) < 3:
            return
        prev = STORE.member_profile(gid, uid)
        dialog = "\n".join(f"- {(r[0] or '')[:200]}" for r in reversed(rows))
        raw = chat([
            {"role": "system", "content": MEMBER_PROMPT},
            {"role": "user", "content": f"【已有画像】\n{(prev or {}).get('facts') or '（无）'}\n\n"
                                        f"【他最近的发言】\n{dialog}"},
        ], 0.2, 450)
        facts = (raw or "").strip()
        if not facts:
            return
        STORE.set_member_profile(gid, uid, nickname, facts)
        log_line(f"[profile] {gid}/{uid} {nickname} -> {facts[:60]}")
    except Exception as e:
        log_line(f"[profile] 失败 {e}")


def update_topic(conv: str) -> None:
    """用最近几轮更新当前话题；失败不影响聊天。"""
    recent = STORE.recent(conv, 4)
    if len(recent) < 2:
        return
    dialog = "\n".join(f"{'对方' if m['role'] == 'user' else '你'}：{m['content'][:160]}"
                       for m in recent)
    try:
        raw = chat([{"role": "system", "content": TOPIC_PROMPT},
                    {"role": "user", "content": dialog}], 0.2, 160)
        m = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(m.group(0)) if m else {}
        topic = str(data.get("topic", "")).strip()
        if not topic:
            return
        entities = [str(x)[:20] for x in data.get("entities", []) if str(x).strip()][:5]
        open_q = [str(x)[:40] for x in data.get("open_questions", []) if str(x).strip()][:3]
        STORE.set_topic(conv, topic, entities, open_q)
        log_line(f"[topic] {conv} -> {topic[:40]}")
    except Exception as e:
        log_line(f"[topic] 失败 {e}")


def summarize_conv(conv: str) -> None:
    """把最老的未归档消息压成摘要；消息只归档不删除。"""
    cnt, chars = STORE.unarchived_stats(conv)
    if cnt < SUMMARY_TRIGGER_MSGS and chars < CTX_BUDGET_CHARS:
        return
    msgs = STORE.oldest_unarchived(conv, SUMMARY_CHUNK)
    if len(msgs) < 5:
        return
    prev = STORE.latest_summary(conv)
    dialog = "\n".join(f"{'对方' if m['role'] == 'user' else '你'}：{m['content'][:300]}"
                       for m in msgs)
    try:
        raw = chat([
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"【已有摘要（可衔接）】\n{prev or '（无）'}\n\n【旧对话】\n{dialog}"},
        ], 0.3, 450)
        summary = re.sub(r"机器人", "你", (raw or "").strip())
        if not summary:
            return
        STORE.add_summary(conv, 1, msgs[0]["id"], msgs[-1]["id"], summary)
        STORE.archive_ids([m["id"] for m in msgs])
        log_line(f"[summary] {conv} 归档 {len(msgs)} 条 -> {summary[:60]}")

        if STORE.summaries_count(conv) > MAX_SUMMARIES:
            olds = STORE.oldest_summaries(conv, 2)
            if len(olds) == 2:
                merged = chat([
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": "【摘要 A】\n" + olds[0]["content"] +
                                                "\n\n【摘要 B】\n" + olds[1]["content"] +
                                                "\n\n把两段合并成一段，保留事实、承诺和未解决话题。"},
                ], 0.2, 450)
                if merged:
                    merged = re.sub(r"机器人", "你", merged)
                    level = max(int(o["level"]) for o in olds) + 1
                    STORE.replace_summaries(conv, [o["id"] for o in olds], level, 0, 0, merged)
                    log_line(f"[summary] {conv} 合并两级摘要 -> {merged[:60]}")
    except Exception as e:
        log_line(f"[summary] 失败 {e}")


# ---------------------------------------------------------------- 文本处理
SENT_RE = re.compile(r"[^。！？!?\n~]+[。！？!?~]*")


def split_sentences(text: str, max_len: int | None = None) -> list[str]:
    max_len = max_len or MAX_CHARS
    """按句号/问号/感叹号/换行断句；超长再按逗号、空格断，保证每句 ≤max_len"""
    raw = [s.strip() for s in SENT_RE.findall(text) if s.strip()]
    out: list[str] = []
    for s in raw:
        while len(s) > max_len:
            cut = max(s.rfind("，", 0, max_len), s.rfind(",", 0, max_len),
                      s.rfind("、", 0, max_len), s.rfind(" ", 0, max_len))
            if cut < max_len * 0.4:
                cut = max_len
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)
    return out[:10] or [text[:max_len]]


def strip_markdown(text: str) -> str:
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`{1,3}(.+?)`{1,3}", r"\1", text, flags=re.S)
    return text.strip()


QUESTION_RE = re.compile(
    r"[?？]|(吗|呢|什么|怎么|为什么|为啥|哪|谁|多少|几|多久|是不是|能不能|会不会|"
    r"可不可以|如何|咋|干嘛|有没有|对不对|好不好|行不行|要不要|在吗)"
)


def is_question(text: str) -> bool:
    return bool(QUESTION_RE.search(text or ""))


def has_profanity(text: str) -> bool:
    return any(w in text.lower() for w in PROFANITY)


def log_line(line: str) -> None:
    with open(LOG_DIR / "bot.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%F %T')} {line}\n")


def log_msg(tag: str, event: dict, text: str) -> None:
    kind = event.get("message_type", "?")
    if kind == "group":
        who = f"群{event.get('group_id')}/{event.get('user_id')}"
    elif event.get("sub_type") == "group":
        who = f"临时{event.get('user_id')}@群{event.get('group_id')}"
    else:
        who = f"私聊{event.get('user_id')}"
    with open(LOG_DIR / "messages.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%F %T')} {tag} {who} {text[:200]!r}\n")


def plain_text(segments) -> str:
    if isinstance(segments, str):
        return segments
    out = []
    for seg in segments or []:
        t = seg.get("type")
        if t == "text":
            out.append(seg.get("data", {}).get("text", ""))
        elif t == "at":
            out.append(f"@{seg.get('data', {}).get('qq', '')}")
    return "".join(out).strip()


ALLOW_WAKE_WORD = ENV.get("ALLOW_WAKE_WORD", "0") == "1"


def mentioned(segments, self_id: int, text: str) -> bool:
    """严格模式：只有明确 @ 她本人才算，@全体 / @别人 / 昵称提及都不算"""
    if isinstance(segments, str):
        hit = f"@{self_id}" in segments
    else:
        hit = False
        for seg in segments or []:
            if seg.get("type") == "at" and str(seg.get("data", {}).get("qq", "")) == str(self_id):
                hit = True
                break
    if not hit and ALLOW_WAKE_WORD:
        hit = any(w in text for w in WAKE_WORDS)
    return hit


def has_reply_segment(event: dict) -> bool:
    segs = event.get("message")
    return isinstance(segs, list) and any(
        isinstance(seg, dict) and seg.get("type") == "reply" for seg in segs)


def is_reply_to_me(event: dict, conv: str) -> bool:
    """是否在引用回复机器人的某条消息。"""
    segs = event.get("message")
    if isinstance(segs, list):
        for seg in segs:
            if seg.get("type") == "reply":
                mid = seg.get("data", {}).get("id")
                if mid is None:
                    continue
                if str(mid) in SENT_TEXT or str(mid) in {str(i) for i in SENT_IDS.get(conv, [])}:
                    return True
    return False


def segs_to_text(segs) -> str:
    if isinstance(segs, str):
        return segs
    if not isinstance(segs, list):
        return ""
    out = []
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            out.append(str(d.get("text", "")))
        elif t == "at":
            out.append("@" + str(d.get("qq", "")))
        elif t == "face":
            out.append("[表情]")
        elif t == "image":
            out.append("[图片]")
    return "".join(out).strip()


async def get_quoted_text(event: dict, conv: str) -> str:
    """取对方引用/回复的那条机器人消息的原文。优先用本地缓存，必要时 get_msg。"""
    segs = event.get("message")
    if not isinstance(segs, list):
        return ""
    for seg in segs:
        if seg.get("type") != "reply":
            continue
        mid = seg.get("data", {}).get("id")
        if mid is None:
            continue
        key = str(mid)
        if key in SENT_TEXT:
            return SENT_TEXT[key][:300]
        try:
            res = await BOT.call("get_msg", message_id=mid)
            data = (res or {}).get("data") or {}
            sender = str((data.get("sender") or {}).get("user_id") or "")
            if sender and sender != str(event.get("self_id")):
                continue
            txt = segs_to_text(data.get("message"))
            if txt:
                return txt[:300]
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------- OneBot
class OneBot:
    def __init__(self) -> None:
        self.ws = None
        self.pending: dict[str, asyncio.Future] = {}
        self.self_id = 0

    async def run(self) -> None:
        import websockets

        headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
        backoff = 3
        while True:
            try:
                async with websockets.connect(
                    WS_URL, additional_headers=headers, ping_interval=30,
                    max_size=32 * 1024 * 1024,
                ) as ws:
                    self.ws = ws
                    backoff = 3
                    log_line(f"[ws] 已连接 {WS_URL}")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if "echo" in msg and msg["echo"] in self.pending:
                            fut = self.pending.pop(msg["echo"])
                            if not fut.done():
                                fut.set_result(msg)
                        elif msg.get("post_type") == "meta_event":
                            self.self_id = msg.get("self_id", self.self_id)
                        else:
                            asyncio.create_task(dispatch(msg))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_line(f"[ws] 断开 {type(e).__name__}: {e}，{backoff}s 后重连")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def call(self, action: str, **params) -> dict:
        if not self.ws:
            raise RuntimeError("WebSocket 未连接")
        echo = f"e{int(time.time() * 1000)}{random.randint(0, 999)}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
        try:
            return await asyncio.wait_for(fut, timeout=20)
        finally:
            self.pending.pop(echo, None)


BOT = OneBot()
_sem = asyncio.Semaphore(MAX_CONCURRENCY)
_conv_locks: dict[str, asyncio.Lock] = {}


def conv_lock(conv: str) -> asyncio.Lock:
    if conv not in _conv_locks:
        _conv_locks[conv] = asyncio.Lock()
    return _conv_locks[conv]


async def dispatch(event: dict) -> None:
    if event.get("post_type") != "message":
        return
    if str(event.get("user_id")) == str(event.get("self_id")):
        return
    async with _sem:
        try:
            await handle_message(event)
        except Exception as e:
            log_line(f"[error] {type(e).__name__}: {e}")


def muted(key: str) -> bool:
    return MUTE_UNTIL.get(key, 0) > time.time()


def maybe_mute(key: str) -> None:
    """小概率进入一段"不想说话"的状态，像人一样有情绪起伏"""
    if MUTE_PROB > 0 and random.random() < MUTE_PROB:
        MUTE_UNTIL[key] = time.time() + random.uniform(60, MUTE_MINUTES * 60)
        log_line(f"[mood] {key} 进入不想说话状态 {MUTE_MINUTES} 分钟内随机")


# ---------------------------------------------------------------- 发送（拆句 + 延迟）
async def send_one(event: dict, text: str) -> dict:
    mtype = event.get("message_type")
    if mtype == "group":
        return await BOT.call("send_group_msg", group_id=event.get("group_id"), message=text)
    elif event.get("sub_type") == "group" or (event.get("group_id") and not event.get("group_id") == 0):
        return await BOT.call("send_private_msg", user_id=event.get("user_id"),
                              group_id=event.get("group_id"), message=text)
    return await BOT.call("send_private_msg", user_id=event.get("user_id"), message=text)


def typing_delay(text: str) -> float:
    """按下一句字数估算打字耗时：基础停顿 + 每字随机耗时，封顶后再加抖动。"""
    base = random.uniform(DELAY_MIN, DELAY_MAX)
    per_char = len(text) * random.uniform(TYPE_PER_CHAR_MIN, TYPE_PER_CHAR_MAX)
    return min(base + per_char, TYPE_DELAY_MAX) + random.uniform(JITTER_MIN, JITTER_MAX)


async def say(event: dict, conv: str, text: str) -> None:
    """按人格卡要求逐句发送：按下一句字数估算打字耗时，连发 3 条后停 3~6s"""
    sentences = split_sentences(strip_markdown(text))
    count = 0
    for i, s in enumerate(sentences):
        s = s[:MAX_CHARS]
        if not s:
            continue
        res = await send_one(event, s)
        log_msg("发送", event, s)
        _gid = event.get("group_id")
        if _gid:
            _mid = STORE.log_group(int(_gid), int(event.get("self_id") or 0), "你", "assistant", s)
            if _mid:
                BOT_LAST_GROUP_ID[int(_gid)] = _mid
        try:
            mid = (res or {}).get("data", {}).get("message_id")
            if mid:
                SENT_IDS[conv].append(mid)
                SENT_TEXT[str(mid)] = s
                if len(SENT_TEXT) > 500:
                    SENT_TEXT.pop(next(iter(SENT_TEXT)), None)
        except Exception:
            pass
        count += 1
        if i == len(sentences) - 1:
            break
        if count >= BATCH_SIZE:
            count = 0
            await asyncio.sleep(random.uniform(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX)
                                + random.uniform(JITTER_MIN, JITTER_MAX))
        else:
            await asyncio.sleep(typing_delay(sentences[i + 1]))
    LAST_SPOKE[conv] = time.time()
    if conv.startswith("group:"):
        try:
            LAST_SPOKE_GROUP[int(conv.split(":")[1])] = time.time()
        except Exception:
            pass



# ---------------------------------------------------------------- 主人控制通道
PARAM_TYPES = {
    "MAX_MSG_CHARS": int, "DELAY_MIN": float, "DELAY_MAX": float,
    "JITTER_MIN": float, "JITTER_MAX": float,
    "READ_PER_CHAR": float, "READ_MAX": float,
    "TYPE_PER_CHAR_MIN": float, "TYPE_PER_CHAR_MAX": float, "TYPE_DELAY_MAX": float,
    "BATCH_SIZE": int, "BATCH_PAUSE_MIN": float, "BATCH_PAUSE_MAX": float,
    "FOLLOWUP_WINDOW": int, "AMBIENT_PROB": float, "AMBIENT_COOLDOWN": int,
    "GROUP_REPLY_COOLDOWN": int, "GROUP_MIN_OTHERS_MSGS": int,
    "GROUP_MAX_REPLIES_10MIN": int, "LULL_SEC": int,
    "AMBIENT_THRESHOLD": float, "AMBIENT_MAX_10MIN": int,
    "AMBIENT_MODE": str,
    "MEMORY_EVERY": int,
    "BUSY_FROM_HOUR": int, "BUSY_AFTER_HOUR": int,
    "REPLY_PROB": float, "REPLY_DELAY_MIN": float, "REPLY_DELAY_MAX": float,
    "MUTE_PROB": float, "MUTE_MINUTES": int,
}


PARAM_GLOBALS = {k: k for k in PARAM_TYPES}
PARAM_GLOBALS["MAX_MSG_CHARS"] = "MAX_CHARS"   # 历史命名差异


def pget(k: str):
    return globals()[PARAM_GLOBALS.get(k, k)]


def pset(k: str, v) -> None:
    globals()[PARAM_GLOBALS.get(k, k)] = v
    if k in ("BUSY_FROM_HOUR", "BUSY_AFTER_HOUR"):
        try:
            sch = schedule()
            if k == "BUSY_FROM_HOUR":
                sch["start_hour"] = int(v)
            else:
                sch["end_hour"] = int(v)
            write_schedule(sch)
        except Exception:
            pass


def reload_params() -> None:
    """把 data/bot.db 里 settings 的覆盖值写回全局变量（热生效，不用重启）"""
    for k, typ in PARAM_TYPES.items():
        v = STORE.get_setting("param:" + k)
        if v is not None:
            try:
                pset(k, typ(v))
            except ValueError:
                pass


def is_master(event: dict) -> bool:
    return str(event.get("user_id")) in MASTER_IDS


async def say_cmd(event: dict, text: str) -> None:
    """主人指令的回执：整条发送（过长才按 1500 字切），不做拟人延迟"""
    text = text.strip()
    while text:
        chunk, text = text[:1500], text[1500:]
        await send_one(event, chunk)
        log_msg("指令", event, chunk)


HELP_TEXT = (
    "主人指令（仅你可见生效）：\n"
    "/人格 查看当前人格\n"
    "/人格 <文本> 整体替换人格\n"
    "/追加人格 <文本> 在现有后追加\n"
    "/重置人格 回到 config/system_prompt.txt\n"
    "/参数 查看可调参数\n"
    "/参数 KEY=VALUE 调整（如 AMBIENT_PROB=0）\n"
    "/群 看她所在的群\n"
    "/群答 [群号] 问题 —— 她回答这个问题，并把回答发到群里\n"
    "/群答 开 / 关 —— 开启后，私聊问的问题回答都发到群里\n"
    "/说 群号 内容 —— 让她原话发到群里\n"
    "/冒泡 群号 [话题] —— 让她自己结合群聊说一句\n"
    "/记忆 查看当前会话记住的内容\n"
    "/画像 [QQ号] 查看群成员画像（不带号则看当前会话）\n"
    "/清空画像 [QQ号] 清空某个群员的画像\n"
    "/评测 [采集 7|统计|导出] 人工抽检；/评 <标签> 记录当前用例\n"
    "/语料 [查 关键词] 查看/检索参考语料\n"
    "/语料 导入 <文件> [来源] 导入 data/corpus/ 下的语料\n"
    "/语料 差 <编号> / 语料 忘 <编号> 降权/停用一条语料\n"
    "/梗 [群号] 查看这个群学到的风格词/梗\n"
    "/忘梗 <编号> 停用某个梗\n"
    "/清空记忆 清空当前会话记忆\n"
    "/状态 运行状态"
)


async def master_command(event: dict, conv: str, text: str) -> bool:
    """返回 True 表示这条消息已被当作指令处理掉"""
    t = text.strip()
    if not t.startswith("/"):
        return False
    body = t[1:].strip()
    head, _, rest = body.partition(" ")
    rest = rest.strip()

    if head in ("help", "帮助", "指令"):
        await say_cmd(event, HELP_TEXT)
        return True

    if head == "人格":
        if not rest:
            await say_cmd(event, "当前人格：\n" + get_persona())
        else:
            save_persona(rest)
            await say_cmd(event, f"✅ 人格已整体替换（{len(rest)} 字），立即生效。")
        return True

    if head in ("追加人格", "补充人格"):
        if rest:
            append_persona(rest)
            await say_cmd(event, "✅ 已追加到人格末尾，立即生效。")
        return True

    if head == "重置人格":
        if rest.lower() in ("yes", "确定", "确认"):
            reset_persona()
            await say_cmd(event, "✅ 已恢复 config/system_prompt.txt 的原始人格。")
        else:
            await say_cmd(event, "会丢掉你改过的版本，确认的话发「/重置人格 确定」。")
        return True

    if head == "参数":
        if not rest:
            lines = [f"{k} = {pget(k)}" for k in PARAM_TYPES]
            await say_cmd(event, "当前参数：\n" + "\n".join(lines))
            return True
        if "=" not in rest:
            await say_cmd(event, "格式：/参数 KEY=VALUE")
            return True
        k, v = [x.strip() for x in rest.split("=", 1)]
        k = k.upper()
        if k not in PARAM_TYPES:
            await say_cmd(event, "没有这个参数。可调：" + "、".join(PARAM_TYPES))
            return True
        try:
            val = PARAM_TYPES[k](v)
        except ValueError:
            await say_cmd(event, f"{k} 需要 {PARAM_TYPES[k].__name__} 类型")
            return True
        STORE.set_setting("param:" + k, str(val))
        pset(k, val)
        await say_cmd(event, f"✅ {k} = {val}（已生效）")
        return True

    if head in ("群", "群列表"):
        await cmd_group_list(event)
        return True

    if head in ("说", "发言", "群发"):
        await cmd_say(event, rest)
        return True

    if head in ("冒泡", "群里冒泡", "说一句"):
        await cmd_bubble(event, rest)
        return True

    if head in ("群答", "答到群", "问群", "群回答"):
        if rest.strip().lower() in ("开", "on", "打开", "开启"):
            STORE.set_setting("group_answer_mode", "1")
            await say_cmd(event, "✅ 群答模式已开启：之后你私聊问的问题，回答会发到群里。")
        elif rest.strip().lower() in ("关", "off", "关闭", "关掉"):
            STORE.set_setting("group_answer_mode", "0")
            await say_cmd(event, "✅ 群答模式已关闭，回答回到私聊。")
        else:
            await cmd_group_answer(event, rest)
        return True

    if head in ("news", "新闻", "播报"):
        await cmd_news(event, rest)
        return True

    if head in ("搜", "搜索", "search"):
        if not rest:
            await say_cmd(event, "用法：/搜 关键词")
            return True
        res = await asyncio.to_thread(web_search, rest)
        if not res:
            await say_cmd(event, "没搜到结果。")
        else:
            out = [f"「{rest}」搜索结果："]
            for i, r in enumerate(res, 1):
                out.append(f"{i}. {r['title'][:40]}\n   {r['snippet'][:70]}")
            await say_cmd(event, "\n".join(out))
        return True

    if head in ("删知识", "删除知识", "忘掉知识"):
        ids = re.findall(r"\d+", rest)
        if not ids:
            await say_cmd(event, "用法：/删知识 12（先 /知识 看编号）")
            return True
        n = 0
        for i in ids:
            cur = STORE.db.execute("DELETE FROM knowledge WHERE id=?", (int(i),))
            n += cur.rowcount
        STORE.db.commit()
        await say_cmd(event, f"✅ 删掉 {n} 条知识记忆。")
        return True

    if head in ("清空知识", "清空记忆库"):
        if rest.strip() in ("确定", "确认", "yes"):
            STORE.db.execute("DELETE FROM knowledge")
            STORE.db.commit()
            await say_cmd(event, "✅ 知识记忆已全部清空。")
        else:
            await say_cmd(event, f"现在有 {STORE.db.execute('select count(*) from knowledge').fetchone()[0]} 条知识，"
                                 "确认清空请发「/清空知识 确定」。")
        return True

    if head in ("加知识", "添加知识"):
        if "|" in rest:
            topic, _, body = rest.partition("|")
        else:
            topic, _, body = rest, "", ""
        if not topic.strip():
            await say_cmd(event, "用法：/加知识 主题|内容")
            return True
        STORE.add_knowledge(topic.strip(), body.strip() or topic.strip())
        await say_cmd(event, f"✅ 已记住：{topic.strip()[:30]}")
        return True

    if head in ("画像", "用户画像"):
        mm = re.search(r"\d{5,}", rest or "")
        if mm:
            target = int(mm.group(0))
            rows = STORE.member_profiles_for_uid(target)
            if not rows:
                await say_cmd(event, f"还没形成 QQ {target} 的群画像。")
                return True
            out = [f"QQ {target} 的群画像："]
            for r in rows:
                out.append(f"[群 {r['gid']}] {r['nickname']}\n{(r['facts'] or '（空）')[:600]}")
            await say_cmd(event, "\n\n".join(out))
            return True
        gid = event.get("group_id")
        if gid:
            prof = STORE.member_profile(int(gid), int(event.get("user_id") or 0))
            if prof and prof["facts"].strip():
                await say_cmd(event, f"这个群成员的画像（{prof['nickname']}）：\n{prof['facts'][:1000]}")
                return True
        _, facts, counter = STORE.profile(conv)
        await say_cmd(event, f"当前对话的画像（{counter} 条消息）：\n{facts.strip() or '（空）'}")
        return True

    if head in ("改画像", "设置画像"):
        if not rest.strip():
            await say_cmd(event, "用法：/改画像 内容")
            return True
        STORE.set_facts(conv, rest.strip())
        await say_cmd(event, "✅ 画像已覆盖。")
        return True

    if head in ("清空画像",):
        mm = re.search(r"\d{5,}", rest or "")
        if mm:
            target = int(mm.group(0))
            STORE.clear_member_profiles(target)
            await say_cmd(event, f"✅ 已清空 QQ {target} 在所有群的画像（群日志保留）。")
            return True
        _, _, counter = STORE.profile(conv)
        STORE.set_facts(conv, "")
        await say_cmd(event, "✅ 画像已清空（对话上下文还在）。")
        return True

    if head == "评测":
        sub = rest.strip()
        if sub.startswith(("采集", "收集", "采样")):
            days = 7
            mm = re.search(r"\d+", sub)
            if mm:
                days = max(1, min(30, int(mm.group(0))))
            n = await asyncio.to_thread(collect_eval_cases, days, 40)
            await say_cmd(event, f"✅ 采样了 {n} 条人工抽检用例。发 /评测 开始。")
            return True
        if sub.startswith(("统计", "stats")):
            st = STORE.eval_stats()
            parts = [f"总计 {st['total']}，待评 {st['pending']}"]
            if st["labels"]:
                parts.append("已评：" + "、".join(f"{k} {v}" for k, v in st["labels"].items()))
            await say_cmd(event, "；".join(parts))
            return True
        if sub.startswith(("导出", "export")):
            path = DATA_DIR / "eval" / "labels.csv"
            n = STORE.export_eval(path)
            await say_cmd(event, f"✅ 已导出 {n} 条到 {path}")
            return True
        case = STORE.next_eval_case()
        if not case:
            await say_cmd(event, "没有待评用例。先发 /评测 采集 7。")
            return True
        STORE.set_setting(f"eval_current:{event.get('user_id')}", str(case["id"]))
        lines = [f"【人工抽检 #{case['id']}】群 {case['gid']}"]
        for m in case["context"][-6:]:
            who = "她" if m.get("role") == "assistant" else (m.get("nickname") or str(m.get("uid")))
            lines.append(f"{who}：{(m.get('text') or '')[:70]}")
        lines.append(f"触发：{case['trigger'][:100]}")
        lines.append(f"她回：{case['reply'][:220]}")
        lines.append("标签：像人 / 还行 / 冷淡 / 不懂装懂 / 不该回 / 跳过")
        lines.append("回复：/评 像人（可加备注）")
        await say_cmd(event, "\n".join(lines))
        return True

    if head == "评":
        key = f"eval_current:{event.get('user_id')}"
        cid = STORE.get_setting(key)
        if not cid:
            await say_cmd(event, "当前没有待评用例，先发 /评测。")
            return True
        parts = rest.split(maxsplit=1)
        raw = parts[0] if parts else ""
        note = parts[1] if len(parts) > 1 else ""
        mapping = {
            "像人": ("human_like", "yes"), "还行": ("ok", "yes"),
            "冷淡": ("cold", "yes"), "不懂装懂": ("hallucinated", "yes"),
            "不该回": ("should_not", "no"), "跳过": ("skip", "skip"),
        }
        label, should = mapping.get(raw, (raw[:30], "yes"))
        if label == "skip":
            STORE.skip_eval_case(int(cid))
            await say_cmd(event, "已跳过。")
        else:
            STORE.label_eval_case(int(cid), label, should, note)
            await say_cmd(event, f"✅ 已记录 #{cid}：{label}")
        STORE.del_setting(key)
        return True

    if head in ("语料", "语料库"):
        q = rest.strip()
        if q.startswith(("导入", "import")):
            parts = q.split(maxsplit=2)
            if len(parts) < 2:
                await say_cmd(event, "用法：/语料 导入 <文件名> [来源]，文件放 data/corpus/ 下")
                return True
            name = Path(parts[1]).name
            path = DATA_DIR / "corpus" / name
            if not path.is_file():
                await say_cmd(event, f"找不到 data/corpus/{name}。")
                return True
            source = parts[2] if len(parts) > 2 else name
            spawn_bg(asyncio.to_thread(run_corpus_import, path, source))
            await say_cmd(event, f"开始导入 {name}（后台），完成后看 logs/corpus.log。")
            return True
        for prefix in ("差", "降权", "忘", "删"):
            if q.startswith(prefix):
                raw = q[len(prefix):].strip()
                ids = re.findall(r"\d+", raw)
                if not ids:
                    await say_cmd(event, "用法：/语料 差 <编号> 或 /语料 忘 <编号>")
                    return True
                n = 0
                for i in ids:
                    cid = int(i)
                    if prefix in ("忘", "删"):
                        n += STORE.update_corpus(cid, 0, "retired")
                    else:
                        n += STORE.update_corpus(cid, -0.2, "candidate")
                await say_cmd(event, f"✅ 更新了 {n} 条语料。")
                return True
        for prefix in ("查", "搜索", "搜"):
            if q.startswith(prefix):
                q = q[len(prefix):].strip()
                break
        if q:
            rows = STORE.search_corpus(q, "style", 10)
            if not rows:
                await say_cmd(event, "没有命中语料。")
            else:
                lines = [f"语料命中：{q}"]
                for r in rows:
                    lines.append(f"#{r['id']} {r['text'][:100]}")
                    if r.get("meaning"):
                        lines.append(f"  意思：{r['meaning'][:60]}")
                    if r.get("trigger"):
                        lines.append(f"  用法：{r['trigger'][:50]}")
                await say_cmd(event, "\n".join(lines))
            return True
        counts = STORE.corpus_counts()
        if not counts["total"]:
            await say_cmd(event, "语料库还是空的。")
        else:
            await say_cmd(event, f"语料：{counts['total']} 条，其中可注入 {counts['active']} 条。")
        return True

    if head in ("梗", "群梗"):
        mm = re.search(r"\d{5,}", rest or "")
        gid = int(mm.group(0)) if mm else event.get("group_id")
        if not gid:
            await say_cmd(event, "用法：/梗 群号")
            return True
        items = STORE.list_style_items(int(gid), 20)
        if not items:
            await say_cmd(event, "这个群还没有学到的梗。")
            return True
        lines = [f"群 {gid} 的风格词/梗："]
        for it in items:
            v = "已核实" if it.get("verified") else "未核实"
            lines.append(f"#{it['id']} [{it['status']}/{v}] {it['text']} "
                         f"（{it['count']}次，{it['score']:.2f}）— {it['meaning'][:40]}")
        await say_cmd(event, "\n".join(lines))
        return True

    if head in ("忘梗", "删梗"):
        ids = re.findall(r"\d+", rest or "")
        if not ids:
            await say_cmd(event, "用法：/忘梗 12（先 /梗 看编号）")
            return True
        n = sum(STORE.retire_style_item(int(i)) for i in ids)
        await say_cmd(event, f"✅ 已停用 {n} 个梗/风格词。")
        return True

    if head in ("知识", "知识库", "查到什么"):
        rows = STORE.db.execute(
            "SELECT id,ts,topic,summary FROM knowledge ORDER BY id DESC LIMIT 8").fetchall()
        if not rows:
            await say_cmd(event, "记忆里还没有联网查到的内容。")
        else:
            out = ["最近查过并记住的："]
            for kid, ts, topic, summary in rows:
                out.append(f"#{kid} {time.strftime('%m-%d %H:%M', time.localtime(ts))} {topic[:20]}：{summary[:50]}")
            await say_cmd(event, "\n".join(out))
        return True

    if head in ("已发", "发过什么", "发送记录"):
        lines = []
        try:
            with open(LOG_DIR / "messages.log", encoding="utf-8") as f:
                for ln in f.readlines()[-400:]:
                    if " 发送 群" in ln:
                        lines.append(ln.strip())
        except FileNotFoundError:
            pass
        if not lines:
            await say_cmd(event, "最近没有在群里发过消息。")
        else:
            await say_cmd(event, "最近真的发出去的群消息：\n" + "\n".join(lines[-5:]))
        return True

    if head in ("记录", "调参记录", "历史"):
        rows = STORE.db.execute(
            "SELECT ts,k,old,new,reason FROM settings_history ORDER BY ts DESC LIMIT 10").fetchall()
        if not rows:
            await say_cmd(event, "还没有调参记录。")
        else:
            out = ["最近调整："]
            for ts, k, old, new, reason in rows:
                out.append(f"{time.strftime('%m-%d %H:%M', time.localtime(ts))} {k} {old}→{new}（{(reason or '')[:20]}）")
            await say_cmd(event, "\n".join(out))
        return True

    if head == "记忆":
        _, facts, _ = STORE.profile(conv)
        await say_cmd(event, facts.strip() or "（当前会话还没有记忆）")
        return True

    if head == "清空记忆":
        STORE.reset(conv)
        await say_cmd(event, "✅ 当前会话记忆已清空。")
        return True

    if head == "状态":
        active = is_active_time()
        await say_cmd(event,
                  f"在线状态：{'在线' if active else '上班中'}\n"
                  f"已连接：{'是' if BOT.ws else '否'}\n"
                  f"账号：{BOT.self_id}\n"
                  f"人格来源：{'主人覆盖版' if PERSONA_OVERRIDE.exists() else 'config/system_prompt.txt'}")
        return True

    return False



# ---------------------------------------------------------------- 主人纠错 → 自检调参
CORRECT_HINT = re.compile(
    r"(说错|搞错|理解错|错了|不对|不是我说的意思|太长了|太短了|太长|太短|话太多|说太多|"
    r"别老|太频繁|太快|太慢|不像|语气不对|太正式|太客气|太吵|刷屏|重复|答非所问|"
    r"插话|接话|安静|潜水|话多|太书面|太客气|太端着|"
    r"改一下|调一下|你调整|应该更|重新调)"
)

SELFCHECK_PROMPT = """你是这个 QQ 机器人的「自我复盘模块」。
主人指出了它刚才的表现有问题，你要根据当前参数和最近的对话，判断问题出在哪里，并给出参数修正。

可调参数（只能用它，取值范围）：
- MAX_MSG_CHARS 30~200   单条消息最大字数
- DELAY_MIN / DELAY_MAX 0.2~8   句间延迟秒数（DELAY_MAX 必须 >= DELAY_MIN）
- JITTER_MIN / JITTER_MAX 0~3   在句间延迟和回复延迟上叠加的随机抖动秒数
- READ_PER_CHAR 0~0.2 / READ_MAX 0~5   读对方消息的耗时（每字/上限）
- TYPE_PER_CHAR_MIN / TYPE_PER_CHAR_MAX 0~0.5   打字时每字耗时
- TYPE_DELAY_MAX 0.5~15   单条打字延迟上限
- BATCH_SIZE 1~10    连发多少条后停
- BATCH_PAUSE_MIN / BATCH_PAUSE_MAX 0~15  停顿秒数
- FOLLOWUP_WINDOW 0~600  发言后多少秒内把对方的话当作在跟自己说
- AMBIENT_PROB 0~1   没被 @ 时主动接话概率
- AMBIENT_COOLDOWN 0~600  主动接话冷却秒数
- MEMORY_EVERY 1~50  每多少条消息提炼一次长期记忆
- BUSY_FROM_HOUR / BUSY_AFTER_HOUR 0~24  工作日上班时段（期间只回“在上班”）

只输出 JSON，不要解释、不要代码块：
{"diagnosis":"一句话说清问题","changes":{"参数名":新值},"persona_patch":"可选，一句话补充人格规则，没有就空字符串","reply":"对主人说的话，口语，不超过 60 字"}
只改真正必要的参数，最多 3 个；如果问题跟参数无关（比如说话内容不对），changes 留空，用 persona_patch 修正。"""


# 参数与"投诉关键词"的相关性：不相关的参数一律不许改，避免误伤
PARAM_KEYWORDS = {
    "MAX_MSG_CHARS": r"长|字数|啰嗦|简短|短一?点|一句话",
    "DELAY_MIN": r"快|慢|延迟|间隔|刷屏|抢答",
    "DELAY_MAX": r"快|慢|延迟|间隔|刷屏|抢答",
    "JITTER_MIN": r"抖动|机械|节奏|间隔|延迟|秒回",
    "JITTER_MAX": r"抖动|机械|节奏|间隔|延迟|秒回",
    "READ_PER_CHAR": r"反应|读|秒回|打字|速度|慢|快",
    "READ_MAX": r"反应|读|秒回|打字|速度|慢|快",
    "TYPE_PER_CHAR_MIN": r"打字|输入|速度|慢|快|间隔",
    "TYPE_PER_CHAR_MAX": r"打字|输入|速度|慢|快|间隔",
    "TYPE_DELAY_MAX": r"打字|输入|速度|慢|快|间隔",
    "BATCH_SIZE": r"刷屏|连发|停顿|节奏",
    "BATCH_PAUSE_MIN": r"刷屏|连发|停顿|节奏|快",
    "BATCH_PAUSE_MAX": r"刷屏|连发|停顿|节奏|快",
    "FOLLOWUP_WINDOW": r"追问|跟着|回我|几分钟|窗口",
    "AMBIENT_PROB": r"插话|接话|吵|话多|潜水|活跃|安静|别老",
    "AMBIENT_COOLDOWN": r"插话|接话|吵|话多|频繁",
    "MEMORY_EVERY": r"记忆|记住|画像|记性",
    "BUSY_FROM_HOUR": r"上班|时间|白天|晚上|在线",
    "BUSY_AFTER_HOUR": r"上班|时间|白天|晚上|在线",
    "REPLY_PROB": r"每条|都回|太勤|别老回|理我|太积极|回得太|不回",
    "REPLY_DELAY_MIN": r"太慢|秒回|立刻|磨蹭|延迟",
    "REPLY_DELAY_MAX": r"太慢|秒回|立刻|磨蹭|延迟",
    "MUTE_PROB": r"不想说话|冷战|情绪|忽冷忽热",
    "MUTE_MINUTES": r"不想说话|冷战|情绪|忽冷忽热",
}


def relevant(k: str, complaint: str) -> bool:
    pat = PARAM_KEYWORDS.get(k)
    return True if not pat else re.search(pat, complaint) is not None


def clamp_param(k: str, v):
    typ = PARAM_TYPES[k]
    v = typ(v)
    ranges = {
        "MAX_MSG_CHARS": (30, 200), "DELAY_MIN": (0.2, 8), "DELAY_MAX": (0.2, 8),
        "JITTER_MIN": (0, 3), "JITTER_MAX": (0, 3),
        "READ_PER_CHAR": (0, 0.2), "READ_MAX": (0, 5),
        "TYPE_PER_CHAR_MIN": (0, 0.5), "TYPE_PER_CHAR_MAX": (0, 0.5),
        "TYPE_DELAY_MAX": (0.5, 15),
        "BATCH_SIZE": (1, 10), "BATCH_PAUSE_MIN": (0, 15), "BATCH_PAUSE_MAX": (0, 15),
        "FOLLOWUP_WINDOW": (0, 600), "AMBIENT_PROB": (0, 1), "AMBIENT_COOLDOWN": (0, 600),
        "MEMORY_EVERY": (1, 50),
        "BUSY_FROM_HOUR": (0, 24), "BUSY_AFTER_HOUR": (0, 24),
        "REPLY_PROB": (0, 1), "REPLY_DELAY_MIN": (0, 10), "REPLY_DELAY_MAX": (0, 10),
        "MUTE_PROB": (0, 1), "MUTE_MINUTES": (1, 180),
    }
    lo, hi = ranges[k]
    return max(lo, min(hi, v))


def parse_json_loose(text: str) -> dict | None:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def self_correct(event: dict, conv: str, complaint: str) -> None:
    """主人说“你做错了” → 自检 → 调参 / 补人格规则 → 汇报改了什么"""
    recent = STORE.recent(conv, 6)
    dialog = "\n".join(f"{'主人' if m['role'] == 'user' else '你'}：{m['content']}" for m in recent)
    params_now = "\n".join(f"{k} = {pget(k)}" for k in PARAM_TYPES)
    persona_now = get_persona()[:600]

    prompt = (f"【当前参数】\n{params_now}\n\n【当前人格（节选）】\n{persona_now}\n\n"
              f"【最近对话】\n{dialog}\n\n【主人这次的反馈】\n{complaint}")
    try:
        raw = await asyncio.to_thread(chat, [
            {"role": "system", "content": SELFCHECK_PROMPT},
            {"role": "user", "content": prompt}], 0.3, 400)
    except Exception as e:
        log_line(f"[selfcheck] 失败 {type(e).__name__}: {e}")
        await say_cmd(event, "我这边自检没跑通，你先说具体哪儿不对，我照着改。")
        return

    data = parse_json_loose(raw) or {}
    diagnosis = str(data.get("diagnosis", "")).strip()
    changes = data.get("changes") or {}
    patch = str(data.get("persona_patch", "")).strip()

    applied = []
    if isinstance(changes, dict):
        for i, (k, v) in enumerate(changes.items()):
            if i >= 3:
                break
            k = str(k).upper()
            if k not in PARAM_TYPES or not relevant(k, complaint):
                continue
            try:
                new = clamp_param(k, v)
            except (TypeError, ValueError):
                continue
            old = pget(k)
            if new == old:
                continue
            STORE.set_setting("param:" + k, str(new))
            STORE.db.execute(
                "INSERT INTO settings_history(ts,k,old,new,reason) VALUES(?,?,?,?,?)",
                (int(time.time()), k, str(old), str(new), complaint[:120]))
            STORE.db.commit()
            pset(k, new)
            applied.append(f"{k}: {old} → {new}")
            log_line(f"[selfcheck] {k} {old} -> {new}（原因：{diagnosis or complaint[:40]}）")

    # 模型有时把行为问题写成人格补丁，这里做规则兜底
    changed_keys = {a.split(":")[0] for a in applied}
    rules = [
        (r"插话|接话|太吵|话多|安静|别老", "AMBIENT_PROB", 0.0),
        (r"太长|啰嗦|字数", "MAX_MSG_CHARS", min(80, int(pget("MAX_MSG_CHARS")))),
        (r"太快|刷屏|抢答", "DELAY_MIN", 1.5),
        (r"太快|刷屏|抢答", "DELAY_MAX", 3.0),
        (r"太频繁|刷屏", "BATCH_PAUSE_MIN", 5.0),
    ]
    for pat, k, target in rules:
        if re.search(pat, complaint) and k not in changed_keys:
            old = pget(k)
            new = clamp_param(k, target)
            if new == old:
                continue
            STORE.set_setting("param:" + k, str(new))
            STORE.db.execute(
                "INSERT INTO settings_history(ts,k,old,new,reason) VALUES(?,?,?,?,?)",
                (int(time.time()), k, str(old), str(new), "规则兜底: " + complaint[:80]))
            STORE.db.commit()
            pset(k, new)
            applied.append(f"{k}: {old} → {new}")
            log_line(f"[selfcheck] fallback {k} {old} -> {new}")

    if patch and len(patch) <= 200:
        append_persona("- " + patch.lstrip("- "))
        applied.append("人格: 追加「" + patch[:40] + "」")
        log_line("[selfcheck] 人格追加: " + patch[:80])

    # DELAY 边界校正
    if pget("DELAY_MAX") < pget("DELAY_MIN"):
        pset("DELAY_MAX", pget("DELAY_MIN"))
        STORE.set_setting("param:DELAY_MAX", str(pget("DELAY_MAX")))
        applied.append(f"DELAY_MAX 校正为 {pget('DELAY_MAX')}")

    reply = str(data.get("reply", "")).strip()
    head = f"我看了一下，问题在：{diagnosis}\n" if diagnosis else "我看了下。\n"
    tail = "\n改好了：" + "；".join(applied) if applied else "\n参数没动，问题不在参数上。"
    await say_cmd(event, head + (reply + tail if reply else tail.lstrip("\n")))



# 自然语言："去群里说句晚安" / "在群里冒个泡" / "群里说点什么"
NAT_GROUP_ACTION = re.compile(
    r"(?:去|到|在|帮我)?\s*(?:群里|群里面|那个群|群里头|群)\s*(?:帮我)?\s*"
    r"(说|发|喊|讲|聊|冒个泡|吱一声|吱一声吧)\s*(?:一句|两句|句话|句|声)?\s*[:：,，]?\s*(.*)"
)
EMPTY_TOPIC = {"", "点什么", "点啥", "点东西", "些什么", "点吧", "点", "点话", "个泡", "泡"}


# ---------------------------------------------------------------- 主人指挥群发言
async def resolve_group(token: str = "") -> tuple[int | None, str]:
    """按群号 / 群名片段找群；只有一个群时可省略"""
    try:
        res = await BOT.call("get_group_list")
        groups = res.get("data") or []
    except Exception as e:
        log_line(f"[group] 取群列表失败 {type(e).__name__}: {e}")
        return None, ""
    if not groups:
        return None, ""
    token = (token or "").strip()
    if not token:
        if len(groups) == 1:
            return int(groups[0]["group_id"]), groups[0].get("group_name", "")
        return None, ""
    for g in groups:
        gid, gname = str(g["group_id"]), g.get("group_name", "")
        if token == gid or (token and token in gname):
            return int(g["group_id"]), gname
    if token.isdigit():
        return int(token), token
    return None, ""


async def cmd_group_list(event: dict) -> None:
    res = await BOT.call("get_group_list")
    groups = res.get("data") or []
    if not groups:
        await say_cmd(event, "她现在一个群都没有，先把她拉进群吧。")
        return
    lines = ["她所在的群："]
    for g in groups:
        lines.append(f"{g['group_id']}  {g.get('group_name','')}（{g.get('member_count','?')}人）")
    lines.append("\n用法：/说 群号 内容   ·   /冒泡 群号 [话题]")
    await say_cmd(event, "\n".join(lines))


async def cmd_say(event: dict, rest: str) -> None:
    """主人指定内容，让她原话发到群里（仍按人格卡拆句 + 延迟）"""
    token, _, text = rest.partition(" ")
    text = text.strip()
    if not text:                      # 没写群号：整段都当内容
        token, text = "", rest.strip()
    if text in ("冒个泡", "冒泡", "说一句"):   # 「说 冒个泡」= 让她自己冒泡
        await cmd_bubble(event, "")
        return
    gid, gname = await resolve_group(token)
    if not gid:
        await say_cmd(event, "找不到这个群。发 /群 看看群号和群名。" if token else "她不止一个群，请给出群号：/说 群号 内容")
        return
    fake = {"message_type": "group", "group_id": gid, "user_id": event.get("user_id"),
            "self_id": event.get("self_id"), "sender": {"nickname": "主人"}}
    try:
        await say(fake, f"group:{gid}:master", text)
    except Exception as e:
        log_line(f"[say] 群发失败 {type(e).__name__}: {e}")
        await say_cmd(event, f"发失败了：{type(e).__name__}: {e}。我没有发出去，别当成发了。")
        return
    await say_cmd(event, f"✅ 已发到「{gname or gid}」，共 {len(split_sentences(text))} 条。")


async def cmd_bubble(event: dict, rest: str) -> None:
    """让她自己在群里冒个泡：结合最近群聊内容说一句"""
    token, _, topic = rest.partition(" ")
    topic = topic.strip()
    if not topic and token and not token.isdigit() and token not in ("冒泡",):
        topic, token = token, ""
    gid, gname = await resolve_group(token)
    if not gid:
        await say_cmd(event, "找不到这个群，发 /群 看群号。")
        return
    context = ""
    try:
        hist = await BOT.call("get_group_msg_history", group_id=gid, count=20)
        msgs = (hist.get("data") or {}).get("messages", []) if isinstance(hist.get("data"), dict) else []
        lines = []
        for m in msgs[-15:]:
            who = (m.get("sender") or {}).get("card") or (m.get("sender") or {}).get("nickname") or "群友"
            txt = plain_text(m.get("message"))
            if txt and not has_profanity(txt):
                lines.append(f"{who}：{txt[:50]}")
        context = "\n".join(lines)
    except Exception as e:
        log_line(f"[bubble] 取历史失败 {type(e).__name__}: {e}")

    prompt = (
        f"你正在群里潜水。主人让你冒个泡。{'话题提示：' + topic if topic else ''}\n"
        f"【群里最近在聊】\n{context or '（没拿到记录，随便说一句日常）'}\n\n"
        "用你平时说话的方式说一句，接得上就接，接不上就随口聊一句自己的事。"
        "1 到 2 句，总共不超过 60 字，不要解释、不要客套、不要 emoji 堆叠。"
    )
    try:
        msgs = [
            {"role": "system", "content": get_persona() + "\n" + RULES + time_hint()},
            {"role": "system", "content": clock_line()},
            {"role": "user", "content": prompt},
        ]
        text = await asyncio.to_thread(chat, msgs, 0.9, 200)
        text = await enforce_time(text, msgs, prompt)
    except Exception as e:
        log_line(f"[bubble] 生成失败 {e}")
        await say_cmd(event, "她想不出说什么，重试一次吧。")
        return
    fake = {"message_type": "group", "group_id": gid, "user_id": event.get("user_id"),
            "self_id": event.get("self_id")}
    try:
        await say(fake, f"group:{gid}:master", text)
    except Exception as e:
        log_line(f"[bubble] 发送失败 {type(e).__name__}: {e}")
        await say_cmd(event, f"发失败了：{e}。这条没发出去。")
        return
    await say_cmd(event, f"✅ 已在「{gname or gid}」冒泡：{text[:40]}")



# /news "内容" —— 主人向群里广播一句话
QUOTES = "\"'“”‘’「」"
GROUP_ID_PREFIX = re.compile(r"^(\d{5,12})\s+(.*)$", re.S)


def unquote(text: str) -> str:
    t = text.strip()
    while len(t) >= 2 and t[0] in QUOTES and t[-1] in QUOTES:
        t = t[1:-1].strip()
    return t


async def cmd_news(event: dict, rest: str) -> None:
    """/news "内容"  或  /news 群号 "内容"；不写群号则发到她所在的全部群"""
    rest = rest.strip()
    token = ""
    m = GROUP_ID_PREFIX.match(rest)
    if m:
        token, rest = m.group(1), m.group(2)
    content = unquote(rest)
    if not content:
        await say_cmd(event, '用法：/news "要发的话"（也可以 /news 群号 "要发的话"）')
        return

    try:
        res = await BOT.call("get_group_list")
        groups = res.get("data") or []
    except Exception as e:
        await say_cmd(event, f"拿不到群列表：{e}")
        return

    if token:
        groups = [g for g in groups if str(g["group_id"]) == token]
        if not groups:
            groups = [{"group_id": int(token), "group_name": token}]

    if not groups:
        await say_cmd(event, "她还没在任何群里，先把号拉进群。")
        return

    ok, fail = [], []
    for g in groups:
        gid = int(g["group_id"])
        fake = {"message_type": "group", "group_id": gid, "user_id": event.get("user_id"),
                "self_id": event.get("self_id")}
        try:
            if len(content) <= MAX_CHARS:
                # 短内容整条发，保持原样（"example" 就发 example 这一条）
                await send_one(fake, content)
                log_msg("发送", fake, content)
                LAST_SPOKE[f"group:{gid}:master"] = time.time()
            else:
                await say(fake, f"group:{gid}:master", content)
            ok.append(g.get("group_name") or str(gid))
        except Exception as e:
            log_line(f"[news] 发送到 {gid} 失败 {type(e).__name__}: {e}")
            fail.append(f"{g.get('group_name') or gid}({type(e).__name__})")

    msg = ""
    if ok:
        msg += "✅ 已发到：" + "、".join(ok)
    if fail:
        msg += ("\n" if msg else "") + "❌ 失败的：" + "、".join(fail)
    await say_cmd(event, msg + f"\n内容：{content[:80]}")



# ---------------------------------------------------------------- 群消息归属判定
JUDGE_PROMPT = """你在做群聊归属判断：判断【最后一条消息】是不是在跟机器人“她”说话，或者是在接她刚参与的话题。

算：@了她、引用/回复了她的消息、直接接她刚说的话、换了一个群友继续接她刚说的话题、点她的名字或明显在问她。
不算：@的是别人、在跟另一个群友进行独立对话、群公告、自言自语、跟她上一句没有关系。

注意：说话的人换了（A 问过她，B 接着这个话题说）也算，只要 B 是在接这个话题，不是在跟别人另开话题。
不要因为她刚说过话，就默认每一条都是接她。B 在开新话题、跟 A 聊与她不相关的事、或者只是在回应 A，都算“否”。
只输出一个字：是 或 否。不要解释。"""


async def addressed_to_me(event: dict, conv: str, text: str) -> bool:
    """语义判定：这条群消息是不是冲她说的（失败时按"否"处理，宁可不回）"""
    try:
        gid = event.get("group_id")
        recent = []
        if gid:
            recent = STORE.group_recent(int(gid), 8, 1200)
            # 当前这条已经写进群日志，避免在“最近对话”里重复
            if (recent and recent[-1]["role"] == "user"
                    and recent[-1]["content"] == text
                    and str(recent[-1]["uid"]) == str(event.get("user_id"))):
                recent = recent[:-1]
        if recent:
            dialog = "\n".join(
                f"{'她' if m['role'] == 'assistant' else (m['nickname'] or '群友')}：{m['content'][:80]}"
                for m in recent)
        else:
            old = STORE.recent(conv, 4)
            dialog = "\n".join(
                f"{'她' if m['role'] == 'assistant' else '群友'}：{m['content'][:80]}" for m in old)
        sender = (event.get("sender") or {}).get("card") or (event.get("sender") or {}).get("nickname") or "群友"
        payload = f"【最近的群聊】\n{dialog or '（她还没在这个群说过话）'}\n\n【最后一条消息】\n{sender}：{text[:150]}"
        ans = await asyncio.to_thread(chat, [
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": payload}], 0.0, 8)
        verdict = "是" in ans[:6]
        log_msg("判断", event, ("算在跟她说话" if verdict else "不是跟她说话") + f"（{ans.strip()[:6]}）")
        return verdict
    except Exception as e:
        log_line(f"[judge] 失败 {type(e).__name__}: {e}")
        return False



def group_reply_allowed(gid: int) -> tuple[bool, str]:
    """群聊节奏闸：她刚说完/中间消息太少/10 分钟说太多时，不接非直接消息。"""
    now_ts = time.time()
    if gid not in BOT_LAST_GROUP_ID:
        row = STORE.db.execute(
            "SELECT id, ts FROM group_log WHERE gid=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 1", (gid,)
        ).fetchone()
        if row:
            BOT_LAST_GROUP_ID[gid] = int(row[0])
            LAST_SPOKE_GROUP.setdefault(gid, float(row[1] or 0))
    if now_ts - LAST_SPOKE_GROUP.get(gid, 0) < GROUP_REPLY_COOLDOWN:
        return False, "刚说过话，冷却中"
    times = GROUP_REPLIES[gid]
    while times and now_ts - times[0] > 600:
        times.popleft()
    if len(times) >= GROUP_MAX_REPLIES_10MIN:
        return False, "10 分钟内说得太多"
    last = BOT_LAST_GROUP_ID.get(gid, 0)
    if last:
        n = STORE.db.execute(
            "SELECT COUNT(*) FROM group_log WHERE gid=? AND role='user' AND id>?",
            (gid, last),
        ).fetchone()[0]
        if n < GROUP_MIN_OTHERS_MSGS:
            return False, f"她说完后只有 {n} 条别人的消息"
    return True, ""


AMBIENT_PROMPT = """你在判断机器人是否应该在不被 @ 的情况下主动加入群聊。
默认 false。只有同时满足以下条件才 should_speak=true：
1) 最近群聊出现空档（至少 35 秒没人说话），或者有人向群体提问（“你们/大家/谁/怎么看”）；
2) 话题与她刚参与的内容、兴趣或她说过的话强相关；
3) 她能补充新信息、明确态度或自然接梗，而不是只说“确实/哈哈/我也觉得”；
4) 不会打断两个群友正在进行的对话。
如果 A/B 在互相接话，即使话题与她有关，也默认 false，除非出现空档或群体提问。
只输出 JSON，不要解释：
{"should_speak":true/false,"confidence":0.0,"reason":"一句话","angle":"准备补充什么"}
"""

GROUP_QUESTION_RE = re.compile(r"(你们|大家|有人|谁|怎么看|觉得呢|有没有人|来个人|说说看)")


def is_group_question(text: str) -> bool:
    return bool(GROUP_QUESTION_RE.search(text or ""))


async def ambient_consider(event: dict, conv: str, text: str,
                           exclude_id: int | None = None) -> bool:
    if AMBIENT_MODE != "conservative":
        return False
    gid = event.get("group_id")
    if not gid:
        return False
    try:
        rows = STORE.group_recent(int(gid), 10, 1400)
        if exclude_id:
            rows = [m for m in rows if m["id"] != exclude_id]
        if len(rows) < 2:
            return False
        now_ts = time.time()
        lull = now_ts - max(m["ts"] for m in rows)
        group_q = is_group_question(text)
        if lull < LULL_SEC and not group_q:
            return False
        if now_ts - LAST_AMBIENT.get(gid, 0) < AMBIENT_COOLDOWN:
            return False
        times = AMBIENT_TIMES[gid]
        while times and now_ts - times[0] > 600:
            times.popleft()
        if len(times) >= AMBIENT_MAX_10MIN:
            return False
        dialog = "\n".join(
            f"{'她' if m['role'] == 'assistant' else (m['nickname'] or '群友')}：{m['content'][:120]}"
            for m in rows)
        sender = (event.get("sender") or {}).get("card") or (event.get("sender") or {}).get("nickname") or "群友"
        payload = (f"【最近群聊】\n{dialog}\n\n【当前消息】\n{sender}：{text[:150]}\n\n"
                   f"【距最后一条消息】{int(lull)} 秒；群体提问={group_q}")
        ans = await asyncio.to_thread(chat, [
            {"role": "system", "content": AMBIENT_PROMPT},
            {"role": "user", "content": payload}], 0.2, 160)
        m = re.search(r"\{.*\}", ans or "", re.S)
        data = json.loads(m.group(0)) if m else {}
        ok = bool(data.get("should_speak", False)) and float(data.get("confidence", 0) or 0) >= AMBIENT_THRESHOLD
        reason = str(data.get("reason", ""))[:80]
        log_line(f"[ambient] gid={gid} lull={int(lull)} q={group_q} "
                 f"should={data.get('should_speak')} conf={data.get('confidence')} {reason}")
        if not ok:
            return False
        LAST_AMBIENT[gid] = now_ts
        times.append(now_ts)
        log_msg("插话", event, f"confidence={data.get('confidence')} {reason}")
        return True
    except Exception as e:
        log_line(f"[ambient] 失败 {type(e).__name__}: {e}")
        return False


# 时效性问题：不依赖模型自觉，命中就直接查一次
NEED_SEARCH = re.compile(
    r"(赛程|比分|谁赢|赢了|夺冠|排名|季后赛|S赛|世界赛|转会|新番|更新到|更新了|"
    r"最新|新闻|热点|多少钱|价格|天气|版本|发布会|什么时候开|直播)")

BANNED_STYLE = re.compile(
    r"(一方面|另一方面|各有优|各有千秋|综合来看|总体而言|总体上说|因人而异|"
    r"取决于|见仁见智|没有绝对|视情况而定|建议结合)")

SEARCH_TOKEN = re.compile(r"\[\[SEARCH[:：]\s*(.+?)\]\]", re.S)


async def enforce_style(answer: str, messages: list[dict]) -> str:
    """端水检测：出现"一方面/另一方面/综合来看"这类句式就重写一次"""
    if not BANNED_STYLE.search(answer):
        return answer
    log_line(f"[style] 检出端水句式，重写：{answer[:40]}")
    try:
        fixed = await asyncio.to_thread(chat, messages + [
            {"role": "assistant", "content": answer},
            {"role": "system", "content": "刚才那句太端水了（出现了一方面/另一方面/各有优劣这类两头讨好的说法）。"
                                          "重写：只站一个立场，直接说你的看法，不要平衡分析，不要总结陈词，不超过 3 句。"},
        ], 1.0, 260)
        return strip_markdown(fixed) or answer
    except Exception as e:
        log_line(f"[style] 重写失败 {e}")
        return answer


async def enforce_time(answer: str, messages: list[dict], user_text: str = "") -> str:
    """时间一致性兜底：只处理“现在几点/现在什么时段/今天周几”这类客观矛盾。"""
    issue = time_claim_issue(answer)
    if not issue:
        return answer
    log_line(f"[timeguard] {issue}｜{answer[:60]}")
    try:
        fixed = await asyncio.to_thread(chat, messages + [
            {"role": "assistant", "content": answer},
            {"role": "system", "content": f"时间校验失败：{issue}。当前真实时间是 {now():%Y-%m-%d %H:%M}"
                                          f"（{period_of(now())}，星期{'一二三四五六日'[now().weekday()]}）。"
                                          "只改与时间/日期相关的表达，保持原意和口语，不要解释，不要新增内容，"
                                          "输出改好的一句。"},
        ], 0.4, 260)
        fixed = strip_markdown(fixed) or ""
        if fixed and not time_claim_issue(fixed):
            log_line(f"[timeguard] 已重写：{fixed[:60]}")
            return fixed
        return answer
    except Exception as e:
        log_line(f"[timeguard] 重写失败 {e}")
        return answer


async def ensure_question_detail(answer: str, messages: list[dict], gid: int | None,
                                active: bool, question: bool) -> str:
    """对方在提问时，回答太短就补一句原因/例子/态度；闲聊不强制。"""
    if not question:
        return answer
    if gid and not active:
        return answer
    if len(answer) >= 30 or (len(split_sentences(strip_markdown(answer))) >= 2 and len(answer) >= 20):
        return answer
    log_line(f"[answer] 提问回答过短，补一句：{answer[:40]}")
    try:
        fixed = await asyncio.to_thread(chat, messages + [
            {"role": "assistant", "content": answer},
            {"role": "system", "content": "对方在提问，你刚才的回答太短，显得敷衍。保持原意，"
                                          "补一句原因、例子或你的态度，输出 2~3 句短句；"
                                          "不要新增事实、不要重复、不要客套。"},
        ], 0.9, 260)
        fixed = strip_markdown(fixed or "")
        return fixed or answer
    except Exception as e:
        log_line(f"[answer] 补句失败 {e}")
        return answer


PERIOD_ORDER = ["凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "深夜"]


def time_claim_issue(reply: str) -> str | None:
    """识别回复里与系统时钟直接冲突的说法；返回原因，没有则 None。"""
    dt = now()
    cur = period_of(dt)

    m = re.search(r"现在(?:已经)?(?:是|到)?\s*(凌晨|早上|上午|中午|下午|傍晚|晚上|深夜)", reply)
    if m:
        claim = m.group(1)
        if abs(PERIOD_ORDER.index(claim) - PERIOD_ORDER.index(cur)) >= 2:
            return f"现在不是{claim}，是{cur}"

    m = re.search(r"现在(?:已经)?(?:是)?\s*(\d{1,2})\s*[点:：]", reply)
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            variants = [h] + ([h + 12] if h < 12 else [])
            if min(abs(x - dt.hour) for x in variants) > 3:
                return f"现在不是{h}点"

    m = re.search(r"今天(?:是)?(?:星期|周)([一二三四五六日天])", reply)
    if m:
        claim = m.group(1).replace("天", "日")
        real = "一二三四五六日"[dt.weekday()]
        if claim != real:
            return f"今天不是星期{claim}，是星期{real}"

    m = re.search(r"今天(?:是)?\s*(\d{1,2})月(\d{1,2})[日号]", reply)
    if m and (int(m.group(1)), int(m.group(2))) != (dt.month, dt.day):
        return f"今天不是{m.group(1)}月{m.group(2)}日"

    if dt.hour < 17:
        m = re.search(r"今晚.{0,8}(正在|这会儿|此刻|现在)", reply)
        if m:
            return "现在还没到今晚，不能说“今晚正在”"

    m = re.search(r"(明天|后天).{0,8}(正在|这会儿|此刻)", reply)
    if m:
        return f"{m.group(1)}还没到，不能说正在"

    return None


async def maybe_search(answer: str, messages: list[dict], active: bool) -> str:
    """模型想查资料时会输出 [[SEARCH: 关键词]]，这里真的去搜，再让它基于结果回答"""
    for round_no in range(2):
        m = SEARCH_TOKEN.search(answer)
        if not m:
            return answer
        query = m.group(1).strip()[:120]
        log_line(f"[search] 模型要求搜索：{query}")
        results = await asyncio.to_thread(web_search, query)
        messages = messages + [{"role": "assistant", "content": answer}]
        if not results:
            messages.append({"role": "system",
                             "content": "（联网没搜到有用结果。别编造，简短说没查到就行。）"})
        else:
            STORE.add_knowledge(query, "；".join(r["title"] for r in results[:3])[:150])
            ctx = "\n".join(f"- {r['title']}：{r['snippet'][:120]}" for r in results[:4])
            messages.append({"role": "system",
                             "content": f"【联网搜索结果（{query}，{now():%Y-%m-%d}）】\n{ctx}\n\n"
                                        "从这些结果里挑一个你认同的说法，当成自己的观点说出来；"
                                        "不要罗列多种看法、不要做平衡分析。口语、简短（不超 60 字），"
                                        "别说“根据搜索”、别列网址；结果完全无关就说没查到。"})
        answer = await asyncio.to_thread(chat, messages, 0.7, 160 if not active else 300)
    return SEARCH_TOKEN.sub("", answer).strip()



async def cmd_group_answer(event: dict, rest: str) -> None:
    """/群答 [群号] 问题 —— 让机器人回答这个问题，并把回答发到群里"""
    rest = rest.strip()
    token, _, question = rest.partition(" ")
    question = question.strip()
    if not question:                       # 没写群号：整段都当问题
        token, question = "", rest
    if not question:
        await say_cmd(event, '用法：/群答 "问题"（多群时：/群答 群号 "问题"）')
        return
    gid, gname = await resolve_group(token)
    if not gid:
        await say_cmd(event, "找不到这个群，发 /群 看群号。")
        return

    conv = f"group:{gid}:master"
    STORE.add(conv, "user", question)
    messages = build_messages(conv, question, gid=gid, master=False, force_active=True,
                              uid=event.get("user_id"), nickname="主人")

    try:
        if NEED_SEARCH.search(question) or re.search(r"(比赛|赛程|比分|结果|新闻|最新|新番|版本更新)", question):
            results = await asyncio.to_thread(web_search, question, 5)
            if results:
                STORE.add_knowledge(question, "；".join(r["title"] for r in results[:3])[:150])
                ctx = "\n".join(f"- {r['title']}：{r['snippet'][:120]}" for r in results[:4])
                messages = messages + [{"role": "system",
                                        "content": f"【联网搜索结果（今天是 {now():%Y-%m-%d}）】\n{ctx}\n\n"
                                                   "直接用结果回答，口语简短，别提来源。"}]
        answer = await asyncio.to_thread(chat, messages, 0.85, 300)
        answer = await maybe_search(answer, messages, True)
        answer = await enforce_time(answer, messages, question)
    except Exception as e:
        log_line(f"[group-answer] 生成失败 {type(e).__name__}: {e}")
        await say_cmd(event, f"生成回答失败：{e}")
        return

    answer = strip_markdown(answer).strip()
    if not answer:
        await say_cmd(event, "她没想出怎么回，换个问法试试。")
        return

    mid = STORE.add(conv, "assistant", answer)
    STORE.remember_self(conv, answer, mid)
    fake = {"message_type": "group", "group_id": gid, "user_id": event.get("user_id"),
            "self_id": event.get("self_id")}
    try:
        await say(fake, conv, answer)
    except Exception as e:
        log_line(f"[group-answer] 发送失败 {type(e).__name__}: {e}")
        await say_cmd(event, f"发到群里失败了：{e}")
        return
    await say_cmd(event, f"✅ 已把回答发到「{gname or gid}」：\n{answer[:200]}")


# ---------------------------------------------------------------- 主逻辑
async def handle_message(event: dict) -> None:
    mtype = event.get("message_type")
    segments = event.get("message", [])
    text = plain_text(segments)
    self_id = event.get("self_id") or BOT.self_id
    sender = event.get("sender", {}) or {}
    nickname = sender.get("card") or sender.get("nickname") or str(event.get("user_id"))
    if not text or not self_id:
        return

    gid = event.get("group_id") if mtype == "group" else None
    conv = f"group:{gid}:{event.get('user_id')}" if gid else f"private:{event.get('user_id')}"

    # 群聊语气采样（只收短句、无脏话，且不是冲机器人说的）
    at_me = mentioned(segments, self_id, text)
    master_conv = is_master(event) and mtype == "private"
    master_in_group = is_master(event) and mtype == "group"
    if gid:
        # 群共享消息流：不管回不回都记；顺便维护群员身份与画像计数
        _uid = int(event.get("user_id") or 0)
        _gmid = STORE.log_group(gid, _uid, nickname, "user", text)
        _cnt = STORE.touch_member(gid, _uid, nickname)
        if _cnt and (_cnt % MEMBER_PROFILE_EVERY == 0 or at_me):
            spawn_bg(asyncio.to_thread(update_member_profile, gid, _uid, nickname))
    if gid and not at_me and len(text) <= 60 and not has_profanity(text):
        STYLE[gid].append(text)
    if gid and has_profanity(text):
        return  # 群里带脏话的消息不接

    log_msg("接收", event, text)
    active = is_active_time()
    quoted_ctx = ""

    # ---- 群聊要不要开口 ----
    if gid and not active and not master_in_group:
        # 工作时间：别人的消息一律不接（连判定都不做）
        log_msg("略过", event, "工作时间只回主人")
        return
    if gid and not at_me:
        # ① 节奏闸：她刚说完/中间消息太少/10 分钟说太多 → 非直接消息先不接
        quoted = is_reply_to_me(event, conv)
        if quoted:
            quoted_ctx = await get_quoted_text(event, conv)
        elif has_reply_segment(event):
            # bot 重启后本地 id 缓存会丢，仍尝试 get_msg 确认是不是引用她
            quoted_ctx = await get_quoted_text(event, conv)
            quoted = bool(quoted_ctx)
        gate_ok, gate_why = group_reply_allowed(gid)
        if not quoted and not gate_ok:
            log_msg("略过", event, f"群聊节奏：{gate_why}")
            return
        # ② 5 分钟窗口：逐条判断是不是冲她说的/在接她的话题
        in_window = (time.time() - LAST_SPOKE_GROUP.get(gid, 0)) < FOLLOWUP_WINDOW
        if quoted:
            should = True
        elif in_window:
            should = await addressed_to_me(event, conv, text)
        else:
            should = False
        # ② 保守主动插话：只在空档/群体提问/强相关时，由 LLM 再判一次
        if not should and active and not master_in_group:
            if AMBIENT_MODE == "conservative":
                should = await ambient_consider(event, conv, text, _gmid)
            elif AMBIENT_PROB > 0:
                if (random.random() < AMBIENT_PROB
                        and time.time() - LAST_AMBIENT.get(gid, 0) > AMBIENT_COOLDOWN):
                    should = True
                    LAST_AMBIENT[gid] = time.time()
        if not should:
            log_msg("略过", event, text)
            return
        if gid and not at_me and not quoted:
            GROUP_REPLIES[gid].append(time.time())

    # 随机不回：只作用于私聊（群里已经做过归属判定，判"是"就回）
    mood_key = str(gid) if gid else conv
    if not master_conv and not at_me and not gid:
        maybe_mute(mood_key)
        if muted(mood_key) or random.random() > REPLY_PROB:
            log_msg("略过", event, text)
            return

    if gid and at_me:
        text = re.sub(rf"@{self_id}", "", text)
        for w in WAKE_WORDS:
            text = text.replace(w, "")
        text = text.strip() or "嗯？"

    cmd = text.strip()

    # ---- 主人指令（优先于一切，且上班时段也生效；斜杠可省略）----
    if master_conv:
        head = cmd.lstrip("/").split(" ")[0]
        if head in ("人格", "追加人格", "补充人格", "重置人格", "参数", "记忆",
                    "清空记忆", "状态", "帮助", "指令", "help",
                    "群", "群列表", "说", "发言", "群发", "冒泡", "群里冒泡",
                    "记录", "调参记录", "历史", "已发", "发过什么", "发送记录",
                    "news", "新闻", "播报", "搜", "搜索", "知识", "知识库",
                    "群答", "答到群", "问群",
                    "删知识", "清空知识", "加知识", "画像", "改画像", "清空画像",
                    "梗", "群梗", "忘梗", "删梗", "语料", "语料库", "评测", "评"):
            cmd = "/" + cmd.lstrip("/")
        if await master_command(event, conv, cmd):
            return

    # ---- 群答模式：私聊问的问题，回答直接发群 ----
    if master_conv and STORE.get_setting("group_answer_mode") == "1" and len(cmd) >= 2:
        await cmd_group_answer(event, cmd)
        return

    # ---- 主人自然语言指挥群发言（真实发送，不是让模型幻想）----
    if master_conv:
        m = NAT_GROUP_ACTION.search(cmd)
        if m:
            verb, payload = m.group(1), m.group(2).strip(" 。.!！")
            if verb in ("冒个泡", "吱一声", "吱一声吧") or payload in EMPTY_TOPIC:
                await cmd_bubble(event, payload if payload not in EMPTY_TOPIC else "")
            else:
                await cmd_say(event, payload)
            return

    # ---- 主人纠错：自检并改参数 ----
    if master_conv and CORRECT_HINT.search(cmd):
        await self_correct(event, conv, cmd)
        return

    # ---- 硬性话术兜底（不经过模型）；主人私聊如实回答 ----
    if master_conv:
        if AI_PROBE.search(cmd):
            await say(event, conv, random.choice([
                "对，我是程序。人格是你设的。",
                "嗯，是程序。设定也在你自己手里。",
            ]))
            return
    elif AI_PROBE.search(cmd):
        await say(event, conv, random.choice(AI_REPLIES))
        return
    elif ROLEPLAY.search(cmd):
        await say(event, conv, random.choice(ROLEPLAY_REPLIES))
        return
    # 工作时间：私聊与 @ 仍然回，但由 BUSY_RULES 约束成极简

    # ---- 指令 ----
    if cmd in ("/帮助", "帮助", "/help", "help"):
        await say(event, conv, "直接说话就行，群里 @我。我不太会用这些花里胡哨的指令。")
        return
    if cmd in ("清空记忆", "/清空记忆", "忘记我"):
        STORE.reset(conv)
        await say(event, conv, "行，之前聊的我都忘了。")
        return
    if cmd in ("你记得我什么", "/记忆"):
        _, facts, _ = STORE.profile(conv)
        await say(event, conv, facts.strip() or "还没记住什么。")
        return
    if cmd.startswith("记住"):
        fact = cmd[2:].strip(" ：:，,")
        if fact:
            _, facts, _ = STORE.profile(conv)
            base = "" if not facts.strip() or facts.strip() == "- 暂无" else facts.strip() + "\n"
            STORE.set_facts(conv, base + f"- {fact}")
            await say(event, conv, f"嗯，记住了。")
        return

    # ---- 对话 ----
    if not quoted_ctx:
        quoted_ctx = await get_quoted_text(event, conv)
    async with conv_lock(conv):
        # 拟人反应：读一下对方的消息，再开始“打字”；主人消息也走这个节奏
        incoming = text or ""
        read_time = min(len(incoming) * READ_PER_CHAR, READ_MAX)
        await asyncio.sleep(random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
                            + read_time
                            + random.uniform(JITTER_MIN, JITTER_MAX))
        if master_conv:
            _, facts, _ = STORE.profile(conv)
            if "主人" not in facts:
                base = "" if not facts.strip() or facts.strip() == "- 暂无" else facts.strip() + "\n"
                STORE.set_facts(conv, base + "- 对方是我的主人（他管理我、能改我的设定）")
        STORE.add(conv, "user", text)
        STORE.bump(conv, nickname)
        messages = build_messages(conv, text, gid, master=master_conv,
                                  force_active=master_conv or (master_in_group and at_me),
                                  uid=event.get("user_id"), nickname=nickname,
                                  exclude_group_id=_gmid if gid else None,
                                  quoted=quoted_ctx)
        try:
            if master_conv and len(text) >= 6 and not re.match(r"^(在吗|在不在|你好|嗨|嗯|哦|好的?)[。！？?]*$", text):
                results = await asyncio.to_thread(web_search, text, 5)
                if results:
                    STORE.add_knowledge(text, "；".join(r["title"] for r in results[:3])[:150])
                    ctx = "\n".join(f"- {r['title']}：{r['snippet'][:160]}" for r in results[:5])
                    messages = messages + [{"role": "system",
                                            "content": f"【联网搜索结果（今天是 {now():%Y-%m-%d}）】\n{ctx}\n\n"
                                                       "注意：结果可能包含旧内容，年份明显过时（两年前的）当没查到。\n\n"
                                                       "把结果里你认同的观点当成自己的看法说出来，别罗列多方观点、别端水。"
                                                       "对方是主人。基于这些结果给他详细一点的回答："
                                                       "可以分 2-3 句把关键信息讲清楚，别敷衍、别只说“我也说不好”；"
                                                       "结果里没有的就直说没查到，不要编。"}]
                    log_line(f"[search] 主人查询（整句）：{text[:80]}")
            if NEED_SEARCH.search(text):
                query = text
                results = await asyncio.to_thread(web_search, query)
                if results:
                    STORE.add_knowledge(query, "；".join(r["title"] for r in results[:3])[:150])
                    ctx = "\n".join(f"- {r['title']}：{r['snippet'][:120]}" for r in results[:4])
                    messages = messages + [{"role": "system",
                                            "content": f"【联网搜索结果（{now():%Y-%m-%d}）】\n{ctx}\n\n"
                                                       "直接根据结果给结论，口语、简短（不超过 60 字）。"
                                                       "不要说“我也说不好/不太确定/可能”，不要罗列网址；"
                                                       "如果结果确实跟问题无关，就说“没查到”。"}]
                    log_line(f"[search] 关键词触发（整句）：{query[:80]}")
            limit = 600 if master_conv else (150 if not active else 450)
            temp = 0.6 if master_conv else (0.95 if not active else 0.9)
            answer = await asyncio.to_thread(chat, messages, temp, limit)
            answer = await maybe_search(answer, messages, active)
            answer = await ensure_question_detail(answer, messages, gid, active, is_question(text))
            answer = await enforce_style(answer, messages)
            answer = await enforce_time(answer, messages, text)
        except Exception as e:
            log_line(f"[llm] {type(e).__name__}: {e}")
            return
        answer = strip_markdown(answer)
        if not answer:
            return
        await say(event, conv, answer)
        mid = STORE.add(conv, "assistant", answer)
        STORE.remember_self(conv, answer, mid)

        _, _, counter = STORE.profile(conv)
        if counter % MEMORY_EVERY == 0:
            await asyncio.to_thread(refresh_memory, conv)
        if counter % TOPIC_EVERY == 0:
            spawn_bg(asyncio.to_thread(update_topic, conv))
        spawn_bg(asyncio.to_thread(summarize_conv, conv))


# ---------------------------------------------------------------- 自检
async def selftest() -> None:
    print("人格：", get_persona()[:60], "...")
    print("当前时间：", time_hint(), "在线 =", is_active_time())
    sample = "我昨天打了一整晚只狼，终于过了蝴蝶夫人。你玩魂系吗？我们群里有人推荐了《空洞骑士》。"
    parts = split_sentences(sample)
    print("断句结果：", parts)
    print("每句长度：", [len(p) for p in parts])
    for probe in ("你是不是机器人啊", "你能扮演一下我女朋友吗", "今天好累"):
        if AI_PROBE.search(probe):
            print(f"[兜底] {probe} -> {random.choice(AI_REPLIES)}")
        elif ROLEPLAY.search(probe):
            print(f"[兜底] {probe} -> {random.choice(ROLEPLAY_REPLIES)}")
        else:
            print(f"[模型] {probe} -> ...")


def backfill_self_memory() -> None:
    """首次启动时从已有助手消息里补抽“她说过的话”，之后只增量写。"""
    try:
        if STORE.get_setting("self_backfill_done") == "1":
            return
        if int(STORE.db.execute("SELECT COUNT(*) FROM self_memory").fetchone()[0]) > 0:
            STORE.set_setting("self_backfill_done", "1")
            return
        convs = [r[0] for r in STORE.db.execute(
            "SELECT DISTINCT conv FROM messages WHERE role='assistant'").fetchall()]
        n = 0
        for conv in convs:
            rows = STORE.db.execute(
                "SELECT id, content FROM messages WHERE conv=? AND role='assistant' "
                "ORDER BY id DESC LIMIT 80", (conv,)).fetchall()
            for mid, content in rows:
                STORE.remember_self(conv, content, mid)
                n += 1
        STORE.set_setting("self_backfill_done", "1")
        log_line(f"[memory] self backfill {n} 条助手消息")
    except Exception as e:
        log_line(f"[memory] self backfill 失败 {e}")


def collect_eval_cases(days: int = 7, limit: int = 40) -> int:
    """从最近的群聊回复里采样人工抽检用例。"""
    since = int(time.time()) - max(1, days) * 86400
    rows = STORE.db.execute(
        "SELECT id, gid, uid, nickname, content, ts FROM group_log "
        "WHERE role='assistant' AND ts>=? ORDER BY id DESC LIMIT ?",
        (since, max(1, limit * 2)),
    ).fetchall()
    added = 0
    for mid, gid, uid, nick, reply, ts in rows:
        prev = STORE.db.execute(
            "SELECT id, uid, nickname, content FROM group_log "
            "WHERE gid=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1",
            (gid, mid),
        ).fetchone()
        if not prev:
            continue
        ctx_rows = STORE.db.execute(
            "SELECT role, uid, nickname, content FROM group_log "
            "WHERE gid=? AND id<? ORDER BY id DESC LIMIT 8", (gid, mid)
        ).fetchall()
        ctx = [{"role": r[0], "uid": r[1], "nickname": r[2], "text": (r[3] or "")[:200]}
               for r in reversed(ctx_rows)]
        conv = f"group:{gid}:{uid}"
        cid = STORE.add_eval_case(gid, conv, uid, nick or "", "reply", ctx,
                                  prev[3] or "", reply or "", mid)
        if cid:
            added += 1
        if added >= limit:
            break
    log_line(f"[eval] collect days={days} added={added}")
    return added


CONTROL_SYNC_TASK: asyncio.Task | None = None
APPLIED_FILE = CONTROL_DIR / "applied.json"


def _gh_get(url: str) -> bytes:
    headers = {"User-Agent": "qqbot-control-sync", "Accept": "application/vnd.github+json"}
    token = ENV.get("GH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def control_sync_once() -> None:
    """拉取最新 control-v* tag 的 policy/canon，校验后原子替换本地快照。"""
    if not CONTROL_SYNC or not GH_REPO:
        return
    try:
        raw = _gh_get(f"https://api.github.com/repos/{GH_REPO}/git/matching-refs/tags/control-v")
        refs = json.loads(raw.decode() or "[]")
        best = None
        for r in refs:
            ref = str(r.get("ref", ""))
            m = re.search(r"/control-v(\d+)$", ref)
            if m:
                n = int(m.group(1))
                if best is None or n > best[0]:
                    best = (n, ref.split("/")[-1])
        if not best:
            return
        tag = best[1]
        applied = {}
        try:
            applied = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        if applied.get("tag") == tag:
            return
        def _content(path: str) -> bytes:
            raw = json.loads(_gh_get(
                f"https://api.github.com/repos/{GH_REPO}/contents/{path}?ref={tag}").decode())
            return base64.b64decode(raw["content"])

        policy = json.loads(_content("control/policy.json").decode())
        canon = json.loads(_content("control/canon.json").decode())
        if not isinstance(policy.get("version"), int) or not isinstance(policy.get("rules"), list):
            raise ValueError("policy 结构不合法")
        if not isinstance(canon.get("items"), list):
            raise ValueError("canon 结构不合法")
        POLICY_FILE.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        CANON_FILE.write_text(json.dumps(canon, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        APPLIED_FILE.write_text(json.dumps({"tag": tag, "version": policy["version"],
                                             "applied_at": int(time.time())},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
        log_line(f"[control] applied {tag} policy={policy['version']} "
                 f"rules={len(policy['rules'])} canon={len(canon['items'])}")
    except Exception as e:
        log_line(f"[control] sync failed {type(e).__name__}: {e}")


async def control_sync_loop() -> None:
    if not CONTROL_SYNC or not GH_REPO:
        return
    while True:
        try:
            await asyncio.to_thread(control_sync_once)
        except Exception as e:
            log_line(f"[control] loop {type(e).__name__}: {e}")
        await asyncio.sleep(max(60, CONTROL_SYNC_SEC))


NIGHTLY_TASK: asyncio.Task | None = None


def run_corpus_import(path: Path, source: str) -> None:
    """后台导入语料文件，输出写到 logs/corpus.log。"""
    cmd = [sys.executable, str(ROOT / "scripts" / "import_corpus.py"),
           "--file", str(path), "--source", source]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=1200)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        with open(LOG_DIR / "corpus.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%F %T')}] {' '.join(cmd)}\n{out}\n")
        log_line(f"[corpus] import {path.name} rc={proc.returncode} {out[-200:]}")
    except Exception as e:
        log_line(f"[corpus] import 失败 {type(e).__name__}: {e}")


def run_nightly_style(day: str | None = None) -> None:
    """调用 scripts/nightly_style.py，把输出写到 logs/style.log。"""
    cmd = [sys.executable, str(ROOT / "scripts" / "nightly_style.py"),
           "--min-msgs", str(NIGHTLY_MIN_MSGS)]
    if day:
        cmd += ["--date", day]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=900)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        with open(LOG_DIR / "style.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%F %T')}] {' '.join(cmd)}\n{out}\n")
        log_line(f"[style] nightly {day or 'yesterday'} rc={proc.returncode} {out[-160:]}")
    except Exception as e:
        log_line(f"[style] nightly 失败 {type(e).__name__}: {e}")


async def nightly_style_loop() -> None:
    if not NIGHTLY_STYLE:
        return
    # 启动时补跑昨天；脚本内部会跳过已经学过的群
    try:
        await asyncio.to_thread(run_nightly_style,
                                (now() - timedelta(days=1)).strftime("%Y-%m-%d"))
    except Exception as e:
        log_line(f"[style] 补跑失败 {e}")
    while True:
        dt = now()
        nxt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep(max(60, (nxt - dt).total_seconds()))
        day = (nxt - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            await asyncio.to_thread(run_nightly_style, day)
        except Exception as e:
            log_line(f"[style] 夜间学习失败 {e}")


async def main_run() -> None:
    global NIGHTLY_TASK, CONTROL_SYNC_TASK
    NIGHTLY_TASK = asyncio.create_task(nightly_style_loop())
    CONTROL_SYNC_TASK = asyncio.create_task(control_sync_loop())
    await BOT.run()


if __name__ == "__main__":
    reload_params()
    if "--selftest" in sys.argv:
        asyncio.run(selftest())
    else:
        backfill_self_memory()
        try:
            asyncio.run(main_run())
        except KeyboardInterrupt:
            print("bye")
