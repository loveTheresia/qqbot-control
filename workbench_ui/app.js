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
    if (name === "github") await loadGithub();
    if (name === "params") await loadParams();
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
      <div class="guide">
        <b>第一次用？</b> 按顺序来：<span>① 规则</span> → <span>② Canon</span> → <span>③ 评测</span> → <span>④ 变更</span> → <span>⑤ GitHub 发布</span>。
        <br><b>一次只改一个方面</b>：一条规则、一个 Canon 主题、一个 PR；发布后观察 1~2 天，再动下一项。
      </div>
      <div class="grid">
        <div class="card kpi link-card" data-href="/eval"><b>${d.eval.labeled}</b><span>已评样本 · 去评测 →</span><small>待评 ${d.eval.pending}</small></div>
        <div class="card kpi link-card" data-href="/eval"><b>${d.eval.like_rate}%</b><span>像人/还行 · 看问题 →</span><small>明显问题 ${d.eval.bad_rate}%</small></div>
        <div class="card kpi link-card" data-go="pipeline"><b>${d.group.user_7d}</b><span>近 7 天群员消息</span><small>她 ${d.group.bot_7d} 句 · 看管线</small></div>
        <div class="card kpi link-card" data-go="rules"><b>${d.rules}</b><span>启用补充规则 · 去管理 →</span><small>变更 ${d.changes} 条未发布</small></div>
      </div>
      <div class="grid" style="margin-top:13px">
        <div class="card kpi link-card" data-go="canon"><b>${d.canon}</b><span>Canon 条目 · 去录入 →</span><small>先录 3~5 条真正会聊到的</small></div>
        <div class="card kpi link-card" data-go="pipeline"><b>${d.corpus}</b><span>语料条目</span><small>已核实才注入</small></div>
        <div class="card kpi link-card" data-go="release"><b>${r.policy_version ?? "—"}</b><span>当前 policy 版本 · 看发布 →</span><small>${esc(r.note || "")}</small></div>
        <div class="card kpi link-card" data-go="changes"><b>${d.changes}</b><span>未发布变更 · 去记录 →</span><small>一次只推一个</small></div>
      </div>
      <div class="grid two" style="margin-top:16px">
        <div class="card"><h3>评测标签</h3><div class="chips">${labels || `<span class="muted">暂无</span>`}</div></div>
        <div class="card"><h3>最近评测</h3><div class="chips">${recent}</div></div>
      </div>
      <div class="card panel">
        <div class="panel-head"><h2>下一步</h2><a class="eval-link" href="/eval">去评测 →</a></div>
        <p class="hint">先采新一批节奏闸上线后的样本；规则改动先在“规则”页起草，发布前过一次评测。Canon 先录 3~5 条真正会聊到的。</p>
      </div>`;
    $$("[data-go]").forEach(el => el.addEventListener("click", () => switchTab(el.dataset.go)));
    $$("[data-href]").forEach(el => el.addEventListener("click", () => { window.location.href = el.dataset.href; }));
  } catch (e) { $("#tab-dashboard").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

async function loadRules() {
  setLoading("#tab-rules");
  try {
    const d = await api("/api/workbench/rules");
    const b = await api("/api/workbench/builtin");
    const curVer = d.release?.policy_version ?? 0;
    const draft = (d.rules || []).filter(r => r.status === "active" && (r.version || 0) !== curVer).length;
    const rows = (d.rules || []).map(r => `
      <tr>
        <td><code>${esc(r.rule_id)}</code><div class="sub">${esc(r.scope)}</div></td>
        <td>${esc(r.text)}</td>
        <td><b>${r.priority}</b><div class="sub">${r.priority <= 49 ? "高" : r.priority <= 99 ? "中高" : r.priority === 100 ? "默认" : "低"}</div></td>
        <td>${badge(r.status)}</td>
        <td><button class="mini" data-rule-toggle="${r.id}" data-status="${r.status === "active" ? "disabled" : "active"}">${r.status === "active" ? "停用" : "启用"}</button></td>
      </tr>`).join("") || `<tr><td colspan="5" class="empty">还没有补充规则。规则只追加，不改人格核心。</td></tr>`;
    $("#tab-rules").innerHTML = `
      <div class="guide">
        <b>优先级 100 是什么意思：</b>100 是默认中间值。数字越小越先执行（1~49 高，50~99 中高，100 默认，200+ 低）。
        同一场景两条规则冲突时，先看数字小的。一般不用改，保持 100 就行。
        <br><b>一次只加一条：</b>发布并观察 1~2 天，再加下一条。
      </div>
      <div class="card">
        <div class="panel-head"><h2>补充规则</h2>
          <button class="primary" id="publish-policy">本地生效（试运行）</button></div>
        <p class="hint">这些规则追加在人格/RULES 之后；冲突时以人格核心为准。这里的按钮只让本地 bot 立即生效，<b>不走 GitHub</b>；正式发布请去 GitHub 页。</p>
        ${draft >= 1 ? `<div class="guide" style="border-color:#e6c9a8;background:#fdf6ea">现在有 <b>${draft}</b> 条未发布规则。先发布观察，再改下一条；后端会阻止同时添加第二条未发布规则。</div>` : ""}
        <form class="form-grid" id="rule-form" style="margin-top:12px">
          <div><label>作用域</label><select name="scope"><option value="group">群聊</option><option value="private">私聊</option><option value="all">全部</option></select></div>
          <div><label>优先级（数字越小越先）</label><input name="priority" type="number" value="100" min="1" max="999"><small class="muted">默认 100；不确定就别改</small></div>
          <div class="wide"><label>规则内容</label><input name="text" placeholder="例如：别人只回哈哈哈、没有新信息时，不接。" maxlength="500" required></div>
          <div class="wide actions"><button class="ghost" type="submit">添加规则</button><span class="hint">只加一条，写清楚适用场景。</span></div>
        </form>
        <table class="table"><thead><tr><th>ID / 范围</th><th>规则</th><th>优先级</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      </div>
      <details class="card panel"><summary><b>内置规则（只读，不能在这里改）</b></summary>
        <p class="hint">这些是 bot.py 里硬编码的规则，包含之前的反馈校准（RULES 22 / 群规）。工作台只能追加补充规则，不能覆盖它们。</p>
        <h4>RULES</h4><pre>${esc(b.rules)}</pre>
        <h4>GROUP_RULES</h4><pre>${esc(b.group)}</pre>
      </details>
      <div class="card panel"><h3>当前发布</h3><p class="hint">policy v${d.release?.policy_version ?? "—"} · ${esc(d.release?.note || "无备注")} · 未发布规则 ${draft}</p></div>`;
    $("#rule-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const f = new FormData(e.target);
      if (!confirm("只改这一条规则吗？\n\n发布后观察 1~2 天，再决定下一条。")) return;
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
      if (!confirm("发布检查：\n1) 这次只改了一个方面？\n2) 写清楚了适用场景？\n3) 想好了怎么回滚？\n\n发布后 bot 下一条消息生效。")) return;
      const note = prompt("发布备注（写一句这次改了什么）：", "规则校准") || "";
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
      <td>${esc(c.summary || "")}</td><td>${esc(c.tags || "")}</td><td>${badge(c.status)}</td>
      <td><button class="mini" data-canon-status="${c.id}" data-status="${c.status === "active" || c.status === "approved" ? "draft" : "approved"}">${c.status === "active" || c.status === "approved" ? "撤回" : "审核通过"}</button></td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">还没有 canon。先录 3~5 条她真的会聊到的作品/概念。</td></tr>`;
    $("#tab-canon").innerHTML = `
      <div class="guide">
        <b>第一次录 Canon：</b>先录 3~5 条她真的会聊到的，不要一次灌 100 条。
        新增是 <b>draft</b>，点“审核通过”后才会进入 GitHub 发布和群聊注入；一次先做一个主题。
      </div>
      <div class="card">
        <div class="panel-head"><h2>Canon 录入</h2><span class="muted">draft → 审核通过 → GitHub 发布 → bot 拉取</span></div>
        <p class="hint">标准字段：类型、标题、来源、一句话摘要、标签。引文核实后再扩字段；现在先保证“有结构”。状态为 draft 的条目不会注入。</p>
        <form id="canon-form" class="form-grid" style="margin-top:12px">
          <div><label>类型</label><select name="type"><option value="work">作品</option><option value="concept">概念</option><option value="person">人物</option><option value="event">事件</option></select></div>
          <div><label>标题</label><input name="title" required maxlength="120" placeholder="《局外人》"></div>
          <div><label>来源</label><input name="source" maxlength="120" placeholder="加缪 / 百度百科"></div>
          <div><label>标签</label><input name="tags" maxlength="200" placeholder="文学,荒诞,存在主义"></div>
          <div class="wide"><label>摘要</label><textarea name="summary" maxlength="1000" placeholder="它讲什么、在什么背景下、最容易误读成什么。"></textarea></div>
          <div class="wide actions"><button class="primary" type="submit">添加 Canon</button></div>
        </form>
        <table class="table" style="margin-top:12px"><thead><tr><th>条目</th><th>摘要</th><th>标签</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      </div>`;
    $$("[data-canon-status]").forEach(b => b.addEventListener("click", async () => {
      try {
        await api("/api/workbench/canon/status", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: Number(b.dataset.canonStatus), status: b.dataset.status }) });
        toast("Canon 状态已更新"); state.loaded.canon = false; await loadCanon();
      } catch (err) { toast("失败：" + err.message); }
    }));
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
      <div class="guide">
        <b>三个东西的关系：</b>
        <b>变更单</b>记录“为什么要改、验收指标、怎么回滚”，本身不生效；
        <b>规则 / Canon</b>是“改什么”；
        <b>GitHub 合并</b>是“正式发布”。
        本地规则页的“本地生效”只是试运行，不走 GitHub。
      </div>
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



