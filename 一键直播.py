# -*- coding: utf-8 -*-
"""一键直播：输入B站房间号和网页直播地址，自动完成 画中画左下角 + 音量 + 弹幕。

用法:
    双击桌面"一键直播"快捷方式（弹两个输入框）
    python 一键直播.py [--test 房间号 央视网址]   # 测试模式：跳过输入框并自动清理
"""
import ctypes
import ctypes.wintypes
import json
import os
import re
import subprocess
import sys
import time

import threading

import requests
import websocket
import tkinter as tk

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
LOG_PATH = os.path.join(BASE, "overlay.log")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.exists(EDGE):
    _edge2 = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    if os.path.exists(_edge2):
        EDGE = _edge2
PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "BiliLiveEdge")
PORT = 9223
PIP_W, PIP_H = 426, 240

# 遍历(含同源iframe)取最大video的表达式主体
JS_FIND_VIDEO = ("(function d(w){var best=null,bw=0;try{"
                 "var vs=w.document.querySelectorAll('video');"
                 "for(var i=0;i<vs.length;i++){var r=vs[i].getBoundingClientRect();"
                 "if(r.width>bw){bw=r.width;best=vs[i];}}"
                 "for(var j=0;j<w.frames.length;j++){var b=d(w.frames[j]);"
                 "if(b&&!best){best=b;}else if(b){var r1=best.getBoundingClientRect();"
                 "if(b.getBoundingClientRect().width>r1.width)best=b;}}"
                 "}catch(e){}return best;})(window)")

user32 = ctypes.windll.user32


def log(msg):
    line = "[%s] [启动器] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_cfg():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    return cfg


def save_cfg(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def alert(msg):
    ctypes.windll.user32.MessageBoxW(0, msg, "一键直播", 0x40)


# ---------------- 浏览器页面控制（本地控制通道） ----------------

def cdp_pages():
    data = requests.get("http://127.0.0.1:%d/json" % PORT, timeout=2).json()
    return [t for t in data if t.get("type") == "page"]


def find_tab(pat):
    for t in cdp_pages():
        if pat in (t.get("url") or ""):
            return t
    return None


def cdp_eval(ws_url, expr, await_promise=True):
    ws = websocket.create_connection(ws_url, timeout=6, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expr, "awaitPromise": await_promise,
            "returnByValue": True, "userGesture": True}}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == 1:
                res = r.get("result", {}).get("result", {})
                return res.get("value")
    finally:
        ws.close()


def wait_for(fn, timeout, desc):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(1.5)
    log("等待超时: %s" % desc)
    return None


def apply_volume(ws_url, v):
    expr = ("(function(){var n=0;(function d(w){try{"
            "var vs=w.document.querySelectorAll('video');"
            "for(var i=0;i<vs.length;i++){vs[i].volume=%f;vs[i].muted=false;n++;}"
            "for(var j=0;j<w.frames.length;j++)d(w.frames[j]);"
            "}catch(e){}})(window);return n;})()" % v)
    cdp_eval(ws_url, expr, await_promise=False)


def video_ready(ws_url):
    # 就绪 = video存在且媒体元数据已加载(readyState>=1)，否则画中画会抛InvalidStateError
    expr = ("(function(){var best=%s;if(!best)return false;"
            "return best.readyState>=1;})()" % JS_FIND_VIDEO)
    return cdp_eval(ws_url, expr, await_promise=False)


def do_fullscreen(ws_url):
    expr = ("(function(){var best=%s;if(!best)return 'novideo';"
            "return best.requestFullscreen().then(function(){return 'ok'},"
            "function(e){return 'err:'+e.name});})()" % JS_FIND_VIDEO)
    return cdp_eval(ws_url, expr)


def do_pip(ws_url):
    expr = ("(function(){var best=%s;if(!best)return 'novideo';"
            "best.muted=false;"
            "return best.requestPictureInPicture().then(function(){return 'ok'},"
            "function(e){return 'err:'+e.name});})()" % JS_FIND_VIDEO)
    return cdp_eval(ws_url, expr)


# ---------------- Win32: 找到/移动 Edge 窗口 ----------------

def enum_edge_windows():
    res = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "Chrome_WidgetWin_1":
                res.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return res


