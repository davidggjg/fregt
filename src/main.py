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
import sounddevice as sd
import soundfile as sf
import subprocess
import tempfile

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


AUDIO_SAMPLERATE = 44100
AUDIO_CHANNELS = 2


def get_loopback_device():
    """מחפש device של WASAPI loopback (סאונד מהמחשב)"""
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            name = d["name"].lower()
            if "loopback" in name or "stereo mix" in name or "what u hear" in name or "wave out" in name:
                if d["max_input_channels"] > 0:
                    return i
        # fallback - default output as loopback
        default_out = sd.query_devices(kind="output")
        for i, d in enumerate(devices):
            if d["name"] == default_out["name"] and d.get("hostapi") is not None:
                hostapi = sd.query_hostapis(d["hostapi"])
                if "wasapi" in hostapi["name"].lower():
                    return i
    except:
        pass
    return None


def get_mic_device():
    """מחפש את ה-default microphone"""
    try:
        return sd.query_devices(kind="input")["index"] if hasattr(
            sd.query_devices(kind="input"), "index") else None
    except:
        return None


class AudioCircularBuffer:
    """Buffer מעגלי לאודיו (chunks של numpy)"""
    def __init__(self, max_seconds, samplerate=AUDIO_SAMPLERATE, channels=AUDIO_CHANNELS):
        self.max_chunks = int(max_seconds * samplerate / 1024) + 1
        self.chunks = []
        self.lock = threading.Lock()
        self.samplerate = samplerate
        self.channels = channels

    def push(self, chunk):
        with self.lock:
            self.chunks.append(chunk.copy())
            if len(self.chunks) > self.max_chunks:
                self.chunks.pop(0)

    def snapshot(self):
        with self.lock:
            if not self.chunks:
                return None
            return np.concatenate(self.chunks, axis=0)

    def resize(self, max_seconds):
        with self.lock:
            self.max_chunks = int(max_seconds * self.samplerate / 1024) + 1
            if len(self.chunks) > self.max_chunks:
                self.chunks = self.chunks[-self.max_chunks:]


class VideoCircularBuffer:
    """Buffer מעגלי לוידאו"""
    def __init__(self, max_frames):
        self.max_frames = max_frames
        self.frames = []
        self.lock = threading.Lock()

    def push(self, frame):
        with self.lock:
            self.frames.append(frame)
            if len(self.frames) > self.max_frames:
                self.frames.pop(0)

    def snapshot(self):
        with self.lock:
            return list(self.frames)

    def resize(self, max_frames):
        with self.lock:
            self.max_frames = max_frames
            if len(self.frames) > max_frames:
                self.frames = self.frames[-max_frames:]


