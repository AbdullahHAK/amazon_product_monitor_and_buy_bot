"""Amazon.com.mx stock monitor + automatic checkout engine.

Runs in a background thread and drives the user's own Microsoft Edge / Google Chrome
through Playwright, using a persistent browser profile so the Amazon login is remembered.
All communication with the GUI happens through the `emit` callback.
"""

import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.amazon.com.mx"

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AmazonMXBot"
PROFILE_DIR = APP_DIR / "browser_profile"
LOG_DIR = APP_DIR / "logs"
SCREENSHOT_DIR = LOG_DIR / "screenshots"
SETTINGS_FILE = APP_DIR / "settings.json"

for _d in (PROFILE_DIR, SCREENSHOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("amazon_bot")
if not logger.handlers:
    _fh = logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_fh)
    logger.setLevel(logging.INFO)

# --- Page selectors -------------------------------------------------------------------

BUY_NOW_SELECTORS = ["#buy-now-button", "input[name='submit.buy-now']"]
ADD_TO_CART_SELECTORS = ["#add-to-cart-button", "input[name='submit.add-to-cart']"]

PRICE_SELECTORS = [
    "#corePrice_feature_div .a-price .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
]

PLACE_ORDER_SELECTORS = [
    "input[name='placeYourOrder1']",
    "#placeYourOrder input",
    "#submitOrderButtonId input",
    "#bottomSubmitOrderButtonId input",
    "#placeOrder",
]
PLACE_ORDER_RE = re.compile(
    r"realiza(r)?\s+(el\s+|tu\s+|su\s+)?pedido|place\s+your\s+order|confirmar\s+(la\s+)?compra", re.I
)
# Any checkout button whose label matches this is treated as the final purchase button.
FINAL_BUTTON_RE = re.compile(r"pedido|comprar|pagar\s+ahora|place|buy|confirm", re.I)

# Buttons that move checkout forward (cart -> checkout, address, payment method).
CONTINUE_SELECTORS = [
    "input[name='proceedToRetailCheckout']",
    "#sc-buy-box-ptc-button input",
    "#hlb-ptc-btn-native",
    "#attach-sidesheet-checkout-button input",
    "input[data-testid='Address_selectShipToThisAddress']",
    "#shipToThisAddressButton input",
    "input[name='ppw-widgetEvent:SetPaymentPlanSelectContinueEvent']",
    "#orderSummaryPrimaryActionBtn input",
]

# Upsell pop-ups (protection plans, Prime trial) that must be declined.
DECLINE_SELECTORS = [
    "#attachSiNoCoverage",
    "#siNoCoverage-announce",
    "#prime-interstitial-nothanks-button",
    "#prime-declineCTA",
    "a[id*='nothanks' i]",
]
DECLINE_RE = re.compile(r"^\s*no,?\s+gracias|^\s*no\s+thanks", re.I)

SUCCESS_RE = re.compile(
    r"pedido\s+(ha\s+sido\s+)?realizado|se\s+realiz[oó]\s+(tu|su)\s+pedido|gracias,?\s+(tu|su)\s+pedido|order\s+placed",
    re.I,
)

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", re.I)


class BrowserClosed(Exception):
    pass


@dataclass
class Product:
    slot: int
    url: str
    quantity: int = 1
    max_price: float | None = None
    title: str | None = None
    done: bool = False
    failures: int = field(default=0)

    @property
    def label(self) -> str:
        name = self.title or self.url
        return f"#{self.slot} {name[:60]}"


def normalize_url(url: str) -> str:
    """Turn any Amazon MX product link into a clean https://www.amazon.com.mx/dp/ASIN link."""
    url = url.strip()
    m = ASIN_RE.search(url)
    if m and "amazon.com.mx" in url.lower():
        return f"{BASE_URL}/dp/{m.group(1).upper()}"
    return url


def is_valid_product_url(url: str) -> bool:
    u = url.lower()
    return u.startswith("http") and ("amazon.com.mx" in u or "amzn.to" in u or "a.co/" in u)


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        logger.exception("Could not save settings")


def first_visible(scope, selectors):
    """Return the first visible element matching any selector, without waiting."""
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc
        except PWError:
            continue
    return None