def window_rect(hwnd):
    r = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def move_window(hwnd, x, y, w, h, topmost=False):
    user32.SetWindowPos(hwnd, -1 if topmost else 0, x, y, w, h, 0x0004)  # SWP_NOZORDER if not topmost


def pip_rect(cfg):
    """小窗目标矩形：优先用户手动调整后记住的位置，其次 config 指定，默认左下角。"""
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    try:
        x = int(cfg.get("pip_x", -1))
        y = int(cfg.get("pip_y", -1))
        w = int(cfg.get("pip_w", -1))
        h = int(cfg.get("pip_h", -1))
    except (TypeError, ValueError):
        x = y = w = h = -1
    if w < 200 or h < 120:
        w, h = PIP_W, PIP_H
    if x < 0:
        x = 0
    if y < 0:
        y = sh - h - 56
    x = max(0, min(x, sw - w))
    y = max(0, min(y, sh - h))
    return x, y, w, h


def place_and_watch_pip(hwnd, cfg):
    """按记忆矩形摆放小窗，并在后台记住用户后续的手动拖动/缩放。"""
    if not hwnd:
        return
    x, y, w, h = pip_rect(cfg)
    move_window(hwnd, x, y, w, h)
    log("画中画小窗已就位 (%d,%d %dx%d)" % (x, y, w, h))

    def watch():
        last = window_rect(hwnd)
        changed_at = None
        while user32.IsWindow(hwnd):
            time.sleep(4)
            if not user32.IsWindow(hwnd):
                break
            try:
                r = window_rect(hwnd)
            except Exception:
                break
            if r != last:
                last, changed_at = r, time.time()
                continue
            # 矩形稳定6秒且尺寸合理 → 视为用户调整完成，记住
            if changed_at and time.time() - changed_at >= 6 \
                    and last[2] >= 200 and last[3] >= 120:
                cfg["pip_x"], cfg["pip_y"] = last[0], last[1]
                cfg["pip_w"], cfg["pip_h"] = last[2], last[3]
                try:
                    save_cfg(cfg)
                    log("已记住小窗新位置 (%d,%d %dx%d)" % last)
                except Exception:
                    pass
                changed_at = None

    threading.Thread(target=watch, daemon=False).start()


# ---------------- 流程 ----------------

def parse_room(s):
    s = (s or "").strip()
    if not s:
        return None
    m = re.search(r"live\.bilibili\.com/(\d+)", s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    return None


def launch(room, cctv, test=False):
    cfg = load_cfg()
    if room:
        cfg["last_room"] = room
    if cctv:
        cfg["last_cctv"] = cctv
    save_cfg(cfg)

    if not os.path.exists(EDGE):
        alert("找不到 Edge: " + EDGE)
        return

    log("启动专用 Edge 实例 (央视: %s, B站: %s)" % (cctv or "跳过", room or "跳过"))

    def port_state():
        try:
            requests.get("http://127.0.0.1:%d/json/version" % PORT, timeout=2)
            return "ok"
        except requests.exceptions.Timeout:
            return "zombie"
        except Exception:
            return "down"

    st = port_state()
    if st == "zombie":
        log("检测到假死的专用 Edge，先关闭它")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                        "Where-Object { $_.CommandLine -like '*BiliLiveEdge*' } | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                       capture_output=True, timeout=40)
        time.sleep(2)
        st = port_state()

    if st == "ok":
        log("专用 Edge 已在运行，直接开新标签页")
        from urllib.parse import quote
        if room:
            requests.put("http://127.0.0.1:%d/json/new?%s" % (
                PORT, quote("https://live.bilibili.com/%s" % room, safe="")), timeout=6)
            time.sleep(1.5)
    else:
        args = [EDGE, "--user-data-dir=" + PROFILE,
                "--remote-debugging-port=%d" % PORT,
                "--no-first-run", "--no-default-browser-check", "--start-maximized"]
        if room:
            args.append("https://live.bilibili.com/%s" % room)
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)

    if cctv:
        try:
            os.startfile(cctv)  # 系统默认浏览器（你的Edge）打开，全屏自己按
            log("已在你日常浏览器打开网页直播：请自行全屏，音量用控制条滑块")
        except Exception as e:
            log("打开直播网址失败: %s" % e)

    tab_bili = wait_for(lambda: find_tab("live.bilibili.com"), 45, "B站标签页") if room else None
    wire_up(tab_bili, room, cfg)