class CaptureEngine:
    """מקליט וידאו + אודיו ברקע כל הזמן"""
    def __init__(self, config):
        self.config = config
        self._running = False
        self._threads = []
        self._init_buffers()

    def _init_buffers(self):
        fps = self.config["fps"]
        minutes = self.config["buffer_minutes"]
        max_f = int(minutes * 60 * fps)
        max_s = minutes * 60 + 10
        self.video_buf = VideoCircularBuffer(max_f)
        self.mic_buf = AudioCircularBuffer(max_s)
        self.speaker_buf = AudioCircularBuffer(max_s)

    def start(self):
        self._running = True
        # וידאו
        t_video = threading.Thread(target=self._video_loop, daemon=True)
        t_video.start()
        self._threads.append(t_video)
        # מיקרופון
        t_mic = threading.Thread(target=self._mic_loop, daemon=True)
        t_mic.start()
        self._threads.append(t_mic)
        # סאונד מהמחשב (loopback)
        t_spk = threading.Thread(target=self._speaker_loop, daemon=True)
        t_spk.start()
        self._threads.append(t_spk)

    def stop(self):
        self._running = False

    def update_config(self, config):
        self.config = config
        fps = config["fps"]
        minutes = config["buffer_minutes"]
        max_f = int(minutes * 60 * fps)
        max_s = minutes * 60 + 10
        self.video_buf.resize(max_f)
        self.mic_buf.resize(max_s)
        self.speaker_buf.resize(max_s)

    def _video_loop(self):
        fps = self.config["fps"]
        interval = 1.0 / fps
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            while self._running:
                t0 = time.time()
                try:
                    img = sct.grab(monitor)
                    frame = np.array(img)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    h, w = frame.shape[:2]
                    if w > 1280:
                        scale = 1280 / w
                        frame = cv2.resize(frame, (1280, int(h * scale)))
                    self.video_buf.push(frame)
                except:
                    pass
                dt = time.time() - t0
                wait = interval - dt
                if wait > 0:
                    time.sleep(wait)

    def _mic_loop(self):
        """מקליט מיקרופון"""
        try:
            def callback(indata, frames, time_info, status):
                if self._running:
                    self.mic_buf.push(indata)

            with sd.InputStream(channels=1, samplerate=AUDIO_SAMPLERATE,
                                blocksize=1024, callback=callback):
                while self._running:
                    time.sleep(0.1)
        except Exception:
            pass  # אם אין מיקרופון - ממשיך בלי

    def _speaker_loop(self):
        """מקליט סאונד מהמחשב (WASAPI loopback)"""
        try:
            loopback_idx = get_loopback_device()

            def callback(indata, frames, time_info, status):
                if self._running:
                    self.speaker_buf.push(indata)

            kwargs = dict(channels=AUDIO_CHANNELS, samplerate=AUDIO_SAMPLERATE,
                          blocksize=1024, callback=callback)
            if loopback_idx is not None:
                kwargs["device"] = loopback_idx

            with sd.InputStream(**kwargs):
                while self._running:
                    time.sleep(0.1)
        except Exception:
            pass  # אם אין loopback - ממשיך בלי

    def save_snapshot(self, on_done):
        frames = self.video_buf.snapshot()
        mic_audio = self.mic_buf.snapshot()
        spk_audio = self.speaker_buf.snapshot()
        if not frames:
            return
        threading.Thread(
            target=self._write,
            args=(frames, mic_audio, spk_audio, on_done),
            daemon=True
        ).start()

    def _write(self, frames, mic_audio, spk_audio, on_done):
        SAVES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        tmp_dir = Path(tempfile.mkdtemp())
        video_tmp = tmp_dir / "video_only.mp4"
        out_final = SAVES_DIR / f"Replay_{ts}.mp4"

        # כתיבת וידאו זמני
        h, w = frames[0].shape[:2]
        fps = self.config["fps"]
        writer = cv2.VideoWriter(str(video_tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        for f in frames:
            writer.write(f)
        writer.release()

        # מיקס אודיו: מיקרופון (mono→stereo) + רמקולים
        audio_tmp = None
        try:
            mixed = None

            if spk_audio is not None and len(spk_audio) > 0:
                if spk_audio.ndim == 1:
                    spk_audio = np.stack([spk_audio, spk_audio], axis=1)
                elif spk_audio.shape[1] == 1:
                    spk_audio = np.concatenate([spk_audio, spk_audio], axis=1)
                mixed = spk_audio.astype(np.float32)

            if mic_audio is not None and len(mic_audio) > 0:
                if mic_audio.ndim == 1:
                    mic_stereo = np.stack([mic_audio, mic_audio], axis=1)
                elif mic_audio.shape[1] == 1:
                    mic_stereo = np.concatenate([mic_audio, mic_audio], axis=1)
                else:
                    mic_stereo = mic_audio
                mic_stereo = mic_stereo.astype(np.float32)
                if mixed is not None:
                    # חותך לאותו אורך
                    min_len = min(len(mixed), len(mic_stereo))
                    mixed = mixed[:min_len] + mic_stereo[:min_len]
                else:
                    mixed = mic_stereo

            if mixed is not None:
                # נרמול
                peak = np.max(np.abs(mixed))
                if peak > 0:
                    mixed = mixed / peak * 0.9
                audio_tmp = tmp_dir / "audio.wav"
                sf.write(str(audio_tmp), mixed, AUDIO_SAMPLERATE)
        except Exception:
            audio_tmp = None

        # מיזוג וידאו + אודיו עם ffmpeg
        try:
            # חיפוש ffmpeg: ליד ה-EXE → PATH
            exe_dir = Path(sys.executable).parent
            ffmpeg_local = exe_dir / "ffmpeg.exe"
            ffmpeg_cmd = str(ffmpeg_local) if ffmpeg_local.exists() else "ffmpeg"

            if audio_tmp and audio_tmp.exists():
                cmd = [
                    ffmpeg_cmd, "-y",
                    "-i", str(video_tmp),
                    "-i", str(audio_tmp),
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-shortest",
                    str(out_final)
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=120)
                if result.returncode != 0:
                    # ffmpeg נכשל - שומר רק וידאו
                    import shutil
                    shutil.copy(str(video_tmp), str(out_final))
            else:
                import shutil
                shutil.copy(str(video_tmp), str(out_final))
        except Exception:
            import shutil
            try:
                shutil.copy(str(video_tmp), str(out_final))
            except:
                pass

        # ניקוי קבצים זמניים
        try:
            video_tmp.unlink(missing_ok=True)
            if audio_tmp:
                audio_tmp.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except:
            pass

        if on_done:
            on_done(str(out_final))


# ────────────────────────────────────────────────────────────────────────────
# Editor window
# ────────────────────────────────────────────────────────────────────────────

class VideoEditor(tk.Toplevel):
    def __init__(self, parent, video_path):
        super().__init__(parent)
        self.title(f"עורך - {Path(video_path).name}")
        self.configure(bg="#0d0d0d")
        self.resizable(True, True)
        self.geometry("900x620")
        self.minsize(700, 500)

        self.video_path = video_path
        self.cap = cv2.VideoCapture(video_path)
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.duration = self.total_frames / self.fps
        self.current_frame = 0
        self.playing = False
        self.play_thread = None
        self.split_points = []  # רשימת נקודות פיצול (בפריימים)
        self._photo = None

        self._build()
        self._seek(0)
        self.protocol("WM_DELETE_WINDOW", self._close)

        # מסך מלא
        self.attributes("-fullscreen", False)
        self.lift()
        self.focus_force()
        self.bind("<F11>", self._toggle_fullscreen)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

    def _build(self):
        # ── Video canvas ──
        self.canvas = tk.Canvas(self, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=0, pady=0)
        self.canvas.bind("<Configure>", lambda e: self._redraw_frame())

        # ── Controls bar ──
        ctrl = tk.Frame(self, bg="#111111", pady=8)
        ctrl.pack(fill="x", side="bottom")

        # Timeline
        tl_frame = tk.Frame(ctrl, bg="#111111")
        tl_frame.pack(fill="x", padx=16, pady=(0, 6))

        # Split markers canvas (thin strip above slider)
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

        # Time label
        time_row = tk.Frame(ctrl, bg="#111111")
        time_row.pack(fill="x", padx=16)
        self.time_lbl = tk.Label(time_row, text="0:00 / 0:00",
                                  font=("Consolas", 9), bg="#111111", fg="#888888")
        self.time_lbl.pack(side="left")

        hint = tk.Label(time_row, text="F11 = מסך מלא",
                        font=("Segoe UI", 8), bg="#111111", fg="#333333")
        hint.pack(side="right")

        # Buttons row
        btn_row = tk.Frame(ctrl, bg="#111111")
        btn_row.pack(fill="x", padx=16, pady=(6, 0))

        def mkbtn(parent, text, cmd, color="#2a2a2a", fg="#cccccc", width=None):
            kw = dict(font=("Segoe UI", 9, "bold"), bg=color, fg=fg,
                      activebackground="#3a3a3a", activeforeground="#ffffff",
                      relief="flat", pady=6, padx=10, cursor="hand2", command=cmd)
            if width:
                kw["width"] = width
            return tk.Button(parent, text=text, **kw)

        self.play_btn = mkbtn(btn_row, "▶  נגן", self._toggle_play, width=10)
        self.play_btn.pack(side="left", padx=(0, 6))

        mkbtn(btn_row, "◀◀ -5s", lambda: self._skip(-5), width=8).pack(side="left", padx=2)
        mkbtn(btn_row, "+5s ▶▶", lambda: self._skip(5), width=8).pack(side="left", padx=2)

        tk.Frame(btn_row, bg="#333333", width=1).pack(side="left", padx=10, fill="y")

        mkbtn(btn_row, "✂  פצל כאן", self._add_split, "#1e3a1e", "#44ff88").pack(side="left", padx=2)
        mkbtn(btn_row, "↩  בטל פיצול", self._undo_split, "#2a1a1a", "#ff8888").pack(side="left", padx=2)

        tk.Frame(btn_row, bg="#333333", width=1).pack(side="left", padx=10, fill="y")

        mkbtn(btn_row, "💾  שמור קטעים", self._export_segments, "#ff4444", "#ffffff").pack(side="left", padx=2)

        # Segments list (right side)
        seg_frame = tk.Frame(ctrl, bg="#111111")
        seg_frame.pack(fill="x", padx=16, pady=(8, 0))

        tk.Label(seg_frame, text="קטעים:", font=("Segoe UI", 8),
                 bg="#111111", fg="#555555").pack(side="left")

        self.seg_lbl = tk.Label(seg_frame, text="אין פיצולים - הכל קטע אחד",
                                 font=("Consolas", 8), bg="#111111", fg="#888888")
        self.seg_lbl.pack(side="left", padx=(6, 0))

    def _toggle_fullscreen(self, event=None):
        state = self.attributes("-fullscreen")
        self.attributes("-fullscreen", not state)

    def _on_timeline(self, val):
        frame_num = int(float(val))
        if not self.playing:
            self._seek(frame_num)

    def _seek(self, frame_num):
        frame_num = max(0, min(frame_num, self.total_frames - 1))
        self.current_frame = frame_num
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = self.cap.read()
        if ret:
            self._display_frame(frame)
        self.timeline.set(frame_num)
        cur = frame_num / self.fps
        self.time_lbl.configure(text=f"{self._fmt(cur)} / {self._fmt(self.duration)}")

    def _display_frame(self, frame):
        self._last_frame = frame
        self._redraw_frame()

    def _redraw_frame(self):
        if not hasattr(self, "_last_frame") or self._last_frame is None:
            return
        frame = self._last_frame
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 2 or ch < 2:
            return
        h, w = frame.shape[:2]
        scale = min(cw / w, ch / h)
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(frame, (nw, nh))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        from PIL import Image, ImageTk
        img = Image.fromarray(rgb)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, anchor="center", image=self._photo)

    def _toggle_play(self):
        if self.playing:
            self.playing = False
            self.play_btn.configure(text="▶  נגן")
        else:
            self.playing = True
            self.play_btn.configure(text="⏸  עצור")
            self.play_thread = threading.Thread(target=self._play_loop, daemon=True)
            self.play_thread.start()

    def _play_loop(self):
        interval = 1.0 / self.fps
        while self.playing and self.current_frame < self.total_frames - 1:
            t0 = time.time()
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame)
            ret, frame = self.cap.read()
            if not ret:
                break
            self.current_frame += 1
            self.after(0, lambda f=frame, n=self.current_frame: self._update_play(f, n))
            dt = time.time() - t0
            wait = interval - dt
            if wait > 0:
                time.sleep(wait)
        self.playing = False
        self.after(0, lambda: self.play_btn.configure(text="▶  נגן"))

    def _update_play(self, frame, frame_num):
        self._display_frame(frame)
        self.timeline.set(frame_num)
        cur = frame_num / self.fps
        self.time_lbl.configure(text=f"{self._fmt(cur)} / {self._fmt(self.duration)}")

    def _skip(self, seconds):
        self.playing = False
        new = self.current_frame + int(seconds * self.fps)
        new = max(0, min(new, self.total_frames - 1))
        self._seek(new)

    def _add_split(self):
        pos = self.current_frame
        if pos not in self.split_points and pos > 0 and pos < self.total_frames - 1:
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
        if cw < 2 or self.total_frames < 1:
            return
        # background segments
        points = [0] + self.split_points + [self.total_frames]
        colors = ["#1e3a1e", "#1a1e3a", "#3a1a1e", "#1e3a3a", "#2a1e3a"]
        for i in range(len(points) - 1):
            x1 = int(points[i] / self.total_frames * cw)
            x2 = int(points[i + 1] / self.total_frames * cw)
            self.marker_canvas.create_rectangle(x1, 0, x2, 14,
                                                 fill=colors[i % len(colors)], outline="")
        # split lines
        for sp in self.split_points:
            x = int(sp / self.total_frames * cw)
            self.marker_canvas.create_line(x, 0, x, 14, fill="#44ff88", width=2)
            self.marker_canvas.create_polygon(x - 5, 0, x + 5, 0, x, 7,
                                               fill="#44ff88", outline="")

    def _update_seg_label(self):
        if not self.split_points:
            self.seg_lbl.configure(text="אין פיצולים - הכל קטע אחד")
            return
        points = [0] + self.split_points + [self.total_frames]
        parts = []
        for i in range(len(points) - 1):
            s = self._fmt(points[i] / self.fps)
            e = self._fmt(points[i + 1] / self.fps)
            parts.append(f"[{s}→{e}]")
        self.seg_lbl.configure(text="  ".join(parts))

    def _export_segments(self):
        self.playing = False
        points = [0] + self.split_points + [self.total_frames]
        if len(points) == 2:
            # no splits - save as trim
            self._export_one(0, self.total_frames, 1, 1)
            return
        for i in range(len(points) - 1):
            self._export_one(points[i], points[i + 1], i + 1, len(points) - 1)

    def _export_one(self, start_f, end_f, idx, total):
        cap2 = cv2.VideoCapture(self.video_path)
        fps = cap2.get(cv2.CAP_PROP_FPS) or 30
        w = int(cap2.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap2.get(cv2.CAP_PROP_FRAME_HEIGHT))
        SAVES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        suffix = f"_part{idx}of{total}" if total > 1 else "_clip"
        out_path = SAVES_DIR / f"Clip_{ts}{suffix}.mp4"
        cap2.set(cv2.CAP_PROP_POS_FRAMES, start_f)
        writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

        def run():
            for _ in range(end_f - start_f):
                ret, frame = cap2.read()
                if not ret:
                    break
                writer.write(frame)
            cap2.release()
            writer.release()
            self.after(0, lambda: messagebox.showinfo(
                "נשמר", f"נשמרו {total} קטעים בתיקיית ReplayBuffer") if idx == total else None)

        threading.Thread(target=run, daemon=True).start()

    def _fmt(self, sec):
        sec = int(sec)
        return f"{sec // 60}:{sec % 60:02d}"

    def _close(self):
        self.playing = False
        time.sleep(0.1)
        self.cap.release()
        self.destroy()


