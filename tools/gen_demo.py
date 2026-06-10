#!/usr/bin/env python3
"""Generate the animated terminal demos (SVG + CSS keyframes, no JS).

Declarative scenes → absolute keyframe percentages on a shared clock, so the
whole thing loops forever: type the command, reveal output with reading pauses,
pop teaching callouts, fade, next scene. Run from the repo root:

    python3 tools/gen_demo.py
"""

from __future__ import annotations

import os

W = 760
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
C = {
    "t": "#e6e9ec", "d": "#8a949e", "dd": "#5c6670", "amber": "#f5d76e",
    "red": "#f7a08a", "green": "#7df0a8", "hot": "#f7775a", "blue": "#5ab0f7",
    "purple": "#c89bf7", "track": "#1b2026", "bg": "#0b0d10", "chrome": "#14181d",
}

_uid = 0


def uid() -> str:
    global _uid
    _uid += 1
    return f"k{_uid}"


class Demo:
    def __init__(self, total: float, height: int):
        self.total = total
        self.h = height
        self.css: list = []
        self.body: list = []

    def pct(self, sec: float) -> float:
        return max(0.0, min(100.0, sec / self.total * 100))

    def _window(self, name, a, b, fade_in=0.45, fade_out=0.6, transform=""):
        """opacity (and optional transform) window from a..b seconds."""
        p0, p1 = self.pct(a), self.pct(a + fade_in)
        p2, p3 = self.pct(b - fade_out), self.pct(b)
        tf_off = f"transform: {transform};" if transform else ""
        tf_on = "transform: none;" if transform else ""
        self.css.append(
            f"@keyframes {name} {{ 0%,{p0:.2f}% {{ opacity:0; {tf_off} }} "
            f"{p1:.2f}% {{ opacity:1; {tf_on} }} {p2:.2f}% {{ opacity:1; {tf_on} }} "
            f"{p3:.2f}%,100% {{ opacity:0; }} }}"
        )

    def fade(self, a, b, fade_in=0.45, rise=True):
        k = uid()
        self._window(k, a, b, fade_in=fade_in, transform="translateY(5px)" if rise else "")
        self.css.append(
            f".{k} {{ opacity:0; animation: {k} {self.total}s linear infinite; }}"
        )
        return k

    def pop(self, a, b):
        k = uid()
        self._window(k, a, b, fade_in=0.35, transform="scale(.92)")
        self.css.append(
            f".{k} {{ opacity:0; transform-box: fill-box; transform-origin: left center; "
            f"animation: {k} {self.total}s linear infinite; }}"
        )
        return k

    def typing(self, a, b, chars: int, dur: float = 1.1):
        k, kt = uid(), uid()
        self._window(k, a, b, fade_in=0.05)
        p0, p1 = self.pct(a), self.pct(a + dur)
        self.css.append(
            f"@keyframes {kt} {{ 0%,{p0:.2f}% {{ clip-path: inset(0 100% 0 0); }} "
            f"{p1:.2f}%,100% {{ clip-path: inset(0 -2% 0 0); }} }}"
        )
        self.css.append(
            f".{kt} {{ animation: {kt} {self.total}s steps({chars},end) infinite; }}"
        )
        self.css.append(f".{k} {{ opacity:0; animation: {k} {self.total}s linear infinite; }}")
        return k, kt

    def grow(self, a, b, dur: float = 0.7):
        k = uid()
        p0, p1 = self.pct(a), self.pct(a + dur)
        p2 = self.pct(b)
        self.css.append(
            f"@keyframes {k} {{ 0%,{p0:.2f}% {{ transform: scaleX(0); }} "
            f"{p1:.2f}% {{ transform: scaleX(1); }} {p2:.2f}%,100% {{ transform: scaleX(0); }} }}"
        )
        self.css.append(
            f".{k} {{ transform: scaleX(0); transform-box: fill-box; transform-origin: left center; "
            f"animation: {k} {self.total}s cubic-bezier(.2,.7,.2,1) infinite; }}"
        )
        return k

    # ---------- primitives ----------
    def text(self, x, y, s, fill="t", size=13.5, bold=False, cls="", mono=True):
        w = ' font-weight="bold"' if bold else ""
        f = MONO if mono else "system-ui, -apple-system, sans-serif"
        self.body.append(
            f'<text x="{x}" y="{y}" font-family="{f}" font-size="{size}" '
            f'fill="{C.get(fill, fill)}"{w} class="{cls}">{s}</text>'
        )

    def cmd(self, y, command, a, b):
        k, kt = self.typing(a, b, len(command) + 2)
        self.body.append(f'<g class="{k}">')
        self.text(24, y, "$", fill="green")
        self.body.append(f'<g class="{kt}">')
        self.text(40, y, command, fill="t")
        self.body.append("</g></g>")

    def bar(self, y, label, share, color, value, a, b):
        k = self.fade(a, b)
        g = self.grow(a + 0.1, b)
        self.body.append(f'<g class="{k}">')
        self.text(24, y + 10, label)
        self.body.append(f'<rect x="120" y="{y}" width="380" height="11" rx="5.5" fill="{C["track"]}"/>')
        self.body.append(
            f'<rect x="120" y="{y}" width="{max(8, int(380 * share))}" height="11" rx="5.5" '
            f'fill="{C[color]}" class="{g}"/>'
        )
        self.text(516, y + 10, value, fill="d")
        self.body.append("</g>")

    def strip(self, y, s, a, b, fill="hot", txt="red"):
        k = self.fade(a, b)
        self.body.append(f'<g class="{k}">')
        self.body.append(f'<rect x="24" y="{y}" width="712" height="34" rx="9" fill="{C[fill]}" opacity="0.13"/>')
        self.text(40, y + 22, s, fill=txt, bold=True)
        self.body.append("</g>")

    def callout(self, x, y, s, a, b, color="amber", w=None):
        k = self.pop(a, b)
        w = w or (len(s) * 7.6 + 26)
        self.body.append(f'<g class="{k}">')
        self.body.append(
            f'<rect x="{x}" y="{y}" width="{w:.0f}" height="26" rx="13" '
            f'fill="{C[color]}" opacity="0.16"/>'
        )
        self.body.append(
            f'<text x="{x + 13}" y="{y + 17}" font-family="system-ui, -apple-system, sans-serif" '
            f'font-size="12.5" font-weight="600" fill="{C[color]}">{s}</text>'
        )
        self.body.append("</g>")

    def scene_chip(self, label, a, b):
        k = self.fade(a, b, rise=False)
        self.body.append(f'<g class="{k}">')
        self.body.append(f'<rect x="660" y="46" width="76" height="22" rx="11" fill="{C["chrome"]}"/>')
        self.text(698, 61, label, fill="dd", size=11.5)
        self.body[-1] = self.body[-1].replace('x="698"', 'x="698" text-anchor="middle"')
        self.body.append("</g>")

    def title(self, s, a, b):
        k = self.fade(a, b, rise=False)
        self.body.append(
            f'<text x="380" y="21" text-anchor="middle" font-family="{MONO}" font-size="12" '
            f'fill="{C["dd"]}" class="{k}">{s}</text>'
        )

    def render(self) -> str:
        css = "\n  ".join(self.css)
        body = "\n".join(self.body)
        return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{self.h}" viewBox="0 0 {W} {self.h}" role="img" aria-label="agentburn animated demo">
