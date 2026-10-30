"""Pocket Agent: a local SLM agent (Ollama) with tools, a live token dashboard,
and an optional hand-off to a cloud LLM (Claude).

    python pocket_agent.py                          # interactive
    python pocket_agent.py --script demo_script.yaml --events runs/x.jsonl   # scripted (recording)
"""
import argparse
import ast
import datetime as dt
import json
import operator
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import ollama
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

LOCAL_MODEL = os.getenv("LOCAL_MODEL", "gemma4:latest")
LOCAL_CTX = int(os.getenv("LOCAL_CTX", "8192"))
CLOUD_MODEL = os.getenv("CLOUD_MODEL", "claude-haiku-5-5")
DEMO_NOW = os.getenv("DEMO_NOW", "")  # "HH:MM" freezes the clock for rehearsals
ALLOW_WEB = os.getenv("ALLOW_WEB", "false").lower() == "true"
HEADLESS = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"

DEMO = ROOT / "demo_files"
WORKSPACE = DEMO / "workspace"
SITE = DEMO / "site"
MAX_STEPS = 15
# USD per 1M tokens (input, output), for the "same tokens in the cloud" estimate
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}
BROWSER_ARGS: list[str] = []  # window position/size, set by --layout

SYSTEM = """You are Pocket Agent, a helpful assistant running fully on this laptop.
Use your tools instead of guessing: never guess the time, the agenda or file contents.
Do time maths with the calculate tool in minutes. Minutes left = end time minus the CURRENT time
from get_time, e.g. from 14:05 until 15:30 is: 15*60+30 - (14*60+5).
File paths are relative to the user's workspace folder. For any request about files,
first call list_files to see what is there; never ask the user for file names.
For travel plans, long-form writing, current events or anything that needs broad world knowledge,
ALWAYS call ask_big_brain instead of answering yourself.
Keep final answers short: at most 3 sentences unless the user asks for more."""


# ---------- tools ----------

def _safe(path: str) -> Path:
    p = (WORKSPACE / path).resolve()
    if not p.is_relative_to(WORKSPACE.resolve()):
        raise PermissionError(f"'{path}' is outside the workspace folder")
    return p


def get_time() -> str:
    """Get the current local date and time."""
    now = dt.datetime.now()
    if DEMO_NOW:
        h, m = map(int, DEMO_NOW.split(":"))
        now = now.replace(hour=h, minute=m)
    return now.strftime("%A %d %B %Y, %H:%M")


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(n):
    if isinstance(n, ast.Constant) and type(n.value) in (int, float):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
        left, right = _eval(n.left), _eval(n.right)
        if isinstance(n.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(n.op)](left, right)
    if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_eval(n.operand))
    raise ValueError("only numbers, + - * / // % ** and parentheses are allowed")


def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression, for example '17*60 - (16*60 + 40)'.

    Args:
        expression: numbers with + - * / // % ** and parentheses
    """
    result = _eval(ast.parse(expression, mode="eval").body)
    if isinstance(result, float):
        result = int(result) if result.is_integer() else round(result, 6)
    return str(result)


def read_agenda() -> str:
    """Read today's event agenda with session names and times."""
    return (DEMO / "agenda.txt").read_text(encoding="utf-8")


def list_files(folder: str = ".") -> str:
    """List the files and folders in the workspace (folders end with /).

    Args:
        folder: folder relative to the workspace, '.' for the top level
    """
    entries = sorted(_safe(folder).iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    return "\n".join(p.name + ("/" if p.is_dir() else "") for p in entries) or "(empty folder)"


def read_file(path: str) -> str:
    """Read a text file from the workspace.

    Args:
        path: file path relative to the workspace
    """
    return _safe(path).read_text(encoding="utf-8", errors="replace")[:4000]


def write_file(path: str, content: str) -> str:
    """Create or overwrite a .md or .txt file in the workspace.

    Args:
        path: file path relative to the workspace, ending in .md or .txt
        content: the full text to write
    """
    p = _safe(path)
    if p.suffix.lower() not in (".md", ".txt"):
        raise PermissionError("only .md and .txt files can be written")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} characters to {path}"