def mutex_exists():
    return bool(ctypes.windll.kernel32.OpenMutexW(0x100000, False, "BiliDanmakuOverlayMutex"))


def find_pip_window():
    for h in enum_edge_windows():
        _, _, w, hgt = window_rect(h)
        if 250 < w < 900 and 140 < hgt < 650:
            return h
    return None


def wire_up(tab_bili, room, cfg):
    """对B站标签页完成 解除静音+画中画（幂等，可重复执行）；音量由 pycaw 进程级控制。"""
    if tab_bili:
        try:
            apply_volume(tab_bili["webSocketDebuggerUrl"], float(cfg.get("vol_bili", 1.0)))
        except Exception as e:
            log("解除B站静音失败(不影响手调): %s" % e)
    cfg["vol_applied"] = True
    save_cfg(cfg)

    # B站: 画中画 → 移到左下角；失败则退化为置顶小窗，并由后台线程持续补齐
    pip_done = False
    degraded_hwnd = None
    if tab_bili:
        ws_url = tab_bili["webSocketDebuggerUrl"]
        wait_for(lambda: video_ready(ws_url), 60, "B站视频就绪")
        time.sleep(2)
        try:
            already = cdp_eval(ws_url, "!!document.pictureInPictureElement",
                               await_promise=False)
        except Exception:
            already = False
        if already:
            pip_done = True
            log("B站已在画中画中")
        for attempt in (1, 2, 3):
            if pip_done:
                break
            try:
                r = do_pip(ws_url)
            except Exception as e:
                r = "err:%s" % e
            log("画中画请求(第%d次): %s" % (attempt, r))
            if r == "ok":
                time.sleep(1.5)
                pip_done = True
                break
            time.sleep(3)
        hwnd = find_pip_window() if pip_done else None
        if hwnd:
            place_and_watch_pip(hwnd, cfg)
        elif not pip_done:
            target = None
            for h in enum_edge_windows():
                t = window_title(h)
                if ("哔哩哔哩" in t or "bilibili" in t.lower()) and "直播" in t:
                    target = h
            if target:
                sh = user32.GetSystemMetrics(1)
                move_window(target, 0, sh - 300 - 56, 440, 300, topmost=True)
                degraded_hwnd = target
                log("暂用置顶小窗，页面就绪后自动升级为标准画中画")
            else:
                log("未找到B站窗口，稍后自动重试画中画")

    # 后台补齐线程：首次加载慢时持续重试画中画，最长8分钟
    def late_complete():
        nonlocal pip_done
        end = time.time() + 480
        while time.time() < end and not pip_done:
            time.sleep(15)
            try:
                if tab_bili:
                    bw = tab_bili["webSocketDebuggerUrl"]
                    if video_ready(bw):
                        if cdp_eval(bw, "!!document.pictureInPictureElement", await_promise=False):
                            pip_done = True
                        else:
                            rr = do_pip(bw)
                            if rr == "ok":
                                time.sleep(1.2)
                                pip_done = True
                        if pip_done:
                            hwnd = find_pip_window()
                            if hwnd:
                                place_and_watch_pip(hwnd, cfg)
                            if degraded_hwnd:
                                user32.SetWindowPos(degraded_hwnd, -2, 0, 0, 0, 0, 0x0003)
                            log("后台补齐: B站画中画完成")
            except Exception:
                pass
        log("后台补齐结束: 画中画=%s" % pip_done)

    threading.Thread(target=late_complete, daemon=False).start()

    # 弹幕悬浮层
    if room:
        if mutex_exists():
            log("弹幕悬浮层已在运行，跳过启动")
        else:
            subprocess.Popen([sys.executable, os.path.join(BASE, "danmaku_overlay.py"), str(room)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, creationflags=0x00000008)
            log("弹幕悬浮层已启动")
    log("一键启动完成")


DLG_BG = "#15161b"
DLG_SURF = "#23252d"
DLG_FG = "#e8eaf0"
DLG_ACC = "#7aa2f7"


class LaunchDialog(object):
    """单窗口双栏输入：B站房间 + 第二路网页直播。关闭/取消返回 None。"""

    def __init__(self, cfg):
        self.result = None
        top = self.top = tk.Toplevel()
        top.title("一键直播")
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.configure(bg=DLG_BG)
        top.protocol("WM_DELETE_WINDOW", self.cancel)

        frm = tk.Frame(top, bg=DLG_BG, padx=16, pady=12)
        frm.pack()
        tk.Label(frm, text="B站直播间号或网址", bg=DLG_BG, fg=DLG_FG,
                 anchor="w").grid(row=0, column=0, sticky="w")
        self.e_room = tk.Entry(frm, width=54, bg=DLG_SURF, fg=DLG_FG,
                               insertbackground=DLG_FG, relief="flat")
        self.e_room.insert(0, cfg.get("last_room", "") or "")
        self.e_room.grid(row=1, column=0, pady=(2, 10), ipady=3)
        tk.Label(frm, text="第二路网页直播网址（任意网页直播，留空只看B站）",
                 bg=DLG_BG, fg=DLG_FG, anchor="w").grid(row=2, column=0, sticky="w")
        self.e_web = tk.Entry(frm, width=54, bg=DLG_SURF, fg=DLG_FG,
                              insertbackground=DLG_FG, relief="flat")
        self.e_web.insert(0, cfg.get("last_cctv", "") or "")
        self.e_web.grid(row=3, column=0, pady=(2, 12), ipady=3)

        btns = tk.Frame(frm, bg=DLG_BG)
        btns.grid(row=4, column=0, sticky="e")
        tk.Button(btns, text="取消", command=self.cancel, width=7,
                  bg=DLG_SURF, fg=DLG_FG, activebackground="#2f313b",
                  activeforeground=DLG_FG, relief="flat").pack(side="left", padx=4)
        tk.Button(btns, text="启动", command=self.ok, width=9,
                  bg=DLG_ACC, fg="#15161b", activebackground="#8fb0f9",
                  activeforeground="#15161b", relief="flat").pack(side="left")

        for e in (self.e_room, self.e_web):
            e.bind("<Return>", lambda ev: self.ok())
            e.bind("<Escape>", lambda ev: self.cancel())
        self.e_room.focus_set()
        self.e_room.select_range(0, "end")
        top.update_idletasks()
        top.geometry("+%d+%d" % ((top.winfo_screenwidth() - top.winfo_width()) // 2,
                                 (top.winfo_screenheight() - top.winfo_height()) // 2))
        top.grab_set()
        top.wait_window()

    def ok(self):
        self.result = (self.e_room.get(), self.e_web.get())
        self.top.destroy()

    def cancel(self):
        self.result = None
        self.top.destroy()


def ask_inputs(cfg):
    tmp = tk.Tk()
    tmp.withdraw()
    dlg = LaunchDialog(cfg)
    tmp.destroy()
    return dlg.result  # None = 用户关闭或取消


def main():
    log("快捷方式已触发")
    try:
        _main()
    except Exception as e:
        import traceback
        log("启动器异常: %s" % traceback.format_exc())
        try:
            alert("启动出错: %s（详见 overlay.log）" % e)
        except Exception:
            pass


def _main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    cfg = load_cfg()
    if len(sys.argv) >= 2 and sys.argv[1] == "--resume":
        room = parse_room(sys.argv[2] if len(sys.argv) > 2 else cfg.get("last_room", ""))
        tab_bili = wait_for(lambda: find_tab("live.bilibili.com"), 10, "B站标签页") if room else None
        log("续跑: B站标签=%s" % bool(tab_bili))
        wire_up(tab_bili, room, cfg)
        return
    if len(sys.argv) >= 4 and sys.argv[1] == "--test":
        room, cctv = sys.argv[2], sys.argv[3]
        launch(room, cctv, test=True)
        return
    res = ask_inputs(cfg)
    if res is None:
        log("已取消，退出")
        return
    room, cctv = res
    room = parse_room(room)
    cctv = (cctv or "").strip()
    if not room and not cctv:
        return
    if room is None:
        alert("B站输入无法识别：请填房间号（纯数字）或 live.bilibili.com 开头的网址")
        return
    try:
        launch(room, cctv)
    except Exception as e:
        log("启动失败: %s" % e)
        alert("启动出错: %s\n详见 overlay.log" % e)


if __name__ == "__main__":
    main()
