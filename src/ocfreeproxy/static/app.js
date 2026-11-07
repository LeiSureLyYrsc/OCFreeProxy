const $ = (sel) => document.querySelector(sel);

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function fmtTime(ts) {
  if (!ts) return "—";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch { return "—"; }
}

function fmtRemain(sec) {
  if (!sec || sec <= 0) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

async function refreshStatus() {
  const st = await api("/api/status");
  $("#useDirect").checked = !!st.use_direct;
  $("#upstreamLabel").textContent = st.upstream_base_url;
  $("#statActive").textContent = st.pool.active_count;
  $("#statCooling").textContent = st.pool.cooling_count;
  $("#poolSummary").textContent = `可用 ${st.pool.active_count} / 冷却 ${st.pool.cooling_count}`;

  const subsBody = $("#subsBody");
  if (!st.subscriptions.length) {
    subsBody.innerHTML = `<tr><td colspan="5" class="muted">暂无订阅</td></tr>`;
  } else {
    subsBody.innerHTML = st.subscriptions.map((s) => `
      <tr>
        <td>${escapeHtml(s.name)}</td>
        <td>${escapeHtml(s.kind)}</td>
        <td>${fmtTime(s.last_refresh_at)}</td>
        <td class="muted">${escapeHtml(s.last_error || "—")}</td>
        <td>
          <button class="btn small" data-refresh-sub="${s.id}">刷新</button>
          <button class="btn small danger" data-del-sub="${s.id}">删除</button>
        </td>
      </tr>`).join("");
  }

  const proxies = st.pool.proxies || [];
  const proxiesBody = $("#proxiesBody");
  if (!proxies.length) {
    proxiesBody.innerHTML = `<tr><td colspan="6" class="muted">暂无代理</td></tr>`;
  } else {
    proxiesBody.innerHTML = proxies.map((p) => `
      <tr>
        <td>${escapeHtml(p.name)}<div class="muted" style="font-size:.75rem">${escapeHtml(p.id)}</div></td>
        <td>${escapeHtml(p.protocol)}</td>
        <td>${escapeHtml(p.source)}</td>
        <td class="state-${p.state}">${escapeHtml(p.state)}${p.cooldown_reason ? ` · ${escapeHtml(p.cooldown_reason)}` : ""}</td>
        <td>${fmtRemain(p.cooldown_remaining)}</td>
        <td>
          ${p.id === "direct" ? "" : `
            <button class="btn small" data-toggle-proxy="${p.id}" data-enabled="${p.enabled ? 0 : 1}">${p.enabled ? "禁用" : "启用"}</button>
            <button class="btn small" data-clear-cd="${p.id}">清冷却</button>
            <button class="btn small danger" data-del-proxy="${p.id}">删除</button>
          `}
          ${p.id === "direct" ? `<button class="btn small" data-clear-cd="${p.id}">清冷却</button>` : ""}
        </td>
      </tr>`).join("");
  }
}

async function refreshModels(force = false) {
  const data = await api(`/api/models?refresh=${force ? "true" : "false"}`);
  $("#statModels").textContent = data.count || (data.data || []).length;
  const body = $("#modelsBody");
  const rows = data.data || [];
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="4" class="muted">未获取到免费模型</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((m) => {
    const ctx = (m.limit && (m.limit.context || m.limit.input)) || "—";
    return `<tr>
      <td><code>${escapeHtml(m.id)}</code></td>
      <td>${escapeHtml(m.name || m.id)}</td>
      <td>${escapeHtml(String(ctx))}</td>
      <td class="muted">${escapeHtml(m.description || "")}</td>
    </tr>`;
  }).join("");
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

$("#useDirect").addEventListener("change", async (e) => {
  try {
    await api("/api/settings/use_direct", {
      method: "POST",
      body: JSON.stringify({ enabled: e.target.checked }),
    });
    toast(e.target.checked ? "已开启本地连接（进入代理池）" : "已关闭本地连接");
    await refreshStatus();
  } catch (err) {
    toast(err.message, true);
    e.target.checked = !e.target.checked;
  }
});

$("#formUpstream").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const data = await api("/api/settings/upstream", {
      method: "POST",
      body: JSON.stringify({ upstream_base_url: fd.get("upstream_base_url") }),
    });
    toast("上游已保存");
    $("#upstreamLabel").textContent = data.upstream_base_url;
  } catch (err) { toast(err.message, true); }
});

