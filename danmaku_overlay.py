# -*- coding: utf-8 -*-
"""B站直播弹幕悬浮层：透明、置顶、鼠标穿透，弹幕飘在屏幕顶部。

原理: 以游客身份读取直播间公开展示的弹幕，在本机绘制悬浮层。
    不登录、只读、不发送任何内容。

用法:
    python danmaku_overlay.py [房间号] [--duration 秒]
    或双击 启动弹幕.bat
退出: 屏幕右下角小控制条 -> 退出
"""
import ctypes
import json
import os
import queue
import random
import re
import struct
import sys
import threading
import time
import uuid
import zlib

import brotli
import requests
import websocket
import tkinter as tk
import tkinter.font as tkfont
from tkinter import simpledialog

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "overlay.log")
MAGIC = "#123456"  # 透明色（确保不会出现在弹幕文字颜色里）

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
HDRS = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}

DEFAULT_CONFIG = {
    "room_id": "",           # B站直播间号，留空则启动时询问
    "font_size": 22,         # 弹幕字号
    "speed": 120,            # 飘行速度（像素/秒）
    "top_ratio": 0.32,       # 弹幕区高度占屏幕高度的比例（从顶部开始）
    "max_lanes": 8,          # 最多同时几条跑道
    "density": 0.25,         # 弹幕密度：0~1，只显示这个比例的弹幕
    "min_medal": 0,          # 粉丝牌等级门槛：只显示牌子等级≥N的弹幕（0=不过滤，无牌=0级，0~40）
    "colored": False,        # 是否透传B站会员彩色弹幕（false=全部统一白色）
    "second_label": "网页",  # 控制条上第二路音量的显示名称（如"央视"/"网页"）
    "opacity": 1.0,          # 整体不透明度（0~1，实验性，低于1可能影响透明背景）
    "shadow": True,          # 黑色阴影描边，增强在亮画面上的可读性
    "show_gift": False,      # 显示礼物
    "show_guard": True,      # 显示上舰
    "show_superchat": True,  # 显示醒目留言(SC)
    "show_enter": False,     # 显示进场
    "block_words": [],       # 屏蔽词（包含即不显示）
    "vol_bili": 1.0,         # B站直播音量(0~1)，拖控制条滑块后生效并记忆
    "vol_cctv": 1.0,         # 央视直播音量(0~1)
    "vol_applied": False,    # 是否已用滑块设过音量（false=不主动改播放器音量）
}


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log("config.json 读取失败，使用默认配置: %s" % e)
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def screen_size():
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def ensure_single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "BiliDanmakuOverlayMutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(0, "弹幕悬浮层已经在运行了。", "B站弹幕", 0x40)
        return False
    return True


BLOCK_FILE = os.path.join(APP_DIR, "屏蔽词.txt")

BLOCK_TEMPLATE = """\
# B站弹幕过滤规则（本文件可随时编辑，保存后约30秒内自动生效，无需重启）
# 一行一条规则，三种写法：
#   普通文字   = 弹幕内容包含该词就屏蔽
#   re:表达式  = 按正则匹配弹幕内容，如  re:^抽奖.*
#   uid:数字   = 屏蔽该用户发的所有弹幕
# 本行及以#开头的行都是注释，不生效。下面是示例（去掉行首#启用）：
# 代练
# re:^欢迎.*来到
# uid:123456
"""


def ensure_block_file():
    if not os.path.exists(BLOCK_FILE):
        with open(BLOCK_FILE, "w", encoding="utf-8") as f:
            f.write(BLOCK_TEMPLATE)


def load_block_file():
    """解析 屏蔽词.txt → (关键词集, 用户集, 正则列表, mtime)。"""
    words, uids, regex = set(), set(), []
    mtime = 0.0
    try:
        mtime = os.path.getmtime(BLOCK_FILE)
        with open(BLOCK_FILE, "r", encoding="utf-8-sig") as f:
            for ln in f:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                low = s.lower()
                if low.startswith("re:"):
                    try:
                        regex.append(re.compile(s[3:]))
                    except re.error:
                        log("过滤正则无效，已跳过: %s" % s[3:50])
                elif low.startswith("uid:"):
                    u = s[4:].strip()
                    if u:
                        uids.add(u)
                else:
                    words.add(s)
    except OSError:
        pass
    return words, uids, regex, mtime


def build_block_filters(cfg):
    """合并 config 的 block_words + 屏蔽词.txt，返回 (词集, 用户集, 正则列表)。"""
    words = set(w for w in (cfg.get("block_words") or []) if w)
    fw, fu, frx, _ = load_block_file()
    words.update(fw)
    return words, fu, frx


def parse_medal(info):
    """从 DANMU_MSG.info[3] 提取粉丝牌等级，无牌/异常返回 0。"""
    try:
        m = info[3] if len(info) > 3 else []
        if isinstance(m, (list, tuple)) and len(m) > 2 and m[2]:
            return int(m[2])
    except Exception:
        pass
    return 0


