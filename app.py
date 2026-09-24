# -*- coding: utf-8 -*-
"""
Screen Translator — Windows
Замена выделенного текста на месте + OCR области экрана + автоперевод
"""

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from io import BytesIO
from pathlib import Path

import keyboard
import mss
import requests
import tkinter as tk
from PIL import Image
from tkinter import messagebox, ttk

# ---------------- Windows helpers ----------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
VK_CTRL = 0x11


def clipboard_sequence():
    try:
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


def win_clipboard_get():
    for _ in range(30):
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return ""
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ""
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        time.sleep(0.02)
    return ""


def win_clipboard_set(text):
    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    for _ in range(30):
        if user32.OpenClipboard(None):
            hmem = None
            try:
                user32.EmptyClipboard()
                hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not hmem:
                    raise OSError("Нет памяти для буфера обмена.")
                ptr = kernel32.GlobalLock(hmem)
                if not ptr:
                    kernel32.GlobalFree(hmem)
                    raise OSError("Нет доступа к буферу обмена.")
                try:
                    ctypes.memmove(ptr, data, len(data))
                finally:
                    kernel32.GlobalUnlock(hmem)
                if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
                    kernel32.GlobalFree(hmem)
                    raise OSError("Windows не принял буфер обмена.")
                hmem = None
                return
            finally:
                if hmem:
                    kernel32.GlobalFree(hmem)
                user32.CloseClipboard()
        time.sleep(0.02)
    raise RuntimeError("Нет доступа к буферу обмена.")


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


def send_key(vk, down=True):
    inp = INPUT()
    inp.type = 1
    inp.ki.wVk = vk
    inp.ki.wScan = 0
    inp.ki.dwFlags = 0 if down else 0x0002
    inp.ki.time = 0
    inp.ki.dwExtraInfo = ctypes.pointer(ctypes.c_ulong(0))
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        raise RuntimeError(
            "Windows заблокировал Ctrl+C/Ctrl+V.\n"
            "Запусти Screen Translator от администратора."
        )


def send_ctrl(letter):
    send_key(VK_CTRL, True)
    send_key(ord(letter.upper()), True)
    send_key(ord(letter.upper()), False)
    send_key(VK_CTRL, False)


def foreground_hwnd():
    return user32.GetForegroundWindow()


def activate_window(hwnd):
    if not hwnd:
        return
    try:
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def normalize_hotkey(s):
    parts = [p.strip().lower() for p in str(s).replace("-", "+").split("+") if p.strip()]
    mods, key = [], None
    mod_map = {"control": "ctrl", "windows": "windows", "win": "windows"}
    for p in parts:
        if p in ("ctrl", "control", "shift", "alt", "win", "windows"):
            m = mod_map.get(p, p)
            if m not in mods:
                mods.append(m)
        else:
            if key is not None:
                raise ValueError("Только одна основная клавиша.")
            key = p
    if not key:
        raise ValueError("Укажи основную клавишу.")
    order = ["ctrl", "shift", "alt", "windows"]
    mods.sort(key=lambda x: order.index(x) if x in order else 99)
    return "+".join(mods + [key])


def pretty_hotkey(h):
    names = {
        "ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "windows": "Win",
        "win": "Win", "enter": "Enter", "space": "Space", "esc": "Esc",
    }
    parts = normalize_hotkey(h).split("+")
    out = []
    for p in parts:
        out.append(names.get(p, p.upper() if len(p) == 1 else p.title()))
    return " + ".join(out)


OCR_URL = "https://api.ocr.space/parse/image"
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ScreenTranslator/10.0"})


def detect_language(text):
    cyr = sum("\u0400" <= c <= "\u04ff" for c in text)
    lat = sum("a" <= c.lower() <= "z" for c in text)
    return "ru" if cyr >= lat else "en"


