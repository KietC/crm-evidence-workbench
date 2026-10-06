const $ = selector => document.querySelector(selector);
const phaseOrder = ["preflight", "root", "tabs", "mail", "documents", "reconcile"];
const desktop = window.okkiDesktop || null;
let nextCandidate = null;
let nextCandidateLoading = false;
let nextCandidateCheckedAt = 0;

async function request(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) },
    body: options.body ?? (options.method === "POST" ? "{}" : undefined)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}

function setBusy(button, busy, label) {
  if (!button.dataset.original) button.dataset.original = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? label : button.dataset.original;
}

function render(status) {
  const {
    app, browser, job,
    autoNext = { mode: "idle", remainingMs: 0, reason: "" },
    deferredRepairs = { pending: 0, pending_error: 0, pending_warning: 0 }
  } = status;
  $("#runtime").textContent = `采集器 #${app.instanceId || "1"} · Node ${app.node} · PID ${app.pid} · ${app.version}`;
  $("#adapter-id").textContent = `${app.adapterId} · ${app.adapterPath}`;
  $("#browser-badge").textContent = browser.connected ? "内置浏览器已接管" : "正在连接";
  $("#browser-badge").className = `status-badge ${browser.connected ? "online" : "offline"}`;
  $("#customer-title").textContent = browser.companyId ? `客户 ${browser.companyId}` : browser.connected ? "请在右侧打开客户详情" : "等待内置浏览器";
  $("#customer-url").textContent = browser.customerUrl || "右侧会话已持久保存；首次登录后下次自动复用";
  $("#start-capture").disabled = !browser.companyId || job.running;
  $("#launch-browser").disabled = job.running;
  $("#cancel-capture").hidden = !job.running;
  $("#job-headline").textContent = job.headline;
  $("#job-detail").textContent = job.detail;
  $("#progress-number").textContent = `${job.progress}%`;
  $("#progress-bar").style.width = `${job.progress}%`;
  $("#case-path").textContent = job.caseRoot || "尚未建立";
  $("#open-case").disabled = !job.caseRoot;

  const activeIndex = phaseOrder.indexOf(job.phase);
  document.querySelectorAll("#phase-list [data-phase]").forEach(element => {
    const index = phaseOrder.indexOf(element.dataset.phase);
    element.classList.toggle("active", index === activeIndex && job.running);
    element.classList.toggle("done", index < activeIndex || ["complete", "incomplete"].includes(job.phase));
  });

  const metricValues = [
    job.metrics.responses_observed ?? "—",
    `${job.metrics.api_rows_dynamic_mail ?? "—"} / ${job.metrics.mail_details_attempted ?? "—"}`,
    job.metrics.resources_downloaded ?? "—",
    job.metrics.reconciliation_failures ?? "—"
  ];
  document.querySelectorAll("#metrics dd").forEach((node, index) => { node.textContent = metricValues[index]; });
  $("#start-next-customer").disabled = job.running || !nextCandidate;
  const reviewRequired = autoNext.mode === "review_required";
  const countdown = autoNext.mode === "countdown";
  const seconds = Math.max(0, Math.ceil(Number(autoNext.remainingMs || 0) / 1000));
  $("#cancel-auto-next").hidden = !(reviewRequired || countdown);
  $("#cancel-auto-next").textContent = countdown ? `取消自动下一个（${seconds}秒）` : "取消持续采集";
  const autoStatus = $("#auto-next-status");
  autoStatus.className = "auto-next-status";
  if (reviewRequired) autoStatus.textContent = autoNext.reason || "当前客户已停止，等待 Codex 匿名审计。";
  else if (countdown) autoStatus.textContent = `${autoNext.reason || "审计通过"}；剩余 ${seconds} 秒。`;
  else if (autoNext.mode === "capturing") autoStatus.textContent = "自动队列运行中：warning 和 error 都会写入本地延期修复队列，采全后统一处理。";
  else if (autoNext.mode === "opening") autoStatus.textContent = "正在打开下一个客户…";
  else if (autoNext.mode === "stopped_error") {
    autoStatus.textContent = autoNext.reason || "检测到 error，自动队列已停止。";
    autoStatus.classList.add("error");
  } else if (autoNext.mode === "stopped_user") {
    autoStatus.textContent = autoNext.reason || "自动下一个已取消。";
    autoStatus.classList.add("paused");
  } else if (autoNext.mode === "no_candidate") autoStatus.textContent = autoNext.reason || "没有待处理客户。";
  else autoStatus.textContent = "每次终态本地记录后等待15秒继续；采全后统一修复。";
  if (Number(deferredRepairs.pending || 0) > 0) {
    autoStatus.textContent += ` 待统一处理 ${deferredRepairs.pending} 项（error ${deferredRepairs.pending_error}，warning ${deferredRepairs.pending_warning}）。`;
  }
  const alerts = [
    ...job.errors.map(text => `<p class="error">ERROR · ${escapeHtml(text)}</p>`),
    ...job.warnings.map(text => `<p class="warning">WARN · ${escapeHtml(text)}</p>`)
  ];
  $("#alerts").innerHTML = alerts.length ? alerts.join("") : "<p>暂无告警。</p>";
}