<style>
  {css}
</style>
<rect width="{W}" height="{self.h}" rx="12" fill="{C['bg']}"/>
<rect width="{W}" height="34" rx="12" fill="{C['chrome']}"/>
<rect y="22" width="{W}" height="12" fill="{C['chrome']}"/>
<circle cx="22" cy="17" r="5.5" fill="#ff5f57"/><circle cx="42" cy="17" r="5.5" fill="#febc2e"/><circle cx="62" cy="17" r="5.5" fill="#28c840"/>
{body}
</svg>
"""


def scene_report(d: Demo, a: float, b: float, chip=None):
    d.title("~ agentburn · report", a, b)
    if chip:
        d.scene_chip(chip, a, b)
    d.cmd(64, "uvx agentburn", a + 0.2, b)
    d.text(24, 100, "🔥 agentburn — hermes · last 30d", bold=True, cls=d.fade(a + 1.8, b))
    k = d.fade(a + 2.4, b)
    d.body.append(f'<g class="{k}">')
    d.text(24, 126, "TL;DR: ≈ ~$431/mo pace; 79% of it is `cron`.", fill="amber", bold=True)
    d.text(24, 148, "First fix: route night work to a cheaper model", fill="d")
    d.body.append("</g>")
    d.text(24, 186, "WHERE IT BURNS", bold=True, cls=d.fade(a + 4.2, b))
    d.bar(202, "cron", 0.79, "hot", "79% · ~$36.00", a + 4.7, b)
    d.bar(228, "cli", 0.09, "blue", "9% · ~$4.00", a + 5.1, b)
    d.bar(254, "telegram", 0.07, "amber", "7% · ~$3.00", a + 5.5, b)
    d.bar(280, "subagent", 0.05, "purple", "5% · ~$2.50", a + 5.9, b)
    d.strip(312, "🌙 WHILE YOU SLEPT (00:00–08:00): ~$36.00 — 79% of spend", a + 7.0, b)
    d.callout(516, 352, "the bill you never see", a + 8.0, b, color="red")
    k = d.fade(a + 9.0, b)
    d.body.append(f'<g class="{k}">')
    d.text(24, 394, "💡 DO THIS", bold=True)
    d.text(24, 418, "1. Point cron jobs at a cheap model")
    d.text(370, 418, "→ frees ≈$300/mo", fill="green")
    d.text(24, 440, "2. heartbeat.activeHours 09:00–24:00")
    d.text(370, 440, "→ night burn ≈ $0", fill="green")
    d.body.append("</g>")
    d.text(24, 478, "local · read-only · nothing leaves this machine", fill="dd",
           cls=d.fade(a + 9.6, b))


def scene_why(d: Demo, a: float, b: float, chip=None):
    d.title("~ agentburn · why", a, b)
    if chip:
        d.scene_chip(chip, a, b)
    d.cmd(64, "agentburn why", a + 0.2, b)
    d.text(24, 100, "🔬 what the agent actually did — from its own records", fill="d",
           cls=d.fade(a + 1.6, b))
    k = d.fade(a + 2.2, b)
    d.body.append(f'<g class="{k}">')
    d.text(24, 136, "WHAT IT ACTUALLY DID", bold=True)
    d.text(24, 160, "browser", fill="t")
    d.text(140, 160, "34×  ≈210K in results", fill="d")
    d.text(24, 182, "web_search", fill="t")
    d.text(140, 182, "18×", fill="d")
    d.body.append("</g>")
    k = d.fade(a + 3.8, b)
    d.body.append(f'<g class="{k}">')
    d.text(24, 220, "RE-READ LOOPS", bold=True)
    d.text(24, 244, "4× read_file(/proj/big.md)  ≈32K re-paid", fill="t")
    d.body.append("</g>")
    d.callout(396, 230, "same file — paid 4 times", a + 4.8, b, color="amber")
    k = d.fade(a + 5.8, b)
    d.body.append(f'<g class="{k}">')
    d.text(24, 282, "RETRY STORMS", bold=True)
    d.text(24, 306, "Bash: 3 errors / 6 calls — re-paid each time", fill="red")
    d.body.append("</g>")
    k = d.fade(a + 7.0, b)
    d.body.append(f'<g class="{k}">')
    d.text(24, 344, "IDLE HEARTBEATS", bold=True)
    d.text(24, 368, "4 of 9 heartbeat runs did NOTHING — $2.40 of idle burn", fill="red")
    d.body.append("</g>")
    d.callout(516, 354, "woke up · thought · slept", a + 8.0, b, color="red")
    d.text(24, 410, "💡 1. cache big.md  2. fix Bash flags  3. heartbeat → cheap model",
           fill="t", cls=d.fade(a + 9.0, b))
    d.text(24, 446, "observations with numbers — not verdicts · content never read", fill="dd",
           cls=d.fade(a + 9.6, b))


def scene_fix(d: Demo, a: float, b: float, chip=None):
    d.title("~ agentburn · fix", a, b)
    if chip:
        d.scene_chip(chip, a, b)
    d.cmd(64, "agentburn fix", a + 0.2, b)
    d.text(24, 100, "🔧 DRY-RUN — nothing was changed", fill="amber", bold=True,
           cls=d.fade(a + 1.6, b))
    k = d.fade(a + 2.4, b)
    d.body.append(f'<g class="{k}">')
    d.text(24, 138, "1. Point Hermes cron jobs at a cheap model", bold=True)
    d.text(40, 162, "file   : ~/.hermes/cron/jobs.json", fill="d")
    d.text(40, 184, "why    : cron is 79% of spend", fill="d")
    d.text(40, 206, "effect : bulk of ≈$341/mo → cheap-model pricing", fill="green")
    d.body.append("</g>")
    k = d.fade(a + 4.4, b)
    d.body.append(f'<g class="{k}">')
    d.text(40, 240, "proposed:", fill="d")
    d.body.append(f'<rect x="40" y="252" width="560" height="30" rx="6" fill="{C["track"]}"/>')
    d.text(52, 272, '"nightly digest": "model": "deepseek/deepseek-chat"', fill="green")
    d.body.append("</g>")
    d.callout(612, 254, "paste-ready", a + 5.4, b, color="green")
    k = d.fade(a + 6.4, b)
    d.body.append(f'<g class="{k}">')
    d.text(24, 320, "2. Tame the OpenClaw heartbeat", bold=True)
    d.text(40, 344, '"activeHours": { "start": "09:00", "end": "24:00" }', fill="green")
    d.text(40, 366, '"lightContext": true', fill="green")
    d.body.append("</g>")
    d.callout(444, 332, "the night-burn killer", a + 7.4, b, color="red")
    d.text(24, 410, "ⓘ keys verified against the agents' source code", fill="dd",
           cls=d.fade(a + 8.4, b))
    d.text(24, 446, "prove it: agentburn --save-baseline → paste → agentburn --compare",
           fill="t", cls=d.fade(a + 9.2, b))


def build_combined() -> str:
    S, GAP = 11.0, 0.0  # seconds per scene
    total = S * 3
    d = Demo(total, 508)
    scene_report(d, 0.0, S - GAP, chip="1 / 3")
    scene_why(d, S, 2 * S - GAP, chip="2 / 3")
    scene_fix(d, 2 * S, 3 * S - GAP, chip="3 / 3")
    return d.render()


def build_single(scene_fn, height=508) -> str:
    total = 12.0
    d = Demo(total, height)
    scene_fn(d, 0.0, total)
    return d.render()


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "..", "assets")
    specs = {
        "demo.svg": build_combined(),
        "demo-why.svg": build_single(scene_why),
        "demo-fix.svg": build_single(scene_fix),
    }
    for name, svg in specs.items():
        path = os.path.join(out, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        print(f"{name}: {len(svg)} bytes")