def move_file(path: str, to_folder: str) -> str:
    """Move a file into a folder in the workspace (the folder is created if needed).

    Args:
        path: file to move, relative to the workspace
        to_folder: destination folder, relative to the workspace
    """
    src, dest = _safe(path), _safe(to_folder)
    if not src.is_file():
        raise FileNotFoundError(f"no file '{path}'")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dest / src.name)
    return f"moved {path} -> {to_folder}/"


_browser: dict = {}
_log = lambda kind, **data: None  # the dashboard swaps in its event logger


def _foreground_title() -> str:
    import ctypes
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(h, buf, 256)
    return buf.value


def _page():
    if "page" not in _browser:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(channel="msedge", headless=HEADLESS, args=BROWSER_ARGS)
        page = browser.new_page(no_viewport=True)
        page.set_default_timeout(8000)
        _browser.update(pw=pw, browser=browser, page=page)
    return _browser["page"]


def close_browser():
    if _browser:
        _browser["browser"].close()
        _browser["pw"].stop()
        _browser.clear()


_FIELDS_JS = """() => [...document.querySelectorAll('input,select,textarea')]
  .filter(e => !['submit', 'hidden', 'button'].includes(e.type))
  .map(e => ({name: e.name, label: (e.labels && e.labels[0] ? e.labels[0].innerText : '').trim(),
              options: e.tagName === 'SELECT' ? [...e.options].map(o => o.text) : null}))"""


def _describe(page) -> str:
    text = re.sub(r"\n\s*\n+", "\n", page.inner_text("body"))[:1500]
    fields = page.evaluate(_FIELDS_JS)
    lines = [f"Page: {page.title()}", text]
    if fields:
        lines.append("Form fields (use the name as the key):")
        lines += [f"- {f['name']}: {f['label']}" + (f" options={f['options']}" if f["options"] else "") for f in fields]
    return "\n".join(lines)


def open_page(url: str) -> str:
    """Open a web page in the browser and return its text and form fields.

    Args:
        url: a local page name such as 'feedback_form.html', or a full https:// address
    """
    if url.startswith(("http://", "https://")):
        if not ALLOW_WEB:
            raise PermissionError("internet pages are turned off (ALLOW_WEB=false); use a local page")
        target = url
    else:
        f = SITE / Path(url).name  # local pages only come from demo_files/site
        if not f.is_file():
            raise FileNotFoundError(f"no local page '{url}'. Pages: {', '.join(p.name for p in SITE.glob('*.html'))}")
        target = f.as_uri()
    page = _page()
    page.goto(target)
    page.bring_to_front()
    if BROWSER_ARGS:  # demo layout: put Edge over the Explorer window (its title shows up a moment later)
        # Windows won't always let us raise Edge above Explorer, but it always lets us minimise Explorer
        minimize_windows("CabinetWClass")
        tries = 0
        while not place_window(page.title(), 0.6, 1.0) and tries < 15:
            tries += 1
            time.sleep(0.2)
        _log("browser", label=page.title(), tries=tries, front=_foreground_title())
    return _describe(page)


def fill_and_submit(values: dict) -> str:
    """Fill in the form on the open page and submit it.

    Args:
        values: an object mapping each form field name (from open_page) to the value to enter
    """
    page = _page()
    for name, value in values.items():
        el = page.locator(f"[name={json.dumps(name)}]")
        if el.count() == 0:
            el = page.get_by_label(name)
        if el.count() == 0:
            raise ValueError(f"no form field '{name}'")
        el, value = el.first, str(value)
        if el.evaluate("e => e.tagName") == "SELECT":
            try:
                el.select_option(label=value)
            except Exception:
                el.select_option(value)
        elif HEADLESS:
            el.fill(value)
        else:
            el.fill("")
            el.press_sequentially(value, delay=45)  # visible typing for the audience
    page.locator("[type=submit]").first.click()
    page.wait_for_load_state()
    return _describe(page)


