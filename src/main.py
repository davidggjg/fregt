import sys
import os
import json
import time
import threading
import datetime
import shutil
import subprocess
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
import keyboard

CONFIG_FILE = Path(os.environ.get("APPDATA", ".")) / "ReplayBuffer" / "config.json"
SAVES_DIR   = Path(os.environ.get("USERPROFILE", ".")) / "Videos" / "ReplayBuffer"
TMP_DIR     = Path(tempfile.gettempdir()) / "ReplayBuffer_chunks"

DEFAULT_CONFIG = {
    "hotkey": "F9",
    "buffer_minutes": 5,
    "fps": 30,
    "use_nvenc": True,   # GPU encoding אם זמין
}

NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW


# ── helpers ──────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def find_ffmpeg():
    if getattr(sys, "frozen", False):
        local = Path(sys.executable).parent / "ffmpeg.exe"
        if local.exists():
            return str(local)
    return "ffmpeg"


def run_ff(*args, timeout=15):
    """מריץ ffmpeg בשקט"""
    return subprocess.run(
        [find_ffmpeg(), *args],
        capture_output=True, timeout=timeout,
        creationflags=NO_WINDOW
    )


def ff_devices():
    """מחזיר dict {audio: [...], video: [...]} של dshow devices"""
    try:
        r = subprocess.run(
            [find_ffmpeg(), "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW
        )
        audio, video = [], []
        cur = None
        for line in r.stderr.splitlines():
            if "audio devices" in line.lower():  cur = audio
            elif "video devices" in line.lower(): cur = video
            elif cur is not None and '"' in line:
                cur.append(line.split('"')[1])
        return {"audio": audio, "video": video}
    except Exception:
        return {"audio": [], "video": []}


def pick_audio_device(devices):
    """בוחר loopback device לפי עדיפות"""
    keywords = ["stereo mix", "cable output", "virtual audio",
                "what u hear", "wave out", "loopback"]
    for kw in keywords:
        for d in devices:
            if kw in d.lower():
                return d
    return None


def pick_mic_device(devices):
    keywords = ["microphone", "mic ", "headset"]
    for kw in keywords:
        for d in devices:
            if kw in d.lower():
                return d
    # fallback - first device that isn't the loopback
    loopback = pick_audio_device(devices)
    for d in devices:
        if d != loopback:
            return d
    return None


def has_nvenc():
    """בודק אם ffmpeg תומך ב-NVENC"""
    try:
        r = subprocess.run(
            [find_ffmpeg(), "-encoders"],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW
        )
        return "h264_nvenc" in r.stdout
    except Exception:
        return False


# ── CaptureEngine ─────────────────────────────────────────────────────────────
#
#  הרעיון:
#  ffmpeg מקליט קטעים קצרים (CHUNK_SECS שניות) ישירות לדיסק - וידאו + אודיו ביחד.
#  שמירנו רק את N הקטעים האחרונים שמכסים את buffer_minutes.
#  כשלוחצים על המקש:
#    1. ffmpeg מסיים את הקטע הנוכחי תוך ~1 שניה
#    2. concat של הקטעים האחרונים → קובץ סופי
#    3. ffmpeg מתחיל קטע חדש → ממשיך להקליט
#  אין כתיבת פריים-פריים → כל השמירה לוקחת 2-5 שניות.

CHUNK_SECS = 30   # כל קטע 30 שניות


class CaptureEngine:
    def __init__(self, config):
        self.config = config
        self._running   = False
        self._proc      = None          # ffmpeg process נוכחי
        self._proc_lock = threading.Lock()
        self._chunks    = []            # [(path, start_time), ...]
        self._chunks_lock = threading.Lock()
        self._rec_thread = None
        self._nvenc = has_nvenc() and config.get("use_nvenc", True)
        # audio devices - נקבע פעם אחת
        devs = ff_devices()
        self._loopback = pick_audio_device(devs["audio"])
        self._mic      = pick_mic_device(devs["audio"])
        TMP_DIR.mkdir(parents=True, exist_ok=True)

    def start(self):
        self._running = True
        self._rec_thread = threading.Thread(target=self._record_loop, daemon=True)
        self._rec_thread.start()

    def stop(self):
        self._running = False
        self._kill_proc()

    def update_config(self, config):
        self.config = config
        self._nvenc = has_nvenc() and config.get("use_nvenc", True)

    # ── הקלטת קטעים ──────────────────────────────────────────────

    def _record_loop(self):
        """לולאה ראשית - מקליט קטעים אחד אחרי השני"""
        while self._running:
            chunk_path = TMP_DIR / f"chunk_{int(time.time())}.mp4"
            start_time = time.time()
            self._start_chunk(chunk_path)
            # ממתין עד CHUNK_SECS
            deadline = start_time + CHUNK_SECS
            while self._running and time.time() < deadline:
                time.sleep(0.2)
            self._finish_chunk(chunk_path, start_time)

    def _build_ffmpeg_cmd(self, out_path):
        """בונה את פקודת ffmpeg להקלטת קטע אחד"""
        ff = find_ffmpeg()
        fps = self.config["fps"]
        cmd = [ff, "-y"]

        # ── אודיו inputs ──
        if self._loopback and self._mic:
            cmd += [
                "-f", "dshow", "-i", f"audio={self._loopback}",
                "-f", "dshow", "-i", f"audio={self._mic}",
            ]
        elif self._loopback:
            cmd += ["-f", "dshow", "-i", f"audio={self._loopback}"]
        elif self._mic:
            cmd += ["-f", "dshow", "-i", f"audio={self._mic}"]

        # ── וידאו input (gdigrab - ישיר ממנהל המסך של Windows) ──
        cmd += [
            "-f", "gdigrab",
            "-framerate", str(fps),
            "-i", "desktop",
        ]

        # ── audio filter (מיקס אם יש שניים) ──
        n_audio = (1 if self._loopback else 0) + (1 if self._mic else 0)
        if n_audio == 2:
            cmd += ["-filter_complex", "amix=inputs=2:duration=longest"]

        # ── video encoder: NVENC > CPU ──
        if self._nvenc:
            cmd += [
                "-c:v", "h264_nvenc",
                "-preset", "p1",       # הכי מהיר
                "-rc", "vbr",
                "-b:v", "8M",
            ]
        else:
            cmd += [
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
            ]

        # ── audio encoder ──
        if n_audio > 0:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "44100"]

        # ── מגביל ל-CHUNK_SECS ──
        cmd += ["-t", str(CHUNK_SECS), str(out_path)]
        return cmd

    def _start_chunk(self, out_path):
        cmd = self._build_ffmpeg_cmd(out_path)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW
            )
            with self._proc_lock:
                self._proc = proc
        except Exception:
            pass

    def _finish_chunk(self, chunk_path, start_time):
        """עוצר ffmpeg ומוסיף את הקטע לרשימה"""
        proc = None
        with self._proc_lock:
            proc = self._proc
            self._proc = None

        if proc and proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    pass

        # מוסיף לרשימת קטעים אם הקובץ נוצר
        if chunk_path.exists() and chunk_path.stat().st_size > 5000:
            with self._chunks_lock:
                self._chunks.append((chunk_path, start_time))
                self._trim_old_chunks()

    def _trim_old_chunks(self):
        """מוחק קטעים ישנים שחורגים מ-buffer_minutes"""
        keep_secs = self.config["buffer_minutes"] * 60 + CHUNK_SECS
        cutoff = time.time() - keep_secs
        to_delete = [(p, t) for p, t in self._chunks if t < cutoff]
        self._chunks = [(p, t) for p, t in self._chunks if t >= cutoff]
        for p, _ in to_delete:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    def _kill_proc(self):
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

    # ── שמירה ─────────────────────────────────────────────────────

    def save_snapshot(self, on_done):
        """
        1. עוצר את הקטע הנוכחי
        2. מחבר קטעים אחרונים → קובץ סופי
        3. מתחיל קטע חדש
        """
        def _do():
            # שלב 1: סיים קטע נוכחי
            now = time.time()
            current_chunk = TMP_DIR / f"chunk_{int(now)}_save.mp4"
            proc = None
            with self._proc_lock:
                proc = self._proc
                self._proc = None

            if proc and proc.poll() is None:
                try:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                    proc.wait(timeout=4)
                except Exception:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        pass

            # מצא קובץ הקטע האחרון שנוצר
            latest = max(TMP_DIR.glob("chunk_*.mp4"),
                         key=lambda p: p.stat().st_mtime,
                         default=None)
            with self._chunks_lock:
                if latest and latest not in [p for p, _ in self._chunks]:
                    self._chunks.append((latest, now - CHUNK_SECS))

            # שלב 2: concat קטעים
            with self._chunks_lock:
                self._trim_old_chunks()
                chunks_to_use = list(self._chunks)

            out_path = self._concat_chunks(chunks_to_use)

            # שלב 3: חזור להקלטה
            if self._running:
                new_chunk = TMP_DIR / f"chunk_{int(time.time())}.mp4"
                self._start_chunk(new_chunk)

            if on_done:
                on_done(str(out_path) if out_path else None)

        threading.Thread(target=_do, daemon=True).start()

    def _concat_chunks(self, chunks):
        """מחבר קטעי mp4 לקובץ אחד - מהיר כי זה copy בלבד"""
        if not chunks:
            return None

        SAVES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = SAVES_DIR / f"Replay_{ts}.mp4"

        if len(chunks) == 1:
            shutil.copy(str(chunks[0][0]), str(out))
            return out

        # כותב concat list
        concat_file = TMP_DIR / "concat.txt"
        with open(concat_file, "w") as f:
            for p, _ in chunks:
                f.write(f"file '{p}'\n")

        # ffmpeg concat demuxer - מהיר מאוד (stream copy)
        run_ff(
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(out),
            timeout=30
        )
        return out if out.exists() else None