# ────────────────────────────────────────────────────────────────────────────
# Main App
# ────────────────────────────────────────────────────────────────────────────

class ReplayBufferApp:
    def __init__(self):
        self.config = load_config()
        self.engine = CaptureEngine(self.config)
        self.hotkey_registered = None
        self.setting_hotkey = False
        self.saving = False

        self._build_ui()
        self._register_hotkey()

        # מתחיל להקליט מיד
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

        # ── Top bar ──────────────────────────────────────────────────
        topbar = tk.Frame(self.root, bg="#111111", pady=10)
        topbar.pack(fill="x")

        tk.Label(topbar, text="⏺", font=("Segoe UI", 16),
                 bg="#111111", fg="#ff4444").pack(side="left", padx=(18, 4))
        tk.Label(topbar, text="REPLAY BUFFER",
                 font=("Segoe UI", 13, "bold"), bg="#111111", fg="#ffffff").pack(side="left")

        # Live indicator
        self.live_dot = tk.Label(topbar, text="● LIVE",
                                  font=("Segoe UI", 9, "bold"),
                                  bg="#111111", fg="#ff4444")
        self.live_dot.pack(side="left", padx=16)

        # ── Main layout ──────────────────────────────────────────────
        body = tk.Frame(self.root, bg="#0a0a0a")
        body.pack(fill="both", expand=True, padx=0, pady=0)

        # Left settings panel
        left = tk.Frame(body, bg="#111111", width=260)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        inner = tk.Frame(left, bg="#111111")
        inner.pack(fill="both", expand=True, padx=18, pady=18)

        # Hotkey
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

        # Buffer duration
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

        tk.Frame(inner, bg="#222222", height=1).pack(fill="x", pady=12)

        # Save button (big)
        self.save_btn = tk.Button(inner, text=f"⬤  שמור ({self.config['hotkey']})",
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

        # Info
        tk.Label(inner, text="הקלטות נשמרות ב:", font=("Segoe UI", 8),
                 bg="#111111", fg="#333333").pack(anchor="w")
        tk.Label(inner, text=str(SAVES_DIR),
                 font=("Segoe UI", 7), bg="#111111", fg="#2a2a2a",
                 wraplength=220, justify="left").pack(anchor="w", pady=(2, 8))

        tk.Button(inner, text="📁  פתח תיקייה",
                  font=("Segoe UI", 9),
                  bg="#1a1a1a", fg="#777777",
                  activebackground="#2a2a2a",
                  relief="flat", pady=6,
                  cursor="hand2",
                  command=self._open_folder).pack(fill="x")

        # ── Right: saves list ────────────────────────────────────────
        right = tk.Frame(body, bg="#0a0a0a")
        right.pack(side="right", fill="both", expand=True, padx=16, pady=16)

        hdr = tk.Frame(right, bg="#0a0a0a")
        hdr.pack(fill="x", pady=(0, 8))
        tk.Label(hdr, text="הקלטות שמורות",
                 font=("Segoe UI", 11, "bold"),
                 bg="#0a0a0a", fg="#cccccc").pack(side="left")
        tk.Label(hdr, text="לחץ פעמיים לעריכה",
                 font=("Segoe UI", 8),
                 bg="#0a0a0a", fg="#333333").pack(side="right")

        # List with scrollbar
        list_frame = tk.Frame(right, bg="#0a0a0a")
        list_frame.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(list_frame, bg="#1a1a1a",
                                  troughcolor="#111111", relief="flat")
        scrollbar.pack(side="right", fill="y")

        self.saves_list = tk.Listbox(list_frame,
                                      bg="#0d0d0d", fg="#cccccc",
                                      font=("Consolas", 9),
                                      selectbackground="#ff4444",
                                      selectforeground="#ffffff",
                                      relief="flat", borderwidth=0,
                                      highlightthickness=0,
                                      activestyle="none",
                                      yscrollcommand=scrollbar.set)
        self.saves_list.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.saves_list.yview)
        self.saves_list.bind("<Double-Button-1>", self._open_editor)

        tk.Button(right, text="🗑  מחק נבחר",
                  font=("Segoe UI", 9),
                  bg="#1a1a1a", fg="#666666",
                  activebackground="#2a1a1a", activeforeground="#ff4444",
                  relief="flat", pady=5,
                  cursor="hand2",
                  command=self._delete_selected).pack(fill="x", pady=(8, 0))

        self._save_paths = []
        self._refresh_saves_list()

    # ── Indicator animation ──────────────────────────────────────────

    def _animate_indicator(self):
        current = self.live_dot.cget("fg")
        self.live_dot.configure(fg="#ff4444" if current == "#330000" else "#330000")
        self.root.after(600, self._animate_indicator)

    # ── Config ───────────────────────────────────────────────────────

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
            except:
                pass
        try:
            self.hotkey_registered = keyboard.add_hotkey(
                self.config["hotkey"].lower(), self._save_now)
        except:
            pass

    # ── Save ─────────────────────────────────────────────────────────

    def _save_now(self):
        if self.saving:
            return
        self.saving = True
        self.save_status.configure(text="💾 שומר...", fg="#ffaa00")

        def done(path):
            self.saving = False
            self.root.after(0, lambda: self.save_status.configure(
                text=f"✅ נשמר!", fg="#44ff88"))
            self.root.after(0, self._refresh_saves_list)
            self.root.after(3000, lambda: self.save_status.configure(text=""))

        self.engine.save_snapshot(done)

    # ── Saves list ───────────────────────────────────────────────────

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
        if not sel:
            return
        path = str(self._save_paths[sel[0]])
        VideoEditor(self.root, path)

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


def setup_autostart_and_shortcut():
    """יוצר קיצור דרך בשולחן העבודה ומוסיף להפעלה אוטומטית עם Windows"""
    # רק כשרץ כ-EXE בנוי (לא בפיתוח)
    if not getattr(sys, "frozen", False):
        return

    exe_path = Path(sys.executable)

    # ── קיצור דרך בשולחן עבודה ──────────────────────────────────
    try:
        import winreg as _wr
        # מוצא את נתיב שולחן העבודה מה-Registry (עובד בכל שפה/משתמש)
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
            lnk.Description = "Replay Buffer - הקלטת מסך"
            lnk.WindowStyle = 1  # normal window
            lnk.Save()
    except Exception:
        pass

    # ── הפעלה אוטומטית עם Windows (Registry) ────────────────────
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path,
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ReplayBuffer", 0,
                              winreg.REG_SZ, f'"{exe_path}"')
    except Exception:
        pass


if __name__ == "__main__":
    setup_autostart_and_shortcut()
    app = ReplayBufferApp()
    app.run()
