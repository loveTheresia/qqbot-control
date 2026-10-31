const $ = (id) => document.getElementById(id);
const LABELS = {
  human_like: "像人", ok: "还行", cold: "冷淡",
  hallucinated: "不懂装懂", should_not: "不该回", skip: "跳过",
};
const state = { case: null, busy: false };

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 1600);
}

function showError(msg) {
  $("error").hidden = false;
  $("error").textContent = msg;
}

function renderStats(s) {
  if (!s) return;
  $("done").textContent = s.done ?? 0;
  $("total").textContent = s.total ?? 0;
  $("pending").textContent = `${s.pending ?? 0} 待评`;
  const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
  $("bar").style.width = pct + "%";
  $("stats").innerHTML = "";
  const entries = [["总用例", s.total], ["待评", s.pending], ["已评", s.done]];
  for (const [name, n] of entries) {
    const d = document.createElement("div");
    d.className = "stat";
    const b = document.createElement("b"); b.textContent = n;
    const sp = document.createElement("span"); sp.textContent = name;
    d.append(b, sp); $("stats").append(d);
  }
  for (const lab of (s.labels || [])) {
    const d = document.createElement("div");
    d.className = "stat";
    const b = document.createElement("b"); b.textContent = lab.count;
    const sp = document.createElement("span"); sp.textContent = lab.name;
    d.append(b, sp); $("stats").append(d);
  }
}

function renderRecent(list) {
  const ul = $("recent");
  ul.innerHTML = "";
  if (!list || !list.length) {
    const li = document.createElement("li");
    li.className = "muted"; li.textContent = "还没有记录";
    ul.append(li); return;
  }
  for (const r of list) {
    const li = document.createElement("li");
    const tag = document.createElement("span");
    tag.className = "tag"; tag.textContent = r.name || r.label;
    const txt = document.createElement("span");
    txt.textContent = `#${r.id} ${r.note ? "· " + r.note : ""}`;
    li.append(tag, txt); ul.append(li);
  }
}

function renderCase(c) {
  state.case = c;
  const empty = $("empty");
  const card = $("case");
  if (!c) {
    card.hidden = true;
    empty.hidden = false;
    $("loading").hidden = true;
    return;
  }
  empty.hidden = true;
  card.hidden = false;
  $("loading").hidden = true;
  $("case-id").textContent = `#${c.id}`;
  $("case-gid").textContent = `群 ${c.gid}`;
  $("case-type").textContent = c.case_type === "silent" ? "她没回" : "她的回复";

  const thread = $("context");
  thread.innerHTML = "";
  for (const m of (c.context || [])) {
    const row = document.createElement("div");
    row.className = "msg" + (m.role === "assistant" ? " her" : "");
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = m.role === "assistant" ? "她" : (m.nickname || m.who || "群友");
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = m.text || m.content || "";
    row.append(who, bubble);
    thread.append(row);
  }
  $("trigger").textContent = c.trigger || "（无）";
  $("reply").textContent = c.reply || "（她没有回）";
  $("note").value = "";
  for (const b of document.querySelectorAll(".lb")) b.disabled = false;
}

async function load() {
  try {
    const data = await api("/api/next");
    renderStats(data.stats);
    renderCase(data.case);
    const rec = await api("/api/recent");
    renderRecent(rec.recent);
  } catch (e) {
    showError("加载失败：" + e.message);
  }
}

async function submit(label) {
  if (state.busy || !state.case) return;
  state.busy = true;
  for (const b of document.querySelectorAll(".lb")) b.disabled = true;
  try {
    const data = await api("/api/label", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: state.case.id,
        label,
        note: $("note").value || "",
      }),
    });
    toast(`已记录：${LABELS[label] || label}`);
    renderStats(data.stats);
    renderCase(data.next);
    const rec = await api("/api/recent");
    renderRecent(rec.recent);
  } catch (e) {
    showError("提交失败：" + e.message);
  } finally {
    state.busy = false;
    for (const b of document.querySelectorAll(".lb")) b.disabled = false;
  }
}

for (const btn of document.querySelectorAll(".lb")) {
  btn.addEventListener("click", () => submit(btn.dataset.label));
}

document.addEventListener("keydown", (e) => {
  if (e.target && ["TEXTAREA", "INPUT"].includes(e.target.tagName)) return;
  const idx = parseInt(e.key, 10);
  if (idx >= 1 && idx <= 6) {
    const btn = document.querySelectorAll(".lb")[idx - 1];
    if (btn) { e.preventDefault(); submit(btn.dataset.label); }
  }
});

$("refresh").addEventListener("click", async () => {
  const rec = await api("/api/recent");
  renderRecent(rec.recent);
  renderStats(rec.stats);
});

$("finish").addEventListener("click", async () => {
  if (!confirm("结束本次评测并停止 8099 服务？\n标签已经写入数据库，训练材料会生成在 data/eval/。")) return;
  $("finish").disabled = true;
  try {
    await api("/api/finish", { method: "POST" });
    document.querySelector("main").style.display = "none";
    document.querySelector(".topbar").style.display = "none";
    $("finished").hidden = false;
  } catch (e) {
    showError("结束失败：" + e.message);
    $("finish").disabled = false;
  }
});

load();
