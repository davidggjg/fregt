import sys
import os
import json
import time
import threading
import subprocess
import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import pystray
from PIL import Image, ImageDraw
import keyboard
import mss
import cv2
import numpy as np

CONFIG_FILE = Path(os.environ.get("APPDATA", ".")) / "ReplayBuffer" / "config.json"
SAVES_DIR = Path(os.environ.get("USERPROFILE", ".")) / "Videos" / "ReplayBuffer"

DEFAULT_CONFIG = {
    "hotkey": "F9",
    "buffer_minutes": 5,
    "fps": 30,
    "quality": 80
}


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                return {**DEFAULT_CONFIG, **cfg}
        except:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


class ReplayBuffer:
    def __init__(self, config):
        self.config = config
        self.frames = []
        self.lock = threading.Lock()
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _capture_loop(self):
        fps = self.config["fps"]
        max_frames = int(self.config["buffer_minutes"] * 60 * fps)
        interval = 1.0 / fps

        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            while self.running:
                start = time.time()
                try:
                    img = sct.grab(monitor)
                    frame = np.array(img)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    # Resize for performance (720p)
                    h, w = frame.shape[:2]
                    if w > 1280:
                        scale = 1280 / w
                        frame = cv2.resize(frame, (1280, int(h * scale)))

                    with self.lock:
                        self.frames.append(frame)
                        if len(self.frames) > max_frames:
                            self.frames = self.frames[-max_frames:]
                except Exception as e:
                    pass
                elapsed = time.time() - start
                sleep_time = interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

    def save(self):
        with self.lock:
            if not self.frames:
                return None
            frames_copy = list(self.frames)

        SAVES_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        output_path = SAVES_DIR / f"Replay_{timestamp}.mp4"

        try:
            h, w = frames_copy[0].shape[:2]
            fps = self.config["fps"]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(str(output_path), fourcc, fps, (w, h))
            for frame in frames_copy:
                writer.write(frame)
            writer.release()
            return str(output_path)
        except Exception as e:
            return None

    def update_config(self, config):
        self.config = config