class AmazonBot(threading.Thread):
    def __init__(self, products, emit, min_interval=10, max_interval=30, test_mode=False):
        super().__init__(daemon=True)
        self.products: list[Product] = products
        self.emit = emit
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.test_mode = test_mode
        self._stop_evt = threading.Event()

    # --- GUI communication ------------------------------------------------------------

    def log(self, msg, level="info"):
        getattr(logger, "error" if level == "error" else "info")(msg)
        self.emit({"type": "log", "msg": msg, "level": level})

    def status(self, p: Product, text, color="black"):
        self.emit({"type": "status", "slot": p.slot, "text": text, "color": color})

    def alert(self, msg):
        logger.warning(msg)
        self.emit({"type": "alert", "msg": msg})

    def stop(self):
        self._stop_evt.set()

    @property
    def stopped(self):
        return self._stop_evt.is_set()

    def screenshot(self, page, p: Product, tag):
        path = SCREENSHOT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_slot{p.slot}_{tag}.png"
        try:
            page.screenshot(path=str(path), full_page=False)
            logger.info("Screenshot saved: %s", path)
        except PWError:
            pass

    # --- Main loop --------------------------------------------------------------------

    def run(self):
        ctx = None
        try:
            with sync_playwright() as pw:
                ctx = self._launch_browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(15000)

                if not self._ensure_logged_in(page):
                    return
                self.log("Logged in. Monitoring started.")
                self._monitor(ctx, page)
                try:
                    ctx.close()
                except PWError:
                    pass
        except BrowserClosed:
            self.log("The browser window was closed, so the bot stopped.", "error")
        except Exception as e:  # noqa: BLE001 - report anything unexpected to the user
            logger.exception("Bot crashed")
            self.log(f"Unexpected error: {e}", "error")
        finally:
            self.emit({"type": "stopped"})

    def _launch_browser(self, pw):
        common = dict(
            headless=False,
            no_viewport=True,
            locale="es-MX",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        last_error = None
        for channel, name in (("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")):
            try:
                # Separate profile per browser: Chrome and Edge can't share one profile folder.
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR / channel), channel=channel, **common)
                self.log(f"Opened {name}.")
                return ctx
            except PWError as e:
                last_error = e
                logger.warning("Could not launch %s: %s", name, e)
        raise RuntimeError(
            "Could not open Microsoft Edge or Google Chrome. Close any window the bot opened "
            "earlier and try again, or install Google Chrome."
        ) from last_error

    def _ensure_page(self, ctx, page):
        if not page.is_closed():
            return page
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        except PWError as e:
            raise BrowserClosed() from e
        page.set_default_timeout(15000)
        return page

    def _sleep(self, seconds):
        self._stop_evt.wait(seconds)

    @staticmethod
    def _wait_on_page(page, seconds):
        """Wait while letting Playwright process browser events (a plain sleep would leave page.url stale)."""
        try:
            page.wait_for_timeout(seconds * 1000)
        except PWError as e:
            raise BrowserClosed() from e
        if page.is_closed():
            raise BrowserClosed()

    @staticmethod
    def _tick_remember_me(page):
        """Tick Amazon's 'Keep me signed in' box, otherwise the login is lost when the browser closes."""
        try:
            box = page.locator("input[name='rememberMe']").first
            if box.count() and not box.is_checked():
                box.check(force=True)
        except PWError:
            pass

    def _monitor(self, ctx, page):
        while not self.stopped:
            active = [p for p in self.products if not p.done]
            if not active:
                self.log("All products are finished. Bot stopped.")
                return
            for p in active:
                if self.stopped:
                    return
                page = self._ensure_page(ctx, page)
                try:
                    self._check_and_buy(page, p)
                except PWTimeout:
                    self.status(p, "Page load timeout - retrying", "orange")
                    logger.warning("Timeout on %s", p.url)
                except PWError as e:
                    if page.is_closed():
                        page = self._ensure_page(ctx, page)
                        continue
                    self.status(p, "Error - retrying", "orange")
                    logger.exception("Browser error on %s", p.url)
                    self.log(f"{p.label}: browser error ({str(e).splitlines()[0]}), will retry.", "error")

            if self.stopped or all(p.done for p in self.products):
                continue
            delay = random.uniform(self.min_interval, self.max_interval)
            self.emit({"type": "next_check", "seconds": round(delay)})
            self._sleep(delay)

    # --- Login / human verification -----------------------------------------------------

    @staticmethod
    def _is_logged_in(page) -> bool:
        try:
            loc = page.locator("#nav-link-accountList-nav-line-1").first
            if not loc.count():
                return False
            text = (loc.text_content() or "").lower()
            return bool(text.strip()) and "identif" not in text and "sign in" not in text
        except PWError:
            return False

    @staticmethod
    def _human_needed(page) -> str | None:
        url = page.url.lower()
        if any(s in url for s in ("/ap/signin", "/ap/mfa", "/ap/cvf", "/ap/challenge", "/ap/forgotpassword", "/ax/claim")):
            return "Amazon is asking you to sign in / verify (OTP). Please complete it in the browser window."
        try:
            if "validatecaptcha" in url or page.locator("form[action*='validateCaptcha']").count():
                if page.locator("#captchacharacters").count():
                    return "Amazon is showing a CAPTCHA. Please type the characters in the browser window."
                # "Click to continue shopping" soft check - no characters to type.
                btn = page.locator("form[action*='validateCaptcha'] button, form[action*='validateCaptcha'] input[type=submit]").first
                if btn.count():
                    btn.click()
                    page.wait_for_load_state("domcontentloaded")
                    return None
                return "Amazon is showing a verification page. Please complete it in the browser window."
        except PWError:
            pass
        return None

    def _wait_for_human(self, page) -> bool:
        """If Amazon needs the user (login, OTP, CAPTCHA) wait until it's done. Returns True if we waited."""
        reason = self._human_needed(page)
        if not reason:
            return False
        self.alert(reason)
        while not self.stopped:
            self._wait_on_page(page, 2)
            self._tick_remember_me(page)
            if not self._human_needed(page):
                self.log("Verification completed, continuing.")
                return True
        return True

    def _ensure_logged_in(self, page) -> bool:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
        self._wait_for_human(page)
        if self._is_logged_in(page):
            return True

        self.alert("Please sign in to your Amazon account in the browser window. "
                   "You only need to do this once - the bot remembers the login.")
        try:
            page.locator("#nav-link-accountList").first.click()
        except PWError:
            pass
        while not self.stopped:
            self._wait_on_page(page, 2)
            self._tick_remember_me(page)
            try:
                if "/ap/" not in page.url and "/ax/" not in page.url and self._is_logged_in(page):
                    return True
            except PWError:
                continue
        return False

    # --- Product check ----------------------------------------------------------------

    def _open_product(self, page, p: Product):
        page.goto(p.url, wait_until="domcontentloaded", timeout=45000)
        if self._wait_for_human(page):
            page.goto(p.url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_selector("#productTitle, #dp, #g", timeout=10000)
        except PWTimeout:
            pass

    @staticmethod
    def _get_price(page) -> float | None:
        for sel in PRICE_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count():
                    price = parse_price(loc.text_content())
                    if price:
                        return price
            except PWError:
                continue
        return None

    def _check_and_buy(self, page, p: Product):
        self.status(p, "Checking...", "gray")
        self._open_product(page, p)

        if p.title is None:
            try:
                title = page.locator("#productTitle").first.text_content(timeout=3000)
                p.title = " ".join((title or "").split()) or None
            except PWError:
                pass
            if p.title:
                self.emit({"type": "title", "slot": p.slot, "title": p.title})
                self.log(f"Product #{p.slot}: {p.title}")
            elif page.locator("#g").count():
                self.status(p, "Page not found - check URL", "red")
                return

        buy_button = first_visible(page, BUY_NOW_SELECTORS + ADD_TO_CART_SELECTORS)
        if not buy_button:
            if page.locator("#buybox-see-all-buying-choices").count():
                self.status(p, "Only via 'other sellers' - waiting", "orange")
            else:
                self.status(p, f"Out of stock (checked {datetime.now():%H:%M:%S})", "black")
            return

        price = self._get_price(page)
        price_txt = f"${price:,.2f}" if price else "price unknown"
        if p.max_price is not None and (price is None or price > p.max_price):
            self.status(p, f"In stock at {price_txt} - above max price", "orange")
            return

        self.status(p, f"IN STOCK ({price_txt}) - buying...", "blue")
        self.log(f"{p.label} is IN STOCK at {price_txt}. Starting checkout (qty {p.quantity}).")
        self.emit({"type": "beep"})

        result = self._buy(page, p)
        if result == "ok":
            p.done = True
            self.screenshot(page, p, "order_placed")
            self.status(p, "PURCHASED ✓", "green")
            self.log(f"ORDER PLACED for {p.label}.")
            self.emit({"type": "purchased", "slot": p.slot, "title": p.title or p.url})
        elif result == "test":
            p.done = True
            self.screenshot(page, p, "test_mode")
            self.status(p, "TEST OK - reached 'Place order'", "green")
            self.log(f"TEST MODE: {p.label} reached the final 'Place order' button. Order NOT placed.")
        elif result == "unknown":
            # The order button was clicked but no confirmation appeared. Never retry here,
            # otherwise the product could be ordered twice.
            p.done = True
            self.screenshot(page, p, "unconfirmed")
            self.status(p, "Order sent - verify in 'Mis pedidos'", "orange")
            self.alert(f"{p.label}: the order was submitted but Amazon's confirmation wasn't detected. "
                       "Please check 'Mis pedidos' on Amazon.")
        elif not self.stopped:
            p.failures += 1
            self.screenshot(page, p, "checkout_failed")
            self.status(p, f"Checkout failed (try {p.failures}) - retrying", "red")
            self.log(f"{p.label}: checkout did not complete, will try again next check.", "error")

    # --- Checkout ---------------------------------------------------------------------

    def _set_quantity(self, page, p: Product):
        if p.quantity <= 1:
            return
        sel = page.locator("select#quantity, select[name='quantity']").first
        if not sel.count():
            self.log(f"{p.label}: this product has no quantity selector, buying 1.")
            return
        values = sel.locator("option").evaluate_all("els => els.map(e => e.value)")
        numbers = [int(v) for v in values if v.strip().isdigit()]
        target = p.quantity
        if numbers and target not in numbers:
            target = max(n for n in numbers if n <= p.quantity) if min(numbers) <= p.quantity else min(numbers)
            self.log(f"{p.label}: quantity {p.quantity} not allowed by Amazon, buying {target}.")
        sel.select_option(str(target), force=True)
        page.wait_for_timeout(800)

    def _buy(self, page, p: Product) -> str:
        """Returns 'ok', 'test', 'unknown' (clicked but unconfirmed) or 'failed'."""
        try:
            self._set_quantity(page, p)
        except PWError:
            logger.exception("Could not set quantity")
            self.log(f"{p.label}: could not set quantity, continuing with Amazon's default.", "error")

        buy_now = first_visible(page, BUY_NOW_SELECTORS)
        if buy_now:
            buy_now.click()
        else:
            add = first_visible(page, ADD_TO_CART_SELECTORS)
            if not add:
                return "failed"
            add.click()
            page.wait_for_timeout(2500)
            page.goto(f"{BASE_URL}/gp/cart/view.html", wait_until="domcontentloaded")
        return self._complete_checkout(page)

    @staticmethod
    def _order_confirmed(page) -> bool:
        if "thankyou" in page.url.lower():
            return True
        for frame in page.frames:
            try:
                if frame.get_by_text(SUCCESS_RE).first.is_visible():
                    return True
            except PWError:
                continue
        return False

    @staticmethod
    def _find_place_order(page):
        try:
            turbo = page.frame_locator("#turbo-checkout-iframe").locator("#turbo-checkout-pyo-button").first
            if page.locator("#turbo-checkout-iframe").count() and turbo.count() and turbo.is_visible():
                return turbo
        except PWError:
            pass
        loc = first_visible(page, PLACE_ORDER_SELECTORS)
        if loc:
            return loc
        try:
            loc = page.get_by_role("button", name=PLACE_ORDER_RE).first
            if loc.count() and loc.is_visible():
                return loc
        except PWError:
            pass
        return None

    @staticmethod
    def _find_decline(page):
        loc = first_visible(page, DECLINE_SELECTORS)
        if loc:
            return loc
        candidates = [
            page.get_by_role("button", name=DECLINE_RE),
            page.get_by_role("link", name=DECLINE_RE),
            # e.g. the Prime sign-up page, where "No gracias" is a plain styled element
            page.get_by_text(re.compile(r"^\s*no,?\s+gracias\s*$", re.I)),
        ]
        for cand in candidates:
            try:
                loc = cand.first
                if loc.count() and loc.is_visible():
                    return loc
            except PWError:
                continue
        return None

    @staticmethod
    def _button_text(loc) -> str:
        try:
            return loc.evaluate(
                "el => { const b = el.closest('.a-button') || el;"
                " return [el.value || '', el.getAttribute('aria-label') || '', b.innerText || ''].join(' '); }"
            )
        except PWError:
            return ""

    def _complete_checkout(self, page) -> str:
        deadline = time.time() + 120
        order_clicked = False
        while time.time() < deadline and not self.stopped:
            if page.is_closed():
                raise BrowserClosed()
            if self._wait_for_human(page):
                deadline = time.time() + 120
                continue
            if self._order_confirmed(page):
                return "ok"
            if order_clicked:
                page.wait_for_timeout(1000)
                continue

            try:
                place = self._find_place_order(page)
                if place:
                    if self.test_mode:
                        return "test"
                    place.click()
                    order_clicked = True
                    deadline = time.time() + 60
                    self.log("Clicked 'Place order', waiting for Amazon's confirmation...")
                    continue

                decline = self._find_decline(page)
                if decline:
                    decline.click()
                    page.wait_for_timeout(1500)
                    continue

                cont = first_visible(page, CONTINUE_SELECTORS)
                if cont and FINAL_BUTTON_RE.search(self._button_text(cont)):
                    # Amazon labelled a "continue" slot as the final purchase button.
                    if self.test_mode:
                        return "test"
                    cont.click()
                    order_clicked = True
                    deadline = time.time() + 60
                    self.log("Clicked 'Place order', waiting for Amazon's confirmation...")
                    continue
                if cont:
                    cont.click()
                    page.wait_for_timeout(2000)
                    continue
            except PWError as e:
                # Elements often detach while Amazon re-renders the checkout; just look again.
                logger.info("Checkout step retry: %s", str(e).splitlines()[0])
            page.wait_for_timeout(1000)

        return "unknown" if order_clicked else "failed"