def is_blocked(text, uid, filters):
    words, uids, regex = filters
    if uid and uid in uids:
        return True
    if any(w in text for w in words):
        return True
    return any(c.search(text) for c in regex)


# ---------------- B站弹幕连接 ----------------

def pack(protover, op, body: bytes) -> bytes:
    return struct.pack(">IHHII", 16 + len(body), 16, protover, op, 1) + body


def parse_packets(data: bytes):
    off = 0
    n = len(data)
    while off + 16 <= n:
        total, hlen, ver, op, _seq = struct.unpack_from(">IHHII", data, off)
        if total < hlen or off + total > n:
            break
        yield ver, op, data[off + hlen: off + total]
        off += total


class DanmakuClient:
    """游客身份连接直播间弹幕服务器（官方公开端点，只读）。"""

    def __init__(self, room_id, out_q, cfg):
        self.room_id = int(room_id)
        self.q = out_q
        self.cfg = cfg
        self.running = True
        self.ws = None
        self.hello = False
        self.last_rx = time.monotonic()
        self.buvid3 = str(uuid.uuid4()).upper() + "infoc"

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def _get(self, url):
        cookies = {"buvid3": self.buvid3, "b_nut": str(int(time.time()))}
        return requests.get(url, headers=HDRS, cookies=cookies, timeout=10).json()

    def resolve_room(self):
        j = self._get("https://api.live.bilibili.com/room/v1/Room/room_init?id=%d" % self.room_id)
        d = (j or {}).get("data") or {}
        if d.get("room_id"):
            if d.get("live_status") == 0:
                self.q.put(("note", "房间未开播（可能是轮播）"))
            return int(d["room_id"])
        return self.room_id

    def get_conf(self, room_id):
        j = self._get("https://api.live.bilibili.com/room/v1/Danmu/getConf?room_id=%d&platform=web&player=web" % room_id)
        if (j or {}).get("code") != 0:
            raise RuntimeError("getConf: %s" % (j or {}).get("message", j))
        d = j["data"]
        return d.get("token", ""), d.get("server_list") or []

    def _loop(self):
        while self.running:
            try:
                real = self.resolve_room()
                token, servers = self.get_conf(real)
                urls = ["wss://%s:2245/sub" % s["host"] for s in servers[:3]]
                urls.append("wss://broadcastlv.chat.bilibili.com:443/sub")
                joined = False
                for url in urls:
                    if not self.running:
                        return
                    log("连接 %s（房间 %d）" % (url, real))
                    self.hello = False
                    self.ws = websocket.WebSocketApp(
                        url,
                        header=["User-Agent: " + UA, "Origin: https://live.bilibili.com"],
                        on_open=lambda ws: self.on_open(ws, real, token),
                        on_message=self.on_message,
                        on_error=lambda ws, e: log("WS错误: %s" % e),
                        on_close=lambda ws, c, m: log("连接关闭(%s)" % c),
                    )
                    self.ws.run_forever()
                    if self.hello:
                        joined = True
                        break
                    log("该服务器未能建立弹幕会话，换下一个")
                if not joined and self.running:
                    log("所有弹幕服务器都失败，5秒后重试")
                    time.sleep(5)
            except Exception as e:
                log("弹幕连接出错: %s" % e)
                if self.running:
                    time.sleep(5)

    def on_open(self, ws, room_id, token):
        auth = json.dumps({
            "uid": 0, "roomid": room_id, "protover": 3, "platform": "web",
            "type": 2, "key": token, "clientver": "2.4.2",
        }).encode("utf-8")
        ws.send(pack(1, 7, auth))
        threading.Thread(target=self._heartbeat, args=(ws,), daemon=True).start()
        threading.Thread(target=self._watchdog, args=(ws,), daemon=True).start()

    def _watchdog(self, ws):
        # 心跳每15秒一次必有回复；长时间无任何数据视为死连接
        while self.running:
            time.sleep(10)
            if time.monotonic() - self.last_rx > 45:
                log("超过45秒未收到任何数据，强制重连")
                try:
                    ws.close()
                except Exception:
                    pass
                return

    def _heartbeat(self, ws):
        # comet 服务器对心跳间隔要求苛刻（实测30秒会话会静默失效），固定15秒
        time.sleep(1)
        while self.running:
            try:
                ws.send(pack(1, 2, b"[object Object]"))
            except Exception:
                return
            time.sleep(15)

    def on_message(self, ws, message):
        self.last_rx = time.monotonic()
        if not isinstance(message, bytes):
            return
        try:
            for ver, op, body in parse_packets(message):
                self.handle(op, ver, body)
        except Exception as e:
            log("消息解析异常: %s" % e)

    def handle(self, op, ver, body):
        if op == 8 and not self.hello:
            self.hello = True
            log("已连接弹幕，开始接收")
        elif op == 5:
            if ver == 3:
                body = brotli.decompress(body)
            elif ver == 2:
                body = zlib.decompress(body)
            if ver in (2, 3):
                for v2, op2, b2 in parse_packets(body):
                    self.handle(op2, v2, b2)
            elif ver == 0:
                self.dispatch(json.loads(body.decode("utf-8")))

    def dispatch(self, msg):
        try:
            cmd = msg.get("cmd", "")
            if cmd.startswith("DANMU_MSG"):
                info = msg.get("info", [])
                if len(info) >= 2 and info[1]:
                    text = str(info[1]).strip()
                    if not text:
                        return
                    color = 0xFFFFFF
                    try:
                        if len(info[3]) > 3 and info[3][3]:
                            color = int(info[3][3]) & 0xFFFFFF
                    except Exception:
                        pass
                    uid = ""
                    try:
                        if len(info) >= 3 and isinstance(info[2], list) and info[2]:
                            uid = str(info[2][0])
                    except Exception:
                        pass
                    medal = parse_medal(info)
                    self.q.put(("dm", text, "#%06x" % color, uid, medal))
            elif cmd == "SUPER_CHAT_MESSAGE" and self.cfg.get("show_superchat"):
                d = msg.get("data", {})
                uname = (d.get("user_info") or {}).get("uname", "")
                self.q.put(("dm", "[SC %s元] %s: %s" % (d.get("price", "?"), uname, d.get("message", "")), "#FFD700"))
            elif cmd == "GUARD_BUY" and self.cfg.get("show_guard"):
                d = msg.get("data", {})
                self.q.put(("dm", "欢迎 %s 开通%s" % (d.get("username", ""), d.get("gift_name", "")), "#FF9DB8"))
            elif cmd == "SEND_GIFT" and self.cfg.get("show_gift"):
                d = msg.get("data", {})
                self.q.put(("dm", "%s 送出 %s x%d" % (d.get("uname", ""), d.get("gift_name", d.get("giftName", "")), d.get("num", 1)), "#7FDFFF"))
            elif cmd == "INTERACT_WORD" and self.cfg.get("show_enter"):
                d = msg.get("data", {})
                self.q.put(("dm", "%s 进入直播间" % d.get("uname", ""), "#B0B0B0"))
        except Exception as e:
            log("dispatch异常: %s" % e)