def online(host="api.anthropic.com", port=443, timeout=1.5) -> bool:
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except OSError:
        return False


_cloud_usage: list[dict] = []  # drained by the agent into the dashboard


def ask_big_brain(question: str) -> str:
    """Hand a hard question to a large cloud model. Use it for travel plans, long writing,
    current events or anything that needs broad world knowledge.

    Args:
        question: the full question, including any context the big model needs
    """
    fallback = " Tell the user the big cloud model is unavailable, then give a short best-effort answer yourself (max 5 lines)."
    if not online():
        return "CLOUD UNREACHABLE: no internet." + fallback
    import anthropic
    t0 = time.time()
    try:
        client = anthropic.Anthropic(max_retries=1, timeout=30)
        r = client.messages.create(
            model=CLOUD_MODEL,
            max_tokens=4000,
            output_config={"effort": "low"},
            system="Answer concisely in under 200 words.",
            messages=[{"role": "user", "content": question}],
        )
    except anthropic.AuthenticationError:
        return "CLOUD UNAVAILABLE: the API key was rejected." + fallback
    except anthropic.APIConnectionError:
        return "CLOUD UNREACHABLE: could not connect." + fallback
    except anthropic.APIStatusError as e:
        return f"CLOUD ERROR {e.status_code}." + fallback
    except anthropic.AnthropicError as e:  # e.g. no API key configured
        return f"CLOUD UNAVAILABLE: {type(e).__name__}." + fallback
    secs = time.time() - t0
    _cloud_usage.append(dict(who="cloud", label=CLOUD_MODEL, tokens_in=r.usage.input_tokens,
                             tokens_out=r.usage.output_tokens, secs=secs, tps=r.usage.output_tokens / max(secs, 1e-6)))
    if r.stop_reason == "refusal":
        return "CLOUD DECLINED this request." + fallback
    return "".join(b.text for b in r.content if b.type == "text")


TOOLS = [get_time, calculate, read_agenda, list_files, read_file, write_file, move_file,
         open_page, fill_and_submit, ask_big_brain]
CHANGES = {"write_file", "move_file", "fill_and_submit"}  # need the user's OK


def reset_workspace():
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    shutil.copytree(DEMO / "workspace_template", WORKSPACE)


def prewarm(model=LOCAL_MODEL):
    ollama.generate(model=model, prompt="", keep_alive="30m", options={"num_ctx": LOCAL_CTX})


# ---------- agent loop ----------

class Agent:
    def __init__(self, model=LOCAL_MODEL, emit=lambda kind, **d: None, confirm=lambda question: True):
        self.model, self.emit, self.confirm = model, emit, confirm
        self.tools = {f.__name__: f for f in TOOLS}
        self.reset()

    def reset(self):
        self.messages = [{"role": "system", "content": SYSTEM}]

    def ask(self, question: str) -> str:
        self.messages.append({"role": "user", "content": question})
        approved = None
        for _ in range(MAX_STEPS):
            self.emit("thinking")
            t0 = time.time()
            r = ollama.chat(model=self.model, messages=self.messages, tools=TOOLS, think=False,
                            keep_alive="30m", options={"num_ctx": LOCAL_CTX, "temperature": 0})
            calls = r.message.tool_calls or []
            out_tok = r.eval_count or 0
            self.emit("llm", who="local", label=", ".join(c.function.name for c in calls) or "answer",
                      tokens_in=r.prompt_eval_count or 0, tokens_out=out_tok, secs=time.time() - t0,
                      tps=out_tok / max((r.eval_duration or 1) / 1e9, 1e-6))
            self.messages.append(r.message)
            if not calls:
                self.emit("answer", text=r.message.content or "")
                return r.message.content or ""
            for c in calls:
                name, args = c.function.name, dict(c.function.arguments or {})
                self.emit("tool", name=name, args=args)
                slow = name == "open_page" and not _browser
                if slow:
                    self.emit("slow", on=True)
                try:
                    if name not in self.tools:
                        raise ValueError(f"unknown tool '{name}'")
                    if name in CHANGES and approved is None:
                        approved = self.confirm("Allow the agent to change files / submit forms for this request?")
                    if name in CHANGES and not approved:
                        raise PermissionError("the user did not allow this change")
                    out = str(self.tools[name](**args))
                except Exception as e:
                    out = f"ERROR: {e}"
                if slow:
                    self.emit("slow", on=False)
                while _cloud_usage:
                    self.emit("llm", **_cloud_usage.pop(0))
                self.emit("tool_result", name=name, text=out)
                self.messages.append({"role": "tool", "content": out, "tool_name": name})
        msg = "I hit my step limit without finishing. Try a simpler request."
        self.emit("answer", text=msg)
        return msg


