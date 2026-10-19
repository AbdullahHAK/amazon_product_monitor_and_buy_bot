// Runs on every amazon.com.mx page. Only acts inside the bot tab while a run is active.
// Each page load executes one step of the state machine stored in chrome.storage.local:
//   check  -> product page: detect stock, then Buy Now / Add to cart / other sellers
//   cart   -> cart page: move other items aside, set exact quantity, proceed to checkout
//   checkout -> checkout pages: decline upsells, continue, place order, confirm
// Selectors come from a live map of amazon.com.mx (match ids/data-attributes, not text where possible).

(() => {
  if (window.top !== window) return; // never run a second copy inside iframes

  const BASE = "https://www.amazon.com.mx";
  const CART_URL = `${BASE}/gp/cart/view.html`;

  const PLACE_ORDER_SELECTORS = [
    "input[name='placeYourOrder1']", "#placeOrder", "#submitOrderButtonId input",
    "#bottomSubmitOrderButtonId input", "#placeYourOrder input", "#turbo-checkout-pyo-button",
  ];
  const CONTINUE_SELECTORS = [
    "[data-testid='bottom-continue-button']", "#checkout-primary-continue-button-id",
    "input[name='proceedToRetailCheckout']", "input[data-testid='Address_selectShipToThisAddress']",
    "#orderSummaryPrimaryActionBtn input", "input[name='ppw-widgetEvent:SetPaymentPlanSelectContinueEvent']",
  ];
  const DECLINE_SELECTORS = [
    "#attachSiNoCoverage", "#prime-interstitial-nothanks-button", "#prime-declineCTA", "a[id*='nothanks' i]",
  ];
  const PLACE_ORDER_RE = /realiza(r)?\s+(el\s+|tu\s+|su\s+)?pedido|place\s+(your\s+)?order|confirmar\s+(la\s+)?compra/i;
  // A "continue" button whose label looks like a purchase button is treated as the final step.
  const FINAL_BUTTON_RE = /pedido|comprar|pagar\s+ahora|place|buy|confirm/i;
  const DECLINE_RE = /^\s*no,?\s+(gracias|thanks)\s*$/i;
  const SUCCESS_RE = /pedido\s+(ha\s+sido\s+)?realizado|se\s+realiz[oó]\s+(tu|su)\s+pedido|gracias,?\s+(tu|su)\s+pedido|order\s+(has\s+been\s+)?placed/i;

  // --- helpers -------------------------------------------------------------------------

  let active = true;
  chrome.storage.onChanged.addListener((ch) => {
    if (ch.running && !ch.running.newValue) active = false;
  });

  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const visible = (el) => !!el && el.getClientRects().length > 0;
  const isDisabled = (el) => el.disabled || el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
  const text = (el) => (el?.textContent || "").replace(/\s+/g, " ").trim();
  const rand = (a, b) => a + Math.random() * (b - a);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const money = (n) => (n == null ? "price unknown" : `$${n.toLocaleString("en-US", { minimumFractionDigits: 2 })}`);

  async function wait(ms) { // cancellable sleep
    const end = Date.now() + ms;
    while (active && Date.now() < end) await sleep(Math.min(500, end - Date.now()));
    return active;
  }

  async function waitFor(fn, ms = 10000) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const v = fn();
      if (v) return v;
      await sleep(250);
    }
    return null;
  }

  function parsePrice(s) {
    const m = (s || "").match(/\d[\d,]*(\.\d+)?/);
    return m ? parseFloat(m[0].replace(/,/g, "")) : null;
  }

  function label(el) {
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    const byId = ids.map((id) => text(document.getElementById(id))).join(" ");
    return [el.value, el.getAttribute("aria-label"), byId, text(el.closest(".a-button") || el)].filter(Boolean).join(" ");
  }

  const getRun = async () => (await chrome.storage.local.get("run")).run;
  const getConfig = async () => (await chrome.storage.local.get("config")).config || {};

  async function setRun(patch) {
    const run = await getRun();
    Object.assign(run, patch);
    await chrome.storage.local.set({ run });
    return run;
  }

  async function setProduct(i, patch) {
    const run = await getRun();
    Object.assign(run.products[i], patch);
    await chrome.storage.local.set({ run });
    return run;
  }

  async function log(msg, level = "info") {
    const { log = [] } = await chrome.storage.local.get("log");
    log.push({ t: Date.now(), msg, level });
    await chrome.storage.local.set({ log: log.slice(-150) });
  }

  const notify = (title, message, focus = true) => chrome.runtime.sendMessage({ type: "notify", title, message, focus });
  const name = (p) => `#${p.slot} ${(p.title || p.url).slice(0, 60)}`;

  // --- human verification (never automated: the user completes it) ---------------------

  function humanNeeded() {
    if (/^\/(ap|ax)\//.test(location.pathname)) return "Amazon is asking you to sign in or verify (OTP).";
    if ($("form[action*='validateCaptcha']")) return "Amazon is showing a CAPTCHA.";
    return null;
  }

  async function handleHuman(run) {
    const reason = humanNeeded();
    if (!reason) {
      if (run.alerted) {
        await setRun({ alerted: false });
        await log("Verification completed, continuing.");
      }
      return false;
    }
    // Tick "Mantener la sesión iniciada" so the login survives a browser restart.
    const remember = $("input[name='rememberMe']");
    if (remember && !remember.checked) remember.click();
    if (!run.alerted) {
      await setRun({ alerted: true });
      await log(`ACTION NEEDED: ${reason} Complete it in the bot tab; the bot continues by itself.`, "alert");
      notify("Action needed", `${reason} Complete it in the Amazon tab and the bot will continue.`);
    }
    return true; // the page navigates once the user is done, and this script runs again
  }

  function loggedIn() {
    const t = text($("#nav-link-accountList-nav-line-1")).toLowerCase();
    return !!t && !t.includes("identif") && !t.includes("sign in");
  }

  // --- flow control --------------------------------------------------------------------

  async function goNext() {
    const run = await getRun();
    const n = run.products.length;
    let next = -1;
    for (let k = 1; k <= n; k++) {
      const j = (run.idx + k) % n;
      if (!run.products[j].done) { next = j; break; }
    }
    if (next === -1) {
      await log("All products are finished.");
      notify("Amazon bot finished", "All products are finished.", false);
      chrome.runtime.sendMessage({ type: "stop", reason: "Bot stopped." });
      return;
    }
    const cfg = await getConfig();
    const delay = next <= run.idx ? rand(cfg.minInt || 10, cfg.maxInt || 30) * 1000 : rand(1000, 2500);
    await setRun({ idx: next, nav: next, phase: "check", asin: null, nextCheckAt: Date.now() + delay });
    if (await wait(delay)) location.href = run.products[next].url;
  }

  async function finish(i, status, tone) {
    await setProduct(i, { done: true, status, tone });
    await setRun({ phase: "check", orderClickedAt: 0 });
    await goNext();
  }

  async function fail(i, why) {
    const run = await getRun();
    const p = run.products[i];
    await setProduct(i, { failures: p.failures + 1, status: `Checkout failed (try ${p.failures + 1}) - retrying`, tone: "bad" });
    await setRun({ phase: "check", orderClickedAt: 0 });
    await log(`${name(p)}: ${why}`, "error");
    await goNext();
  }

  // --- product page --------------------------------------------------------------------

  function readPrice() {
    const hidden = $("#addToCart input[name='items[0.base][customerVisiblePrice][amount]']");
    if (hidden?.value) return parseFloat(hidden.value);
    const off = parsePrice(text($("#corePrice_feature_div .a-offscreen")));
    if (off) return off;
    const whole = text($(".priceToPay .a-price-whole")).replace(/[.,]$/, "");
    return whole ? parsePrice(`${whole}.${text($(".priceToPay .a-price-fraction")) || "00"}`) : null;
  }

  function isDigital() {
    return !!$("#dp.ebooks, #tmm-grid-swatch-KINDLE, #kindleALC_SOD_Desktop, #one-click-button, form#buyOneClick");
  }

  function setQuantity(qty) {
    const sel = $("select#quantity");
    if (!sel || qty <= 1) return qty <= 1 ? 1 : null;
    const nums = [...sel.options].map((o) => parseInt(o.value, 10)).filter((n) => !isNaN(n));
    let target = qty;
    if (!nums.includes(qty)) target = Math.max(...nums.filter((n) => n <= qty), Math.min(...nums));
    sel.value = String(target);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return target;
  }

  function priceOk(p, price) {
    return p.maxPrice == null || p.maxPrice === "" || (price != null && price <= Number(p.maxPrice));
  }

  async function announce(i, p, price, source) {
    await setProduct(i, { status: `IN STOCK (${money(price)}) - buying...`, tone: "info" });
    await log(`${name(p)} is IN STOCK at ${money(price)}${source}. Starting checkout (qty ${p.qty}).`);
  }

  async function checkProduct(run) {
    const i = run.idx;
    const p = run.products[i];
    if (run.nav !== i || !/\/(dp|gp\/product)\//.test(location.pathname)) {
      await setRun({ nav: i });
      location.href = p.url;
      return;
    }
    await setRun({ nav: -1 });
    await setProduct(i, { status: "Checking...", tone: "muted" });
    await waitFor(() => $("#productTitle") || $("#outOfStock") || $("#buybox"), 10000);

    if (!loggedIn()) {
      await log("Please sign in to Amazon in the bot tab (only needed once).", "alert");
      notify("Sign in needed", "Sign in to Amazon in the bot tab. The bot continues automatically.");
      await setProduct(i, { status: "Waiting for sign-in", tone: "warn" });
      // #nav-link-accountList is a div; the real sign-in link (returning to this product) is inside it.
      const signIn = $("a[href*='/ap/signin']");
      if (signIn) location.href = signIn.href;
      return;
    }
    if (!p.title) {
      const t = text($("#productTitle"));
      if (t) { await setProduct(i, { title: t }); p.title = t; await log(`Product #${p.slot}: ${t}`); }
    }
    if (isDigital()) return finish(i, "Digital / 1-Click product - skipped", "bad");

    const atc = $("#add-to-cart-button");
    const buyNow = $("#buy-now-button");
    const avail = text($("#availability"));
    const stamp = new Date().toLocaleTimeString();

    if ((atc || buyNow) && !$("#outOfStock")) {
      if (/^no disponible/i.test(avail)) {
        await setProduct(i, { status: `Back-order only (checked ${stamp})`, tone: "muted" });
        return goNext();
      }
      if (!atc && /reserva/i.test(label(buyNow))) {
        await setProduct(i, { status: `Pre-order only (checked ${stamp})`, tone: "muted" });
        return goNext();
      }
      const price = readPrice();
      if (!priceOk(p, price)) {
        await setProduct(i, { status: `In stock at ${money(price)} - above max price`, tone: "warn" });
        return goNext();
      }
      await announce(i, p, price, "");
      const set = setQuantity(p.qty);
      if (set !== p.qty) await log(`${name(p)}: quantity ${p.qty} not available, buying ${set ?? 1}.`, "error");
      await sleep(800);
      await setRun({ phase: buyNow ? "checkout" : "cart", phaseStarted: Date.now(), asin: $("input#ASIN")?.value || null });
      notify("Product in stock", `${p.title || p.url} - buying now.`, false);
      const before = location.href;
      (buyNow || atc).click();
      // Buy Now can open the turbo-checkout pop-up on this same page instead of navigating.
      await sleep(4000);
      if (buyNow && location.href === before) return doCheckout(await getRun());
      return;
    }

    const opener = $("#buybox-see-all-buying-choices a");
    if (opener) return buyFromOtherSellers(i, p, opener, stamp);

    await setProduct(i, { status: `Out of stock (checked ${stamp})`, tone: "muted" });
    return goNext();
  }

  async function buyFromOtherSellers(i, p, opener, stamp) {
    opener.click();
    const found = await waitFor(() => $$("[id='aod-offer'], #aod-pinned-offer")
      .some((o) => o.querySelector("input[name='submit.addToCart']")), 10000);
    if (!found) {
      await setProduct(i, { status: `Other sellers: none in stock (checked ${stamp})`, tone: "muted" });
      return goNext();
    }
    await sleep(800);
    const offers = $$("#aod-pinned-offer, [id='aod-offer']").map((o) => {
      const btn = o.querySelector("input[name='submit.addToCart']");
      const priceBox = o.querySelector("[id^='aod-price-']");
      // Price-to-pay only, never the crossed-out list price (.a-text-price).
      const priceText = text(priceBox?.querySelector(".apex-pricetopay-accessibility-label"))
        || text(priceBox?.querySelector(".a-price:not(.a-text-price)"))
        || text(priceBox?.querySelector(".aok-offscreen"))
        || `${text(priceBox?.querySelector(".a-price-whole")).replace(/[.,]$/, "")}.${text(priceBox?.querySelector(".a-price-fraction")) || "00"}`;
      return {
        btn, price: parsePrice(priceText),
        condition: text(o.querySelector("#aod-offer-heading")),
        asin: o.querySelector("input[name='items[0.base][asin]']")?.value || null,
      };
    }).filter((o) => o.btn && visible(o.btn) && (!o.condition || /nuevo|new/i.test(o.condition)));

    const allowed = offers.filter((o) => priceOk(p, o.price))
      .sort((a, b) => (a.price ?? Infinity) - (b.price ?? Infinity));
    if (!offers.length) {
      await setProduct(i, { status: `Other sellers: no new offers (checked ${stamp})`, tone: "muted" });
      return goNext();
    }
    if (!allowed.length) {
      await setProduct(i, { status: `Other sellers from ${money(Math.min(...offers.map((o) => o.price ?? Infinity)))} - above max price`, tone: "warn" });
      return goNext();
    }
    const best = allowed[0];
    await announce(i, p, best.price, " from another seller");
    await setRun({ phase: "cart", phaseStarted: Date.now(), asin: best.asin || $("input#ASIN")?.value || null });
    notify("Product in stock", `${p.title || p.url} - buying from another seller.`, false);
    best.btn.click();
    await sleep(2500);
    location.href = CART_URL;
  }

  // --- cart ----------------------------------------------------------------------------

  async function doCart(run) {
    const i = run.idx;
    const p = run.products[i];
    if (!/\/(gp\/cart\/view\.html|cart)$/.test(location.pathname)) {
      location.href = CART_URL;
      return;
    }
    await waitFor(() => $("#sc-active-cart"), 10000);
    const rows = () => $$("#sc-active-cart .sc-list-item[data-itemtype='active'][data-asin]");
    let items = rows();
    if (!items.length) return fail(i, "the product didn't appear in the cart.");

    let target = run.asin;
    if (!target && items.length === 1) target = items[0].dataset.asin;
    if (!target) return fail(i, "cart has other items and the product ID is unknown; not checking out.");

    // Only the monitored product may be bought: move everything else to "Guardar para más tarde".
    const other = items.find((r) => r.dataset.asin !== target);
    if (other) {
      const sfl = other.querySelector("input[data-action='save-for-later']");
      if (!sfl) return fail(i, "couldn't move other cart items aside (it would buy the whole cart).");
      sfl.click();
      await log(`Moved "${(other.dataset.producttitle || other.dataset.asin).slice(0, 40)}" to 'Guardar para más tarde'.`);
      await sleep(2500);
      location.reload(); // moved rows stay as placeholders until the cart reloads
      return;
    }
    const row = () => rows().find((r) => r.dataset.asin === target);
    if (!row()) return fail(i, "the product isn't in the cart.");

    // Exact quantity (a retry may have added the product twice).
    for (let n = 0; n < 60 && active; n++) {
      const cur = parseInt(row()?.dataset.quantity || "1", 10);
      if (cur === p.qty) break;
      const btn = row()?.querySelector(`button[data-a-selector='${cur < p.qty ? "increment" : "decrement"}']`);
      if (!btn || isDisabled(btn)) {
        if (cur > p.qty) return fail(i, `cart has ${cur} units but ${p.qty} were requested; not checking out.`);
        await log(`${name(p)}: couldn't raise quantity to ${p.qty}, buying ${cur}.`, "error");
        break;
      }
      btn.click();
      await sleep(1800);
    }
    if (!active) return;
    const ptc = $("input[name='proceedToRetailCheckout']");
    if (!ptc) return fail(i, "no 'Proceder al pago' button in the cart.");
    await setRun({ phase: "checkout", phaseStarted: Date.now() });
    ptc.click();
  }

  // --- checkout ------------------------------------------------------------------------

  function orderConfirmed() {
    return /thankyou/i.test(location.href) || SUCCESS_RE.test((document.body?.innerText || "").slice(0, 6000));
  }

  function findPlaceOrder() {
    const turbo = $("#turbo-checkout-iframe")?.contentDocument?.querySelector("#turbo-checkout-pyo-button");
    if (visible(turbo)) return turbo;
    for (const s of PLACE_ORDER_SELECTORS) {
      const el = $(s);
      if (visible(el) && !isDisabled(el)) return el;
    }
    return $$("input[type='submit'], button").find((el) => visible(el) && !isDisabled(el) && PLACE_ORDER_RE.test(label(el))) || null;
  }

  function findDecline() {
    for (const s of DECLINE_SELECTORS) {
      const el = $(s);
      if (visible(el)) return el;
    }
    return $$("a, button, input[type='submit'], span.a-button-text").find((el) => visible(el) && DECLINE_RE.test(label(el))) || null;
  }

  function findContinue() {
    for (const s of CONTINUE_SELECTORS) {
      const el = $(s);
      if (visible(el)) return el;
    }
    return null;
  }

  // Imported items add a customs step (/kyc): "Agregar RFC o CURP para despacho de aduana".
  // Choose "Omitir por ahora" (skip for now) and press that section's own Continuar button.
  async function handleCustomsStep() {
    const skip = $$("label, span.a-label, span.a-radio-label").find((el) => visible(el) && /^\s*(omitir por ahora|skip for now)\s*$/i.test(text(el)));
    if (!skip) return false;
    const radio = skip.closest("label")?.querySelector("input[type='radio']")
      || skip.closest(".a-radio")?.querySelector("input[type='radio']");
    if (radio && !radio.checked) {
      skip.click();
      await log("Customs step (RFC/CURP): choosing 'Omitir por ahora'.");
      await sleep(1000);
    }
    const section = skip.closest("form") || skip.closest(".a-box, section, [data-testid]") || document;
    const isContinue = (el) => visible(el) && !isDisabled(el) && /continuar|continue/i.test(label(el));
    const btn = $$("input[type='submit'], button", section).find(isContinue)
      || $$("input[type='submit'], button").filter(isContinue).pop(); // the section's button comes after the summary's
    if (!btn) return false;
    btn.click();
    await sleep(2500);
    return true;
  }

  async function doCheckout(run) {
    const i = run.idx;
    const p = run.products[i];
    const cfg = await getConfig();

    while (active) {
      run = await getRun();
      if (humanNeeded()) return handleHuman(run);
      if (orderConfirmed()) {
        await log(`ORDER PLACED for ${name(p)}.`, "success");
        notify("Order placed!", p.title || p.url);
        return finish(i, "PURCHASED ✓", "good");
      }
      if (run.orderClickedAt) {
        if (Date.now() - run.orderClickedAt > 60000) {
          // Never retry an order that may have gone through: it could be bought twice.
          await log(`${name(p)}: order submitted but no confirmation seen. Check 'Mis pedidos'.`, "alert");
          notify("Check your orders", `${p.title || p.url}: order submitted but not confirmed. Check 'Mis pedidos'.`);
          return finish(i, "Order sent - verify in 'Mis pedidos'", "warn");
        }
        await sleep(1000);
        continue;
      }
      if (Date.now() - run.phaseStarted > 120000) return fail(i, "checkout did not complete within 2 minutes.");

      let place = findPlaceOrder();
      const cont = place ? null : findContinue();
      if (!place && cont && FINAL_BUTTON_RE.test(label(cont))) place = cont;

      if (place) {
        if (cfg.testMode) {
          await log(`TEST MODE: ${name(p)} reached the final 'Place order' button. Order NOT placed.`, "success");
          return finish(i, "TEST OK - reached 'Place order'", "good");
        }
        await setRun({ orderClickedAt: Date.now() });
        await log("Clicked 'Place order', waiting for Amazon's confirmation...");
        place.click();
        continue;
      }
      const decline = findDecline();
      if (decline) {
        decline.click();
        await sleep(1500);
        continue;
      }
      if (await handleCustomsStep()) continue;
      if (cont) {
        if (isDisabled(cont)) {
          await log(`${name(p)}: checkout is blocked - the account has no usable payment method. Add a card in Amazon.`, "alert");
          notify("Checkout blocked", "Amazon won't continue: add a valid payment method to the account.");
          return finish(i, "Blocked: add a payment method", "bad");
        }
        cont.click();
        await sleep(2000);
        continue;
      }
      await sleep(1000);
    }
  }

  // --- entry point ---------------------------------------------------------------------

  (async () => {
    const { running, run } = await chrome.storage.local.get(["running", "run"]);
    if (!running || !run) return;
    const me = await chrome.runtime.sendMessage({ type: "whoami" });
    if (me?.tabId !== run.tabId) return;
    if (await handleHuman(run)) return;

    // Back from a login / verification page: resume where the run left off.
    if (run.phase === "check") return checkProduct(run);
    if (run.phase === "cart") return /\/cart\/smart-wagon/.test(location.pathname) ? (location.href = CART_URL) : doCart(run);
    if (run.phase === "checkout") return doCheckout(run);
  })().catch((e) => log(`Unexpected error: ${e.message}`, "error"));
})();