# ---------------- 音量控制 ----------------

class VolumeControl:
    """基于 Windows 音频接口(pycaw)的进程级音量，不依赖任何浏览器插件或端口。

    B站 = 专用 Edge 实例(BiliLiveEdge 配置)的进程；央视 = 你日常 Edge 的进程。
    两者天然独立，边看边调不影响全屏、不抢焦点。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.vol = {"bili": float(cfg.get("vol_bili", 1.0)),
                    "cctv": float(cfg.get("vol_cctv", 1.0))}
        self.applied = bool(cfg.get("vol_applied", False))
        self.linked = {"bili": False, "cctv": False}
        self._pids = {"bili": set(), "cctv": set()}
        self._q = queue.Queue()
        threading.Thread(target=self._poll, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

    def _poll(self):
        while True:
            try:
                import psutil
                bili, main = set(), set()
                for p in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        info = p.info
                        if info["name"] and info["name"].lower() == "msedge.exe":
                            cl = " ".join(info["cmdline"] or [])
                            if "BiliLiveEdge" in cl:
                                bili.add(info["pid"])
                            else:
                                main.add(info["pid"])
                    except Exception:
                        pass
                self._pids["bili"], self._pids["cctv"] = bili, main
                self.linked["bili"] = bool(bili)
                self.linked["cctv"] = bool(main)
                if self.applied:  # 音频会话可能在页面静音解除后才出现，定期重申
                    self._apply("bili")
                    self._apply("cctv")
            except Exception:
                pass
            time.sleep(10)

    def _worker(self):
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        while True:
            key, v = self._q.get()
            self._apply_now(key, v)

    def set(self, key, v):
        v = max(0.0, min(1.0, float(v)))
        self.vol[key] = v
        self.applied = True
        self.cfg["vol_bili" if key == "bili" else "vol_cctv"] = round(v, 3)
        self.cfg["vol_applied"] = True
        save_config(self.cfg)
        self._q.put((key, v))

    def _apply(self, key):
        self._q.put((key, self.vol[key]))

    def _apply_now(self, key, v):
        try:
            from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        except ImportError:
            return
        pids = self._pids.get(key) or set()
        for s in AudioUtilities.GetAllSessions():
            try:
                if s.ProcessId in pids:
                    s._ctl.QueryInterface(ISimpleAudioVolume).SetMasterVolume(float(v), None)
            except Exception:
                pass


# ---------------- 悬浮层 UI ----------------

THEME = {
    "bg": "#15161b", "surf": "#23252d", "hover": "#2f313b",
    "fg": "#e8eaf0", "mut": "#8b8f9c", "acc": "#7aa2f7",
    "green": "#9ece6a", "red": "#f7768e", "track": "#2c2e37",
}


class ModernSlider(tk.Canvas):
    """轻量现代滑条：canvas 绘制，仅交互/尺寸变化时重绘；可随容器拉伸。"""

    def __init__(self, master, value=0.0, width=90, command=None, color=None):
        super().__init__(master, width=width, height=14, bg=THEME["bg"],
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.color = color or THEME["acc"]
        self.v = max(0.0, min(1.0, float(value)))
        self._sw = width  # 不能叫 _w：会覆盖 tkinter 内部的 widget 路径
        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<Configure>", self._on_conf)
        self._draw()

    def _on_conf(self, e):
        if e.width != self._sw:
            self._sw = max(30, e.width)
            self._draw()

    def _draw(self):
        self.delete("all")
        w = self._sw
        y = 7
        self.create_oval(2, y - 2, 6, y + 2, fill=THEME["track"], width=0)
        self.create_oval(w - 6, y - 2, w - 2, y + 2, fill=THEME["track"], width=0)
        self.create_rectangle(4, y - 2, w - 4, y + 2, fill=THEME["track"], width=0)
        x = 4 + (w - 8) * self.v
        if x > 5:
            self.create_rectangle(4, y - 2, x, y + 2, fill=self.color, width=0)
        self.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#eef0f6", width=0)

    def _pos(self, e):
        self.v = max(0.0, min(1.0, (e.x - 4) / float(self._sw - 8)))
        self._draw()
        if self.command:
            self.command(self.v)

    def _on_click(self, e):
        self._pos(e)

    def _on_drag(self, e):
        self._pos(e)

    def set(self, v):
        self.v = max(0.0, min(1.0, float(v)))
        self._draw()


def _chip(master, text, cmd, theme, font, padx=10):
    """扁平按钮：hover 变色；theme dict 可动态改bg（暂停态）。"""
    b = tk.Label(master, text=text, fg=theme["fg"], bg=theme["bg"], font=font,
                 padx=padx, pady=3, cursor="hand2")
    b.bind("<Enter>", lambda e: b.config(bg=theme["hover"]))
    b.bind("<Leave>", lambda e: b.config(bg=theme["bg"]))
    if cmd:
        b.bind("<Button-1>", lambda e: cmd())
    return b


def _repeat_chip(master, text, cmd, theme, font):
    """±微调按钮：长按连续触发。"""
    b = tk.Label(master, text=text, fg=theme["fg"], bg=theme["bg"], font=font,
                 width=2, pady=3, cursor="hand2")
    job = {}

    def fire():
        cmd()
        job["id"] = b.after(110, fire)

    def start(e):
        b.config(bg=theme["hover"])
        cmd()
        job["id"] = b.after(420, fire)

    def stop(e):
        b.config(bg=theme["bg"])
        if "id" in job:
            b.after_cancel(job.pop("id"))

    b.bind("<ButtonPress-1>", start)
    b.bind("<ButtonRelease-1>", stop)
    return b


def _dwm_round(widget):
    """Win11 圆角 + 深色模式（由桌面合成器完成，零资源开销）。"""
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(ctypes.c_int(33)), 4)
    except Exception:
        pass


class Overlay:
    def __init__(self, cfg, room_id, duration=None):
        self.cfg = cfg
        self.room_id = str(room_id)
        self.duration = duration
        self.q = queue.Queue()
        self.paused = False
        self.dm_count = 0
        self.items = []
        self.n = 0
        self.scan = 0
        self._last_log = time.monotonic()

        self.sw, self.sh = screen_size()
        line_h = int(cfg["font_size"] * 1.8)
        self.line_h = line_h
        h = max(line_h + 16, int(self.sh * float(cfg["top_ratio"])))
        self.h = h
        self.nlane = max(1, min(int(cfg["max_lanes"]), (h - 16) // line_h))

        self.root = tk.Tk()
        self.root.title("bili-danmaku-overlay")
        self.root.geometry("%dx%d+0+0" % (self.sw, h))
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", MAGIC)
        op = float(cfg.get("opacity", 1.0))
        if 0 < op < 1:
            self.root.attributes("-alpha", op)
        self.canvas = tk.Canvas(self.root, bg=MAGIC, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.font = tkfont.Font(family="Microsoft YaHei", size=int(cfg["font_size"]), weight="bold")
        self._lane_factor = [random.uniform(0.85, 1.15) for _ in range(self.nlane)]
        self.lane_v = [float(cfg["speed"]) * f for f in self._lane_factor]
        self.lane_last = [None] * self.nlane

        self.root.deiconify()
        self.root.update_idletasks()
        self.root.after(150, self.make_clickthrough)

        self.volctl = VolumeControl(cfg)
        ensure_block_file()
        self.block_filters = build_block_filters(cfg)
        _, _, _, self._block_mtime = load_block_file()
        w, u, r = self.block_filters
        if w or u or r:
            log("过滤规则已加载：关键词%d 正则%d 用户%d（编辑 屏蔽词.txt 约30秒内热生效）" % (
                len(w), len(r), len(u)))
        self.build_bar()
        self.root.after(30000, self.watch_block_file)
        # QA测试钩子（平时不设置环境变量，无任何效果）
        for _ev, _fn in (("DMO_TEST_PAUSE", self.toggle_pause),
                         ("DMO_TEST_COLLAPSE", self.collapse)):
            if os.environ.get(_ev):
                try:
                    self.root.after(int(float(os.environ[_ev])), _fn)
                except Exception:
                    pass
        if os.environ.get("DMO_TEST_DUMP"):
            def _dump():
                try:
                    data = {"bar": {"w": self.bar.winfo_width(), "h": self.bar.winfo_height()}}

                    def walk(w, out):
                        for c in w.winfo_children():
                            try:
                                out[str(c)] = {"x": c.winfo_x(), "y": c.winfo_y(),
                                               "w": c.winfo_width(), "h": c.winfo_height(),
                                               "cls": c.winfo_name(),
                                               "parent": w.winfo_name()}
                            except Exception:
                                pass
                            walk(c, out)

                    walk(self.bar, data)
                    with open(os.environ["DMO_TEST_DUMP"], "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=1)
                except Exception:
                    pass
            self.root.after(1500, _dump)
        self.last_t = time.monotonic()
        self.root.after(30, self.tick)
        self.root.after(60, self.pump)

        self.client = DanmakuClient(self.room_id, self.q, cfg)
        self.client.start()
        if duration:
            self.root.after(int(duration * 1000), self.quit)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.quit()

    def watch_block_file(self):
        """屏蔽词.txt 热重载：文件变化后自动重建过滤规则。"""
        try:
            m = os.path.getmtime(BLOCK_FILE)
            if m != self._block_mtime:
                self._block_mtime = m
                self.block_filters = build_block_filters(self.cfg)
                w, u, r = self.block_filters
                log("过滤规则已重载：关键词%d 正则%d 用户%d" % (len(w), len(r), len(u)))
        except OSError:
            pass
        self.root.after(30000, self.watch_block_file)

    # 鼠标穿透 + 不进 Alt+Tab
    def make_clickthrough(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x80000, 0x20, 0x80
            st = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, st | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)
            log("悬浮层已置顶并鼠标穿透")
        except Exception as e:
            log("设置穿透失败(300ms后重试): %s" % e)
            self.root.after(300, self.make_clickthrough)

    def build_bar(self):
        T = THEME
        bar = tk.Toplevel(self.root)
        bar.overrideredirect(True)
        bar.attributes("-topmost", True)
        bar.attributes("-alpha", 0.96)
        bar.configure(bg=T["bg"])
        bar.geometry("+%d+%d" % (self.sw - 492, self.sh - getattr(self, "bar_h", 132) - 14))
        self.bar = bar
        ui_font = ("Microsoft YaHei UI", 9)
        sm_font = ("Microsoft YaHei UI", 8)

        f1 = tk.Frame(bar, bg=T["bg"])
        f1.pack(fill="x", padx=12, pady=(9, 3))
        self.bar_lbl = tk.Label(f1, text="B站 %s" % self.room_id,
                                fg=T["fg"], bg=T["bg"], font=(*ui_font, "bold"))
        self.bar_lbl.pack(side="left")
        self.bar_cnt = tk.Label(f1, text="连接中…", fg=T["mut"], bg=T["bg"], font=sm_font)
        self.bar_cnt.pack(side="left", padx=(10, 0))
        self._pause_theme = {"bg": T["surf"], "hover": "#8fb0f9", "fg": T["fg"]}
        self.btn_pause = _chip(f1, "暂停", self.toggle_pause, self._pause_theme, sm_font)
        self.btn_pause.pack(side="right", padx=(6, 0))
        _chip(f1, "退出", self.quit, {"bg": T["surf"], "hover": T["hover"], "fg": T["fg"]},
              sm_font).pack(side="right")
        _chip(f1, "◀", self.collapse, {"bg": T["bg"], "hover": T["hover"], "fg": T["mut"]},
              sm_font, padx=5).pack(side="right")
        self.vol_status = tk.Label(f1, text="● 未连接", fg=T["red"], bg=T["bg"], font=sm_font)
        self.vol_status.pack(side="right", padx=(0, 10))

        tk.Frame(bar, height=1, bg=T["track"]).pack(fill="x", padx=12)

        body = tk.Frame(bar, bg=T["bg"])
        body.pack(fill="both", expand=True, padx=12, pady=(3, 9))
        body.columnconfigure(0, weight=1, uniform="a")
        body.columnconfigure(1, weight=1, uniform="a")

        def cluster(name, minus, plus, ptext, slider_factory):
            """一组控件：名称 − 数值 + [拉伸滑条]，pack 布局自左向右填满。"""
            box = tk.Frame(body, bg=T["bg"])
            tk.Label(box, text=name, fg=T["fg"], bg=T["bg"], font=sm_font).pack(side="left")
            th = {"bg": T["surf"], "hover": T["hover"], "fg": T["fg"]}
            _repeat_chip(box, "−", minus, th, sm_font).pack(side="left", pady=2)
            p = tk.Label(box, text=ptext, fg=T["fg"], bg=T["bg"],
                         font=("Segoe UI", 8), anchor="e", width=4)
            p.pack(side="left")
            _repeat_chip(box, "+", plus, th, sm_font).pack(side="left", pady=2)
            sl = slider_factory(box)
            sl.pack(side="left", fill="x", expand=True, padx=(3, 0), pady=1)
            return box, p, sl

        self.vol_pcts = {}
        self.vol_sliders = {}
        row_specs = (
            (("B站", lambda: self.step_vol("bili", -2), lambda: self.step_vol("bili", 2),
              "%d%%" % int(self.volctl.vol["bili"] * 100),
              lambda b: ModernSlider(b, value=self.volctl.vol["bili"],
                                     command=lambda v: self.on_vol("bili", v))),
             (self.cfg.get("second_label", "网页"),
              lambda: self.step_vol("cctv", -2), lambda: self.step_vol("cctv", 2),
              "%d%%" % int(self.volctl.vol["cctv"] * 100),
              lambda b: ModernSlider(b, value=self.volctl.vol["cctv"],
                                     command=lambda v: self.on_vol("cctv", v)))),
            (("密度", lambda: self.step_density(-5), lambda: self.step_density(5),
              "%d%%" % int(float(self.cfg.get("density", 0.25)) * 100),
              lambda b: ModernSlider(b, value=float(self.cfg.get("density", 0.25)),
                                     command=self.on_density)),
             ("速度", lambda: self.step_speed(-10), lambda: self.step_speed(10),
              str(int(self.cfg.get("speed", 120))),
              lambda b: ModernSlider(b, value=(float(self.cfg.get("speed", 120)) - 40) / 360.0,
                                     command=self.on_speed))),
            (("字号", lambda: self.step_fontsize(-2), lambda: self.step_fontsize(2),
              str(int(self.cfg.get("font_size", 22))),
              lambda b: ModernSlider(b, value=(float(self.cfg.get("font_size", 22)) - 12) / 32.0,
                                     command=self.on_fontsize)),
             ("区域", lambda: self.step_area(-5), lambda: self.step_area(5),
              "%d%%" % int(float(self.cfg.get("top_ratio", 0.32)) * 100),
              lambda b: ModernSlider(b, value=float(self.cfg.get("top_ratio", 0.32)),
                                     command=self.on_area))),
        )
        made = [[None, None] for _ in row_specs]
        for r, pair in enumerate(row_specs):
            for c, spec in enumerate(pair):
                made[r][c] = cluster(*spec)
                made[r][c][0].grid(row=r, column=c, sticky="ew", pady=1)
        (self.vol_pcts["bili"], self.vol_sliders["bili"]) = made[0][0][1], made[0][0][2]
        (self.vol_pcts["cctv"], self.vol_sliders["cctv"]) = made[0][1][1], made[0][1][2]
        self.den_pct, self.den_slider = made[1][0][1], made[1][0][2]
        self.spd_pct, self.spd_slider = made[1][1][1], made[1][1][2]
        self.fsz_pct, self.fsz_slider = made[2][0][1], made[2][0][2]
        self.area_pct, self.area_slider = made[2][1][1], made[2][1][2]

        mm = int(self.cfg.get("min_medal", 0) or 0)
        mmbox, self.mm_pct, self.mm_slider = cluster(
            "粉丝牌", lambda: self.step_minmedal(-1), lambda: self.step_minmedal(1),
            "关" if mm <= 0 else "≥%d" % mm,
            lambda b: ModernSlider(b, value=min(mm, 40) / 40.0,
                                   command=self.on_minmedal))
        mmbox.grid(row=3, column=0, columnspan=2, sticky="ew", pady=1)

        for w in (bar, f1, self.bar_lbl, self.bar_cnt, self.vol_status, body):
            w.bind("<Button-1>", self.drag_start)
            w.bind("<B1-Motion>", self.drag_move)
        self._dx = self._dy = 0
        # 高度按实际内容自适应，避免固定值截断最后一行
        bar.update_idletasks()
        self.bar_h = max(bar.winfo_reqheight(), 120)
        bar.geometry("478x%d+%d+%d" % (self.bar_h, self.sw - 492, self.sh - self.bar_h - 14))
        self.root.after(120, lambda: _dwm_round(bar))
        self.root.after(2000, self.update_bar)

    def on_vol(self, key, v):
        v = max(0.0, min(1.0, float(v)))
        self.vol_pcts[key].config(text="%d%%" % int(round(v * 100)))
        self.volctl.set(key, v)

    def step_vol(self, key, delta):
        v = int(round(self.volctl.vol[key] * 100)) + delta
        v = max(0, min(100, v))
        self.vol_sliders[key].set(v / 100.0)
        self.on_vol(key, v / 100.0)

    def on_density(self, v):
        d = max(0.05, min(1.0, round(v * 20) / 20.0))  # 5%步进
        self.cfg["density"] = d
        save_config(self.cfg)
        self.den_pct.config(text="%d%%" % int(round(d * 100)))

    def step_density(self, delta):
        v = int(round(float(self.cfg.get("density", 0.25)) * 100)) + delta
        v = max(5, min(100, v))
        self.den_slider.set(v / 100.0)
        self.on_density(v / 100.0)

    def on_speed(self, v):
        sp = 40 + int(round(max(0.0, min(1.0, v)) * 36)) * 10  # 10步进
        self.cfg["speed"] = sp
        save_config(self.cfg)
        self.lane_v = [sp * f for f in self._lane_factor]  # 现有弹幕也会实时变速
        self.spd_pct.config(text=str(sp))

    def step_speed(self, delta):
        v = int(float(self.cfg.get("speed", 120))) + delta
        v = max(40, min(400, v))
        self.spd_slider.set((v - 40) / 360.0)
        self.on_speed((v - 40) / 360.0)

    def on_fontsize(self, v):
        n = 12 + int(round(max(0.0, min(1.0, v)) * 16)) * 2  # 12~44 步进2
        if n != int(self.cfg.get("font_size", 22)):
            self.cfg["font_size"] = n
            save_config(self.cfg)
            self.font.configure(size=n)
            self._rebuild_lanes()
        self.fsz_pct.config(text=str(n))

    def step_fontsize(self, delta):
        n = max(12, min(44, int(self.cfg.get("font_size", 22)) + delta))
        self.fsz_slider.set((n - 12) / 32.0)
        self.on_fontsize((n - 12) / 32.0)

    def on_area(self, v):
        r = max(0.10, min(0.90, round(v * 20) / 20.0))  # 10%~90% 步进5%
        if abs(r - float(self.cfg.get("top_ratio", 0.32))) > 1e-9:
            self.cfg["top_ratio"] = r
            save_config(self.cfg)
            self._rebuild_lanes()
        self.area_pct.config(text="%d%%" % int(round(r * 100)))

    def step_area(self, delta):
        cur = int(round(float(self.cfg.get("top_ratio", 0.32)) * 100))
        n = max(10, min(90, cur + delta))
        self.area_slider.set(n / 100.0)
        self.on_area(n / 100.0)

    def on_minmedal(self, v):
        n = int(round(max(0.0, min(1.0, v)) * 40))  # 0~40 级
        if n != int(self.cfg.get("min_medal", 0) or 0):
            self.cfg["min_medal"] = n
            save_config(self.cfg)
        self.mm_pct.config(text="关" if n <= 0 else "≥%d" % n)

    def step_minmedal(self, delta):
        n = max(0, min(40, int(self.cfg.get("min_medal", 0) or 0) + delta))
        self.mm_slider.set(n / 40.0)
        self.on_minmedal(n / 40.0)

    def _rebuild_lanes(self):
        """字号/区域变化后：清屏并按新行高与区高重建跑道（避免新旧行高错位）。"""
        for it in self.items:
            self.canvas.delete(it["tag"])
        self.items = []
        self.line_h = int(self.cfg["font_size"] * 1.8)
        h = max(self.line_h + 16, int(self.sh * float(self.cfg["top_ratio"])))
        if h != self.h:
            self.h = h
            self.root.geometry("%dx%d+0+0" % (self.sw, h))
        self.nlane = max(1, min(int(self.cfg["max_lanes"]), (h - 16) // self.line_h))
        sp = float(self.cfg.get("speed", 120))
        self._lane_factor = [random.uniform(0.85, 1.15) for _ in range(self.nlane)]
        self.lane_v = [sp * f for f in self._lane_factor]
        self.lane_last = [None] * self.nlane
        self.scan = 0

    def drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def drag_move(self, e):
        self.bar.geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self._pause_theme["bg"] = THEME["acc"]
            self._pause_theme["fg"] = "#15161b"
            self._pause_theme["hover"] = "#8fb0f9"
            self.btn_pause.config(text="继续", bg=THEME["acc"], fg="#15161b")
            # 清屏：去掉屏幕上所有弹幕（暂停期间新弹幕也被丢弃）
            for it in self.items:
                self.canvas.delete(it["tag"])
            self.items = []
            self.lane_last = [None] * self.nlane
            self.scan = 0
        else:
            self._pause_theme["bg"] = THEME["surf"]
            self._pause_theme["fg"] = THEME["fg"]
            self._pause_theme["hover"] = THEME["hover"]
            self.btn_pause.config(text="暂停", bg=THEME["surf"], fg=THEME["fg"])

    def collapse(self):
        """把控制条缩成右缘一个小箭头（弹幕照常），点箭头展开。"""
        try:
            self.bar.withdraw()
        except Exception:
            return
        self.handle = tk.Toplevel(self.root)
        self.handle.overrideredirect(True)
        self.handle.attributes("-topmost", True)
        self.handle.attributes("-alpha", 0.95)
        self.handle.configure(bg=THEME["surf"],
                              highlightthickness=1, highlightbackground=THEME["track"])
        h = getattr(self, "bar_h", 132)
        self.handle.geometry("18x%d+%d+%d" % (h, self.sw - 18, self.sh - h - 14))
        lbl = tk.Label(self.handle, text="▶", fg="#c9cdd6", bg=THEME["surf"],
                       font=("Segoe UI", 10), cursor="hand2")
        lbl.pack(expand=True, fill="both")
        lbl.bind("<Enter>", lambda e: (lbl.config(fg=THEME["acc"]), self.handle.config(bg=THEME["hover"])))
        lbl.bind("<Leave>", lambda e: (lbl.config(fg="#c9cdd6"), self.handle.config(bg=THEME["surf"])))
        for w in (self.handle, lbl):
            w.bind("<Button-1>", lambda e: self.expand())
        self.root.after(120, lambda: _dwm_round(self.handle))

    def expand(self):
        try:
            if getattr(self, "handle", None):
                self.handle.destroy()
        except Exception:
            pass
        self.handle = None
        try:
            self.bar.deiconify()
        except Exception:
            pass

    # --- 消息入列 ---
    def pump(self):
        try:
            for _ in range(12):
                kind, *rest = self.q.get_nowait()
                if kind == "dm":
                    text, color, uid, medal = rest
                    if not self.paused:
                        self.spawn(text, color, uid, medal)
        except queue.Empty:
            pass
        except Exception as e:
            log("pump异常: %s" % e)
        self.root.after(30, self.pump)

    def spawn(self, text, color, uid="", medal=0):
        mm = int(self.cfg.get("min_medal", 0) or 0)
        if mm > 0 and medal < mm:
            return
        if is_blocked(text, uid, self.block_filters):
            return
        density = float(self.cfg.get("density", 1.0))
        if density < 1.0 and random.random() > density:
            return
        if not self.cfg.get("colored", False):
            color = "#FFFFFF"  # 默认不透传B站彩色弹幕，统一白色
        if len(text) > 45:
            text = text[:45] + "…"
        w = self.font.measure(text)
        # 找一条"上一条已完全进场"的跑道
        for k in range(self.nlane):
            i = (self.scan + k) % self.nlane
            last = self.lane_last[i]
            if last is None or last["x"] + last["w"] <= self.sw:
                tag = "d%d" % self.n
                self.n += 1
                x = self.sw
                y = 10 + i * self.line_h + self.line_h // 2
                if self.cfg.get("shadow", True):
                    self.canvas.create_text(x + 2, y + 2, text=text, fill="#000000",
                                            font=self.font, anchor="w", tags=tag)
                self.canvas.create_text(x, y, text=text, fill=color,
                                        font=self.font, anchor="w", tags=tag)
                item = {"tag": tag, "x": x, "w": w, "lane": i}
                self.items.append(item)
                self.lane_last[i] = item
                self.scan = (i + 1) % self.nlane
                self.dm_count += 1
                return
        # 没有空跑道就丢弃这条（同B站策略）

    # --- 动画 ---
    def tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self.last_t)
        self.last_t = now
        if not self.paused:
            alive = []
            for it in self.items:
                dx = -self.lane_v[it["lane"]] * dt  # 从右往左飘
                it["x"] += dx
                self.canvas.move(it["tag"], dx, 0)
                if it["x"] + it["w"] > -60:
                    alive.append(it)
                else:
                    self.canvas.delete(it["tag"])
                    if self.lane_last[it["lane"]] is it:
                        self.lane_last[it["lane"]] = None
            self.items = alive
        if now - self._last_log > 20:
            self._last_log = now
            log("运行中: 已显示 %d 条弹幕" % self.dm_count)
        self.root.after(30, self.tick)

    def update_bar(self):
        if self.client.hello:
            cnt = "弹幕 %d" % self.dm_count
        else:
            cnt = "连接中…"
        if self.paused:
            cnt += " · 已清屏"
        self.bar_cnt.config(text=cnt)
        if self.volctl.linked.get("bili") or self.volctl.linked.get("cctv"):
            self.vol_status.config(text="● 已连接", fg=THEME["green"])
        else:
            self.vol_status.config(text="● 未连接", fg=THEME["red"])
        self.root.after(2000, self.update_bar)

    def quit(self):
        log("退出，共显示 %d 条弹幕" % self.dm_count)
        try:
            self.client.stop()
        finally:
            try:
                self.root.destroy()
            except Exception:
                pass


def main():
    enable_dpi_awareness()
    if not ensure_single_instance():
        return
    cfg = load_config()
    args = sys.argv[1:]
    duration = None
    room = None
    i = 0
    while i < len(args):
        if args[i] == "--duration" and i + 1 < len(args):
            duration = float(args[i + 1])
            i += 2
        else:
            room = args[i]
            i += 1
    if not room:
        room = cfg.get("room_id")
    if not room:
        tmp = tk.Tk()
        tmp.withdraw()
        room = simpledialog.askstring(
            "B站弹幕悬浮层",
            "请输入B站直播间号：\n（直播间网址 live.bilibili.com/ 后面的数字）",
            parent=tmp)
        tmp.destroy()
        if not room:
            return
        room = str(room).strip()
        cfg["room_id"] = room
        save_config(cfg)
    log("启动: 房间 %s, 配置 %s" % (room, {k: cfg[k] for k in ("font_size", "speed", "top_ratio", "max_lanes")}))
    Overlay(cfg, room, duration)


if __name__ == "__main__":
    main()
