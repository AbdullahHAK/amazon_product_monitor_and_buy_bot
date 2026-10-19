// Service worker: starts/stops a run, owns the bot tab, shows notifications.
// The buying logic itself lives in content.js, which runs inside the bot tab.

async function log(msg, level = "info") {
  const { log = [] } = await chrome.storage.local.get("log");
  log.push({ t: Date.now(), msg, level });
  await chrome.storage.local.set({ log: log.slice(-150) });
}

async function start() {
  const { config } = await chrome.storage.local.get("config");
  const products = (config?.products || [])
    .map((p, i) => ({ ...p, slot: i + 1 }))
    .filter((p) => p.url)
    .map((p) => ({ slot: p.slot, url: p.url, qty: p.qty, maxPrice: p.maxPrice,
                   title: null, status: "Starting...", tone: "muted", done: false, failures: 0 }));
  if (!products.length) return { ok: false, error: "Add at least one product link." };

  // Open the tab blank first so the run knows its tabId before any Amazon page loads.
  const tab = await chrome.tabs.create({ url: "about:blank", active: true });
  const run = {
    tabId: tab.id, products, idx: 0, phase: "check", nav: 0,
    phaseStarted: 0, orderClickedAt: 0, asin: null, alerted: false, nextCheckAt: 0,
  };
  await chrome.storage.local.set({ run, running: true, log: [] });
  await log(`Started for ${products.length} product(s)${config.testMode ? " in TEST MODE" : ""}.`);
  await chrome.tabs.update(tab.id, { url: products[0].url });
  return { ok: true };
}

async function stop(reason = "Stopped.") {
  const { running } = await chrome.storage.local.get("running");
  if (!running) return;
  await chrome.storage.local.set({ running: false });
  await log(reason);
}

async function notify(title, message, tabId) {
  chrome.notifications.create({
    type: "basic", iconUrl: "icon.png", title, message, priority: 2, requireInteraction: true,
  });
  if (tabId) {
    chrome.tabs.update(tabId, { active: true });
    chrome.tabs.get(tabId, (t) => t && chrome.windows.update(t.windowId, { focused: true }));
  }
  // Optional phone push (https://ntfy.sh) so the user is alerted even away from the PC.
  const { config } = await chrome.storage.local.get("config");
  const topic = (config?.ntfyTopic || "").trim();
  if (topic) {
    fetch(`https://ntfy.sh/${encodeURIComponent(topic)}`, {
      method: "POST", body: message, headers: { Title: title, Priority: "high" },
    }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  switch (msg.type) {
    case "whoami":
      sendResponse({ tabId: sender.tab?.id });
      return false;
    case "start":
      start().then(sendResponse);
      return true;
    case "stop":
      stop(msg.reason).then(() => sendResponse({ ok: true }));
      return true;
    case "notify":
      notify(msg.title, msg.message, msg.focus ? sender.tab?.id : null);
      sendResponse({ ok: true });
      return false;
  }
  return false;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const { run, running } = await chrome.storage.local.get(["run", "running"]);
  if (running && run?.tabId === tabId) await stop("The bot tab was closed, so the bot stopped.");
});