def translate_text(text, source="auto", target="auto"):
    text = text.strip()
    if not text:
        raise ValueError("Нет текста для перевода.")
    if source == "auto":
        source = detect_language(text)
    if target == "auto":
        target = "en" if source == "ru" else "ru"
    if source == target:
        target = "en" if source == "ru" else "ru"
    try:
        r = SESSION.get(
            GOOGLE_URL,
            params={"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text},
            timeout=(3.0, 10.0),
        )
        r.raise_for_status()
        data = r.json()
        result = "".join(part[0] for part in data[0] if part and part[0]).strip()
        if result:
            return result
    except Exception:
        pass
    try:
        r = SESSION.get(
            MYMEMORY_URL,
            params={"q": text, "langpair": f"{source}|{target}"},
            timeout=(3.0, 10.0),
        )
        r.raise_for_status()
        result = r.json().get("responseData", {}).get("translatedText", "").strip()
        if result and "MYMEMORY WARNING" not in result.upper():
            return result
    except Exception as e:
        raise RuntimeError("Не удалось перевести.\nПроверь интернет.\n\n" + str(e)) from e
    raise RuntimeError("Сервисы перевода не ответили.")


def ocr_image(image, api_key="helloworld"):
    max_side = 2200
    if max(image.size) > max_side:
        scale = max_side / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )
    buf = BytesIO()
    image.save(buf, format="PNG", optimize=True)
    r = SESSION.post(
        OCR_URL,
        headers={"apikey": (api_key or "helloworld").strip()},
        files={"file": ("screen.png", buf.getvalue(), "image/png")},
        data={
            "language": "auto",
            "OCREngine": "2",
            "isOverlayRequired": "false",
            "scale": "true",
        },
        timeout=(5.0, 30.0),
    )
    r.raise_for_status()
    data = r.json()
    if data.get("IsErroredOnProcessing"):
        errors = data.get("ErrorMessage") or ["Ошибка OCR"]
        msg = "; ".join(errors) if isinstance(errors, list) else str(errors)
        low = msg.lower()
        if "limit" in low or "exceeded" in low or "apikey" in low:
            raise RuntimeError(
                "Лимит OCR или неверный ключ.\n\n"
                "Ключ: https://ocr.space/ocrapi\nВставь в Настройки → Применить."
            )
        raise RuntimeError(msg)
    text = "\n".join(
        x.get("ParsedText", "") for x in (data.get("ParsedResults") or [])
    ).strip()
    if not text:
        raise RuntimeError("Текст в области не найден.")
    return text


