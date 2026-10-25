# -*- coding: utf-8 -*-
"""UI验收框架：状态场景截图 + 几何对齐断言 + WCAG对比度检查。

用法: python qa_ui.py
输出: qa_shots/*.png 截图 + 终端 PASS/FAIL 报告（退出码=是否全部通过）
"""
import ctypes
import json
import os
import subprocess
import sys
import time

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
from PIL import ImageGrab

BASE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(BASE, "qa_shots")
os.makedirs(SHOTS, exist_ok=True)
import shutil
PYW = shutil.which("pythonw") or os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
SCRIPT = os.path.join(BASE, "danmaku_overlay.py")
ROOM = "6"  # 公开赛事房间，仅验收用

sys.path.insert(0, BASE)
from danmaku_overlay import THEME


def kill_overlays():
    import psutil
    pat = "danmaku" + "_" + "overlay"  # 拼接避免与本进程命令行自匹配
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.info["name"] and "python" in p.info["name"].lower() \
                    and p.info["cmdline"] and any(pat in c for c in p.info["cmdline"]):
                p.kill()
        except Exception:
            pass


def spawn(env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    subprocess.Popen([PYW, SCRIPT, ROOM], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, creationflags=0x00000008)


def shot(name):
    w = ctypes.windll.user32.GetSystemMetrics(0)
    h = ctypes.windll.user32.GetSystemMetrics(1)
    img = ImageGrab.grab(bbox=(w - 580, h - 230, w, h))
    img.save(os.path.join(SHOTS, name + ".png"))


print("== 场景截图 ==")
t0 = time.time()
kill_overlays(); time.sleep(1.2)
spawn({"DMO_TEST_DUMP": os.path.join(SHOTS, "geometry.json")})
time.sleep(5.0); shot("01_normal")
kill_overlays(); time.sleep(1.2)
spawn({"DMO_TEST_PAUSE": "2600"})
time.sleep(4.2); shot("02_paused")
kill_overlays(); time.sleep(1.2)
spawn({"DMO_TEST_COLLAPSE": "2600"})
time.sleep(4.2); shot("03_collapsed")
kill_overlays()
print("截图完成: qa_shots/01_normal 02_paused 03_collapsed")

print("\n== 几何对齐断言（网格列）==")
checks = []


def chk(name, ok, detail=""):
    checks.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + ("   %s" % detail if detail and not ok else ""))


g = None
gpath = os.path.join(SHOTS, "geometry.json")
if not os.path.exists(gpath) or os.path.getmtime(gpath) < t0:
    print("FAIL  geometry.json 为旧文件（本轮dump未生成，可能有残留进程占单实例锁）")
    kill_overlays()
    sys.exit(1)
g = json.load(open(gpath, encoding="utf-8"))

# 两列三行 + 粉丝牌独占行：簇(box)是滑条的父容器，按 box 坐标分行分列
sliders = [v for v in g.values() if isinstance(v, dict) and "cls" in v
           and "modernslider" in v["cls"]]
chk("滑条数量=7", len(sliders) == 7, str(len(sliders)))
narrow = sorted(s["w"] for s in sliders if s["w"] < 200)
wide = [s["w"] for s in sliders if s["w"] >= 200]
chk("六条窄滑条等宽", len(narrow) == 6 and max(narrow) - min(narrow) <= 6, str(narrow))
chk("窄滑条已拉伸利用空间", all(w >= 120 for w in narrow), str(narrow))
chk("粉丝牌滑条独占整行", len(wide) == 1, str(wide))
# 簇 = 滑条的父容器（winfo_name 为 !frameN）
cls_map = {v.get("cls"): v for v in g.values() if isinstance(v, dict) and "cls" in v}
boxes = []
for s in sliders:
    pv = cls_map.get(s.get("parent"))
    if pv and pv not in boxes:
        boxes.append(pv)
chk("控件簇数量=7", len(boxes) == 7, str(len(boxes)))
ys = sorted(round(b["y"]) for b in boxes)
rows = [ys[0]]
for y in ys[1:]:
    if y - rows[-1] > 8:
        rows.append(y)
chk("簇分4行", len(rows) == 4, str(rows))
by_row = {}
for b in boxes:
    ry = min(rows, key=lambda r: abs(r - b["y"]))
    by_row.setdefault(ry, []).append(b)
shape_ok = (len(by_row) == 4
            and all(len(v) == 2 for k, v in list(by_row.items())[:3])
            and len(by_row[max(by_row)]) == 1)
chk("前3行各2簇+末行1簇", shape_ok, str({k: len(v) for k, v in by_row.items()}))
chk("面板无截断", all(b["y"] + b["h"] <= g.get("bar", {}).get("h", 0) for b in boxes), "")
bar = g.get("bar", {})
chk("面板尺寸紧凑", 400 <= bar.get("w", 0) <= 560 and 100 <= bar.get("h", 0) <= 210, str(bar))

print("\n== WCAG 对比度 ==")


def lum(hx):
    r, g, b = (int(hx[i:i + 2], 16) / 255.0 for i in (1, 3, 5))

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def cr(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


pairs = [
    ("正文/背景", THEME["fg"], THEME["bg"], 7.0),
    ("次级文字/背景", THEME["mut"], THEME["bg"], 4.5),
    ("强调(滑条填充)/背景", THEME["acc"], THEME["bg"], 3.0),
    ("状态绿/背景", THEME["green"], THEME["bg"], 3.0),
    ("状态红/背景", THEME["red"], THEME["bg"], 3.0),
]
for label, fg, bg, need in pairs:
    ratio = cr(fg, bg)
    chk(label, ratio >= need, "%.2f (需≥%.1f)" % (ratio, need))

print("\n== 汇总 ==")
print("PASS %d/%d" % (sum(checks), len(checks)))
sys.exit(0 if all(checks) else 1)
