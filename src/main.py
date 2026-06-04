import sys
import os
import json
import time
import threading
import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
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
}


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


class ReplayBufferApp:
    def __init__(self):
        self.config = load_config()
        self.recording = False
        self.rec_thread = None
        self.frames = []
        self.hotkey_registered = None
        self.setting_hotkey = False
        self.current_writer = None
        self.rec_start_time = None
        self.rec_duration = 0

        self._build_ui()
        self._register_hotkey()

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Replay Buffer")
        self.root.geometry("900x560")
        self.root.resizable(False, False)
        self.root.configure(bg="#0d0d0d")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Left panel ──────────────────────────────────────────────
        left = tk.Frame(self.root, bg="#0d0d0d", width=320)
        left.pack(side="left", fill="y", padx=(25, 0), pady=25)
        left.pack_propagate(False)

        tk.Label(left, text="⏺  REPLAY BUFFER",
                 font=("Segoe UI", 15, "bold"), bg="#0d0d0d", fg="#ffffff").pack(anchor="w")
        tk.Frame(left, bg="#2a2a2a", height=1).pack(fill="x", pady=12)

        # Hotkey
        tk.Label(left, text="מקש הפעלה", font=("Segoe UI", 9),
                 bg="#0d0d0d", fg="#666666").pack(anchor="w")
        hk_row = tk.Frame(left, bg="#0d0d0d")
        hk_row.pack(fill="x", pady=(4, 14))

        self.hotkey_label = tk.Label(hk_row, text=self.config["hotkey"],
                                     font=("Consolas", 18, "bold"),
                                     bg="#1a1a1a", fg="#ff4444",
                                     width=8, pady=6, relief="flat")
        self.hotkey_label.pack(side="left")

        self.set_key_btn = tk.Button(hk_row, text="שנה",
                                     font=("Segoe UI", 9),
                                     bg="#2a2a2a", fg="#cccccc",
                                     activebackground="#3a3a3a", activeforeground="#ffffff",
                                     relief="flat", padx=12, pady=6,
                                     cursor="hand2", command=self._start_hotkey_capture)
        self.set_key_btn.pack(side="left", padx=(8, 0))

        # Duration
        tk.Label(left, text="אורך הקלטה (דקות)", font=("Segoe UI", 9),
                 bg="#0d0d0d", fg="#666666").pack(anchor="w")
        dur_row = tk.Frame(left, bg="#0d0d0d")
        dur_row.pack(fill="x", pady=(4, 14))

        self.dur_btns = {}
        for val in [1, 2, 5, 10, 15]:
            b = tk.Button(dur_row, text=str(val),
                          font=("Segoe UI", 10, "bold"),
                          bg="#ff4444" if val == self.config["buffer_minutes"] else "#1e1e1e",
                          fg="#ffffff", relief="flat", width=4, pady=5,
                          cursor="hand2",
                          command=lambda v=val: self._set_minutes(v))
            b.pack(side="left", padx=2)
            self.dur_btns[val] = b

        # Status
        tk.Frame(left, bg="#2a2a2a", height=1).pack(fill="x", pady=8)
        self.status_label = tk.Label(left, text="⏸  ממתין להפעלה",
                                     font=("Segoe UI", 10), bg="#0d0d0d", fg="#555555")
        self.status_label.pack(anchor="w", pady=(0, 8))

        self.progress_frame = tk.Frame(left, bg="#1a1a1a", height=6)
        self.progress_frame.pack(fill="x")
        self.progress_bar = tk.Frame(self.progress_frame, bg="#ff4444", height=6, width=0)
        self.progress_bar.place(x=0, y=0)

        tk.Frame(left, bg="#2a2a2a", height=1).pack(fill="x", pady=12)

        # Info
        tk.Label(left, text="📁  " + str(SAVES_DIR),
                 font=("Segoe UI", 7), bg="#0d0d0d", fg="#333333",
                 wraplength=280, justify="left").pack(anchor="w")

        tk.Button(left, text="פתח תיקיית הקלטות",
                  font=("Segoe UI", 9),
                  bg="#1e1e1e", fg="#888888",
                  activebackground="#2a2a2a", activeforeground="#ffffff",
                  relief="flat", pady=6, cursor="hand2",
                  command=self._open_folder).pack(fill="x", pady=(8, 0))

        # ── Right panel ─────────────────────────────────────────────
        right = tk.Frame(self.root, bg="#111111")
        right.pack(side="right", fill="both", expand=True, padx=20, pady=25)

        header_r = tk.Frame(right, bg="#111111")
        header_r.pack(fill="x", pady=(0, 10))
        tk.Label(header_r, text="הקלטות שמורות",
                 font=("Segoe UI", 11, "bold"), bg="#111111", fg="#cccccc").pack(side="left")

        # Saves list
        list_frame = tk.Frame(right, bg="#111111")
        list_frame.pack(fill="both", expand=True)

        self.saves_list = tk.Listbox(list_frame,
                                     bg="#0d0d0d", fg="#cccccc",
                                     font=("Consolas", 9),
                                     selectbackground="#ff4444",
                                     selectforeground="#ffffff",
                                     relief="flat", borderwidth=0,
                                     highlightthickness=0,
                                     activestyle="none")
        self.saves_list.pack(fill="both", expand=True)
        self.saves_list.bind("<<ListboxSelect>>", self._on_select_save)

        # Trim panel
        trim_frame = tk.Frame(right, bg="#111111")
        trim_frame.pack(fill="x", pady=(12, 0))

        tk.Label(trim_frame, text="✂  חיתוך קטע",
                 font=("Segoe UI", 9, "bold"), bg="#111111", fg="#888888").pack(anchor="w")

        sliders_frame = tk.Frame(trim_frame, bg="#111111")
        sliders_frame.pack(fill="x", pady=(6, 0))

        tk.Label(sliders_frame, text="מ-", font=("Segoe UI", 9),
                 bg="#111111", fg="#666666").grid(row=0, column=0, padx=(0, 4))
        self.trim_start = tk.Scale(sliders_frame, from_=0, to=100,
                                   orient="horizontal", bg="#111111", fg="#cccccc",
                                   troughcolor="#2a2a2a", activebackground="#ff4444",
                                   highlightthickness=0, bd=0, length=180,
                                   command=self._update_trim_labels)
        self.trim_start.grid(row=0, column=1)
        self.trim_start_lbl = tk.Label(sliders_frame, text="0:00",
                                       font=("Consolas", 9), bg="#111111", fg="#ff4444", width=5)
        self.trim_start_lbl.grid(row=0, column=2, padx=(6, 0))

        tk.Label(sliders_frame, text="עד-", font=("Segoe UI", 9),
                 bg="#111111", fg="#666666").grid(row=1, column=0, padx=(0, 4))
        self.trim_end = tk.Scale(sliders_frame, from_=0, to=100,
                                 orient="horizontal", bg="#111111", fg="#cccccc",
                                 troughcolor="#2a2a2a", activebackground="#ff4444",
                                 highlightthickness=0, bd=0, length=180,
                                 command=self._update_trim_labels)
        self.trim_end.grid(row=1, column=1)
        self.trim_end.set(100)
        self.trim_end_lbl = tk.Label(sliders_frame, text="0:00",
                                     font=("Consolas", 9), bg="#111111", fg="#ff4444", width=5)
        self.trim_end_lbl.grid(row=1, column=2, padx=(6, 0))

        self.trim_btn = tk.Button(trim_frame, text="✂  שמור קטע חתוך",
                                  font=("Segoe UI", 10, "bold"),
                                  bg="#2a2a2a", fg="#666666",
                                  activebackground="#ff4444", activeforeground="#ffffff",
                                  relief="flat", pady=8, cursor="hand2",
                                  state="disabled",
                                  command=self._do_trim)
        self.trim_btn.pack(fill="x", pady=(10, 0))

        self._refresh_saves_list()

    # ── Config ───────────────────────────────────────────────────────

    def _set_minutes(self, val):
        self.config["buffer_minutes"] = val
        save_config(self.config)
        for v, b in self.dur_btns.items():
            b.configure(bg="#ff4444" if v == val else "#1e1e1e")

    def _start_hotkey_capture(self):
        if self.setting_hotkey:
            return
        self.setting_hotkey = True
        self.hotkey_label.configure(text="לחץ...", fg="#ffaa00")
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
                self.config["hotkey"].lower(), self._on_hotkey_press)
        except:
            pass

    # ── Recording ────────────────────────────────────────────────────

    def _on_hotkey_press(self):
        if not self.recording:
            self.root.after(0, self._start_recording)

    def _start_recording(self):
        if self.recording:
            return
        self.recording = True
        self.frames = []
        self.rec_start_time = time.time()
        self.rec_duration = self.config["buffer_minutes"] * 60
        self.status_label.configure(text="🔴  מקליט...", fg="#ff4444")
        self.rec_thread = threading.Thread(target=self._record_loop, daemon=True)
        self.rec_thread.start()
        self._update_progress()

    def _record_loop(self):
        fps = self.config["fps"]
        interval = 1.0 / fps
        total_frames = int(self.rec_duration * fps)

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            for _ in range(total_frames):
                if not self.recording:
                    break
                start = time.time()
                try:
                    img = sct.grab(monitor)
                    frame = np.array(img)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    h, w = frame.shape[:2]
                    if w > 1280:
                        scale = 1280 / w
                        frame = cv2.resize(frame, (1280, int(h * scale)))
                    self.frames.append(frame)
                except:
                    pass
                elapsed = time.time() - start
                sleep_t = interval - elapsed
                if sleep_t > 0:
                    time.sleep(sleep_t)

        self.root.after(0, self._finish_recording)

    def _update_progress(self):
        if not self.recording:
            return
        elapsed = time.time() - self.rec_start_time
        pct = min(elapsed / self.rec_duration, 1.0)
        bar_width = int(self.progress_frame.winfo_width() * pct)
        self.progress_bar.configure(width=bar_width)
        remaining = max(0, self.rec_duration - elapsed)
        m, s = divmod(int(remaining), 60)
        self.status_label.configure(text=f"🔴  מקליט... {m}:{s:02d} נותרו")
        if pct < 1.0:
            self.root.after(200, self._update_progress)

    def _finish_recording(self):
        self.recording = False
        self.status_label.configure(text="💾  שומר...", fg="#ffaa00")
        threading.Thread(target=self._save_video, daemon=True).start()

    def _save_video(self):
        if not self.frames:
            self.root.after(0, lambda: self.status_label.configure(
                text="⚠  אין פריימים", fg="#ff8800"))
            return

        SAVES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_path = SAVES_DIR / f"Replay_{ts}.mp4"

        h, w = self.frames[0].shape[:2]
        fps = self.config["fps"]
        writer = cv2.VideoWriter(str(out_path),
                                 cv2.VideoWriter_fourcc(*"mp4v"),
                                 fps, (w, h))
        for frame in self.frames:
            writer.write(frame)
        writer.release()
        self.frames = []

        self.root.after(0, lambda: self.status_label.configure(
            text="✅  נשמר!", fg="#44ff88"))
        self.root.after(0, self._refresh_saves_list)
        self.root.after(3000, lambda: self.status_label.configure(
            text="⏸  ממתין להפעלה", fg="#555555"))
        self.root.after(0, lambda: self.progress_bar.configure(width=0))

    # ── Saves list ───────────────────────────────────────────────────

    def _refresh_saves_list(self):
        self.saves_list.delete(0, tk.END)
        self._save_paths = []
        if SAVES_DIR.exists():
            files = sorted(SAVES_DIR.glob("*.mp4"), reverse=True)
            for f in files[:30]:
                size_mb = f.stat().st_size / (1024 * 1024)
                name = f.stem.replace("Replay_", "").replace("_", "  ")
                self.saves_list.insert(tk.END, f"  {name}    {size_mb:.1f} MB")
                self._save_paths.append(f)

    def _on_select_save(self, event):
        sel = self.saves_list.curselection()
        if not sel:
            return
        path = self._save_paths[sel[0]]
        cap = cv2.VideoCapture(str(path))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        cap.release()
        self._selected_path = path
        self._selected_total_frames = total_frames
        self._selected_fps = fps
        duration_sec = total_frames / fps
        self.trim_start.configure(to=int(duration_sec))
        self.trim_end.configure(to=int(duration_sec))
        self.trim_start.set(0)
        self.trim_end.set(int(duration_sec))
        self._update_trim_labels()
        self.trim_btn.configure(state="normal", bg="#ff4444", fg="#ffffff")

    def _update_trim_labels(self, *_):
        def fmt(sec):
            sec = int(sec)
            return f"{sec // 60}:{sec % 60:02d}"
        self.trim_start_lbl.configure(text=fmt(self.trim_start.get()))
        self.trim_end_lbl.configure(text=fmt(self.trim_end.get()))

    def _do_trim(self):
        if not hasattr(self, "_selected_path"):
            return
        start_sec = self.trim_start.get()
        end_sec = self.trim_end.get()
        if end_sec <= start_sec:
            messagebox.showerror("שגיאה", "זמן הסיום חייב להיות אחרי זמן ההתחלה")
            return
        self.trim_btn.configure(state="disabled", text="חותך...", bg="#2a2a2a", fg="#888888")
        threading.Thread(target=self._run_trim,
                         args=(self._selected_path, start_sec, end_sec),
                         daemon=True).start()

    def _run_trim(self, src_path, start_sec, end_sec):
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_path = SAVES_DIR / f"Clip_{ts}.mp4"
        cap = cv2.VideoCapture(str(src_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        start_frame = int(start_sec * fps)
        end_frame = int(end_sec * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        writer = cv2.VideoWriter(str(out_path),
                                 cv2.VideoWriter_fourcc(*"mp4v"),
                                 fps, (w, h))
        for _ in range(end_frame - start_frame):
            ret, frame = cap.read()
            if not ret:
                break
            writer.write(frame)
        cap.release()
        writer.release()
        self.root.after(0, self._refresh_saves_list)
        self.root.after(0, lambda: self.trim_btn.configure(
            state="normal", text="✂  שמור קטע חתוך",
            bg="#ff4444", fg="#ffffff"))

    # ── Misc ─────────────────────────────────────────────────────────

    def _open_folder(self):
        SAVES_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(SAVES_DIR))

    def _on_close(self):
        if self.recording:
            if messagebox.askyesno("יציאה", "מקליט כרגע, לצאת?"):
                self.recording = False
                self.root.destroy()
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = ReplayBufferApp()
    app.run()