class ReplayBufferApp:
    def __init__(self):
        self.config = load_config()
        self.buffer = ReplayBuffer(self.config)
        self.recording = False
        self.hotkey_registered = None
        self.setting_hotkey = False
        self.tray_icon = None

        self._build_ui()
        self._register_hotkey()

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Replay Buffer")
        self.root.geometry("480x520")
        self.root.resizable(False, False)
        self.root.configure(bg="#0d0d0d")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        try:
            self.root.iconbitmap(default="")
        except:
            pass

        # Header
        header = tk.Frame(self.root, bg="#0d0d0d")
        header.pack(fill="x", padx=30, pady=(30, 10))

        tk.Label(header, text="⏺", font=("Segoe UI", 28), bg="#0d0d0d", fg="#ff4444").pack(side="left")
        tk.Label(header, text=" REPLAY BUFFER", font=("Segoe UI", 18, "bold"), bg="#0d0d0d", fg="#ffffff").pack(side="left", pady=(4, 0))

        # Status bar
        self.status_frame = tk.Frame(self.root, bg="#1a1a1a", height=3)
        self.status_frame.pack(fill="x")
        self.status_bar = tk.Frame(self.status_frame, bg="#333333", height=3)
        self.status_bar.pack(fill="x")

        # Main card
        card = tk.Frame(self.root, bg="#161616", padx=25, pady=20)
        card.pack(fill="x", padx=25, pady=15)

        # Hotkey section
        tk.Label(card, text="מקש שמירה", font=("Segoe UI", 10), bg="#161616", fg="#888888").pack(anchor="w")
        hk_row = tk.Frame(card, bg="#161616")
        hk_row.pack(fill="x", pady=(5, 15))

        self.hotkey_label = tk.Label(hk_row, text=self.config["hotkey"],
                                     font=("Consolas", 20, "bold"), bg="#1e1e1e", fg="#ff4444",
                                     width=10, relief="flat", pady=8)
        self.hotkey_label.pack(side="left")

        self.set_key_btn = tk.Button(hk_row, text="שנה מקש", font=("Segoe UI", 9),
                                     bg="#2a2a2a", fg="#ffffff", activebackground="#3a3a3a",
                                     activeforeground="#ffffff", relief="flat", padx=15, pady=8,
                                     cursor="hand2", command=self._start_hotkey_capture)
        self.set_key_btn.pack(side="left", padx=(10, 0))

        # Buffer time section
        tk.Label(card, text="אורך הזיכרון (דקות)", font=("Segoe UI", 10), bg="#161616", fg="#888888").pack(anchor="w")
        time_row = tk.Frame(card, bg="#161616")
        time_row.pack(fill="x", pady=(5, 5))

        self.minutes_var = tk.IntVar(value=self.config["buffer_minutes"])
        for val in [1, 2, 5, 10, 15]:
            is_selected = val == self.config["buffer_minutes"]
            btn = tk.Button(time_row, text=str(val),
                            font=("Segoe UI", 11, "bold"),
                            bg="#ff4444" if is_selected else "#2a2a2a",
                            fg="#ffffff", relief="flat", width=4, pady=6,
                            cursor="hand2",
                            command=lambda v=val: self._set_minutes(v))
            btn.pack(side="left", padx=3)

        self.time_btns = time_row.winfo_children()

        # Separator
        tk.Frame(card, bg="#2a2a2a", height=1).pack(fill="x", pady=15)

        # Saves location
        saves_row = tk.Frame(card, bg="#161616")
        saves_row.pack(fill="x")
        tk.Label(saves_row, text="📁", font=("Segoe UI", 12), bg="#161616", fg="#888888").pack(side="left")
        tk.Label(saves_row, text=str(SAVES_DIR), font=("Segoe UI", 8), bg="#161616", fg="#555555",
                 wraplength=330, justify="left").pack(side="left", padx=(5, 0))

        # Main toggle button
        self.toggle_btn = tk.Button(self.root, text="▶  התחל הקלטה",
                                    font=("Segoe UI", 14, "bold"),
                                    bg="#222222", fg="#888888",
                                    activebackground="#2a2a2a", activeforeground="#aaaaaa",
                                    relief="flat", pady=18, cursor="hand2",
                                    command=self._toggle_recording)
        self.toggle_btn.pack(fill="x", padx=25)

        # Saves list
        saves_card = tk.Frame(self.root, bg="#161616", padx=20, pady=15)
        saves_card.pack(fill="both", expand=True, padx=25, pady=(15, 25))

        header_row = tk.Frame(saves_card, bg="#161616")
        header_row.pack(fill="x")
        tk.Label(header_row, text="הקלטות שמורות", font=("Segoe UI", 10), bg="#161616", fg="#888888").pack(side="left")
        tk.Button(header_row, text="פתח תיקייה", font=("Segoe UI", 8),
                  bg="#161616", fg="#555555", activebackground="#2a2a2a", activeforeground="#888888",
                  relief="flat", cursor="hand2", command=self._open_saves_folder).pack(side="right")

        self.saves_list = tk.Listbox(saves_card, bg="#111111", fg="#cccccc",
                                     font=("Consolas", 9), selectbackground="#ff4444",
                                     selectforeground="#ffffff", relief="flat",
                                     borderwidth=0, highlightthickness=0)
        self.saves_list.pack(fill="both", expand=True, pady=(8, 0))
        self.saves_list.bind("<Double-Button-1>", self._open_selected_save)

        self._refresh_saves_list()
        self._start_status_animation()

    def _start_status_animation(self):
        """Animate the status bar"""
        if not self.recording:
            self.status_bar.configure(bg="#333333")
        else:
            current = self.status_bar.cget("bg")
            self.status_bar.configure(bg="#ff4444" if current == "#333333" else "#333333")
        self.root.after(500, self._start_status_animation)

    def _set_minutes(self, val):
        self.config["buffer_minutes"] = val
        save_config(self.config)
        if self.recording:
            self.buffer.update_config(self.config)
        # Update button colors
        for btn in self.time_btns:
            btn_val = int(btn.cget("text"))
            btn.configure(bg="#ff4444" if btn_val == val else "#2a2a2a")

    def _start_hotkey_capture(self):
        if self.setting_hotkey:
            return
        self.setting_hotkey = True
        self.hotkey_label.configure(text="לחץ מקש...", fg="#ffaa00")
        self.set_key_btn.configure(state="disabled")
        threading.Thread(target=self._capture_hotkey, daemon=True).start()

    def _capture_hotkey(self):
        event = keyboard.read_event(suppress=True)
        if event.event_type == keyboard.KEY_DOWN:
            new_key = event.name.upper()
            self.config["hotkey"] = new_key
            save_config(self.config)
            self._register_hotkey()
            self.root.after(0, lambda: self.hotkey_label.configure(text=new_key, fg="#ff4444"))
            self.root.after(0, lambda: self.set_key_btn.configure(state="normal"))
        self.setting_hotkey = False

    def _register_hotkey(self):
        if self.hotkey_registered:
            try:
                keyboard.remove_hotkey(self.hotkey_registered)
            except:
                pass
        try:
            self.hotkey_registered = keyboard.add_hotkey(
                self.config["hotkey"].lower(),
                self._on_hotkey_press
            )
        except Exception as e:
            pass

    def _on_hotkey_press(self):
        if self.recording:
            self.root.after(0, self._save_replay)

    def _toggle_recording(self):
        if not self.recording:
            self.recording = True
            self.buffer.start()
            self.toggle_btn.configure(text="⏹  עצור הקלטה", bg="#ff4444", fg="#ffffff",
                                       activebackground="#cc3333", activeforeground="#ffffff")
        else:
            self.recording = False
            self.buffer.stop()
            self.toggle_btn.configure(text="▶  התחל הקלטה", bg="#222222", fg="#888888",
                                       activebackground="#2a2a2a", activeforeground="#aaaaaa")

    def _save_replay(self):
        self.hotkey_label.configure(fg="#ffaa00")
        self.root.after(300, lambda: self.hotkey_label.configure(fg="#ff4444"))

        def do_save():
            path = self.buffer.save()
            if path:
                self.root.after(0, self._refresh_saves_list)
                # Flash notification
                self.root.after(0, lambda: self.root.title(f"✅ נשמר!"))
                self.root.after(2000, lambda: self.root.title("Replay Buffer"))
            else:
                self.root.after(0, lambda: self.root.title("❌ שגיאה בשמירה"))
                self.root.after(2000, lambda: self.root.title("Replay Buffer"))

        threading.Thread(target=do_save, daemon=True).start()

    def _refresh_saves_list(self):
        self.saves_list.delete(0, tk.END)
        if SAVES_DIR.exists():
            files = sorted(SAVES_DIR.glob("*.mp4"), reverse=True)
            for f in files[:20]:
                size_mb = f.stat().st_size / (1024 * 1024)
                name = f.stem.replace("Replay_", "")
                display = f"{name}  ({size_mb:.1f} MB)"
                self.saves_list.insert(tk.END, display)

    def _open_selected_save(self, event):
        sel = self.saves_list.curselection()
        if not sel:
            return
        item = self.saves_list.get(sel[0])
        name = item.split("  (")[0]
        path = SAVES_DIR / f"Replay_{name}.mp4"
        if path.exists():
            os.startfile(str(path))

    def _open_saves_folder(self):
        SAVES_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(SAVES_DIR))

    def _on_close(self):
        if self.recording:
            if messagebox.askyesno("יציאה", "האפליקציה מקליטה כרגע.\nלצאת בכל זאת?"):
                self.buffer.stop()
                self.root.destroy()
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = ReplayBufferApp()
    app.run()