# ── VideoEditor ───────────────────────────────────────────────────────────────

class VideoEditor(tk.Toplevel):
    def __init__(self, parent, video_path):
        super().__init__(parent)
        self.title(f"עורך - {Path(video_path).name}")
        self.configure(bg="#0d0d0d")
        self.resizable(True, True)
        self.geometry("900x620")
        self.minsize(700, 500)

        self.video_path = video_path
        import cv2
        self.cap = cv2.VideoCapture(video_path)
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.duration = self.total_frames / self.fps
        self.current_frame = 0
        self.playing = False
        self.split_points = []
        self._photo = None
        self._last_frame = None
        self._cv2 = cv2

        self._build()
        self._seek(0)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<F11>", self._toggle_fullscreen)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))
        self.lift()
        self.focus_force()

    def _build(self):
        self.canvas = tk.Canvas(self, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw_frame())

        ctrl = tk.Frame(self, bg="#111111", pady=8)
        ctrl.pack(fill="x", side="bottom")

        tl_frame = tk.Frame(ctrl, bg="#111111")
        tl_frame.pack(fill="x", padx=16, pady=(0, 6))

        self.marker_canvas = tk.Canvas(tl_frame, bg="#1a1a1a",
                                        height=14, highlightthickness=0)
        self.marker_canvas.pack(fill="x", pady=(0, 2))
        self.marker_canvas.bind("<Configure>", lambda e: self._draw_markers())

        self.timeline = tk.Scale(tl_frame, from_=0, to=max(1, self.total_frames - 1),
                                  orient="horizontal", bg="#111111", fg="#cccccc",
                                  troughcolor="#2a2a2a", activebackground="#ff4444",
                                  highlightthickness=0, bd=0, showvalue=False,
                                  command=self._on_timeline)
        self.timeline.pack(fill="x")

        time_row = tk.Frame(ctrl, bg="#111111")
        time_row.pack(fill="x", padx=16)
        self.time_lbl = tk.Label(time_row, text="0:00 / 0:00",
                                  font=("Consolas", 9), bg="#111111", fg="#888888")
        self.time_lbl.pack(side="left")
        tk.Label(time_row, text="F11 = מסך מלא",
                 font=("Segoe UI", 8), bg="#111111", fg="#333333").pack(side="right")

        btn_row = tk.Frame(ctrl, bg="#111111")
        btn_row.pack(fill="x", padx=16, pady=(6, 0))

        def mkbtn(text, cmd, bg="#2a2a2a", fg="#cccccc", w=None):
            kw = dict(font=("Segoe UI", 9, "bold"), bg=bg, fg=fg,
                      activebackground="#3a3a3a", activeforeground="#ffffff",
                      relief="flat", pady=6, padx=10, cursor="hand2", command=cmd)
            if w: kw["width"] = w
            return tk.Button(btn_row, text=text, **kw)

        self.play_btn = mkbtn("▶  נגן", self._toggle_play, w=10)
        self.play_btn.pack(side="left", padx=(0, 6))
        mkbtn("◀◀ -5s", lambda: self._skip(-5), w=8).pack(side="left", padx=2)
        mkbtn("+5s ▶▶", lambda: self._skip(5), w=8).pack(side="left", padx=2)

        tk.Frame(btn_row, bg="#333333", width=1).pack(side="left", padx=10, fill="y")

        mkbtn("✂  פצל כאן", self._add_split, "#1e3a1e", "#44ff88").pack(side="left", padx=2)
        mkbtn("↩  בטל", self._undo_split, "#2a1a1a", "#ff8888").pack(side="left", padx=2)

        tk.Frame(btn_row, bg="#333333", width=1).pack(side="left", padx=10, fill="y")

        mkbtn("💾  שמור קטעים", self._export_segments, "#ff4444", "#ffffff").pack(side="left", padx=2)

        seg_frame = tk.Frame(ctrl, bg="#111111")
        seg_frame.pack(fill="x", padx=16, pady=(8, 0))
        tk.Label(seg_frame, text="קטעים:", font=("Segoe UI", 8),
                 bg="#111111", fg="#555555").pack(side="left")
        self.seg_lbl = tk.Label(seg_frame, text="אין פיצולים - הכל קטע אחד",
                                 font=("Consolas", 8), bg="#111111", fg="#888888")
        self.seg_lbl.pack(side="left", padx=(6, 0))

    def _toggle_fullscreen(self, e=None):
        self.attributes("-fullscreen", not self.attributes("-fullscreen"))

    def _on_timeline(self, val):
        if not self.playing:
            self._seek(int(float(val)))

    def _seek(self, n):
        import cv2
        n = max(0, min(n, self.total_frames - 1))
        self.current_frame = n
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, n)
        ret, frame = self.cap.read()
        if ret:
            self._last_frame = frame
            self._redraw_frame()
        self.timeline.set(n)
        self.time_lbl.configure(text=f"{self._fmt(n/self.fps)} / {self._fmt(self.duration)}")

    def _redraw_frame(self):
        if self._last_frame is None:
            return
        from PIL import Image, ImageTk
        import cv2
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 2 or ch < 2:
            return
        h, w = self._last_frame.shape[:2]
        scale = min(cw / w, ch / h)
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(self._last_frame, (nw, nh))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, anchor="center", image=self._photo)

    def _toggle_play(self):
        if self.playing:
            self.playing = False
            self.play_btn.configure(text="▶  נגן")
        else:
            self.playing = True
            self.play_btn.configure(text="⏸  עצור")
            threading.Thread(target=self._play_loop, daemon=True).start()

    def _play_loop(self):
        import cv2
        interval = 1.0 / self.fps
        while self.playing and self.current_frame < self.total_frames - 1:
            t0 = time.time()
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame)
            ret, frame = self.cap.read()
            if not ret:
                break
            self.current_frame += 1
            f, n = frame, self.current_frame
            self.after(0, lambda fr=f, fn=n: self._update_play(fr, fn))
            elapsed = time.time() - t0
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)
        self.playing = False
        self.after(0, lambda: self.play_btn.configure(text="▶  נגן"))

    def _update_play(self, frame, n):
        self._last_frame = frame
        self._redraw_frame()
        self.timeline.set(n)
        self.time_lbl.configure(text=f"{self._fmt(n/self.fps)} / {self._fmt(self.duration)}")

    def _skip(self, secs):
        self.playing = False
        self._seek(self.current_frame + int(secs * self.fps))

    def _add_split(self):
        pos = self.current_frame
        if 0 < pos < self.total_frames - 1 and pos not in self.split_points:
            self.split_points.append(pos)
            self.split_points.sort()
            self._draw_markers()
            self._update_seg_label()

    def _undo_split(self):
        if self.split_points:
            self.split_points.pop()
            self._draw_markers()
            self._update_seg_label()

    def _draw_markers(self):
        self.marker_canvas.delete("all")
        cw = self.marker_canvas.winfo_width()
        if cw < 2:
            return
        points = [0] + self.split_points + [self.total_frames]
        colors = ["#1e3a1e", "#1a1e3a", "#3a1a1e", "#1e3a3a", "#2a1e3a"]
        for i in range(len(points) - 1):
            x1 = int(points[i] / self.total_frames * cw)
            x2 = int(points[i + 1] / self.total_frames * cw)
            self.marker_canvas.create_rectangle(x1, 0, x2, 14,
                                                 fill=colors[i % len(colors)], outline="")
        for sp in self.split_points:
            x = int(sp / self.total_frames * cw)
            self.marker_canvas.create_line(x, 0, x, 14, fill="#44ff88", width=2)
            self.marker_canvas.create_polygon(x-5, 0, x+5, 0, x, 7, fill="#44ff88")

    def _update_seg_label(self):
        if not self.split_points:
            self.seg_lbl.configure(text="אין פיצולים - הכל קטע אחד")
            return
        pts = [0] + self.split_points + [self.total_frames]
        parts = [f"[{self._fmt(pts[i]/self.fps)}→{self._fmt(pts[i+1]/self.fps)}]"
                 for i in range(len(pts)-1)]
        self.seg_lbl.configure(text="  ".join(parts))

    def _export_segments(self):
        self.playing = False
        pts = [0] + self.split_points + [self.total_frames]
        total = len(pts) - 1
        for i in range(total):
            self._export_one(pts[i], pts[i+1], i+1, total)

    def _export_one(self, sf_, ef, idx, total):
        import cv2
        SAVES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        suffix = f"_part{idx}of{total}" if total > 1 else "_clip"
        out = SAVES_DIR / f"Clip_{ts}{suffix}.mp4"

        # שימוש ב-ffmpeg עם -ss/-to - מהיר מאוד
        fps = self.fps
        start_sec = sf_ / fps
        end_sec   = ef  / fps
        run_ff(
            "-i", self.video_path,
            "-ss", str(start_sec),
            "-to", str(end_sec),
            "-c", "copy",
            str(out),
            timeout=60
        )
        if idx == total:
            self.after(0, lambda: messagebox.showinfo(
                "נשמר", f"נשמרו {total} קטעים בתיקיית ReplayBuffer"))

    def _fmt(self, sec):
        sec = int(sec)
        return f"{sec//60}:{sec%60:02d}"

    def _close(self):
        self.playing = False
        time.sleep(0.05)
        self.cap.release()
        self.destroy()


