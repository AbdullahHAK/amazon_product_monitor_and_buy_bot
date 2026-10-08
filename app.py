"""Amazon México Bot - desktop GUI."""

import os
import queue
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

try:
    import winsound
except ImportError:  # not on Windows
    winsound = None

from bot import LOG_DIR, AmazonBot, Product, is_valid_product_url, load_settings, normalize_url, save_settings

NUM_PRODUCTS = 5
APP_TITLE = "Amazon México Bot"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(980, 620)
        self.events: queue.Queue = queue.Queue()
        self.bot: AmazonBot | None = None
        self.rows = []

        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))

        self._build()
        self._load()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll_events)

    # --- Layout -----------------------------------------------------------------------

    def _build(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Paste up to 5 amazon.com.mx product links. The bot buys each one "
                              "automatically as soon as it is in stock.").pack(anchor="w", pady=(0, 8))

        products = ttk.LabelFrame(outer, text="Products", padding=8)
        products.pack(fill="x")
        products.columnconfigure(1, weight=1)
        headers = ["#", "Product URL", "Quantity", "Max price (MXN)\noptional", "Status"]
        for col, text in enumerate(headers):
            ttk.Label(products, text=text, style="Header.TLabel").grid(row=0, column=col, sticky="w", padx=4)

        for i in range(NUM_PRODUCTS):
            r = i + 1
            ttk.Label(products, text=str(r)).grid(row=r, column=0, padx=4, pady=3)
            url = ttk.Entry(products)
            url.grid(row=r, column=1, sticky="ew", padx=4, pady=3)
            qty = ttk.Spinbox(products, from_=1, to=99, width=5)
            qty.set(1)
            qty.grid(row=r, column=2, padx=4, pady=3)
            max_price = ttk.Entry(products, width=12)
            max_price.grid(row=r, column=3, padx=4, pady=3)
            status = tk.Label(products, text="Idle", width=36, anchor="w", fg="gray")
            status.grid(row=r, column=4, sticky="w", padx=4, pady=3)
            self.rows.append({"url": url, "qty": qty, "max": max_price, "status": status})

        settings = ttk.LabelFrame(outer, text="Settings", padding=8)
        settings.pack(fill="x", pady=8)
        ttk.Label(settings, text="Check stock every").pack(side="left")
        self.min_int = ttk.Spinbox(settings, from_=5, to=600, width=5)
        self.min_int.pack(side="left", padx=4)
        ttk.Label(settings, text="to").pack(side="left")
        self.max_int = ttk.Spinbox(settings, from_=5, to=600, width=5)
        self.max_int.pack(side="left", padx=4)
        ttk.Label(settings, text="seconds (random, looks more human)").pack(side="left")
        self.test_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings, text="Test mode (go to checkout but do NOT place the order)",
                        variable=self.test_mode).pack(side="right")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.start_btn = tk.Button(buttons, text="▶  START", width=14, bg="#1e8e3e", fg="white",
                                   font=("Segoe UI", 11, "bold"), relief="flat", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = tk.Button(buttons, text="■  STOP", width=14, bg="#c5221f", fg="white",
                                  font=("Segoe UI", 11, "bold"), relief="flat", command=self._stop,
                                  state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(buttons, text="Open logs folder", command=lambda: os.startfile(LOG_DIR)).pack(side="right")
        ttk.Button(buttons, text="Clear", command=self._clear).pack(side="right", padx=8)

        log_frame = ttk.LabelFrame(outer, text="Activity log", padding=4)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log = ScrolledText(log_frame, height=12, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("error", foreground="#c5221f")
        self.log.tag_config("alert", foreground="#b06000", font=("Consolas", 9, "bold"))
        self.log.tag_config("success", foreground="#1e8e3e", font=("Consolas", 9, "bold"))

        self.statusbar = ttk.Label(self, text="Stopped", relief="sunken", anchor="w", padding=(8, 2))
        self.statusbar.pack(fill="x", side="bottom")

    # --- Settings persistence ---------------------------------------------------------

    def _load(self):
        data = load_settings()
        for row, saved in zip(self.rows, data.get("products", [])):
            row["url"].insert(0, saved.get("url", ""))
            row["qty"].set(saved.get("quantity", 1))
            if saved.get("max_price"):
                row["max"].insert(0, saved["max_price"])
        self.min_int.set(data.get("min_interval", 10))
        self.max_int.set(data.get("max_interval", 30))

    def _save(self):
        save_settings({
            "products": [{"url": r["url"].get().strip(), "quantity": r["qty"].get(),
                          "max_price": r["max"].get().strip()} for r in self.rows],
            "min_interval": self.min_int.get(),
            "max_interval": self.max_int.get(),
        })

    def _clear(self):
        if self.bot:
            return
        for r in self.rows:
            r["url"].delete(0, "end")
            r["max"].delete(0, "end")
            r["qty"].set(1)
            self._set_status(r, "Idle", "gray")

    # --- Start / stop -----------------------------------------------------------------

    def _collect_products(self):
        products, errors = [], []
        for i, r in enumerate(self.rows, start=1):
            url = r["url"].get().strip()
            if not url:
                self._set_status(r, "Idle", "gray")
                continue
            if not is_valid_product_url(url):
                errors.append(f"Product {i}: not an amazon.com.mx link.")
                continue
            try:
                qty = int(r["qty"].get())
                assert qty >= 1
            except (ValueError, AssertionError):
                errors.append(f"Product {i}: quantity must be a whole number (1 or more).")
                continue
            max_text = r["max"].get().strip().replace("$", "").replace(",", "")
            max_price = None
            if max_text:
                try:
                    max_price = float(max_text)
                except ValueError:
                    errors.append(f"Product {i}: max price must be a number, or leave it empty.")
                    continue
            products.append(Product(slot=i, url=normalize_url(url), quantity=qty, max_price=max_price))
        return products, errors

    def _start(self):
        products, errors = self._collect_products()
        try:
            lo, hi = int(self.min_int.get()), int(self.max_int.get())
            if lo < 5 or hi < lo:
                raise ValueError
        except ValueError:
            errors.append("Check interval: use whole numbers, minimum 5, and 'to' must be >= 'every'.")
        if not products and not errors:
            errors.append("Paste at least one product URL.")
        if errors:
            messagebox.showerror(APP_TITLE, "\n".join(errors))
            return
        if not self.test_mode.get() and not messagebox.askyesno(
                APP_TITLE, f"The bot will automatically BUY {len(products)} product(s) as soon as they "
                           "are in stock, using your default address and payment method.\n\nStart now?"):
            return

        self._save()
        for p in products:
            self._set_status(self.rows[p.slot - 1], "Starting...", "gray")
        self._set_running(True)
        self._write_log(f"Starting bot for {len(products)} product(s)"
                        f"{' in TEST MODE' if self.test_mode.get() else ''}...")
        self.bot = AmazonBot(products, self.events.put, lo, hi, self.test_mode.get())
        self.bot.start()

    def _stop(self):
        if self.bot:
            self.bot.stop()
            self.stop_btn.config(state="disabled")
            self.statusbar.config(text="Stopping... (finishing current step)")

    def _set_running(self, running):
        state = "disabled" if running else "normal"
        for r in self.rows:
            for key in ("url", "qty", "max"):
                r[key].config(state=state)
        self.min_int.config(state=state)
        self.max_int.config(state=state)
        self.start_btn.config(state=state)
        self.stop_btn.config(state="normal" if running else "disabled")
        self.statusbar.config(text="Running" if running else "Stopped")

    # --- Events from the bot thread ----------------------------------------------------

    def _poll_events(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._poll_events)

    def _handle(self, ev):
        kind = ev["type"]
        if kind == "log":
            self._write_log(ev["msg"], "error" if ev.get("level") == "error" else None)
        elif kind == "status":
            self._set_status(self.rows[ev["slot"] - 1], ev["text"], ev["color"])
        elif kind == "title":
            self._write_log(f"Product #{ev['slot']} found: {ev['title']}")
        elif kind == "next_check":
            self.statusbar.config(text=f"Running - next check in {ev['seconds']} s "
                                       f"(last check {datetime.now():%H:%M:%S})")
        elif kind == "beep":
            self._beep(winsound.MB_ICONASTERISK if winsound else None)
        elif kind == "alert":
            self._write_log("ACTION NEEDED: " + ev["msg"], "alert")
            self._beep(winsound.MB_ICONEXCLAMATION if winsound else None)
            self.statusbar.config(text="ACTION NEEDED - see the browser window")
            self.lift()
        elif kind == "purchased":
            self._write_log(f"ORDER PLACED: #{ev['slot']} {ev['title']}", "success")
            self._beep(winsound.MB_OK if winsound else None)
            self.after(10, lambda t=ev["title"]: messagebox.showinfo(APP_TITLE, f"Order placed!\n\n{t}"))
        elif kind == "stopped":
            self.bot = None
            self._set_running(False)
            self._write_log("Bot stopped.")

    def _beep(self, kind):
        if winsound and kind is not None:
            try:
                winsound.MessageBeep(kind)
            except RuntimeError:
                pass

    def _set_status(self, row, text, color):
        row["status"].config(text=text, fg=color)

    def _write_log(self, msg, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", f"[{datetime.now():%H:%M:%S}] {msg}\n", tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_close(self):
        if self.bot:
            if not messagebox.askyesno(APP_TITLE, "The bot is running. Stop it and exit?"):
                return
            self.bot.stop()
            self.bot.join(timeout=10)
        self._save()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