async function refreshNextCustomer(status, force = false) {
  if (nextCandidateLoading || status.job.running) return;
  if (!force && Date.now() - nextCandidateCheckedAt < 5_000) return;
  nextCandidateLoading = true;
  nextCandidateCheckedAt = Date.now();
  try {
    const value = await request("/api/next-customer");
    nextCandidate = value.candidate || null;
    $("#next-customer-name").value = nextCandidate?.name || "";
    $("#next-customer-name").placeholder = nextCandidate ? "" : "没有待处理客户";
    $("#next-customer-meta").textContent = nextCandidate
      ? `${nextCandidate.stageName} · 第 ${nextCandidate.page} 页第 ${nextCandidate.row} 位 · cases 已有同名目录或已完整落盘客户自动跳过`
      : "全部阶段均未发现待处理客户。";
  } catch {
    nextCandidate = null;
    $("#next-customer-name").value = "";
    $("#next-customer-name").placeholder = "返回客户列表后自动计算";
    $("#next-customer-meta").textContent = "顺序：客户阶段从下到上，每个阶段内从上到下；cases 已有同名目录或已完整落盘的客户自动跳过。";
  } finally {
    nextCandidateLoading = false;
    $("#start-next-customer").disabled = status.job.running || !nextCandidate;
  }
}

async function refresh() {
  try {
    const status = await request("/api/status");
    render(status);
    await refreshNextCustomer(status);
  }
  catch (error) { $("#alerts").innerHTML = `<p class="error">控制台连接失败 · ${escapeHtml(error.message)}</p>`; }
}

function renderBrowserState(state) {
  if (!state) return;
  $("#browser-address").value = state.url || "";
  $("#browser-back").disabled = !state.canGoBack;
  $("#browser-forward").disabled = !state.canGoForward;
  $("#browser-reload").textContent = state.loading ? "×" : "↻";
}

if (desktop) {
  document.body.classList.add("embedded");
  $("#browser-toolbar").hidden = false;
  $("#resize-handle").hidden = false;
  let sidebarWidth = 500;
  const applyLayout = state => {
    if (!state || typeof state.sidebarWidth !== "number") return;
    sidebarWidth = state.sidebarWidth;
    document.documentElement.style.setProperty("--sidebar-width", `${sidebarWidth}px`);
  };
  desktop.onLayout(applyLayout);
  applyLayout(await desktop.getLayout());
  desktop.onBrowserState(renderBrowserState);
  renderBrowserState(await desktop.getBrowserState());
  $("#browser-back").addEventListener("click", desktop.back);
  $("#browser-forward").addEventListener("click", desktop.forward);
  $("#browser-home").addEventListener("click", desktop.home);
  $("#browser-reload").addEventListener("click", desktop.reload);
  $("#sidebar-smaller").addEventListener("click", () => desktop.setSidebarWidth(sidebarWidth - 60));
  $("#sidebar-larger").addEventListener("click", () => desktop.setSidebarWidth(sidebarWidth + 60));
  const resizeHandle = $("#resize-handle");
  let drag = null;
  resizeHandle.addEventListener("pointerdown", event => {
    drag = { startX: event.clientX, startWidth: sidebarWidth };
    resizeHandle.setPointerCapture(event.pointerId);
    resizeHandle.classList.add("dragging");
    document.body.classList.add("resizing");
  });
  resizeHandle.addEventListener("pointermove", event => {
    if (!drag) return;
    const next = drag.startWidth + event.clientX - drag.startX;
    applyLayout({ sidebarWidth: next });
    desktop.setSidebarWidth(next);
  });
  const stopResize = event => {
    if (!drag) return;
    drag = null;
    if (resizeHandle.hasPointerCapture(event.pointerId)) resizeHandle.releasePointerCapture(event.pointerId);
    resizeHandle.classList.remove("dragging");
    document.body.classList.remove("resizing");
  };
  resizeHandle.addEventListener("pointerup", stopResize);
  resizeHandle.addEventListener("pointercancel", stopResize);
  $("#browser-address-form").addEventListener("submit", event => {
    event.preventDefault();
    desktop.navigate($("#browser-address").value.trim());
  });
  $("#import-cookies").addEventListener("click", async event => {
    const button = event.currentTarget;
    setBusy(button, true, "导入中…");
    try {
      const result = await desktop.importCookies();
      if (result.file) alert(`已从 ${result.file} 导入 ${result.imported} 条，跳过 ${result.skipped} 条。`);
    } catch (error) { alert(error.message); }
    finally { setBusy(button, false, ""); }
  });
}

$("#launch-browser").addEventListener("click", async event => {
  const button = event.currentTarget;
  setBusy(button, true, "正在连接…");
  try {
    if (desktop) desktop.home();
    await request("/api/browser/launch", { method: "POST" });
    await refresh();
  } catch (error) { alert(error.message); }
  finally { setBusy(button, false, ""); }
});

$("#start-capture").addEventListener("click", async event => {
  const button = event.currentTarget;
  setBusy(button, true, "任务已提交…");
  try { await request("/api/capture/start", { method: "POST" }); await refresh(); }
  catch (error) { alert(error.message); }
  finally { setBusy(button, false, ""); }
});

$("#start-next-customer").addEventListener("click", async event => {
  const button = event.currentTarget;
  setBusy(button, true, "正在打开…");
  try {
    await request("/api/next-customer/start", { method: "POST" });
    nextCandidate = null;
    $("#next-customer-name").value = "";
    $("#next-customer-name").placeholder = "正在采集当前客户";
    nextCandidateCheckedAt = 0;
    await refresh();
  } catch (error) { alert(error.message); }
  finally { setBusy(button, false, ""); }
});

$("#cancel-capture").addEventListener("click", async () => {
  if (!confirm("停止后，已经原子落盘的数据仍会保留。确认停止？")) return;
  await request("/api/capture/cancel", { method: "POST" });
  await refresh();
});

$("#cancel-auto-next").addEventListener("click", async () => {
  await request("/api/auto-next/cancel", { method: "POST" });
  await refresh();
});

$("#open-case").addEventListener("click", async () => request("/api/open-case", { method: "POST" }));

await refresh();
setInterval(refresh, 1500);