# ---------- Windows helpers (demo layout + Wi-Fi) ----------

def place_window(title_part: str, left: float, right: float, cls: str | None = None) -> bool:
    """Move the first visible window whose title contains title_part to a vertical strip of the screen."""
    import ctypes
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    area = wt.RECT()
    u.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)  # SPI_GETWORKAREA
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n and u.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            cbuf = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cbuf, 256)
            if title_part in buf.value and (cls is None or cbuf.value == cls):
                found.append(hwnd)
        return True

    u.EnumWindows(cb, 0)
    if not found:
        return False
    w = area.right - area.left
    u.ShowWindow(found[0], 9)  # SW_RESTORE
    u.MoveWindow(found[0], area.left + int(w * left), area.top, int(w * (right - left)), area.bottom - area.top, True)
    # raise it even when another app has focus (SetForegroundWindow alone is often refused)
    for z in (-1, -2):  # HWND_TOPMOST, then HWND_NOTOPMOST
        u.SetWindowPos(found[0], z, 0, 0, 0, 0, 0x0001 | 0x0002)  # SWP_NOSIZE | SWP_NOMOVE
    u.SetForegroundWindow(found[0])
    return True


def minimize_windows(cls: str):
    import ctypes
    import ctypes.wintypes as wt
    u = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, buf, 256)
        if buf.value == cls and u.IsWindowVisible(hwnd):
            u.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        return True

    u.EnumWindows(cb, 0)


def browser_args_for(left: float, right: float) -> list[str]:
    import ctypes
    import ctypes.wintypes as wt
    area = wt.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)
    w = area.right - area.left
    return [f"--window-position={area.left + int(w * left)},{area.top}",
            f"--window-size={int(w * (right - left))},{area.bottom - area.top}"]


_wifi_profile: list[str] = []
_wifi_auto: list[str] = []  # profiles switched to manual-connect while Wi-Fi is "off"


def _netsh(*args) -> str:
    return subprocess.run(["netsh", "wlan", *args], capture_output=True, text=True).stdout


def wifi(on: bool):
    if not on:
        m = re.search(r"^\s*Profile\s*:\s*(.+)$", _netsh("show", "interfaces"), re.M)
        if m:
            _wifi_profile[:] = [m.group(1).strip()]
        # otherwise Windows hops to another saved network within seconds
        for p in re.findall(r"All User Profile\s*:\s*(.+?)\s*$", _netsh("show", "profiles"), re.M):
            if "Connect automatically" in _netsh("show", "profile", f"name={p}"):
                _netsh("set", "profileparameter", f"name={p}", "connectionmode=manual")
                _wifi_auto.append(p)
        _netsh("disconnect")
    else:
        while _wifi_auto:
            _netsh("set", "profileparameter", f"name={_wifi_auto.pop()}", "connectionmode=auto")
        if _wifi_profile:
            _netsh("connect", f"name={_wifi_profile[0]}")


