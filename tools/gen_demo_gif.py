#!/usr/bin/env python3
"""Render the demo as a GIF — for the places that won't animate SVG
(Reddit, X, Telegram previews). Same three scenes, compressed to ~15s.

Pure PIL frame-by-frame: python3 tools/gen_demo_gif.py → assets/demo.gif
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

W, H = 760, 508
FPS = 6
SCENE_SEC = 5.0
BG, CHROME, TRACK = "#0b0d10", "#14181d", "#1b2026"
TXT, MUT, DIM = "#e6e9ec", "#8a949e", "#5c6670"
HOT, BLUE, AMBER, PURPLE, GREEN, RED = "#f7775a", "#5ab0f7", "#f5d76e", "#c89bf7", "#7df0a8", "#f7a08a"
F = "/usr/share/fonts/truetype/dejavu/"
MONO = ImageFont.truetype(F + "DejaVuSansMono.ttf", 14)
MONO_B = ImageFont.truetype(F + "DejaVuSansMono-Bold.ttf", 14)
SANS = ImageFont.truetype(F + "DejaVuSans.ttf", 12)
SANS_B = ImageFont.truetype(F + "DejaVuSans-Bold.ttf", 12)


def ease(t0: float, dur: float, t: float) -> float:
    if t <= t0:
        return 0.0
    x = min(1.0, (t - t0) / dur)
    return 1 - (1 - x) ** 3


def chrome(d: ImageDraw.ImageDraw, title: str, chip: str):
    d.rounded_rectangle((0, 0, W, H), 12, fill=BG)
    d.rectangle((0, 0, W, 34), fill=CHROME)
    for cx, c in ((22, "#ff5f57"), (42, "#febc2e"), (62, "#28c840")):
        d.ellipse((cx - 5, 12, cx + 5, 22), fill=c)
    d.text((W / 2, 11), title, font=SANS, fill=DIM, anchor="ma")
    d.rounded_rectangle((660, 46, 736, 68), 11, fill=CHROME)
    d.text((698, 51), chip, font=SANS, fill=DIM, anchor="ma")


def typed(d, y, cmd, t, t0):
    d.text((24, y), "$", font=MONO, fill=GREEN)
    n = int(ease(t0, 0.9, t) * len(cmd))
    d.text((40, y), cmd[:n], font=MONO, fill=TXT)
    if n < len(cmd) and int(t * 3) % 2 == 0:
        d.text((40 + 8.4 * n, y), "▍", font=MONO, fill=TXT)


def line(d, y, parts, t, t0, dy=4):
    a = ease(t0, 0.4, t)
    if a <= 0:
        return
    off = (1 - a) * dy
    x = 24
    for text, fill, bold in parts:
        f = MONO_B if bold else MONO
        d.text((x, y + off), text, font=f, fill=fill)
        x += d.textlength(text, font=f)


def bar(d, y, label, share, color, value, t, t0):
    if ease(t0, 0.3, t) <= 0:
        return
    d.text((24, y), label, font=MONO, fill=TXT)
    d.rounded_rectangle((120, y + 2, 500, y + 13), 5, fill=TRACK)
    w = max(6, int(380 * share * ease(t0 + 0.05, 0.6, t)))
    d.rounded_rectangle((120, y + 2, 120 + w, y + 13), 5, fill=color)
    d.text((516, y), value, font=MONO, fill=MUT)


def pill(d, x, y, text, color, t, t0):
    if ease(t0, 0.35, t) <= 0:
        return
    w = d.textlength(text, font=SANS_B) + 26
    d.rounded_rectangle((x, y, x + w, y + 26), 13, fill=_alpha(color, 0.16))
    d.text((x + 13, y + 6), text, font=SANS_B, fill=color)


def _alpha(hex_color: str, a: float) -> tuple:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    br, bg_, bb = 0x0B, 0x0D, 0x10
    return (int(br + (r - br) * a), int(bg_ + (g - bg_) * a), int(bb + (b - bb) * a))


def strip(d, y, text, t, t0):
    if ease(t0, 0.4, t) <= 0:
        return
    d.rounded_rectangle((24, y, 736, y + 34), 9, fill=_alpha(HOT, 0.13))
    cx, cy = 46, y + 17
    d.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=RED)
    d.ellipse((cx - 3, cy - 10, cx + 13, cy + 6), fill=_alpha(HOT, 0.13))
    d.text((64, y + 8), text, font=MONO_B, fill=RED)


def scene_report(d, t):
    chrome(d, "~ agentburn · report", "1 / 3")
    typed(d, 54, "uvx agentburn", t, 0.1)
    line(d, 90, [("agentburn — hermes · last 30d", TXT, True)], t, 1.1)
    line(d, 114, [("TL;DR: ≈ ~$431/mo pace; 79% of it is `cron`.", AMBER, True)], t, 1.5)
    line(d, 136, [("First fix: route night work to a cheaper model", MUT, False)], t, 1.7)
    line(d, 172, [("WHERE IT BURNS", TXT, True)], t, 2.1)
    bar(d, 196, "cron", 0.79, HOT, "79% · ~$36.00", t, 2.3)
    bar(d, 222, "cli", 0.09, BLUE, "9% · ~$4.00", t, 2.5)
    bar(d, 248, "telegram", 0.07, AMBER, "7% · ~$3.00", t, 2.7)
    bar(d, 274, "subagent", 0.05, PURPLE, "5% · ~$2.50", t, 2.9)
    strip(d, 306, "WHILE YOU SLEPT (00:00-08:00): ~$36.00 - 79% of spend", t, 3.4)
    pill(d, 516, 348, "the bill you never see", RED, t, 3.9)
    line(d, 396, [(">> 1. cron -> cheap model ", TXT, False), ("-> frees ~$300/mo", GREEN, False)], t, 4.2)
    line(d, 420, [(">> 2. heartbeat.activeHours 09-24 ", TXT, False), ("-> night ~ $0", GREEN, False)], t, 4.4)
    line(d, 470, [("local · read-only · nothing leaves this machine", DIM, False)], t, 4.6)


def scene_why(d, t):
    chrome(d, "~ agentburn · why", "2 / 3")
    typed(d, 54, "agentburn why", t, 0.1)
    line(d, 92, [("WHAT IT ACTUALLY DID", TXT, True)], t, 1.1)
    line(d, 116, [("browser      34×  ≈210K in results", MUT, False)], t, 1.3)
    line(d, 138, [("web_search   18×", MUT, False)], t, 1.45)
    line(d, 174, [("RE-READ LOOPS", TXT, True)], t, 1.9)
    line(d, 198, [("4× read_file(/proj/big.md)  ≈32K re-paid", TXT, False)], t, 2.1)
    pill(d, 420, 188, "same file — paid 4 times", AMBER, t, 2.5)
    line(d, 234, [("RETRY STORMS", TXT, True)], t, 2.9)
    line(d, 258, [("Bash: 3 errors / 6 calls — re-paid each time", RED, False)], t, 3.1)
    line(d, 294, [("IDLE HEARTBEATS", TXT, True)], t, 3.5)
    line(d, 318, [("4 of 9 runs did NOTHING — $2.40 idle burn", RED, False)], t, 3.7)
    pill(d, 480, 308, "woke up · thought · slept", RED, t, 4.0)
    line(d, 354, [("CRON RUNS", TXT, True)], t, 4.2)
    line(d, 378, [("nightly digest   31 runs   ~$18.40   avg 41K/run", TXT, False)], t, 4.35)
    line(d, 424, [(">> cache big.md · fix Bash flags · heartbeat -> cheap model", TXT, False)], t, 4.6)
    line(d, 470, [("observations, not verdicts · content never read", DIM, False)], t, 4.75)


def scene_fix(d, t):
    chrome(d, "~ agentburn · fix", "3 / 3")
    typed(d, 54, "agentburn fix", t, 0.1)
    line(d, 92, [("DRY-RUN — nothing was changed", AMBER, True)], t, 1.1)
    line(d, 128, [("1. Point Hermes cron jobs at a cheap model", TXT, True)], t, 1.5)
    line(d, 152, [("   file   : ~/.hermes/cron/jobs.json", MUT, False)], t, 1.7)
    line(d, 174, [("   effect : ≈$341/mo → ≈$4/mo = saves ≈$337/mo", GREEN, False)], t, 1.9)
    if ease(2.3, 0.4, t) > 0:
        d.rounded_rectangle((40, 196, 600, 226), 6, fill=TRACK)
        d.text((52, 202), '"nightly digest": "model": "deepseek/deepseek-chat"', font=MONO, fill=GREEN)
    pill(d, 612, 198, "paste-ready", GREEN, t, 2.7)
    line(d, 252, [("2. Tame the OpenClaw heartbeat", TXT, True)], t, 3.1)
    line(d, 276, [('   "activeHours": { "start": "09:00", "end": "24:00" }', GREEN, False)], t, 3.3)
    line(d, 298, [('   "lightContext": true', GREEN, False)], t, 3.45)
    pill(d, 444, 266, "the night-burn killer", RED, t, 3.8)
    line(d, 344, [(" i  keys verified against the agents' source code", DIM, False)], t, 4.1)
    line(d, 380, [("prove it: --save-baseline → paste → --compare", TXT, False)], t, 4.3)
    line(d, 470, [("uvx agentburn · github.com/Socialpranker/agentburn", DIM, False)], t, 4.6)


def main():
    scenes = (scene_report, scene_why, scene_fix)
    frames = []
    total_frames = int(SCENE_SEC * FPS)
    for scene in scenes:
        for i in range(total_frames):
            t = i / FPS
            img = Image.new("RGB", (W, H), BG)
            d = ImageDraw.Draw(img)
            scene(d, t)
            frames.append(img.quantize(colors=128, dither=Image.Dither.NONE))
    out = os.path.join(os.path.dirname(__file__), "..", "assets", "demo.gif")
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=int(1000 / FPS),
        loop=0,
        optimize=True,
    )
    print(f"demo.gif: {os.path.getsize(out):,} bytes · {len(frames)} frames · {len(scenes) * SCENE_SEC:.0f}s loop")


if __name__ == "__main__":
    main()