class ScreenSelector:
    def __init__(self, master):
        self.result = None
        self.start = None
        self.rect = None
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#0a0c10")
        left = user32.GetSystemMetrics(76)
        top = user32.GetSystemMetrics(77)
        width = user32.GetSystemMetrics(78)
        height = user32.GetSystemMetrics(79)
        self.origin = (left, top)
        self.win.geometry(f"{width}x{height}{left:+d}{top:+d}")
        self.win.attributes("-alpha", 0.30)
        self.canvas = tk.Canvas(
            self.win, bg="#0a0c10", cursor="crosshair", highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            width // 2, 48,
            text="Выдели область  ·  Esc — отмена",
            fill="#e8ecf4", font=("Segoe UI", 14),
        )
        self.canvas.bind("<ButtonPress-1>", self.down)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.up)
        self.win.bind("<Escape>", lambda e: self.cancel())
        self.win.focus_force()
        self.canvas.focus_set()

    def down(self, e):
        self.start = (e.x, e.y)
        self.rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#6c9bff", width=2
        )

    def move(self, e):
        if self.start and self.rect:
            self.canvas.coords(self.rect, self.start[0], self.start[1], e.x, e.y)

    def up(self, e):
        if not self.start:
            return
        x1, x2 = sorted((self.start[0], e.x))
        y1, y2 = sorted((self.start[1], e.y))
        ox, oy = self.origin
        self.result = (ox + x1, oy + y1, ox + x2, oy + y2)
        self.win.destroy()

    def cancel(self):
        self.result = None
        self.win.destroy()


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Screen Translator")
        self.root.geometry("1100x720")
        self.root.minsize(900, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.settings_path = (
            Path(os.environ.get("APPDATA", str(Path.home())))
            / "ScreenTranslator" / "settings.json"
        )
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = self.load_settings()
        self.theme = self.settings.get("theme", "dark")
        self.replace_hotkey = self.settings.get("replace_hotkey", "shift+alt+s")
        self.screen_hotkey = self.settings.get("screen_hotkey", "shift+alt+x")
        self.ocr_key = self.settings.get("ocr_key", "helloworld")
        self.history = self.settings.get("history", [])[-30:]
        self.busy = False
        self.closing = False
        self.status_var = None
        self.status_dot = None
        self._auto_job = None

        self.setup_style()
        self.build_ui()
        self.register_hotkeys()

    def load_settings(self):
        defaults = {
            "theme": "dark",
            "replace_hotkey": "shift+alt+s",
            "screen_hotkey": "shift+alt+x",
            "ocr_key": "helloworld",
            "history": [],
        }
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                defaults.update({k: data[k] for k in defaults if k in data})
        except Exception:
            pass
        for key in ("replace_hotkey", "screen_hotkey"):
            try:
                defaults[key] = normalize_hotkey(defaults[key])
            except Exception:
                pass
        return defaults

    def save_settings(self):
        self.settings.update({
            "theme": self.theme,
            "replace_hotkey": self.replace_hotkey,
            "screen_hotkey": self.screen_hotkey,
            "ocr_key": self.ocr_key,
            "history": self.history[-30:],
        })
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.settings_path)

    def setup_style(self):
        if self.theme == "light":
            self.colors = {
                "bg": "#f0f2f6", "panel": "#ffffff", "panel2": "#e8ecf2",
                "text": "#141a22", "muted": "#5c6775", "border": "#d0d7e2",
                "accent": "#3b6ef5", "accent_hover": "#2f5ad4",
                "input": "#ffffff", "output": "#f7f9fc",
                "success": "#1a9c5b", "danger": "#d14343", "sidebar": "#f7f8fb",
            }
        else:
            self.colors = {
                "bg": "#0c0f14", "panel": "#141a22", "panel2": "#1c2430",
                "text": "#eef2f8", "muted": "#8b97a8", "border": "#2a3444",
                "accent": "#5b8cff", "accent_hover": "#4a7aef",
                "input": "#0f141b", "output": "#111820",
                "success": "#3dca7f", "danger": "#f07178", "sidebar": "#10151c",
            }
        self.root.configure(bg=self.colors["bg"])
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        c = self.colors
        style.configure("TCombobox", fieldbackground=c["input"], background=c["panel2"],
                        foreground=c["text"], arrowcolor=c["text"], padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", c["input"])],
                  foreground=[("readonly", c["text"])])

    def make_text(self, parent, bg):
        return tk.Text(
            parent, wrap="word", undo=True, font=("Segoe UI", 12),
            padx=14, pady=12, bg=bg, fg=self.colors["text"],
            insertbackground=self.colors["text"],
            selectbackground=self.colors["accent"], selectforeground="#ffffff",
            relief="flat", highlightthickness=1,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["accent"], borderwidth=0,
        )

    def build_ui(self):
        c = self.colors
        for child in self.root.winfo_children():
            child.destroy()

        outer = tk.Frame(self.root, bg=c["bg"])
        outer.pack(fill="both", expand=True)

        sidebar = tk.Frame(outer, bg=c["sidebar"], width=200)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Frame(sidebar, bg=c["border"], width=1).pack(side="right", fill="y")

        brand = tk.Frame(sidebar, bg=c["sidebar"])
        brand.pack(fill="x", padx=18, pady=(24, 24))
        logo = tk.Canvas(brand, width=36, height=36, bg=c["sidebar"], highlightthickness=0)
        logo.pack(side="left")
        logo.create_oval(1, 1, 35, 35, fill=c["accent"], outline="")
        logo.create_text(18, 18, text="ST", fill="#fff", font=("Segoe UI", 10, "bold"))
        btxt = tk.Frame(brand, bg=c["sidebar"])
        btxt.pack(side="left", padx=(10, 0))
        tk.Label(btxt, text="Screen", bg=c["sidebar"], fg=c["text"],
                 font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(btxt, text="Translator", bg=c["sidebar"], fg=c["muted"],
                 font=("Segoe UI", 9)).pack(anchor="w")

        def nav(text, cmd, active=False):
            bg = c["panel2"] if active else c["sidebar"]
            fg = c["text"] if active else c["muted"]
            f = tk.Frame(sidebar, bg=bg, cursor="hand2")
            f.pack(fill="x", padx=10, pady=2)
            tk.Frame(f, bg=c["accent"] if active else bg, width=3).pack(side="left", fill="y")
            lbl = tk.Label(f, text=text, bg=bg, fg=fg, anchor="w",
                           font=("Segoe UI", 10, "bold" if active else "normal"),
                           padx=12, pady=10)
            lbl.pack(fill="x")
            for w in (f, lbl):
                w.bind("<Button-1>", lambda e, c=cmd: c())

        nav("Перевод", lambda: None, True)
        nav("История", self.show_history)
        nav("Настройки", self.open_settings)

        bottom = tk.Frame(sidebar, bg=c["sidebar"])
        bottom.pack(side="bottom", fill="x", padx=16, pady=18)
        tk.Label(bottom, text="СТАТУС", bg=c["sidebar"], fg=c["muted"],
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.status_dot = tk.Label(bottom, text="●  Готово", bg=c["sidebar"],
                                   fg=c["success"], font=("Segoe UI", 9, "bold"))
        self.status_dot.pack(anchor="w", pady=(4, 0))

        main = tk.Frame(outer, bg=c["bg"])
        main.pack(side="left", fill="both", expand=True)
        content = tk.Frame(main, bg=c["bg"])
        content.pack(fill="both", expand=True, padx=28, pady=24)

        header = tk.Frame(content, bg=c["bg"])
        header.pack(fill="x", pady=(0, 16))
        left_h = tk.Frame(header, bg=c["bg"])
        left_h.pack(side="left")
        tk.Label(left_h, text="Переводчик", bg=c["bg"], fg=c["text"],
                 font=("Segoe UI", 22, "bold")).pack(anchor="w")
        tk.Label(left_h, text="Выделение заменяется на месте · Shift+Alt+S / Shift+Alt+X",
                 bg=c["bg"], fg=c["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        right_h = tk.Frame(header, bg=c["bg"])
        right_h.pack(side="right")
        self._btn(right_h, "Светлая" if self.theme == "dark" else "Тёмная",
                  self.toggle_theme).pack(side="left", padx=(0, 6))
        self._btn(right_h, "Настройки", self.open_settings).pack(side="left")

        cards = tk.Frame(content, bg=c["bg"])
        cards.pack(fill="x", pady=(0, 14))
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)
        self._card(cards, "Заменить выделение", "Текст меняется там, где выделен",
                   pretty_hotkey(self.replace_hotkey)).grid(
            row=0, column=0, sticky="ew", padx=(0, 8))
        self._card(cards, "Область экрана", "OCR + перевод",
                   pretty_hotkey(self.screen_hotkey),
                   command=self.screen_translate).grid(
            row=0, column=1, sticky="ew", padx=(8, 0))

        card = tk.Frame(content, bg=c["panel"],
                        highlightthickness=1, highlightbackground=c["border"])
        card.pack(fill="both", expand=True)

        controls = tk.Frame(card, bg=c["panel"])
        controls.pack(fill="x", padx=18, pady=(14, 10))
        tk.Label(controls, text="Язык", bg=c["panel"], fg=c["muted"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.source = tk.StringVar(value="auto")
        self.target = tk.StringVar(value="ru")
        ttk.Combobox(controls, textvariable=self.source, state="readonly",
                     values=("auto", "ru", "en"), width=10).pack(side="left", padx=(8, 8))
        tk.Button(controls, text="↔", command=self.swap, bg=c["panel2"], fg=c["text"],
                  relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                  activebackground=c["border"]).pack(side="left")
        ttk.Combobox(controls, textvariable=self.target, state="readonly",
                     values=("ru", "en", "auto"), width=10).pack(side="left", padx=(8, 0))
        tk.Button(controls, text="Очистить", command=self.clear, bg=c["panel2"],
                  fg=c["muted"], relief="flat", bd=0, padx=12, pady=5, cursor="hand2",
                  activebackground=c["border"], font=("Segoe UI", 9)).pack(side="right")

        pane = tk.PanedWindow(card, orient="horizontal", sashwidth=6,
                              bg=c["panel"], bd=0, relief="flat")
        pane.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        left = tk.Frame(pane, bg=c["input"],
                        highlightbackground=c["border"], highlightthickness=1)
        right = tk.Frame(pane, bg=c["output"],
                         highlightbackground=c["border"], highlightthickness=1)
        pane.add(left, minsize=260)
        pane.add(right, minsize=260)

        def head(parent, title, hint):
            h = tk.Frame(parent, bg=parent.cget("bg"))
            h.pack(fill="x", padx=12, pady=(10, 2))
            tk.Label(h, text=title, bg=parent.cget("bg"), fg=c["text"],
                     font=("Segoe UI", 9, "bold")).pack(side="left")
            tk.Label(h, text=hint, bg=parent.cget("bg"), fg=c["muted"],
                     font=("Segoe UI", 8)).pack(side="right")

        head(left, "ОРИГИНАЛ", "перевод автоматически")
        head(right, "ПЕРЕВОД", "результат")
        self.input = self.make_text(left, c["input"])
        self.input.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.input.bind("<KeyRelease>", self._on_input_change)
        self.output = self.make_text(right, c["output"])
        self.output.configure(state="disabled")
        self.output.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        footer = tk.Frame(card, bg=c["panel"])
        footer.pack(fill="x", padx=18, pady=(0, 14))
        for label, cmd in (("Вставить", self.paste), ("Копировать", self.copy)):
            tk.Button(footer, text=label, command=cmd, bg=c["panel2"], fg=c["text"],
                      relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
                      activebackground=c["border"], font=("Segoe UI", 9)).pack(
                side="left", padx=(0, 6))
        self.status_var = tk.StringVar(value="Готово")
        tk.Label(footer, textvariable=self.status_var, bg=c["panel"], fg=c["muted"],
                 font=("Segoe UI", 9)).pack(side="right")

    def _btn(self, parent, text, command):
        c = self.colors
        return tk.Button(
            parent, text=text, command=command, bg=c["panel2"], fg=c["text"],
            activebackground=c["border"], relief="flat", bd=0, padx=12, pady=7,
            cursor="hand2", font=("Segoe UI", 9, "bold"),
        )

    def _card(self, parent, title, desc, hotkey, command=None):
        c = self.colors
        card = tk.Frame(parent, bg=c["panel"],
                        highlightthickness=1, highlightbackground=c["border"],
                        cursor="hand2" if command else "arrow")
        inner = tk.Frame(card, bg=c["panel"])
        inner.pack(fill="both", expand=True, padx=14, pady=12)
        icon = tk.Label(inner, text="⌘", bg=c["accent"], fg="#fff",
                        font=("Segoe UI", 11, "bold"), width=3, pady=3)
        icon.pack(side="left", padx=(0, 10))
        txt = tk.Frame(inner, bg=c["panel"])
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text=title, bg=c["panel"], fg=c["text"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(txt, text=desc, bg=c["panel"], fg=c["muted"],
                 font=("Segoe UI", 8)).pack(anchor="w")
        chip = tk.Label(inner, text=hotkey, bg=c["panel2"], fg=c["text"],
                        font=("Segoe UI", 9, "bold"), padx=10, pady=5)
        chip.pack(side="right")
        if command:
            for w in (card, inner, icon, txt, chip):
                w.bind("<Button-1>", lambda e, cmd=command: cmd())
        return card

    def set_status(self, text, ok=True):
        if self.status_var:
            self.status_var.set(text)
        if self.status_dot:
            self.status_dot.configure(
                text="●  " + text,
                fg=self.colors["success"] if ok else self.colors["danger"],
            )
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def _on_input_change(self, event=None):
        if self._auto_job:
            self.root.after_cancel(self._auto_job)
        self._auto_job = self.root.after(600, self._auto_translate)

    def _auto_translate(self):
        self._auto_job = None
        if self.busy:
            return
        text = self.input.get("1.0", "end").strip()
        if not text:
            self.set_output("")
            return
        self.start_translation(text, self.source.get(), self.target.get(), "auto")

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.save_settings()
        self.setup_style()
        self.build_ui()
        self.register_hotkeys()
        self.set_status("Тема изменена")

    def unregister_hotkeys(self):
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass

    def register_hotkeys(self):
        self.unregister_hotkeys()
        errors = []

        def safe_replace():
            if self.closing:
                return
            try:
                self.root.after(0, self._hotkey_replace)
            except Exception:
                pass

        def safe_screen():
            if self.closing:
                return
            try:
                self.root.after(0, self.screen_translate)
            except Exception:
                pass

        try:
            hk1 = normalize_hotkey(self.replace_hotkey)
            keyboard.add_hotkey(hk1, safe_replace, suppress=False)
        except Exception as e:
            errors.append(f"Замена: {pretty_hotkey(self.replace_hotkey)} — {e}")

        try:
            hk2 = normalize_hotkey(self.screen_hotkey)
            keyboard.add_hotkey(hk2, safe_screen, suppress=False)
        except Exception as e:
            errors.append(f"Экран: {pretty_hotkey(self.screen_hotkey)} — {e}")

        if errors:
            self.set_status("Ошибка хоткеев", ok=False)
            messagebox.showwarning("Горячие клавиши", "\n".join(errors))
        else:
            self.set_status(
                f"{pretty_hotkey(self.replace_hotkey)}  ·  {pretty_hotkey(self.screen_hotkey)}"
            )

    def _hotkey_replace(self):
        target = foreground_hwnd()
        self.replace_selection(target)

    def open_settings(self):
        c = self.colors
        win = tk.Toplevel(self.root)
        win.title("Настройки")
        win.geometry("520x480")
        win.minsize(480, 440)
        win.configure(bg=c["bg"])
        win.transient(self.root)
        win.resizable(False, False)

        footer = tk.Frame(win, bg=c["bg"])
        footer.pack(side="bottom", fill="x", padx=20, pady=16)

        body = tk.Frame(win, bg=c["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=(18, 0))

        tk.Label(body, text="Настройки", bg=c["bg"], fg=c["text"],
                 font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(body, text="Хоткеи, OCR-ключ, тема", bg=c["bg"], fg=c["muted"],
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 12))

        box = tk.Frame(body, bg=c["panel"],
                       highlightthickness=1, highlightbackground=c["border"])
        box.pack(fill="x", pady=(0, 10))
        tk.Label(box, text="Горячие клавиши", bg=c["panel"], fg=c["text"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(12, 8))

        rv = tk.StringVar(value=pretty_hotkey(self.replace_hotkey))
        sv = tk.StringVar(value=pretty_hotkey(self.screen_hotkey))

        def row(parent, label, var, kind):
            f = tk.Frame(parent, bg=c["panel"])
            f.pack(fill="x", padx=14, pady=4)
            tk.Label(f, text=label, bg=c["panel"], fg=c["text"],
                     font=("Segoe UI", 9), width=22, anchor="w").pack(side="left")
            tk.Label(f, textvariable=var, bg=c["panel2"], fg=c["text"],
                     font=("Segoe UI", 9, "bold"), padx=8, pady=4).pack(side="left", padx=6)
            b = tk.Button(f, text="Изменить", bg=c["panel2"], fg=c["text"],
                          relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                          activebackground=c["border"], font=("Segoe UI", 9))
            b.pack(side="right")
            b.configure(command=lambda: self._capture_hk(kind, b, var, win))

        row(box, "Заменить выделение", rv, "replace")
        row(box, "Область экрана", sv, "screen")
        tk.Label(box, text="Рекомендуется Shift+Alt+S и Shift+Alt+X",
                 bg=c["panel"], fg=c["muted"], font=("Segoe UI", 8)).pack(
            anchor="w", padx=14, pady=(4, 12))

        ocr_box = tk.Frame(body, bg=c["panel"],
                           highlightthickness=1, highlightbackground=c["border"])
        ocr_box.pack(fill="x", pady=(0, 10))
        tk.Label(ocr_box, text="OCR API ключ", bg=c["panel"], fg=c["text"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        tk.Label(ocr_box, text="https://ocr.space/ocrapi",
                 bg=c["panel"], fg=c["muted"], font=("Segoe UI", 8)).pack(
            anchor="w", padx=14, pady=(0, 6))
        ocr_var = tk.StringVar(
            value="" if self.ocr_key == "helloworld" else self.ocr_key
        )
        ent = tk.Entry(ocr_box, textvariable=ocr_var, font=("Segoe UI", 10),
                       bg=c["input"], fg=c["text"], insertbackground=c["text"],
                       relief="flat", highlightthickness=1,
                       highlightbackground=c["border"], highlightcolor=c["accent"])
        ent.pack(fill="x", padx=14, pady=(0, 12), ipady=5)

        th_box = tk.Frame(body, bg=c["panel"],
                          highlightthickness=1, highlightbackground=c["border"])
        th_box.pack(fill="x")
        tk.Label(th_box, text="Тема", bg=c["panel"], fg=c["text"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(12, 6))
        theme_var = tk.StringVar(value=self.theme)
        row_t = tk.Frame(th_box, bg=c["panel"])
        row_t.pack(fill="x", padx=14, pady=(0, 12))
        for text, val in (("Тёмная", "dark"), ("Светлая", "light")):
            tk.Radiobutton(
                row_t, text=text, variable=theme_var, value=val,
                bg=c["panel"], fg=c["text"], selectcolor=c["panel2"],
                activebackground=c["panel"], font=("Segoe UI", 9),
            ).pack(side="left", padx=(0, 16))

        def apply():
            self.theme = theme_var.get() if theme_var.get() in ("dark", "light") else "dark"
            self.ocr_key = ocr_var.get().strip() or "helloworld"
            self.save_settings()
            win.destroy()
            self.setup_style()
            self.build_ui()
            self.register_hotkeys()
            self.set_status("Настройки сохранены")

        tk.Button(footer, text="Применить", command=apply, bg=c["accent"], fg="#fff",
                  activebackground=c["accent_hover"], relief="flat", bd=0,
                  padx=20, pady=9, cursor="hand2",
                  font=("Segoe UI", 10, "bold")).pack(side="right")
        tk.Button(footer, text="Закрыть", command=win.destroy, bg=c["panel2"],
                  fg=c["text"], relief="flat", bd=0, padx=16, pady=9, cursor="hand2",
                  activebackground=c["border"], font=("Segoe UI", 10)).pack(
            side="right", padx=(0, 8))

    def _capture_hk(self, kind, button, variable, window):
        button.configure(state="disabled", text="...")
        variable.set("Нажми Ctrl/Shift/Alt + клавишу…")
        window.focus_force()

        def on_key(e):
            key = e.keysym.lower()
            if key in ("control_l", "control_r", "shift_l", "shift_r",
                       "alt_l", "alt_r", "win_l", "win_r", "meta_l", "meta_r"):
                return "break"
            if key == "escape":
                variable.set("Отменено")
                button.configure(state="normal", text="Изменить")
                window.unbind("<KeyPress>")
                return "break"
            key_map = {"return": "enter", "space": "space"}
            key = key_map.get(key, key)
            mods = []
            if e.state & 0x4:
                mods.append("ctrl")
            if e.state & 0x1:
                mods.append("shift")
            if e.state & 0x20000:
                mods.append("alt")
            if not mods:
                variable.set("Нужен Ctrl/Shift/Alt")
                return "break"
            try:
                combo = normalize_hotkey("+".join(mods + [key]))
                if kind == "replace":
                    self.replace_hotkey = combo
                else:
                    self.screen_hotkey = combo
                self.save_settings()
                variable.set(pretty_hotkey(combo))
                button.configure(state="normal", text="Изменить")
                window.unbind("<KeyPress>")
                self.register_hotkeys()
            except Exception as ex:
                variable.set(str(ex))
            return "break"

        window.bind("<KeyPress>", on_key)

    def start_translation(self, text, source, target, mode):
        self.busy = True
        self.set_status("Перевожу…")
        threading.Thread(
            target=self._tr_thread, args=(text, source, target, mode), daemon=True
        ).start()

    def _tr_thread(self, text, source, target, mode):
        try:
            result = translate_text(text, source, target)
            self.root.after(0, lambda: self._tr_done(text, result, mode))
        except Exception as e:
            self.root.after(0, lambda: self._tr_err(str(e)))

    def _tr_done(self, text, result, mode):
        self.busy = False
        self.set_output(result)
        if mode != "auto":
            self.add_history(text, result)
        self.set_status("Готово" if mode != "replace" else "Заменено на месте")

    def _tr_err(self, err):
        self.busy = False
        self.set_status("Ошибка", ok=False)
        messagebox.showerror("Screen Translator", err)

    def set_output(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def add_history(self, src, dst):
        self.history.insert(0, [src, dst])
        self.history = self.history[:30]
        try:
            self.save_settings()
        except Exception:
            pass

    def replace_selection(self, target_hwnd):
        if self.busy:
            return
        self.root.after(120, lambda: self._do_replace(target_hwnd))

    def _do_replace(self, target_hwnd):
        old_clip = ""
        try:
            old_clip = win_clipboard_get()
            before = clipboard_sequence()
            win_clipboard_set("")
            cleared = clipboard_sequence()
            time.sleep(0.06)

            selected = ""
            for _ in range(3):
                send_ctrl("c")
                deadline = time.time() + 1.2
                while time.time() < deadline:
                    seq = clipboard_sequence()
                    cand = win_clipboard_get().strip()
                    if cand and seq != before and seq != cleared:
                        selected = cand
                        break
                    time.sleep(0.03)
                if selected:
                    break
                time.sleep(0.08)

            if not selected:
                try:
                    win_clipboard_set(old_clip)
                except Exception:
                    pass
                self.set_status("Нет выделения", ok=False)
                return

            self.busy = True
            self.set_status("Перевожу…")

            def worker():
                try:
                    result = translate_text(selected, "auto", "auto")
                    self.root.after(
                        0, lambda: self._paste_replace(selected, result, target_hwnd, old_clip)
                    )
                except Exception as e:
                    self.root.after(0, lambda: self._tr_err(str(e)))

            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            self.set_status("Ошибка", ok=False)
            messagebox.showerror("Замена", str(e))

    def _paste_replace(self, selected, result, target_hwnd, old_clip):
        try:
            activate_window(target_hwnd)
            time.sleep(0.08)
            win_clipboard_set(result)
            time.sleep(0.06)
            send_ctrl("v")
            time.sleep(0.35)
            try:
                win_clipboard_set(old_clip)
            except Exception:
                pass
            try:
                self.input.delete("1.0", "end")
                self.input.insert("1.0", selected)
                self.set_output(result)
                self.add_history(selected, result)
            except Exception:
                pass
            self.busy = False
            self.set_status("Заменено на месте")
        except Exception as e:
            self.busy = False
            self.set_status("Ошибка", ok=False)
            messagebox.showerror("Замена", str(e))

    def screen_translate(self):
        if self.busy:
            return
        self.root.withdraw()
        self.root.update_idletasks()
        selector = ScreenSelector(self.root)
        self.root.wait_window(selector.win)
        self.root.deiconify()
        if not selector.result:
            self.set_status("Отменено")
            return
        x1, y1, x2, y2 = selector.result
        if x2 - x1 < 8 or y2 - y1 < 8:
            messagebox.showinfo("Screen Translator", "Область слишком маленькая.")
            return
        self.busy = True
        self.set_status("Распознаю…")

        def worker():
            try:
                with mss.mss() as sct:
                    shot = sct.grab({
                        "left": x1, "top": y1,
                        "width": x2 - x1, "height": y2 - y1,
                    })
                image = Image.frombytes("RGB", shot.size, shot.rgb)
                text = ocr_image(image, self.ocr_key)
                result = translate_text(text, "auto", "auto")
                self.root.after(0, lambda: self._screen_done(text, result))
            except Exception as e:
                self.root.after(0, lambda: self._tr_err(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _screen_done(self, text, result):
        self.busy = False
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)
        self.set_output(result)
        self.add_history(text, result)
        self.set_status("Готово")
        self.root.lift()

    def swap(self):
        s, t = self.source.get(), self.target.get()
        self.source.set(t)
        self.target.set(s)
        left = self.input.get("1.0", "end").strip()
        right = self.output.get("1.0", "end").strip()
        if right:
            self.input.delete("1.0", "end")
            self.input.insert("1.0", right)
            self.set_output(left)

    def paste(self):
        text = win_clipboard_get()
        if text:
            self.input.delete("1.0", "end")
            self.input.insert("1.0", text)
            self._on_input_change()
            self.set_status("Вставлено")

    def copy(self):
        text = self.output.get("1.0", "end").strip()
        if text:
            win_clipboard_set(text)
            self.set_status("Скопировано")

    def clear(self):
        self.input.delete("1.0", "end")
        self.set_output("")
        self.set_status("Готово")

    def show_history(self):
        c = self.colors
        win = tk.Toplevel(self.root)
        win.title("История")
        win.geometry("700x420")
        win.configure(bg=c["bg"])
        win.transient(self.root)
        frame = tk.Frame(win, bg=c["bg"])
        frame.pack(fill="both", expand=True, padx=16, pady=14)
        tk.Label(frame, text="История", bg=c["bg"], fg=c["text"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 8))
        box = tk.Listbox(frame, font=("Segoe UI", 10), bg=c["panel"], fg=c["text"],
                         selectbackground=c["accent"], selectforeground="#fff",
                         relief="flat", highlightthickness=1,
                         highlightbackground=c["border"])
        box.pack(fill="both", expand=True)
        for src, dst in self.history:
            box.insert("end", f"{' '.join(src.split())[:90]} → {' '.join(dst.split())[:90]}")

        def use():
            sel = box.curselection()
            if not sel:
                return
            src, dst = self.history[sel[0]]
            self.input.delete("1.0", "end")
            self.input.insert("1.0", src)
            self.set_output(dst)
            win.destroy()

        tk.Button(frame, text="Открыть", command=use, bg=c["accent"], fg="#fff",
                  relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
                  font=("Segoe UI", 9, "bold")).pack(pady=(10, 0))

    def close(self):
        self.closing = True
        self.unregister_hotkeys()
        self.root.destroy()


if __name__ == "__main__":
    try:
        App().root.mainloop()
    except Exception as exc:
        try:
            messagebox.showerror("Screen Translator", str(exc))
        except Exception:
            print(exc)