async function loadParams() {
  setLoading("#tab-params");
  try {
    const d = await api("/api/workbench/params");
    const sections = Object.entries(d.groups || {}).map(([name, rows]) => `
      <div class="card panel"><h3>${esc(name)}</h3>
        <table class="table"><thead><tr><th>参数</th><th>当前值</th><th>说明</th></tr></thead><tbody>
        ${rows.map(r => `<tr><td><code>${esc(r.key)}</code></td><td><b>${esc(r.value ?? "—")}</b></td><td>${esc(r.desc)}</td></tr>`).join("")}
        </tbody></table></div>`).join("");
    $("#tab-params").innerHTML = `
      <div class="guide">
        <b>怎么改：</b>带 <code>/参数</code> 的键可以在 QQ 私聊里热调，例如 <code>/参数 REPLY_DELAY_MAX=3.2</code>；
        其余改 <code>config/.env</code> 后重启 bot。工作台目前只展示，避免误改。
        <br><b>没有单一“热情值”：</b>热情 = 回复概率 + 主动插话 + 提问补全共同作用，要分开调。
      </div>
      ${sections}
      <div class="card panel"><h3>可直接热调的键</h3><div class="chips">${(d.hot || []).map(k => `<span class="chip">${esc(k)}</span>`).join("")}</div></div>`;
  } catch (e) { $("#tab-params").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

async function loadGithub() {
  setLoading("#tab-github");
  try {
    const d = await api("/api/workbench/github");
    const configured = d.configured;
    const checks = (d.checks || []).map(c => {
      const ok = c.conclusion === "success";
      const bad = c.conclusion && c.conclusion !== "success";
      return `<span class="chip ${ok ? "good" : bad ? "bad" : ""}">${esc(c.name)} · ${esc(c.conclusion || c.status || "pending")}</span>`;
    }).join("") || `<span class="muted">还没有检查</span>`;
    const pr = d.pr;
    const prLine = pr ? `#${pr.number} · ${esc(pr.state || "open")} · ${esc(pr.branch || "")}` : "当前没有待合并 PR";
    const allGreen = (d.checks || []).length > 0 && (d.checks || []).every(c => c.conclusion === "success");
    const canMerge = configured && pr && pr.state === "open" && allGreen;
    $("#tab-github").innerHTML = `
      <div class="grid two">
        <div class="card kpi"><b>${configured ? "已连接" : "未配置"}</b><span>${esc(d.repo || "GH_REPO 未设置")}</span><small>分支 ${esc(d.branch || "main")}</small></div>
        <div class="card kpi"><b>${esc(d.head ? d.head.slice(0, 7) : "—")}</b><span>当前 commit</span><small>${esc(d.message || "")}</small></div>
      </div>
      <div class="guide">
        <b>发布前检查：</b>这次只改一个方面？PR 里只包含一个主题？写清楚了验收指标和回滚方式？
        <br><b>流程：</b>提交 → PR → validate 绿 → 合并并发布 → bot 约 2 分钟内自动拉取。
      </div>
      <div class="card panel">
        <div class="panel-head"><h2>控制面发布</h2><span class="muted">${prLine}</span></div>
        <p class="hint">流程：提交当前 control 到分支 → 创建 PR → GitHub Actions 运行 validate → 检查通过后，点“合并并发布”。合并后以 GitHub 上的 policy 为准写回本地，bot 下一条消息生效。</p>
        <div class="chips" style="margin:10px 0">${checks}</div>
        <div class="actions">
          <button class="ghost" id="gh-refresh">刷新状态</button>
          <button class="ghost" id="gh-pull" ${!configured ? "disabled" : ""}>从 GitHub 拉取</button>
          <button class="primary" id="gh-pr" ${!configured || pr ? "disabled" : ""}>提交并创建 PR</button>
          <button class="primary" id="gh-merge" ${canMerge ? "" : "disabled"}>合并并发布</button>
        </div>
        ${!configured ? `<p class="hint">在 <code>config/.env</code> 配置 <code>GH_REPO</code> 和 <code>GH_TOKEN</code> 后刷新。</p>` : ""}
        ${pr ? `<p class="hint">PR 地址：<a href="${esc(pr.url || "#")}" target="_blank" rel="noreferrer">${esc(pr.url || "")}</a></p>` : ""}
      </div>
      <div class="card panel"><h3>边界</h3><p class="hint">v0.1 只同步 <code>control/policy.json</code> 和 <code>release.json</code>；Canon/评测数据仍在本地。合并前必须 validate 通过；没有通过时按钮不可用。</p></div>`;
    $("#gh-refresh").addEventListener("click", loadGithub);
    $("#gh-pull")?.addEventListener("click", async () => {
      try {
        const r = await api("/api/workbench/github/pull", { method: "POST" });
        toast(`已拉取 policy v${r.policy_version}`); state.loaded.github = false; await loadGithub(); state.loaded.dashboard = false;
      } catch (e) { toast("失败：" + e.message); }
    });
    $("#gh-pr")?.addEventListener("click", async () => {
      if (!confirm("这次 PR 只包含一个方面吗？\n\n一次只改一个变量，才能判断是哪个改动起作用。")) return;
      try {
        await api("/api/workbench/github/pr", { method: "POST" });
        toast("PR 已创建，等待 validate"); state.loaded.github = false; await loadGithub();
      } catch (e) { toast("失败：" + e.message); }
    });
    $("#gh-merge")?.addEventListener("click", async () => {
      if (!confirm("合并 PR 并打 tag？合并后本地 policy 会以 GitHub 为准。")) return;
      try {
        const r = await api("/api/workbench/github/merge", { method: "POST" });
        toast(`已发布 ${r.tag}`); state.loaded.github = false; await loadGithub(); state.loaded.dashboard = false;
      } catch (e) { toast("失败：" + e.message); }
    });
  } catch (e) { $("#tab-github").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
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
