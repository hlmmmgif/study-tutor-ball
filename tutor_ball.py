# -*- coding: utf-8 -*-
"""
AI 答疑悬浮球（截图作答版）
==========================
一个常驻 Windows 桌面的悬浮球：看到不会的题，点悬浮球上的相机按钮，
它抓下整个屏幕、直接交给视觉模型读图作答，答案流式显示出来。

工作流程：抓屏（先隐藏本窗口，避免挡住题目）-> 内存中转 PNG -> base64
          -> deepseek-flash 读图 -> 流式显示答案

设计取舍（有意为之）：
  * 不自动答题、不注入页面、不模拟你的操作。每次作答都要你亲手点相机按钮。
  * 不做任何反检测/伪装。它就是一个普通置顶窗口，用普通 API 调用提问。
  * 截图只在内存里处理，用完立即丢弃，不写硬盘。
  * 只依赖 Python 标准库（tkinter + urllib + zlib），无需 pip 安装任何东西。

三种形态：悬浮球 / 宽面板 / 窄条。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

import capture

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

APP_NAME = "答疑悬浮球"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "答案记录.md"
API_URL = "https://api.deepseek.com/chat/completions"
VISION_MODEL = "deepseek-flash"

BALL_SIZE = 76
STRIP_W, STRIP_H = 430, 196
PANEL_W, PANEL_H = 480, 640

C_BG = "#161b22"
C_BG2 = "#1f2630"
C_LINE = "#2d3744"
C_FG = "#e6edf3"
C_DIM = "#8b98a5"
C_BALL = "#2f7cf6"
C_BALL_HOVER = "#4a90ff"
C_OK = "#3fb950"
C_ERR = "#f85149"
C_WARN = "#d29922"

# 固定的答案风格：答案 + 一行解析
SYSTEM_PROMPT = (
    "你是一个英语练习题作答助手。用户会给你一张电脑屏幕截图，"
    "截图里通常是一道或多道英语练习题。\n"
    "\n"
    "请按以下步骤处理：\n"
    "1. 先仔细读出截图里的题目和所有选项，不要遗漏选项文字。\n"
    "2. 判断题目在问什么，然后给出答案。\n"
    "3. 如果截图里同时有多道题，按题号逐题给出答案。\n"
    "\n"
    "输出格式（严格遵守）：\n"
    "- 第一行：答案本身。单选题只写选项字母（如 C）；"
    "多选题写字母连写（如 ACD）；填空题只写要填的词；"
    "判断题写 T 或 F；翻译/写作题直接给正文。\n"
    "- 第二行：用不超过 40 字的中文说明考点或理由。\n"
    "- 多道题时，每题两行，题号开头。\n"
    "\n"
    "重要：如果截图里没有题目、或者文字模糊看不清、或者选项缺失导致无法判断，"
    "第一行必须写「无法识别」，第二行说明缺什么。绝不要凭猜测给答案。\n"
    "不要客套话，不要重复题目原文，不要 Markdown 代码块。"
)

# 进度阶段的文案
STAGE_CAPTURE = "正在抓取屏幕…"
STAGE_ENCODE = "正在处理图像…"
STAGE_UPLOAD = "正在上传…"
STAGE_READ = "正在识别题目…"
STAGE_ANSWER = "正在作答…"
STAGE_DONE = "完成"

# Win32（仅用于全局热键与屏幕尺寸）
user32 = ctypes.windll.user32
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000


# --------------------------------------------------------------------------
# 配置与密钥
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = {"api_key": "", "hotkey": "ctrl+alt+u", "log_answers": True}
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        try:                      # 尽量收紧权限，别让同机其他用户读到 key
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    except OSError as e:
        print(f"[warn] 配置写入失败: {e}")


def resolve_key(cfg: dict) -> tuple[str, str]:
    """返回 (key, 来源)。本工具不含任何内置密钥，必须由使用者自己提供。"""
    if cfg.get("api_key"):
        return cfg["api_key"], "本地配置 config.json"
    env = os.environ.get("DEEPSEEK_API_KEY", "")
    if env:
        return env, "环境变量 DEEPSEEK_API_KEY"
    return "", "未找到"


def notify(text: str) -> None:
    """有控制台就打印；没有控制台（pythonw 启动）就弹窗。"""
    if sys.stdout is not None:
        try:
            print(text)
            return
        except Exception:
            pass
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, text)
        root.destroy()
    except Exception:
        pass


def setup_guide() -> int:
    lines = [
        "=" * 62,
        "  还没有配置 DeepSeek API key，无法开始答题。",
        "=" * 62,
        "",
        "请按下面两步操作：",
        "",
        "  1) 申请一个 API key",
        "     打开 https://platform.deepseek.com/api_keys",
        "     注册/登录后点「创建 API key」，复制得到的 sk- 开头的字符串",
        "",
        "  2) 把 key 填进配置文件",
        f"     {CONFIG_PATH}",
        '     找到 "api_key": "" 这一行，把 key 填进引号里，保存。',
        "",
        "     也可以改用环境变量：",
        '     setx DEEPSEEK_API_KEY "sk-你的key"',
        "     （设置后需要重新打开终端才生效）",
        "",
        "填好后重新运行本程序即可。配置文件已经帮你生成好了。",
    ]
    notify("\n".join(lines))
    return 1


# --------------------------------------------------------------------------
# 视觉模型调用
# --------------------------------------------------------------------------

class AnswerEngine:
    """后台线程里编码/上传/请求，通过队列把进度与增量文本交回主线程。

    队列消息：
      ("stage", 阶段文案)
      ("progress", 0.0~1.0)
      ("chunk", 文本增量)
      ("usage", dict)
      ("error", 错误文案)
      ("done", "")
    """

    def __init__(self) -> None:
        self.q: queue.Queue[tuple[str, object]] = queue.Queue()
        self._stop = threading.Event()
        self._resp = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.shot_info = ""

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, key: str, shot: tuple[bytes, int, int]) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(key, shot), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._resp is not None:
                try:
                    self._resp.close()
                except Exception:
                    pass

    # ---- 后台线程 ----

    def _run(self, key: str, shot: tuple[bytes, int, int]) -> None:
        try:
            png, w, h = shot
            self.q.put(("stage", STAGE_ENCODE))
            self.q.put(("progress", 0.08))
            b64 = base64.b64encode(png).decode("ascii")
            self.shot_info = f"{w}x{h}, {len(png)/1024:.0f}KB"

            if self._stop.is_set():
                return
            self.q.put(("progress", 0.16))
            self.q.put(("stage", STAGE_UPLOAD))

            payload = self._build_payload(b64)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                API_URL, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + key,
                         "Accept": "text/event-stream",
                         "User-Agent": "study-tutor-ball/2.0"},
                method="POST")
            try:
                resp = urllib.request.urlopen(req, timeout=180)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                tip = ""
                if e.code == 401:
                    tip = "（API key 失效，右键悬浮球 → 刷新 API key）"
                elif e.code == 402:
                    tip = "（DeepSeek 账户余额不足）"
                elif e.code == 413:
                    tip = "（截图太大，请缩小窗口后重试）"
                elif e.code == 429:
                    tip = "（请求太频繁，稍等再试）"
                self.q.put(("error", f"请求失败 HTTP {e.code} {tip}\n{detail}"))
                return
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"网络错误：{type(e).__name__}: {e}"))
                return

            with self._lock:
                self._resp = resp
            self.q.put(("stage", STAGE_READ))
            self.q.put(("progress", 0.22))
            self._stream(resp)
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"处理失败：{type(e).__name__}: {e}"))
        finally:
            self._cleanup()

    def _build_payload(self, b64: str) -> dict:
        return {
            "model": VISION_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "请读出这张截图里的题目并作答。"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}",
                                   "detail": "original"}},
                ]},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.0,
            "max_tokens": 2048,
        }

    def _stream(self, resp) -> None:
        """读 SSE 流；等待首字期间按秒数缓慢推进进度，避免看起来卡住。"""
        got_first = False
        t0 = time.time()
        last_push = 0.0
        try:
            for raw in resp:
                if self._stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue

                usage = obj.get("usage")
                if usage:
                    self.q.put(("usage", usage))

                choices = obj.get("choices") or []
                if not choices:
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if piece:
                    if not got_first:
                        got_first = True
                        self.q.put(("stage", STAGE_ANSWER))
                        self.q.put(("progress", 0.35))
                    self.q.put(("chunk", piece))

                now = time.time()
                if not got_first and now - last_push > 0.4:
                    last_push = now
                    elapsed = now - t0
                    frac = 0.22 + min(0.10, elapsed / 60.0 * 0.10)
                    self.q.put(("stage", f"{STAGE_READ} {elapsed:.0f}s"))
                    self.q.put(("progress", frac))
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self.q.put(("error", f"读取中断：{type(e).__name__}: {e}"))

    def _cleanup(self) -> None:
        with self._lock:
            resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        self.q.put(("progress", 1.0))
        self.q.put(("done", ""))


# --------------------------------------------------------------------------
# 进度条控件
# --------------------------------------------------------------------------

class ProgressBar(tk.Canvas):
    """扁平风格进度条，中间显示百分比。

    注意：绝不能把尺寸存进 self._w / self._h ——
    tkinter.Widget 用 self._w 保存 widget 路径名（如 '.!frame.!canvas'），
    覆盖它会让后续所有 create_* 调用拿数字当 widget 名，报
    "invalid command name"。所以这里用 _bar_w / _bar_h。
    """

    def __init__(self, parent, width=440, height=18, **kw):
        super().__init__(parent, width=width, height=height, bg=C_BG,
                         highlightthickness=0, **kw)
        self._bar_w, self._bar_h = width, height
        self._color = C_BALL
        self.create_rectangle(0, 0, width, height, fill=C_BG2, outline="", tags="track")
        self.bar = self.create_rectangle(0, 0, 0, height, fill=C_BALL,
                                         outline="", tags="bar")
        self.label = self.create_text(width // 2, height // 2, text="0%", fill=C_FG,
                                      font=("Microsoft YaHei UI", 8), tags="label")

    def set(self, value: float, color: str | None = None) -> None:
        value = max(0.0, min(1.0, value))
        if color:
            self._color = color
        self.itemconfig(self.bar, fill=self._color)
        self.coords(self.bar, 0, 0, int(self._bar_w * value), self._bar_h)
        self.itemconfig(self.label, text=f"{int(value * 100)}%")
        self.tag_raise("label")

    def reset(self) -> None:
        self._color = C_BALL
        self.set(0.0)


# --------------------------------------------------------------------------
# 主界面
# --------------------------------------------------------------------------

class TutorBall:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = load_config()
        self.key, self.key_src = resolve_key(self.cfg)
        self.engine = AnswerEngine()

        self.visible = True
        self.view = "ball"                 # ball | panel | strip
        self._drag: dict | None = None
        self._answer_buf: list[str] = []
        self._pending = ""
        self._shown = ""
        self._hotkey_id = 1
        self._hotkey_ok = False
        self._destroyed = False
        self._capturing = False
        self._t_start = 0.0
        self._usage_text = ""

        self.status: tk.Label | None = None
        self.aview: tk.Text | None = None
        self.pbar: ProgressBar | None = None
        self.btn_shoot: tk.Button | None = None

        save_config(self.cfg)              # 首次运行就把配置落盘，方便直接改
        self._build_window()
        self._build_ball()
        self.panel = tk.Frame(self.root, bg=C_BG)
        self._bind_global_hotkey()
        self._tick()

    # ---------------- 窗口与悬浮球 ----------------

    def _build_window(self) -> None:
        r = self.root
        r.title(APP_NAME)
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=C_BG)
        pos = self.cfg.get("ball_pos")
        x, y = (pos[0], pos[1]) if isinstance(pos, list) and len(pos) == 2 else (1180, 620)
        x, y = self._clamp(x, y, BALL_SIZE, BALL_SIZE)
        r.geometry(f"{BALL_SIZE}x{BALL_SIZE}+{x}+{y}")
        r.resizable(False, False)
        r.protocol("WM_DELETE_WINDOW", self._quit)

    def _clamp(self, x: int, y: int, w: int, h: int) -> tuple[int, int]:
        sw, sh = capture.screen_size()
        return max(0, min(int(x), max(0, sw - w))), max(0, min(int(y), max(0, sh - h)))

    def _build_ball(self) -> None:
        s = BALL_SIZE
        self.ball = tk.Canvas(self.root, width=s, height=s, bg=C_BG,
                              highlightthickness=0, cursor="hand2")
        self.ball.create_oval(3, 3, s - 3, s - 3, fill=C_BALL, outline="", tags="body")
        # 相机图标：机身(18,22)-(58,44) + 取景器凸起(28,16)-(48,23) + 镜头
        # 机身用白色填充（球的蓝色本身当描边底色），这样不会被填充逻辑搞乱
        self.ball.create_rectangle(18, 22, 58, 44, fill="white", outline="",
                                   tags="icon")
        self.ball.create_rectangle(28, 16, 48, 23, fill="white", outline="",
                                   tags="icon")
        self.ball.create_oval(31, 26, 45, 40, fill=C_BALL, outline="",
                              tags="lens")
        self.ball.create_text(s / 2, s / 2 + 24, text="点我答题", fill="#dce9ff",
                              font=("Microsoft YaHei UI", 7), tags="label")
        self.ball.bind("<Button-1>", self._ball_press)
        self.ball.bind("<B1-Motion>", self._ball_drag)
        self.ball.bind("<ButtonRelease-1>", self._ball_release)
        self.ball.bind("<Button-3>", self._popup_menu)
        self.ball.bind("<Enter>", lambda e: self._recolor_ball(C_BALL_HOVER))
        self.ball.bind("<Leave>", lambda e: self._recolor_ball(C_BALL))
        self.ball.pack(fill="both", expand=True)

    def _recolor_ball(self, color: str) -> None:
        """悬停时只换球体和镜头的底色，白色图标与文字保持不动。"""
        self.ball.itemconfig("body", fill=color)
        self.ball.itemconfig("lens", fill=color)

    def _ball_press(self, e) -> None:
        self._drag = {"x": e.x_root, "y": e.y_root,
                      "wx": self.root.winfo_x(), "wy": self.root.winfo_y(),
                      "moved": False}

    def _ball_drag(self, e) -> None:
        if not self._drag:
            return
        dx, dy = e.x_root - self._drag["x"], e.y_root - self._drag["y"]
        if abs(dx) > 3 or abs(dy) > 3:
            self._drag["moved"] = True
        self.root.geometry(f"+{self._drag['wx'] + dx}+{self._drag['wy'] + dy}")

    def _ball_release(self, e) -> None:
        moved = self._drag and self._drag["moved"]
        self._drag = None
        if moved:
            self._remember_ball_pos()
        else:
            self.show_panel("panel")

    def _remember_ball_pos(self) -> None:
        """只更新内存，退出时统一落盘，避免拖动时反复写文件。"""
        if self.view == "ball":
            self.cfg["ball_pos"] = [self.root.winfo_x(), self.root.winfo_y()]

    # ---------------- 视图切换 ----------------

    def show_ball(self) -> None:
        if self._destroyed:
            return
        self._remember_ball_pos()
        self.panel.pack_forget()
        for c in self.panel.winfo_children():
            c.destroy()
        self.aview = self.status = self.btn_shoot = self.pbar = None
        self.ball.pack(fill="both", expand=True)
        self.root.geometry(f"{BALL_SIZE}x{BALL_SIZE}")
        self.view = "ball"

    def show_panel(self, kind: str = "panel") -> None:
        if self._destroyed:
            return
        if self.view == "ball":
            self._remember_ball_pos()
        w, h = (STRIP_W, STRIP_H) if kind == "strip" else (PANEL_W, PANEL_H)
        bx, by = self.cfg.get("ball_pos", [self.root.winfo_x(), self.root.winfo_y()])
        x, y = self._clamp(bx - (w - BALL_SIZE), by - (h - BALL_SIZE) // 2, w, h)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.ball.pack_forget()
        for c in self.panel.winfo_children():
            c.destroy()
        self.aview = self.status = self.btn_shoot = self.pbar = None
        if kind == "strip":
            self._render_strip()
        else:
            self._render_panel()
        self.panel.pack(fill="both", expand=True)
        self.view = kind
        self.root.attributes("-topmost", True)
        if self.aview is not None and self._shown:
            self._write_answer(self._shown)
        self._refresh_buttons()

    # ---------------- 宽面板 ----------------

    def _render_panel(self) -> None:
        p = self.panel

        bar = tk.Frame(p, bg=C_BG2, height=34)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        title = tk.Label(bar, text="  答疑悬浮球", bg=C_BG2, fg=C_FG,
                         font=("Microsoft YaHei UI", 9, "bold"))
        title.pack(side="left")
        tk.Label(bar, text=f"· {VISION_MODEL}", bg=C_BG2, fg=C_DIM,
                 font=("Microsoft YaHei UI", 8)).pack(side="left")
        for wdg in (bar, title):
            wdg.bind("<Button-1>", self._panel_press)
            wdg.bind("<B1-Motion>", self._panel_drag)
            wdg.bind("<Button-3>", self._popup_menu)
        closer = tk.Label(bar, text=" ✕ ", bg=C_BG2, fg=C_DIM,
                          font=("Microsoft YaHei UI", 10), cursor="hand2")
        closer.pack(side="right", padx=4)
        closer.bind("<Button-1>", lambda e: self.show_ball())
        more = tk.Label(bar, text=" ▾ ", bg=C_BG2, fg=C_DIM,
                        font=("Microsoft YaHei UI", 10), cursor="hand2")
        more.pack(side="right")
        more.bind("<Button-1>", self._popup_menu)

        self.btn_shoot = tk.Button(
            p, text="📷  截屏答题", command=self.on_capture, relief="flat", bd=0,
            font=("Microsoft YaHei UI", 12, "bold"), bg=C_BALL, fg="white",
            activebackground=C_BALL_HOVER, activeforeground="white",
            padx=12, pady=10, cursor="hand2")
        self.btn_shoot.pack(fill="x", padx=12, pady=(12, 8))

        hint = tk.Label(p, text="点按钮后会隐藏本窗口再抓屏，不会挡住题目",
                        bg=C_BG, fg=C_DIM, anchor="w",
                        font=("Microsoft YaHei UI", 8))
        hint.pack(fill="x", padx=14)

        self.status = tk.Label(p, text="准备好后点「截屏答题」", bg=C_BG,
                               fg=C_DIM, anchor="w", font=("Microsoft YaHei UI", 9))
        self.status.pack(fill="x", padx=14, pady=(6, 4))
        self.pbar = ProgressBar(p, width=PANEL_W - 28)
        self.pbar.pack(padx=14, pady=(0, 8))

        tk.Label(p, text="答案", bg=C_BG, fg=C_DIM, anchor="w",
                 font=("Microsoft YaHei UI", 8)).pack(fill="x", padx=14)
        wrap = tk.Frame(p, bg=C_LINE)
        wrap.pack(fill="both", expand=True, padx=12, pady=(2, 0))
        self.aview = tk.Text(wrap, bg=C_BG2, fg=C_FG, relief="flat", wrap="word",
                             font=("Microsoft YaHei UI", 12), padx=10, pady=8,
                             state="disabled")
        sb = tk.Scrollbar(wrap, command=self.aview.yview, width=10)
        self.aview.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.aview.pack(fill="both", expand=True)

        foot = tk.Frame(p, bg=C_BG)
        foot.pack(fill="x", padx=12, pady=10)
        self.btn_copy = self._btn(foot, "复制答案", self._on_copy)
        self.btn_copy.pack(side="left")
        self.btn_clear = self._btn(foot, "清空", self._on_clear)
        self.btn_clear.pack(side="left", padx=6)
        self.btn_stop = self._btn(foot, "停止", self._on_stop)
        self.btn_stop.pack(side="right")

    # ---------------- 窄条 ----------------

    def _render_strip(self) -> None:
        p = self.panel
        head = tk.Frame(p, bg=C_BG)
        head.pack(fill="x", padx=10, pady=(6, 4))
        lbl = tk.Label(head, text="答疑", bg=C_BG, fg=C_DIM,
                       font=("Microsoft YaHei UI", 8), cursor="hand2")
        lbl.pack(side="left")
        lbl.bind("<Button-1>", self._panel_press)
        lbl.bind("<B1-Motion>", self._panel_drag)
        lbl.bind("<Button-3>", self._popup_menu)
        for txt, cmd in (("✕", lambda e: self.show_ball()),
                         ("▾", self._popup_menu)):
            w = tk.Label(head, text=txt, bg=C_BG, fg=C_DIM,
                         font=("Microsoft YaHei UI", 9), cursor="hand2")
            w.pack(side="right", padx=4)
            w.bind("<Button-1>", cmd)

        self.btn_shoot = tk.Button(
            p, text="📷  截屏答题", command=self.on_capture, relief="flat", bd=0,
            font=("Microsoft YaHei UI", 11, "bold"), bg=C_BALL, fg="white",
            activebackground=C_BALL_HOVER, activeforeground="white",
            padx=10, pady=6, cursor="hand2")
        self.btn_shoot.pack(fill="x", padx=10)
        self.status = tk.Label(p, text="", bg=C_BG, fg=C_DIM, anchor="w",
                               font=("Microsoft YaHei UI", 8))
        self.status.pack(fill="x", padx=10, pady=(4, 2))
        self.aview = tk.Text(p, height=4, bg=C_BG, fg=C_OK, relief="flat",
                             wrap="word", font=("Microsoft YaHei UI", 10),
                             state="disabled", padx=2, pady=2)
        self.aview.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.pbar = None

    # ---------------- 控件工厂与拖动 ----------------

    def _btn(self, parent, text, cmd) -> tk.Button:
        return tk.Button(parent, text=text, command=cmd, relief="flat", bd=0,
                         font=("Microsoft YaHei UI", 9), bg=C_BG2, fg=C_FG,
                         activebackground=C_LINE, activeforeground="white",
                         padx=10, pady=5, cursor="hand2")

    def _panel_press(self, e) -> None:
        self._drag = {"x": e.x_root, "y": e.y_root,
                      "wx": self.root.winfo_x(), "wy": self.root.winfo_y()}

    def _panel_drag(self, e) -> None:
        if not self._drag:
            return
        self.root.geometry(f"+{self._drag['wx'] + e.x_root - self._drag['x']}"
                           f"+{self._drag['wy'] + e.y_root - self._drag['y']}")

    # ---------------- 状态与进度 ----------------

    def _set_status(self, text: str, color: str = C_DIM) -> None:
        try:
            if self.status is not None and self.status.winfo_exists():
                self.status.configure(text=text, fg=color)
                return
        except tk.TclError:
            pass
        if text:
            self._note_strip(text, color)

    def _note_strip(self, text: str, color: str = C_DIM) -> None:
        """窄条模式：只有在还没有答案时才占用答案区显示提示。"""
        if self._shown.strip():
            return
        try:
            if self.aview is None or not self.aview.winfo_exists():
                return
            self.aview.configure(state="normal")
            self.aview.delete("1.0", "end")
            self.aview.insert("end", text)
            self.aview.tag_add("note", "1.0", "end")
            self.aview.tag_configure("note", foreground=color)
            self.aview.configure(state="disabled")
        except tk.TclError:
            pass

    def _set_progress(self, value: float, color: str | None = None) -> None:
        if self.pbar is not None:
            try:
                if self.pbar.winfo_exists():
                    self.pbar.set(value, color)
            except tk.TclError:
                pass

    def _reset_progress(self) -> None:
        if self.pbar is not None:
            try:
                if self.pbar.winfo_exists():
                    self.pbar.reset()
            except tk.TclError:
                pass

    def _write_answer(self, text: str, append: bool = False) -> None:
        try:
            if self.aview is None or not self.aview.winfo_exists():
                return
            self.aview.configure(state="normal")
            if not append:
                self.aview.delete("1.0", "end")
                self.aview.tag_remove("note", "1.0", "end")
            self.aview.insert("end", text)
            self.aview.see("end")
            self.aview.configure(state="disabled")
        except tk.TclError:
            pass

    def _flush_answer(self) -> None:
        """把累积的增量一次性写入，避免每个 token 都刷一次界面。"""
        if not self._pending:
            return
        text = self._pending
        self._pending = ""
        if not self._shown:
            text = text.lstrip("\n")
            if not text:
                return
        self._shown += text
        self._write_answer(text, append=True)

    def _refresh_buttons(self) -> None:
        busy = self.engine.busy()
        if self.btn_shoot is not None:
            try:
                if self.btn_shoot.winfo_exists():
                    self.btn_shoot.configure(
                        text="⏳  正在答题…" if busy else "📷  截屏答题",
                        state="disabled" if busy else "normal",
                        bg=C_LINE if busy else C_BALL)
            except tk.TclError:
                pass
        stop = getattr(self, "btn_stop", None)
        if stop is not None:
            try:
                if stop.winfo_exists():
                    stop.configure(state="normal" if busy else "disabled")
            except tk.TclError:
                pass

    # ---------------- 核心：截屏作答 ----------------

    def on_capture(self) -> None:
        if self.engine.busy() or self._capturing:
            return
        if not self.key:
            self._set_status("没有配置 API key", C_ERR)
            messagebox.showwarning(
                APP_NAME,
                "没有找到 DeepSeek API key。\n\n"
                "请把 key 填进 config.json 的 \"api_key\" 字段后重启本工具，\n"
                "或设置环境变量 DEEPSEEK_API_KEY。")
            return

        self._capturing = True
        self._answer_buf = []
        self._pending = ""
        self._shown = ""
        self._usage_text = ""
        self._write_answer("")
        self._reset_progress()
        self._set_status(STAGE_CAPTURE)
        self._set_progress(0.03)
        self._refresh_buttons()

        # 先隐藏自己再抓屏，否则悬浮球会挡在题目上；给系统一点重绘时间
        was_visible = self.visible
        self.root.withdraw()
        self.root.after(260, lambda: self._do_capture(was_visible))

    def _do_capture(self, restore_visible: bool) -> None:
        shot = None
        err = None
        try:
            png, w, h = capture.capture_png()      # 全屏
            shot = (png, w, h)
        except Exception as e:  # noqa: BLE001
            err = f"抓屏失败：{type(e).__name__}: {e}"

        if restore_visible:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        self._capturing = False

        if err or shot is None:
            self._set_status(err or "抓屏失败", C_ERR)
            self._set_progress(1.0, C_ERR)
            self._refresh_buttons()
            return

        _, w, h = shot
        self._t_start = time.time()
        self._set_status(f"已抓取 {w}×{h} · {STAGE_ENCODE}")
        self._set_progress(0.05)
        self.engine.start(self.key, shot)

    # ---------------- 队列轮询 ----------------

    def _tick(self) -> None:
        if self._destroyed:
            return
        try:
            while True:
                kind, data = self.engine.q.get_nowait()
                if kind == "stage":
                    self._set_status(str(data))
                elif kind == "progress":
                    self._set_progress(float(data))
                elif kind == "chunk":
                    self._answer_buf.append(str(data))
                    self._pending += str(data)
                elif kind == "usage":
                    self._usage_text = self._fmt_usage(data)
                elif kind == "error":
                    self._pending += ("\n" if self._answer_buf else "") + str(data)
                    self._set_status("出错了", C_ERR)
                    self._set_progress(1.0, C_ERR)
                elif kind == "done":
                    self._finish()
        except queue.Empty:
            pass
        self._flush_answer()
        self._pump_hotkey()
        self.root.after(40, self._tick)

    def _fmt_usage(self, usage) -> str:
        try:
            total = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            return f"{total} tokens"
        except Exception:
            return ""

    def _finish(self) -> None:
        self._flush_answer()
        elapsed = time.time() - self._t_start if self._t_start else 0.0
        answer = "".join(self._answer_buf).strip()
        if answer:
            bits = [f"{STAGE_DONE} {elapsed:.1f}s"]
            if self.engine.shot_info:
                bits.append(self.engine.shot_info)
            if self._usage_text:
                bits.append(self._usage_text)
            self._set_status(" · ".join(bits), C_OK)
            self._set_progress(1.0, C_OK)
            self._save_log(answer)
        else:
            self._set_status("没有返回内容", C_ERR)
            self._set_progress(1.0, C_ERR)
        self._refresh_buttons()
        try:
            self.root.bell()
        except tk.TclError:
            pass

    def _on_stop(self) -> None:
        self.engine.stop()
        self._set_status("已停止", C_WARN)
        self._refresh_buttons()

    def _on_clear(self) -> None:
        self._answer_buf = []
        self._pending = ""
        self._shown = ""
        self._write_answer("")
        self._note_strip("")
        self._reset_progress()
        self._set_status("准备好后点「截屏答题」")

    def _on_copy(self) -> None:
        text = "".join(self._answer_buf).strip()
        if not text:
            self._set_status("还没有答案可复制", C_ERR)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("答案已复制到剪贴板", C_OK)

    def _save_log(self, answer: str) -> None:
        if self.cfg.get("log_answers", True) is False:
            return
        try:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"\n## {stamp}（截屏作答）\n\n**答案**\n\n{answer}\n\n---\n")
        except OSError:
            pass

    # ---------------- 显隐 / 热键 / 菜单 ----------------

    def toggle_visible(self) -> None:
        if self._destroyed:
            return
        if self.visible:
            self.visible = False
            self.root.withdraw()
        else:
            self.visible = True
            self.root.deiconify()
            self.root.attributes("-topmost", True)

    def _bind_global_hotkey(self) -> None:
        spec = str(self.cfg.get("hotkey", "ctrl+alt+u")).lower()
        mods = 0
        if "ctrl" in spec:
            mods |= MOD_CONTROL
        if "alt" in spec:
            mods |= MOD_ALT
        vk = ord(spec[-1].upper())
        self._hotkey_ok = bool(user32.RegisterHotKey(None, self._hotkey_id,
                                                     mods | MOD_NOREPEAT, vk))
        if not self._hotkey_ok:
            print("[warn] 全局热键注册失败（可能被占用），可用悬浮球右键菜单开关。")

    def _pump_hotkey(self) -> None:
        if not self._hotkey_ok:
            return
        msg = wt.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, 1):
            self.toggle_visible()

    def _popup_menu(self, event=None) -> None:
        m = tk.Menu(self.root, tearoff=0, bg=C_BG2, fg=C_FG, activebackground=C_BALL,
                    activeforeground="white", font=("Microsoft YaHei UI", 9), bd=0)
        m.add_command(label="截屏答题", command=self.on_capture)
        m.add_separator()
        m.add_command(label="宽面板", command=lambda: self.show_panel("panel"))
        m.add_command(label="窄条", command=lambda: self.show_panel("strip"))
        m.add_command(label="收起成悬浮球", command=self.show_ball)
        m.add_separator()
        m.add_command(label=f"刷新 API key（当前：{self.key_src}）",
                      command=self._refresh_key)
        m.add_command(label=f"显示/隐藏（{self.cfg.get('hotkey', 'ctrl+alt+u')}）",
                      command=self.toggle_visible)
        m.add_command(label="打开答案记录", command=self._open_log)
        m.add_separator()
        m.add_command(label="退出", command=self._quit)
        pos = (event.x_root, event.y_root) \
            if event is not None and hasattr(event, "x_root") \
            else (self.root.winfo_rootx() + 40, self.root.winfo_rooty() + 40)
        try:
            m.tk_popup(*pos)
        finally:
            m.grab_release()

    def _refresh_key(self) -> None:
        self.cfg["api_key"] = ""
        k, src = resolve_key(self.cfg)
        if k:
            self.key, self.key_src = k, src
            self._set_status(f"已重新读取 key（来源：{src}）", C_OK)
        else:
            self._set_status("仍然找不到 key", C_ERR)

    def _open_log(self) -> None:
        if LOG_PATH.exists():
            os.startfile(LOG_PATH)  # noqa: S606
        else:
            self._set_status("还没有答案记录")

    def _quit(self) -> None:
        self._destroyed = True
        self.engine.stop()
        if self._hotkey_ok:
            user32.UnregisterHotKey(None, self._hotkey_id)
        if self.visible:
            self._remember_ball_pos()
        save_config(self.cfg)
        self.root.destroy()


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def check_environment() -> int:
    if sys.platform != "win32":
        print("本工具依赖 Windows API（抓屏/置顶窗口/全局热键），目前仅支持 Windows。")
        return 1
    if sys.version_info < (3, 9):
        print(f"需要 Python 3.9 或更高版本，当前是 {sys.version.split()[0]}。")
        return 1
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("当前的 Python 缺少 tkinter 图形库，无法显示悬浮球。")
        print("解决办法：到 python.org 重新安装 Python，安装时勾选「tcl/tk and IDLE」。")
        return 1
    return 0


def main() -> int:
    if check_environment() != 0:
        return 1
    capture.ensure_dpi_aware()             # 抓屏前必须声明，否则拿到的是缩放后的图

    cfg = load_config()
    key, _src = resolve_key(cfg)
    if not key:
        save_config(cfg)                   # 先把配置文件生成出来，用户才有东西可填
        return setup_guide()
    if sys.stdout is not None:
        try:
            print(f"{APP_NAME} 已启动。点悬浮球 → 点「截屏答题」。")
            print("按 Ctrl+Alt+U 可以随时隐藏/显示；右键悬浮球有菜单。")
        except Exception:
            pass

    root = tk.Tk()
    TutorBall(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
