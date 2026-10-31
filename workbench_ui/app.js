const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const state = { tab: "dashboard", loaded: {} };

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function toast(t) {
  const el = $("#toast"); el.textContent = t; el.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove("show"), 1600);
}
function setLoading(id, text = "加载中…") {
  const el = $(id); if (el) el.innerHTML = `<div class="empty">${esc(text)}</div>`;
}
function badge(s) {
  const m = { active: "启用", disabled: "停用", draft: "草稿", approved: "已批准", canary: "灰度", released: "已发布", rolled_back: "已回滚" };
  return `<span class="badge ${esc(s)}">${esc(m[s] || s)}</span>`;
}
function when(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

async function switchTab(name) {
  state.tab = name;
  $$(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach(s => s.classList.toggle("on", s.id === `tab-${name}`));
  if (!state.loaded[name]) {
    state.loaded[name] = true;
    if (name === "dashboard") await loadDashboard();
    if (name === "rules") await loadRules();
    if (name === "canon") await loadCanon();
    if (name === "pipeline") await loadPipeline();
    if (name === "changes") await loadChanges();
    if (name === "release") await loadRelease();
  }
}

async function loadDashboard() {
  setLoading("#tab-dashboard");
  try {
    const d = await api("/api/workbench/dashboard");
    const r = d.release || {};
    $("#release-badge").textContent = `policy v${r.policy_version ?? "?"} · canon v${r.canon_version ?? "?"}`;
    const labels = (d.eval.labels || []).map(x =>
      `<span class="chip ${["human_like","ok"].includes(x.key) ? "good" : "bad"}">${esc(x.name)} ${x.count}</span>`).join("");
    const recent = (d.recent || []).map(x =>
      `<div class="chip">#${x.id} ${esc(x.label)}${x.note ? " · " + esc(x.note) : ""}</div>`).join("")
      || `<div class="empty">还没有评测记录</div>`;
    $("#tab-dashboard").innerHTML = `
      <div class="grid">
        <div class="card kpi"><b>${d.eval.labeled}</b><span>已评样本</span><small>待评 ${d.eval.pending}</small></div>
        <div class="card kpi"><b>${d.eval.like_rate}%</b><span>像人/还行</span><small>明显问题 ${d.eval.bad_rate}%</small></div>
        <div class="card kpi"><b>${d.group.user_7d}</b><span>近 7 天群员消息</span><small>她 ${d.group.bot_7d} 句</small></div>
        <div class="card kpi"><b>${d.rules}</b><span>启用补充规则</span><small>变更 ${d.changes} 条未发布</small></div>
      </div>
      <div class="grid" style="margin-top:13px">
        <div class="card kpi"><b>${d.canon}</b><span>Canon 条目</span><small>待审核</small></div>
        <div class="card kpi"><b>${d.corpus}</b><span>语料条目</span><small>已核实才注入</small></div>
        <div class="card kpi"><b>${r.policy_version ?? "—"}</b><span>当前 policy 版本</span><small>${esc(r.note || "")}</small></div>
        <div class="card kpi"><b>${d.changes}</b><span>未发布变更</span><small>一次只推一个</small></div>
      </div>
      <div class="grid two" style="margin-top:16px">
        <div class="card"><h3>评测标签</h3><div class="chips">${labels || `<span class="muted">暂无</span>`}</div></div>
        <div class="card"><h3>最近评测</h3><div class="chips">${recent}</div></div>
      </div>
      <div class="card panel">
        <div class="panel-head"><h2>下一步</h2><a class="eval-link" href="/eval">去评测 →</a></div>
        <p class="hint">先采新一批节奏闸上线后的样本；规则改动先在“规则”页起草，发布前过一次评测。Canon 先录 3~5 条真正会聊到的。</p>
      </div>`;
  } catch (e) { $("#tab-dashboard").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

async function loadRules() {
  setLoading("#tab-rules");
  try {
    const d = await api("/api/workbench/rules");
    const rows = (d.rules || []).map(r => `
      <tr>
        <td><code>${esc(r.rule_id)}</code><div class="sub">${esc(r.scope)} · P${r.priority}</div></td>
        <td>${esc(r.text)}</td>
        <td>${badge(r.status)}</td>
        <td><button class="mini" data-rule-toggle="${r.id}" data-status="${r.status === "active" ? "disabled" : "active"}">${r.status === "active" ? "停用" : "启用"}</button></td>
      </tr>`).join("") || `<tr><td colspan="4" class="empty">还没有补充规则。规则只追加，不改人格核心。</td></tr>`;
    $("#tab-rules").innerHTML = `
      <div class="card">
        <div class="panel-head"><h2>补充规则</h2>
          <button class="primary" id="publish-policy">发布当前规则</button></div>
        <p class="hint">这些规则追加在人格/RULES 之后；冲突时以人格核心为准。发布后 bot 下一条消息就会读到新版本，不需要重启。</p>
        <form class="row" id="rule-form" style="margin-top:12px">
          <select name="scope"><option value="group">群聊</option><option value="private">私聊</option><option value="all">全部</option></select>
          <input name="priority" type="number" value="100" min="1" max="999">
          <input name="text" placeholder="例如：别人只回哈哈哈、没有新信息时，不接。" maxlength="500" required>
          <button class="ghost" type="submit">添加规则</button>
        </form>
        <table class="table"><thead><tr><th>ID / 范围</th><th>规则</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      </div>
      <div class="card panel"><h3>当前发布</h3><p class="hint">policy v${d.release?.policy_version ?? "—"} · ${esc(d.release?.note || "无备注")}</p></div>`;
    $("#rule-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const f = new FormData(e.target);
      try {
        await api("/api/workbench/rules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope: f.get("scope"), priority: Number(f.get("priority")), text: f.get("text") }) });
        toast("规则已添加"); state.loaded.rules = false; await loadRules(); await loadDashboard();
      } catch (err) { toast("失败：" + err.message); }
    });
    $$("[data-rule-toggle]").forEach(b => b.addEventListener("click", async () => {
      try {
        await api("/api/workbench/rules/toggle", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: Number(b.dataset.ruleToggle), status: b.dataset.status }) });
        toast("已更新"); state.loaded.rules = false; await loadRules();
      } catch (err) { toast("失败：" + err.message); }
    }));
    $("#publish-policy").addEventListener("click", async () => {
      const note = prompt("发布备注（可选）：", "规则校准") || "";
      try {
        const r = await api("/api/workbench/publish", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note }) });
        toast(`已发布 v${r.version}，${r.rules} 条规则`); state.loaded.rules = false; await loadRules(); await loadDashboard();
      } catch (err) { toast("发布失败：" + err.message); }
    });
  } catch (e) { $("#tab-rules").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

async function loadCanon() {
  setLoading("#tab-canon");
  try {
    const d = await api("/api/workbench/canon");
    const rows = (d.canon || []).map(c => `
      <tr><td><b>${esc(c.title)}</b><div class="sub">${esc(c.type)} · ${esc(c.source || "无来源")}</div></td>
      <td>${esc(c.summary || "")}</td><td>${esc(c.tags || "")}</td><td>${badge(c.status)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">还没有 canon。先录 3~5 条她真的会聊到的作品/概念。</td></tr>`;
    $("#tab-canon").innerHTML = `
      <div class="card">
        <div class="panel-head"><h2>Canon 录入</h2><span class="muted">v0.1 只存储，不注入</span></div>
        <p class="hint">标准字段：类型、标题、来源、一句话摘要、标签。引文核实后再扩字段；现在先保证“有结构”。</p>
        <form id="canon-form" class="form-grid" style="margin-top:12px">
          <div><label>类型</label><select name="type"><option value="work">作品</option><option value="concept">概念</option><option value="person">人物</option><option value="event">事件</option></select></div>
          <div><label>标题</label><input name="title" required maxlength="120" placeholder="《局外人》"></div>
          <div><label>来源</label><input name="source" maxlength="120" placeholder="加缪 / 百度百科"></div>
          <div><label>标签</label><input name="tags" maxlength="200" placeholder="文学,荒诞,存在主义"></div>
          <div class="wide"><label>摘要</label><textarea name="summary" maxlength="1000" placeholder="它讲什么、在什么背景下、最容易误读成什么。"></textarea></div>
          <div class="wide actions"><button class="primary" type="submit">添加 Canon</button></div>
        </form>
        <table class="table" style="margin-top:12px"><thead><tr><th>条目</th><th>摘要</th><th>标签</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table>
      </div>`;
    $("#canon-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const f = new FormData(e.target);
      try {
        await api("/api/workbench/canon", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(f)) });
        toast("Canon 已添加"); state.loaded.canon = false; await loadCanon(); state.loaded.dashboard = false;
      } catch (err) { toast("失败：" + err.message); }
    });
  } catch (e) { $("#tab-canon").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

async function loadPipeline() {
  setLoading("#tab-pipeline");
  try {
    const d = await api("/api/workbench/pipeline");
    $("#tab-pipeline").innerHTML = `
      <div class="card">
        <div class="panel-head"><h2>数据漏斗</h2><a class="eval-link" href="/eval">去评测 →</a></div>
        <div class="funnel">
          <div><b>${d.raw_messages}</b><span>私聊消息</span></div>
          <div><b>${d.group_log}</b><span>群日志</span></div>
          <div><b>${d.eval_cases}</b><span>抽检用例</span></div>
          <div><b>${d.labeled}</b><span>已标注</span></div>
          <div><b>${d.preferences}</b><span>偏好数据</span></div>
        </div>
        <p class="hint" style="margin-top:14px">原始 → 可采样 → 已标注 → 偏好对 → 训练可用。当前瓶颈是“已标注”样本太少，而且旧样本早于节奏闸。</p>
      </div>
      <div class="grid two panel">
        <div class="card"><h3>下一步</h3><p class="hint">用 <code>/评测 采集 7</code> 采新一批；评 20 条后看标签分布，再决定规则或 canon。不要直接拿旧日志微调。</p></div>
        <div class="card"><h3>产物</h3><p class="hint"><code>data/eval/preferences.jsonl</code><br><code>data/eval/training-report.md</code></p></div>
      </div>`;
  } catch (e) { $("#tab-pipeline").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

async function loadChanges() {
  setLoading("#tab-changes");
  try {
    const d = await api("/api/workbench/changes");
    const rows = (d.changes || []).map(c => `
      <tr><td>#${c.id}<div class="sub">${when(c.created_at)}</div></td>
      <td><b>${esc(c.title)}</b><div class="sub">${esc(c.hypothesis || "")}</div></td>
      <td>${esc(c.artifact || "")}<div class="sub">${esc(c.metric || "")}</div></td>
      <td>${badge(c.status)}</td>
      <td><select data-change-select="${c.id}">${["draft","approved","canary","released","rolled_back"].map(s => `<option value="${s}" ${s === c.status ? "selected" : ""}>${s}</option>`).join("")}</select></td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">还没有变更单。一次只推一个变量。</td></tr>`;
    $("#tab-changes").innerHTML = `
      <div class="card">
        <div class="panel-head"><h2>变更单</h2><span class="muted">draft → approved → canary → released</span></div>
        <form id="change-form" class="form-grid">
          <div class="wide"><label>标题</label><input name="title" required placeholder="降低 A/B 对话中的主动插话"></div>
          <div><label>问题</label><textarea name="problem" placeholder="什么现象、多少条样本"></textarea></div>
          <div><label>假设</label><textarea name="hypothesis" placeholder="改什么、预期哪个指标变化"></textarea></div>
          <div><label>改动对象</label><input name="artifact" placeholder="policy.rule / canon / prompt"></div>
          <div><label>验收指标</label><input name="metric" placeholder="should_not 率从 8% 降到 4%"></div>
          <div class="wide"><label>回滚方案</label><input name="rollback" placeholder="回滚到 policy vN"></div>
          <div class="wide actions"><button class="primary" type="submit">创建变更单</button></div>
        </form>
        <table class="table" style="margin-top:12px"><thead><tr><th>#</th><th>变更</th><th>对象 / 指标</th><th>状态</th><th>流转</th></tr></thead><tbody>${rows}</tbody></table>
      </div>`;
    $("#change-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const f = new FormData(e.target);
      try {
        await api("/api/workbench/changes", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(f)) });
        toast("变更单已创建"); state.loaded.changes = false; await loadChanges();
      } catch (err) { toast("失败：" + err.message); }
    });
    $$("[data-change-select]").forEach(sel => sel.addEventListener("change", async () => {
      try {
        await api("/api/workbench/changes/status", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: Number(sel.dataset.changeSelect), status: sel.value }) });
        toast("状态已更新"); state.loaded.changes = false; await loadChanges();
      } catch (err) { toast("失败：" + err.message); }
    }));
  } catch (e) { $("#tab-changes").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

async function loadRelease() {
  setLoading("#tab-release");
  try {
    const d = await api("/api/workbench/release");
    const rows = (d.versions || []).map(v => `<tr><td>v${v.version}</td><td>${esc(v.note || "")}</td><td>${when(v.created_at)}</td><td>${v.active ? badge("released") : ""}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">还没有发布记录。</td></tr>`;
    const r = d.release || {};
    $("#tab-release").innerHTML = `
      <div class="grid two">
        <div class="card kpi"><b>policy v${r.policy_version ?? "—"}</b><span>当前规则版本</span><small>${esc(r.note || "")}</small></div>
        <div class="card kpi"><b>canon v${r.canon_version ?? "—"}</b><span>当前 Canon 版本</span><small>canon 发布后续接入</small></div>
      </div>
      <div class="card panel"><h2>发布历史</h2><table class="table"><thead><tr><th>版本</th><th>备注</th><th>时间</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  } catch (e) { $("#tab-release").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

$$(".tabs button").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));
loadDashboard();
