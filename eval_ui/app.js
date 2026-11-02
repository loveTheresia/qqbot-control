const $ = (id) => document.getElementById(id);
const LABELS = {
  human_like: "像人", ok: "还行", cold: "冷淡",
  hallucinated: "不懂装懂", should_not: "不该回", skip: "跳过",
};
const state = { case: null, stats: null, busy: false };

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

function renderEmpty(s) {
  s = s || state.stats || {};
  const title = $("empty-title");
  const text = $("empty-text");
  const submit = $("submit-batch");
  if (!title || !text) return;
  const open = s.open_batch;
  if (s.pending > 0) {
    title.textContent = "还有待评内容";
    text.textContent = `还有 ${s.pending} 条没评完，刷新一下继续。`;
    submit.hidden = true;
  } else if ((s.submitted_batches || []).length) {
    title.textContent = "已提交，训练中";
    text.textContent = "这批结果已经参与训练材料生成；服务继续运行，不需要停。";
    submit.hidden = true;
  } else if (open) {
    title.textContent = "本批已评完，待提交";
    text.textContent = `${open.day || "本批"}：${open.labeled}/${open.total} 已评。只有点“提交本批并训练”后，这批结果才会参与训练。`;
    submit.hidden = false;
  } else {
    title.textContent = "今天没有可测评内容";
    text.textContent = "每天 00:00 会自动采集前一天的群聊回复；也可以手动采集昨天。";
    submit.hidden = true;
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
    renderEmpty(state.stats);
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
    state.stats = data.stats;
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
    state.stats = data.stats;
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

$("submit-batch").addEventListener("click", async () => {
  if (!confirm("提交本批测评？\n\n提交后这批结果才会参与训练；服务不会停止。")) return;
  $("submit-batch").disabled = true;
  try {
    await api("/api/submit", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast("已提交，训练材料生成中");
    $("empty-title").textContent = "本批已提交";
    $("empty-text").textContent = "训练材料正在生成；服务继续运行。";
    $("submit-batch").hidden = true;
  } catch (e) {
    showError("提交失败：" + e.message);
  } finally {
    $("submit-batch").disabled = false;
  }
});

$("collect-batch").addEventListener("click", async () => {
  try {
    const r = await api("/api/workbench/eval/collect", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(`已采集 ${r.added ?? 0} 条`);
    setTimeout(load, 600);
  } catch (e) { showError("采集失败：" + e.message); }
});

load();