$("#formClash").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const data = await api("/api/subscriptions/clash", {
      method: "POST",
      body: JSON.stringify({ name: fd.get("name"), url: fd.get("url") }),
    });
    toast(data.refresh?.ok ? `Clash 导入成功：可用 ${data.refresh.summary?.usable ?? "?"}` : `导入完成但刷新失败：${data.refresh?.error || ""}`);
    e.target.reset();
    await refreshStatus();
  } catch (err) { toast(err.message, true); }
});

$("#formPaste").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const data = await api("/api/subscriptions/paste", {
      method: "POST",
      body: JSON.stringify({ name: fd.get("name"), content: fd.get("content") }),
    });
    if (data.mode === "proxies") {
      toast(`已加入代理池：${data.imported} 个 HTTP/SOCKS5`);
    } else {
      toast(data.refresh?.ok ? `粘贴导入成功：可用 ${data.refresh.summary?.usable ?? "?"}` : `导入完成但解析失败：${data.refresh?.error || ""}`);
    }
    e.target.reset();
    await refreshStatus();
  } catch (err) { toast(err.message, true); }
});

$("#formManual").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    await api("/api/proxies", {
      method: "POST",
      body: JSON.stringify({ name: fd.get("name"), proxy_url: fd.get("proxy_url") }),
    });
    toast("已添加代理");
    e.target.reset();
    await refreshStatus();
  } catch (err) { toast(err.message, true); }
});

document.body.addEventListener("click", async (e) => {
  const t = e.target;
  if (!(t instanceof HTMLElement)) return;
  try {
    if (t.dataset.refreshSub) {
      const r = await api(`/api/subscriptions/${t.dataset.refreshSub}/refresh`, { method: "POST" });
      toast(r.ok ? `刷新成功：可用 ${r.summary?.usable ?? 0}` : `失败：${r.error}`);
      await refreshStatus();
    } else if (t.dataset.delSub) {
      if (!confirm("删除该订阅及其导入的代理？")) return;
      await api(`/api/subscriptions/${t.dataset.delSub}`, { method: "DELETE" });
      toast("已删除订阅");
      await refreshStatus();
    } else if (t.dataset.delProxy) {
      await api(`/api/proxies/${t.dataset.delProxy}`, { method: "DELETE" });
      toast("已删除代理");
      await refreshStatus();
    } else if (t.dataset.toggleProxy) {
      await api(`/api/proxies/${t.dataset.toggleProxy}/enable?enabled=${t.dataset.enabled === "1"}`, { method: "POST" });
      await refreshStatus();
    } else if (t.dataset.clearCd) {
      await api(`/api/proxies/${t.dataset.clearCd}/clear_cooldown`, { method: "POST" });
      toast("已清除冷却");
      await refreshStatus();
    }
  } catch (err) { toast(err.message, true); }
});

$("#btnRefreshAll").addEventListener("click", async () => {
  try {
    await Promise.all([refreshStatus(), refreshModels(false)]);
    toast("已刷新");
  } catch (err) { toast(err.message, true); }
});

$("#btnRefreshModels").addEventListener("click", async () => {
  try {
    await refreshModels(true);
    toast("模型列表已更新");
  } catch (err) { toast(err.message, true); }
});

(async () => {
  try {
    await refreshStatus();
    await refreshModels(false);
  } catch (err) {
    toast(err.message, true);
  }
})();