# ---------- terminal dashboard ----------

class Dashboard:
    def __init__(self, events_path: Path | None):
        from rich.console import Console
        self.console = Console()
        self.lines, self.rows = [], []
        self.prompt, self.buf = "", ""
        self.busy_since = None
        self.request_start = 0
        self.net, self.vram = None, ""
        self.events = open(events_path, "a", encoding="utf-8") if events_path else None
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        while True:
            self.net = online("1.1.1.1", 53, 1.0)
            try:
                for m in ollama.ps().models:
                    if m.model == LOCAL_MODEL or m.name == LOCAL_MODEL:
                        gpu = 100 * m.size_vram / max(m.size, 1)
                        self.vram = f"{m.size_vram / 1e9:.1f} GB in VRAM ({gpu:.0f}% GPU)"
            except Exception:
                pass
            time.sleep(1.5)

    def log(self, kind, **data):
        if self.events:
            # y: where the newest chat line is, as a fraction of the window height (record.py zooms there)
            w, h = self.console.size
            n = len(self._wrap(w)) if self.lines else 0
            self.events.write(json.dumps({"t": time.time(), "kind": kind, "y": round((1 + min(n, h - 5)) / h, 3),
                                          **data}, ensure_ascii=False) + "\n")
            self.events.flush()

    def _wrap(self, w):
        from rich.text import Text
        return list(Text("\n").join(self.lines).wrap(self.console, max(w * 11 // 20 - 4, 20)))

    def say(self, markup):
        from rich.text import Text
        self.lines.append(Text.from_markup(markup))

    def emit(self, kind, **d):
        from rich.markup import escape
        self._emit(kind, d, escape)
        self.log(kind, **{k: v for k, v in d.items()
                          if k in ("on", "name", "who", "label", "tokens_in", "tokens_out", "secs", "tps")})

    def _emit(self, kind, d, escape):
        if kind == "thinking":
            self.busy_since = time.time()
        elif kind == "llm":
            self.busy_since = None
            self.rows.append(d)
        elif kind == "tool":
            args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in d["args"].items())
            if len(args) > 140:
                args = args[:140] + "…"
            self.say(f"  [yellow]🔧 {d['name']}[/]([dim]{escape(args)}[/])")
        elif kind == "tool_result":
            text = " | ".join(line.strip() for line in d["text"].splitlines() if line.strip())
            style = "red" if text.startswith(("ERROR", "CLOUD")) else "dim"
            self.say(f"     [{style}]→ {escape(text[:150] + ('…' if len(text) > 150 else ''))}[/]")
        elif kind == "answer":
            self.busy_since = None
            self.say(f"[bold green]🐈 {escape(d['text'].strip())}[/]\n")

    def render(self):
        from rich import box
        from rich.layout import Layout
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        w, h = self.console.size
        left_w = w * 11 // 20
        body_h = h - 3
        wrapped = self._wrap(w) if self.lines else []
        if self.busy_since:
            wrapped.append(Text.from_markup(f"[cyan]  🐈 thinking… {time.time() - self.busy_since:.1f}s[/]"))
        transcript = Text("\n").join(wrapped[-(body_h - 2):])

        layout = Layout()
        layout.split_row(Layout(name="left", ratio=11), Layout(name="right", ratio=9))
        layout["left"].split_column(Layout(Panel(transcript, title="🐈 Pocket Agent", title_align="left",
                                                 border_style="green"), name="chat"),
                                    Layout(Panel(Text(self.prompt + self.buf + "▌"), border_style="cyan"), size=3))

        net = "[bold red]📴 OFFLINE[/]" if self.net is False else "[bold green]📶 ONLINE[/]" if self.net else "…"
        head = Table.grid(padding=(0, 1))
        head.add_row("[dim]Network[/]", net)
        head.add_row("[dim]Local brain[/]", f"🐈 {LOCAL_MODEL}")
        head.add_row("[dim]GPU[/]", self.vram or "loading…")
        head.add_row("[dim]Big brain[/]", f"🐘 {CLOUD_MODEL}")

        steps = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 0, 0, 1), pad_edge=False)
        steps.add_column("step", no_wrap=True, overflow="ellipsis", ratio=1)
        for col, width in (("in", 6), ("out", 5), ("tok/s", 5), ("sec", 4)):
            steps.add_column(col, justify="right", no_wrap=True, min_width=width)
        rows = self.rows[self.request_start:]
        max_rows = max(body_h - 22, 3)
        for r in rows[-max_rows:]:
            icon = "🐘" if r["who"] == "cloud" else "🐈"
            steps.add_row(f"{icon} {r['label']}", f"{r['tokens_in']:,}", f"{r['tokens_out']:,}",
                          f"{r['tps']:.0f}", f"{r['secs']:.1f}")

        def tot(rs, who):
            sel = [r for r in rs if r["who"] == who]
            return sum(r["tokens_in"] for r in sel), sum(r["tokens_out"] for r in sel)

        req_in = sum(r["tokens_in"] for r in rows)
        req_out = sum(r["tokens_out"] for r in rows)
        req_secs = sum(r["secs"] for r in rows)
        li, lo = tot(self.rows, "local")
        ci, co = tot(self.rows, "cloud")
        price = PRICES.get(CLOUD_MODEL)
        cost = lambda i, o: (i * price[0] + o * price[1]) / 1e6 if price else 0.0
        summary = Table.grid(padding=(0, 1))
        summary.add_row("[dim]This request[/]", f"{req_in + req_out:,} tokens · {req_secs:.1f}s · {len(rows)} model calls")
        summary.add_row("[dim]Session local[/]", f"🐈 {li + lo:,} tokens · [bold green]$0.00[/]")
        summary.add_row("[dim]Session cloud[/]", f"🐘 {ci + co:,} tokens · ${cost(ci, co):.4f}")
        if price:
            summary.add_row("[dim]Local work if[/]", f"sent to {CLOUD_MODEL}: ≈${cost(li, lo):.4f}")
            summary.add_row("[dim]× 1,000 runs[/]", f"≈${cost(li, lo) * 1000:.2f} (estimate)")

        from rich.console import Group
        from rich.rule import Rule
        layout["right"].update(Panel(Group(head, Rule(style="dim"), steps, Rule(style="dim"), summary),
                                     title="📊 Live stats", title_align="left", border_style="magenta"))
        return layout

    def read_line(self, prompt: str) -> str:
        import msvcrt
        self.prompt, self.buf = prompt, ""
        while True:
            ch = msvcrt.getwch()
            if ch in "\r\n":
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x08":
                self.buf = self.buf[:-1]
            elif ch in ("\x00", "\xe0"):
                msvcrt.getwch()  # skip arrow / function key codes
            elif ch.isprintable():
                self.buf += ch
        line, self.prompt, self.buf = self.buf, "", ""
        return line

    def type_line(self, prompt: str, text: str, cps=28) -> str:
        self.log("typing", label=prompt.strip())
        self.prompt, self.buf = prompt, ""
        for ch in text:
            self.buf += ch
            time.sleep(1 / cps)
        time.sleep(0.6)
        self.prompt, self.buf = "", ""
        return text


