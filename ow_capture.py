"""
OW Scoreboard Capture: press a hotkey to save a screenshot of the end-of-match
scoreboard, then click one button to zip everything up for sending.

Windows only. Built into a standalone .exe with build.ps1; see README.md.

What it does:
  - registers one global hotkey with the Win32 RegisterHotKey API (the same
    mechanism ShareX / OBS use). It is not a keyboard hook: no other key
    presses are seen, and no input is read or sent to the game;
  - on that hotkey, saves a lossless PNG of the chosen monitor to the chosen
    folder;
  - on request, zips the saved PNGs plus a short info.txt (app version,
    optional name, capture count, monitor resolutions) into a packages/ folder
    and opens that folder;
  - remembers its settings in settings.json, in an OWScoreboardCapture folder
    under %APPDATA%.
It makes no network connections and starts no other programs. The process is
per-monitor DPI aware so captures are at full native resolution on displays
with Windows scaling (125%, 150%, ...).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime as dt
import io
import json
import math
import os
import queue
import shutil
import sys
import threading
import tkinter as tk
import wave
import winsound
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import ImageGrab

APP_NAME = "OW Scoreboard Capture"
APP_VERSION = "1.0.2"
SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "OWScoreboardCapture" / "settings.json"
DEFAULT_SAVE_DIR = Path.home() / "Pictures" / "OW Scoreboard Captures"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
HOTKEY_ID = 1

# Keys offered in the picker. F9/F10 are left out: Overwatch uses them on the
# scoreboard screen ("PRESS F9 TO CAPTURE PLAY OF THE GAME", "F10 TO REQUEUE"),
# and a registered hotkey takes the key away from the game.
KEYS = {**{f"F{i}": 0x6F + i for i in range(1, 25) if i not in (9, 10)},
        "Insert": 0x2D, "Home": 0x24, "End": 0x23, "Page Up": 0x21, "Page Down": 0x22,
        "Pause": 0x13, "Scroll Lock": 0x91, "Print Screen": 0x2C,
        **{f"Numpad {i}": 0x60 + i for i in range(10)}}
DEFAULT_KEY = "F8"


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------
def make_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


def monitors() -> list[dict]:
    """[{"name", "bbox": (l, t, r, b), "primary"}], primary first."""
    found = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM)

    def cb(hmon, _hdc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcMonitor
        found.append({"bbox": (r.left, r.top, r.right, r.bottom), "primary": bool(info.dwFlags & 1)})
        return True

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    found.sort(key=lambda m: not m["primary"])
    for i, m in enumerate(found, 1):
        l, t, r, b = m["bbox"]
        m["name"] = f"Monitor {i}: {r - l}x{b - t}" + (" (primary)" if m["primary"] else "")
    return found


class HotkeyThread(threading.Thread):
    """Registers one global hotkey and calls `on_press` from its own thread.
    RegisterHotKey binds to the calling thread, so that thread must also pump
    its message queue."""

    def __init__(self, modifiers: int, vk: int, on_press, on_result):
        super().__init__(daemon=True)
        self.modifiers, self.vk = modifiers | MOD_NOREPEAT, vk
        self.on_press, self.on_result = on_press, on_result
        self.thread_id = None

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        ok = user32.RegisterHotKey(None, HOTKEY_ID, self.modifiers, self.vk)
        self.on_result(bool(ok))
        if not ok:
            return
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.on_press()
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.join(timeout=2)


# ---------------------------------------------------------------------------
# Capture sound
# ---------------------------------------------------------------------------
def make_beep(freq: float = 1047.0, ms: int = 90, volume: float = 0.20, rate: int = 44100) -> bytes:
    """A short sine tone as an in-memory WAV, faded in and out so it doesn't click.
    Played through the normal audio device, so it follows the system volume."""
    n, fade = rate * ms // 1000, rate * 8 // 1000
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / fade, (n - 1 - i) / fade)
        v = int(32767 * volume * env * math.sin(2 * math.pi * freq * i / rate))
        frames += v.to_bytes(2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


BEEP = make_beep()


def play_beep():
    """Blocks for the length of the beep (winsound can't play from memory asynchronously)."""
    winsound.PlaySound(BEEP, winsound.SND_MEMORY)


# ---------------------------------------------------------------------------
# Capture and packaging
# ---------------------------------------------------------------------------
def capture(bbox, save_dir: Path) -> tuple[Path, bool]:
    """Grab `bbox` and save it as PNG. Returns (path, looks_black)."""
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    save_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = save_dir / f"scoreboard_{stamp}.png"
    n = 2
    while path.exists():
        path = save_dir / f"scoreboard_{stamp}_{n}.png"
        n += 1
    # An exclusive-fullscreen game often captures as a solid black frame.
    looks_black = max(img.convert("L").resize((64, 36)).getextrema()) < 12
    img.save(path, compress_level=3)
    return path, looks_black


def pending(save_dir: Path) -> list[Path]:
    """Captures not yet packaged (top-level PNGs only; sent/ and packages/ are excluded)."""
    return sorted(save_dir.glob("*.png")) if save_dir.exists() else []


def package(save_dir: Path, contributor: str = "") -> tuple[Path, int]:
    """Zip all pending captures into save_dir/packages/, then move them into
    save_dir/sent/ so the next package only contains new ones."""
    files = pending(save_dir)
    if not files:
        raise ValueError("There are no new captures to package.")
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    who = "".join(c for c in contributor if c.isalnum() or c in "-_")[:32]
    name = f"OW-scoreboards_{who + '_' if who else ''}{stamp}_{len(files)}shots.zip"
    out_dir = save_dir / "packages"
    out_dir.mkdir(exist_ok=True)
    zip_path = out_dir / name
    info = {
        "app": APP_NAME, "version": APP_VERSION, "contributor": contributor or None,
        "packaged": dt.datetime.now().isoformat(timespec="seconds"),
        "captures": len(files), "monitors": [m["name"] for m in monitors()],
    }
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:  # PNGs are already compressed
        for f in files:
            z.write(f, f.name)
        z.writestr("info.txt", json.dumps(info, indent=2, ensure_ascii=False))
    sent = save_dir / "sent"
    sent.mkdir(exist_ok=True)
    for f in files:
        shutil.move(str(f), str(sent / f.name))
    return zip_path, len(files)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load_settings(path: Path) -> dict:
    s = {"save_dir": str(DEFAULT_SAVE_DIR), "key": DEFAULT_KEY, "ctrl": False, "alt": False,
         "shift": False, "monitor": 0, "beep": True, "contributor": ""}
    try:
        s.update(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if s["key"] not in KEYS:
        s["key"] = DEFAULT_KEY
    return s


def save_settings(path: Path, s: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def open_folder(path: Path):
    """Show a folder in File Explorer (the Windows shell's default action for a folder)."""
    os.startfile(str(path))


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk, settings_path: Path):
        self.root, self.settings_path = root, settings_path
        self.s = load_settings(settings_path)
        self.events: queue.Queue = queue.Queue()
        self.saver = ThreadPoolExecutor(max_workers=1)
        self.hotkey: HotkeyThread | None = None
        self.mons = monitors()
        self.session_count = 0

        root.title(f"{APP_NAME} {APP_VERSION}")
        root.resizable(False, False)
        f = ttk.Frame(root, padding=12)
        f.grid(sticky="nsew")

        ttk.Label(f, text="Hotkey:").grid(row=0, column=0, sticky="w")
        hk = ttk.Frame(f)
        hk.grid(row=0, column=1, columnspan=2, sticky="w", pady=2)
        self.ctrl, self.alt, self.shift = (tk.BooleanVar(value=self.s[k]) for k in ("ctrl", "alt", "shift"))
        for text, var in (("Ctrl", self.ctrl), ("Alt", self.alt), ("Shift", self.shift)):
            ttk.Checkbutton(hk, text=text, variable=var, command=self.apply_hotkey).pack(side="left")
        self.key = tk.StringVar(value=self.s["key"])
        cb = ttk.Combobox(hk, textvariable=self.key, values=list(KEYS), state="readonly", width=12)
        cb.pack(side="left", padx=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.apply_hotkey())

        ttk.Label(f, text="Monitor:").grid(row=1, column=0, sticky="w")
        self.mon = ttk.Combobox(f, values=[m["name"] for m in self.mons], state="readonly", width=34)
        self.mon.current(min(self.s["monitor"], len(self.mons) - 1))
        self.mon.grid(row=1, column=1, columnspan=2, sticky="w", pady=2)
        self.mon.bind("<<ComboboxSelected>>", lambda _e: self.persist())

        ttk.Label(f, text="Save to:").grid(row=2, column=0, sticky="w")
        self.save_dir = tk.StringVar(value=self.s["save_dir"])
        ttk.Entry(f, textvariable=self.save_dir, width=38, state="readonly").grid(row=2, column=1, sticky="w", pady=2)
        ttk.Button(f, text="Browse…", command=self.browse).grid(row=2, column=2, padx=(6, 0))

        ttk.Label(f, text="Your name (optional):").grid(row=3, column=0, sticky="w")
        self.contributor = tk.StringVar(value=self.s["contributor"])
        e = ttk.Entry(f, textvariable=self.contributor, width=24)
        e.grid(row=3, column=1, sticky="w", pady=2)
        e.bind("<FocusOut>", lambda _e: self.persist())

        self.beep = tk.BooleanVar(value=self.s["beep"])
        ttk.Checkbutton(f, text="Beep on capture", variable=self.beep, command=self.persist).grid(
            row=4, column=1, sticky="w", pady=2)

        ttk.Separator(f).grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
        self.status = tk.StringVar()
        ttk.Label(f, textvariable=self.status, wraplength=380, justify="left").grid(
            row=6, column=0, columnspan=3, sticky="w")
        self.counts = tk.StringVar()
        ttk.Label(f, textvariable=self.counts, font=("Segoe UI", 10, "bold")).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(4, 8))

        b = ttk.Frame(f)
        b.grid(row=8, column=0, columnspan=3, sticky="ew")
        ttk.Button(b, text="Open folder", command=self.open_folder).pack(side="left")
        ttk.Button(b, text="Package for sending", command=self.package).pack(side="right")

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.apply_hotkey()
        self.refresh_counts()
        self.root.after(100, self.poll)

    # -- hotkey ---------------------------------------------------------------
    def hotkey_label(self) -> str:
        mods = [n for n, v in (("Ctrl", self.ctrl), ("Alt", self.alt), ("Shift", self.shift)) if v.get()]
        return "+".join(mods + [self.key.get()])

    def apply_hotkey(self):
        if self.hotkey:
            self.hotkey.stop()
        mods = (MOD_CONTROL if self.ctrl.get() else 0) | (MOD_ALT if self.alt.get() else 0) | \
               (MOD_SHIFT if self.shift.get() else 0)
        self.hotkey = HotkeyThread(mods, KEYS[self.key.get()],
                                   on_press=lambda: self.events.put(("press", None)),
                                   on_result=lambda ok: self.events.put(("registered", ok)))
        self.hotkey.start()
        self.persist()

    # -- events (all UI updates happen on the Tk thread) ----------------------
    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "registered":
                    self.status.set(f"Ready. Press {self.hotkey_label()} on the end-of-match scoreboard."
                                    if data else
                                    f"{self.hotkey_label()} is already used by another program. Pick a different key.")
                elif kind == "press":
                    bbox = self.mons[self.mon.current()]["bbox"]
                    self.saver.submit(self._capture, bbox, Path(self.save_dir.get()), self.beep.get())
                elif kind == "saved":
                    path, black = data
                    self.session_count += 1
                    self.status.set(f"Saved {path.name}" + (
                        "\n⚠ That capture is black. Set Overwatch to Borderless Windowed "
                        "(Options → Video → Display Mode) and try again." if black else ""))
                    self.refresh_counts()
                elif kind == "error":
                    self.status.set(f"Capture failed: {data}")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def _capture(self, bbox, save_dir, beep: bool):
        """Runs on the saver thread, so the beep never stalls the window."""
        try:
            result = capture(bbox, save_dir)
        except Exception as e:  # report any capture/save failure in the UI rather than dying
            self.events.put(("error", str(e)))
            return
        if beep:
            play_beep()      # after the PNG is written: the beep confirms the save
        self.events.put(("saved", result))

    # -- buttons --------------------------------------------------------------
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.save_dir.get(), title="Choose where captures are saved")
        if d:
            self.save_dir.set(str(Path(d)))
            self.persist()
            self.refresh_counts()

    def open_folder(self):
        p = Path(self.save_dir.get())
        p.mkdir(parents=True, exist_ok=True)
        open_folder(p)

    def package(self):
        try:
            zip_path, n = package(Path(self.save_dir.get()), self.contributor.get().strip())
        except ValueError as e:
            messagebox.showinfo(APP_NAME, str(e))
            return
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Packaging failed: {e}")
            return
        self.refresh_counts()
        self.status.set(f"Packaged {n} capture(s) into {zip_path.name}. Send that file.")
        open_folder(zip_path.parent)

    def refresh_counts(self):
        n = len(pending(Path(self.save_dir.get())))
        self.counts.set(f"{n} capture(s) waiting to be sent" +
                        (f"  ·  {self.session_count} this session" if self.session_count else ""))

    def persist(self):
        self.s.update(save_dir=self.save_dir.get(), key=self.key.get(), ctrl=self.ctrl.get(),
                      alt=self.alt.get(), shift=self.shift.get(), monitor=max(self.mon.current(), 0),
                      beep=self.beep.get(), contributor=self.contributor.get().strip())
        save_settings(self.settings_path, self.s)

    def quit(self):
        self.persist()
        if self.hotkey:
            self.hotkey.stop()
        self.saver.shutdown(wait=True)
        self.root.destroy()


def main():
    make_dpi_aware()
    root = tk.Tk()
    App(root, SETTINGS_PATH)
    root.mainloop()


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit(f"{APP_NAME} runs on Windows only.")
    main()
