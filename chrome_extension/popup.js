const NUM_PRODUCTS = 5;
const $ = (id) => document.getElementById(id);
const rows = [];

function buildRows() {
  const tpl = $("productRow");
  for (let i = 0; i < NUM_PRODUCTS; i++) {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.querySelector(".slot").textContent = i + 1;
    const row = {
      el: node,
      url: node.querySelector(".url"),
      qty: node.querySelector(".qty"),
      max: node.querySelector(".max"),
      status: node.querySelector(".status"),
    };
    row.url.addEventListener("input", () => node.classList.toggle("filled", !!row.url.value.trim()));
    for (const input of [row.url, row.qty, row.max]) input.addEventListener("change", saveConfig);
    rows.push(row);
    $("products").appendChild(node);
  }
  for (const id of ["minInt", "maxInt", "testMode", "ntfyTopic"]) $(id).addEventListener("change", saveConfig);
}

function normalizeUrl(url) {
  const m = url.match(/\/(?:dp|gp\/product|gp\/aw\/d)\/([A-Z0-9]{10})/i);
  return m && /amazon\.com\.mx/i.test(url) ? `https://www.amazon.com.mx/dp/${m[1].toUpperCase()}` : url.trim();
}

function readConfig() {
  return {
    products: rows.map((r) => ({
      url: r.url.value.trim(),
      qty: Math.max(1, parseInt(r.qty.value, 10) || 1),
      maxPrice: r.max.value === "" ? null : Number(r.max.value),
    })),
    minInt: parseInt($("minInt").value, 10),
    maxInt: parseInt($("maxInt").value, 10),
    testMode: $("testMode").checked,
    ntfyTopic: $("ntfyTopic").value.trim(),
  };
}

async function saveConfig() {
  await chrome.storage.local.set({ config: readConfig() });
}

function validate(cfg) {
  const errors = [];
  cfg.products.forEach((p, i) => {
    if (p.url && !/^https:\/\/(www\.)?amazon\.com\.mx\//i.test(p.url)) errors.push(`Product ${i + 1}: not an amazon.com.mx link.`);
    if (p.maxPrice !== null && (isNaN(p.maxPrice) || p.maxPrice < 0)) errors.push(`Product ${i + 1}: max price must be a number.`);
  });
  if (!cfg.products.some((p) => p.url)) errors.push("Add at least one product link.");
  if (!(cfg.minInt >= 5) || !(cfg.maxInt >= cfg.minInt)) errors.push("Interval: minimum 5 sec, and the second value must be ≥ the first.");
  return errors;
}

function showError(msg) {
  $("error").textContent = msg;
  $("error").hidden = !msg;
}

async function start() {
  rows.forEach((r) => { if (r.url.value.trim()) r.url.value = normalizeUrl(r.url.value); });
  const cfg = readConfig();
  const errors = validate(cfg);
  if (errors.length) return showError(errors.join("\n"));
  showError("");
  const count = cfg.products.filter((p) => p.url).length;
  if (!cfg.testMode && !confirm(`The bot will automatically BUY ${count} product(s) as soon as they are in stock, `
      + "using your default address and payment method.\n\nStart now?")) return;
  await chrome.storage.local.set({ config: cfg });
  const res = await chrome.runtime.sendMessage({ type: "start" });
  if (!res?.ok) showError(res?.error || "Couldn't start.");
}

async function stop() {
  await chrome.runtime.sendMessage({ type: "stop" });
}

function render({ config, run, running, log }) {
  const cfg = config || {};
  const testMode = !!cfg.testMode;

  $("state").className = `state ${running ? (testMode ? "test" : "running") : "idle"}`;
  $("stateText").textContent = running ? (testMode ? "Testing" : "Running") : "Idle";
  $("start").disabled = !!running;
  $("stop").disabled = !running;
  for (const r of rows) for (const input of [r.url, r.qty, r.max]) input.disabled = !!running;
  for (const id of ["minInt", "maxInt", "testMode", "ntfyTopic"]) $(id).disabled = !!running;

  rows.forEach((r, i) => {
    const p = run?.products?.find((x) => x.slot === i + 1);
    const status = p && (running || p.done || p.status !== "Starting...") ? p : null;
    r.status.textContent = status ? status.status : "Idle";
    r.status.className = `status ${status ? status.tone : "muted"}`;
    r.status.title = status ? `${status.title || ""}\n${status.status}`.trim() : "";
  });

  const next = run?.nextCheckAt ? Math.round((run.nextCheckAt - Date.now()) / 1000) : 0;
  $("nextCheck").textContent = running && next > 0 ? `next check in ${next}s` : "";

  const list = $("log");
  list.innerHTML = "";
  if (!log?.length) {
    list.innerHTML = '<li class="empty">Nothing yet. Press Start.</li>';
    return;
  }
  for (const entry of log.slice().reverse()) {
    const li = document.createElement("li");
    const time = document.createElement("time");
    time.textContent = new Date(entry.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const span = document.createElement("span");
    span.textContent = entry.msg;
    span.className = entry.level;
    li.append(time, span);
    list.appendChild(li);
  }
}

async function refresh() {
  render(await chrome.storage.local.get(["config", "run", "running", "log"]));
}

async function init() {
  buildRows();
  const { config } = await chrome.storage.local.get("config");
  if (config) {
    config.products?.forEach((p, i) => {
      if (!rows[i]) return;
      rows[i].url.value = p.url || "";
      rows[i].qty.value = p.qty || 1;
      rows[i].max.value = p.maxPrice ?? "";
      rows[i].el.classList.toggle("filled", !!p.url);
    });
    $("minInt").value = config.minInt ?? 10;
    $("maxInt").value = config.maxInt ?? 30;
    $("testMode").checked = !!config.testMode;
    $("ntfyTopic").value = config.ntfyTopic || "";
  }
  $("start").addEventListener("click", start);
  $("stop").addEventListener("click", stop);
  chrome.storage.onChanged.addListener(refresh);
  setInterval(refresh, 1000); // keeps the "next check in" countdown live
  refresh();
}

init();