def run(args):
    import ctypes
    from rich.live import Live
    global BROWSER_ARGS, _log
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # emoji render as mojibake otherwise
    sys.stdout.reconfigure(encoding="utf-8")
    if args.layout:
        BROWSER_ARGS = browser_args_for(0.6, 1.0)
    ui = Dashboard(Path(args.events) if args.events else None)
    _log = ui.log
    ui.console.set_window_title("Pocket Agent")
    if args.layout:
        time.sleep(0.5)
        place_window("Pocket Agent", 0.0, 0.6)
    agent = Agent(model=args.model, emit=ui.emit)
    script = None
    if args.script:
        import yaml
        script = yaml.safe_load(Path(args.script).read_text(encoding="utf-8"))
        agent.confirm = lambda q: ui.say(f"[magenta]❓ {q}[/]") or ui.type_line("Allow? (y/n) ", "y", cps=4) == "y"
    else:
        agent.confirm = lambda q: ui.say(f"[magenta]❓ {q}[/]") or ui.read_line("Allow? (y/n) ").strip().lower() == "y"

    with Live(get_renderable=ui.render, console=ui.console, screen=True, refresh_per_second=12):
        ui.say(f"[dim]Loading {LOCAL_MODEL}…[/]")
        ui.log("slow", on=True)
        prewarm(args.model)
        ui.log("slow", on=False)
        ui.say("[dim]Ready. Type a question, /reset to clear the conversation, /quit to exit.[/]\n")
        ui.log("start")
        try:
            for step in script or iter(lambda: {}, None):
                if script is None:
                    q = ui.read_line("You ▸ ").strip()
                    if q in ("/quit", "/exit"):
                        break
                    if q == "/reset":
                        agent.reset()
                        ui.say("[dim]-- conversation cleared --[/]")
                        continue
                    step = {"ask": q} if q else {}
                if "caption" in step:
                    agent.reset()
                    ui.lines.clear()
                    ui.say(f"[bold magenta]━━ {step['caption']} ━━[/]")
                    ui.log("act", label=step["caption"])
                    time.sleep(1.5)
                if "wifi" in step:
                    on = step["wifi"] == "on"
                    ui.log("slow", on=True)  # netsh takes a few seconds
                    wifi(on)
                    ui.log("slow", on=False)
                    ui.net = None if on else False  # show it now; the poller confirms "online"
                    ui.say(f"[bold]{'📶 Turning Wi-Fi back on' if on else '📴 Wi-Fi OFF'}[/]")
                    ui.log("wifi", on=on)
                    if on:  # reconnecting takes a few seconds; record.py fast-forwards it
                        ui.log("slow", on=True)
                        for _ in range(30):
                            if online("1.1.1.1", 53, 1.0):
                                break
                            time.sleep(1)
                        ui.log("slow", on=False)
                    time.sleep(3)
                if step.get("explorer"):
                    ui.log("explorer")
                    os.startfile(WORKSPACE)
                    time.sleep(1.5)
                    place_window("workspace", 0.6, 1.0, cls="CabinetWClass")
                    time.sleep(1)
                if step.get("ask"):
                    if script is not None:
                        ui.type_line("You ▸ ", step["ask"])
                    ui.request_start = len(ui.rows)
                    ui.say(f"[bold cyan]You ▸[/] {step['ask']}")
                    agent.ask(step["ask"])
                    time.sleep(step.get("pause", 3 if script else 0))
        except KeyboardInterrupt:
            pass
        finally:
            ui.log("end")
            close_browser()
            if _wifi_auto:  # never leave the laptop offline
                wifi(True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=LOCAL_MODEL)
    p.add_argument("--script", help="YAML demo script to play instead of reading the keyboard")
    p.add_argument("--events", help="append timing events (JSON lines) here, used by record.py")
    p.add_argument("--layout", action=argparse.BooleanOptionalAction, default=True,
                   help="terminal on the left 60%%, browser / Explorer on the right 40%% (--no-layout to skip)")
    p.add_argument("--reset", action="store_true", help="restore the messy demo workspace before starting")
    p.add_argument("--now", help="freeze the clock at HH:MM (overrides DEMO_NOW)")
    a = p.parse_args()
    if a.reset or not WORKSPACE.exists():
        reset_workspace()
    LOCAL_MODEL = a.model
    DEMO_NOW = a.now or DEMO_NOW
    run(a)