# ── Main App ──────────────────────────────────────────────────────────────────

class ReplayBufferApp:
    def __init__(self):
        self.config = load_config()
        self.engine = CaptureEngine(self.config)
        self.hotkey_registered = None
        self.setting_hotkey = False
        self.saving = False

        self._build_ui()
        self._register_hotkey()
        self.engine.start()
        self._animate_indicator()

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Replay Buffer")
        self.root.geometry("920x580")
        self.root.resizable(True, True)
        self.root.minsize(700, 480)
        self.root.configure(bg="#0a0a0a")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        topbar = tk.Frame(self.root, bg="#111111", pady=10)
        topbar.pack(fill="x")
        tk.Label(topbar, text="⏺", font=("Segoe UI", 16),
                 bg="#111111", fg="#ff4444").pack(side="left", padx=(18, 4))
        tk.Label(topbar, text="REPLAY BUFFER",
                 font=("Segoe UI", 13, "bold"), bg="#111111", fg="#ffffff").pack(side="left")
        self.live_dot = tk.Label(topbar, text="● LIVE",
                                  font=("Segoe UI", 9, "bold"),
                                  bg="#111111", fg="#ff4444")
        self.live_dot.pack(side="left", padx=16)

        # encoder label
        enc = "NVENC 🚀" if self.engine._nvenc else "CPU"
        tk.Label(topbar, text=f"encoder: {enc}",
                 font=("Segoe UI", 8), bg="#111111", fg="#444444").pack(side="right", padx=12)

        body = tk.Frame(self.root, bg="#0a0a0a")
        body.pack(fill="both", expand=True)

        # ── Left panel ──
        left = tk.Frame(body, bg="#111111", width=260)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        inner = tk.Frame(left, bg="#111111")
        inner.pack(fill="both", expand=True, padx=18, pady=18)

        tk.Label(inner, text="מקש שמירה", font=("Segoe UI", 9),
                 bg="#111111", fg="#555555").pack(anchor="w")
        hk_row = tk.Frame(inner, bg="#111111")
        hk_row.pack(fill="x", pady=(4, 16))
        self.hotkey_label = tk.Label(hk_row, text=self.config["hotkey"],
                                      font=("Consolas", 20, "bold"),
                                      bg="#1a1a1a", fg="#ff4444",
                                      width=7, pady=8, relief="flat")
        self.hotkey_label.pack(side="left")
        self.set_key_btn = tk.Button(hk_row, text="שנה",
                                      font=("Segoe UI", 9),
                                      bg="#222222", fg="#aaaaaa",
                                      activebackground="#333333",
                                      relief="flat", padx=10, pady=8,
                                      cursor="hand2",
                                      command=self._start_hotkey_capture)
        self.set_key_btn.pack(side="left", padx=(8, 0))

        tk.Label(inner, text="אורך buffer (דקות)", font=("Segoe UI", 9),
                 bg="#111111", fg="#555555").pack(anchor="w")
        dur_row = tk.Frame(inner, bg="#111111")
        dur_row.pack(fill="x", pady=(4, 16))
        self.dur_btns = {}
        for val in [1, 2, 5, 10, 15]:
            sel = val == self.config["buffer_minutes"]
            b = tk.Button(dur_row, text=str(val),
                          font=("Segoe UI", 10, "bold"),
                          bg="#ff4444" if sel else "#1e1e1e",
                          fg="#ffffff", relief="flat", width=4, pady=5,
                          cursor="hand2",
                          command=lambda v=val: self._set_minutes(v))
            b.pack(side="left", padx=2)
            self.dur_btns[val] = b

        tk.Frame(inner, bg="#222222", height=1).pack(fill="x", pady=8)

        self.save_btn = tk.Button(inner,
                                   text=f"⬤  שמור ({self.config['hotkey']})",
                                   font=("Segoe UI", 12, "bold"),
                                   bg="#ff4444", fg="#ffffff",
                                   activebackground="#cc3333",
                                   relief="flat", pady=14,
                                   cursor="hand2",
                                   command=self._save_now)
        self.save_btn.pack(fill="x")

        self.save_status = tk.Label(inner, text="",
                                     font=("Segoe UI", 9),
                                     bg="#111111", fg="#44ff88")
        self.save_status.pack(pady=(8, 0))

        tk.Frame(inner, bg="#222222", height=1).pack(fill="x", pady=12)

        tk.Label(inner, text=str(SAVES_DIR),
                 font=("Segoe UI", 7), bg="#111111", fg="#2a2a2a",
                 wraplength=220, justify="left").pack(anchor="w")
        tk.Button(inner, text="📁  פתח תיקייה",
                  font=("Segoe UI", 9), bg="#1a1a1a", fg="#777777",
                  activebackground="#2a2a2a", relief="flat", pady=6,
                  cursor="hand2", command=self._open_folder).pack(fill="x", pady=(8, 0))

        # ── Right panel ──
        right = tk.Frame(body, bg="#0a0a0a")
        right.pack(side="right", fill="both", expand=True, padx=16, pady=16)

        hdr = tk.Frame(right, bg="#0a0a0a")
        hdr.pack(fill="x", pady=(0, 8))
        tk.Label(hdr, text="הקלטות שמורות",
                 font=("Segoe UI", 11, "bold"), bg="#0a0a0a", fg="#cccccc").pack(side="left")
        tk.Label(hdr, text="לחץ פעמיים לעריכה",
                 font=("Segoe UI", 8), bg="#0a0a0a", fg="#333333").pack(side="right")

        lf = tk.Frame(right, bg="#0a0a0a")
        lf.pack(fill="both", expand=True)
        sb = tk.Scrollbar(lf, bg="#1a1a1a", troughcolor="#111111", relief="flat")
        sb.pack(side="right", fill="y")
        self.saves_list = tk.Listbox(lf, bg="#0d0d0d", fg="#cccccc",
                                      font=("Consolas", 9),
                                      selectbackground="#ff4444",
                                      selectforeground="#ffffff",
                                      relief="flat", borderwidth=0,
                                      highlightthickness=0, activestyle="none",
                                      yscrollcommand=sb.set)
        self.saves_list.pack(side="left", fill="both", expand=True)
        sb.config(command=self.saves_list.yview)
        self.saves_list.bind("<Double-Button-1>", self._open_editor)

        tk.Button(right, text="🗑  מחק נבחר",
                  font=("Segoe UI", 9), bg="#1a1a1a", fg="#666666",
                  activebackground="#2a1a1a", activeforeground="#ff4444",
                  relief="flat", pady=5, cursor="hand2",
                  command=self._delete_selected).pack(fill="x", pady=(8, 0))

        self._save_paths = []
        self._refresh_saves_list()

    def _animate_indicator(self):
        cur = self.live_dot.cget("fg")
        self.live_dot.configure(fg="#ff4444" if cur == "#330000" else "#330000")
        self.root.after(600, self._animate_indicator)

    def _set_minutes(self, val):
        self.config["buffer_minutes"] = val
        save_config(self.config)
        self.engine.update_config(self.config)
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
            self.root.after(0, lambda: self.save_btn.configure(
                text=f"⬤  שמור ({new_key})"))
            self.root.after(0, lambda: self.set_key_btn.configure(state="normal"))
        self.setting_hotkey = False

    def _register_hotkey(self):
        if self.hotkey_registered:
            try:
                keyboard.remove_hotkey(self.hotkey_registered)
            except Exception:
                pass
        try:
            self.hotkey_registered = keyboard.add_hotkey(
                self.config["hotkey"].lower(), self._save_now)
        except Exception:
            pass

    def _save_now(self):
        if self.saving:
            return
        self.saving = True
        self.save_status.configure(text="💾 שומר...", fg="#ffaa00")
        self.save_btn.configure(state="disabled")

        def done(path):
            self.saving = False
            self.root.after(0, lambda: self.save_status.configure(
                text="✅ נשמר!", fg="#44ff88"))
            self.root.after(0, lambda: self.save_btn.configure(state="normal"))
            self.root.after(0, self._refresh_saves_list)
            self.root.after(3000, lambda: self.save_status.configure(text=""))

        self.engine.save_snapshot(done)

    def _refresh_saves_list(self):
        self.saves_list.delete(0, tk.END)
        self._save_paths = []
        if SAVES_DIR.exists():
            files = sorted(SAVES_DIR.glob("*.mp4"), reverse=True)
            for f in files[:50]:
                size_mb = f.stat().st_size / (1024 * 1024)
                name = f.stem.replace("Replay_", "🎬 ").replace("Clip_", "✂ ").replace("_", "  ")
                self.saves_list.insert(tk.END, f"  {name}    {size_mb:.1f} MB")
                self._save_paths.append(f)

    def _open_editor(self, event):
        sel = self.saves_list.curselection()
        if sel:
            VideoEditor(self.root, str(self._save_paths[sel[0]]))

    def _delete_selected(self):
        sel = self.saves_list.curselection()
        if not sel:
            return
        path = self._save_paths[sel[0]]
        if messagebox.askyesno("מחיקה", f"למחוק את {path.name}?"):
            try:
                path.unlink()
                self._refresh_saves_list()
            except Exception as e:
                messagebox.showerror("שגיאה", str(e))

    def _open_folder(self):
        SAVES_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(SAVES_DIR))

    def _on_close(self):
        self.engine.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup_autostart_and_shortcut():
    if not getattr(sys, "frozen", False):
        return
    exe_path = Path(sys.executable)

    try:
        import winreg as _wr
        with _wr.OpenKey(_wr.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            desktop = Path(_wr.QueryValueEx(k, "Desktop")[0])
        shortcut_path = desktop / "Replay Buffer.lnk"
        if not shortcut_path.exists():
            from win32com.client import Dispatch
            import pythoncom
            pythoncom.CoInitialize()
            shell = Dispatch("WScript.Shell")
            lnk = shell.CreateShortCut(str(shortcut_path))
            lnk.TargetPath = str(exe_path)
            lnk.WorkingDirectory = str(exe_path.parent)
            lnk.IconLocation = str(exe_path)
            lnk.Description = "Replay Buffer"
            lnk.WindowStyle = 1
            lnk.Save()
    except Exception:
        pass

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ReplayBuffer", 0,
                              winreg.REG_SZ, f'"{exe_path}"')
    except Exception:
        pass


if __name__ == "__main__":
    setup_autostart_and_shortcut()
    app = ReplayBufferApp()
    app.run()
