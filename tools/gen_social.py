#!/usr/bin/env python3
"""Render the GitHub social-preview card (1280×640 PNG).

The card is what a link to this repo looks like on X, Slack, Reddit and HN —
it has to carry the hook, not the feature list. Chrome renders the HTML; there
is no image dependency to install.

    python3 tools/gen_social.py

Notes: Chrome is asked to screenshot from a temp dir (macOS TCC can silently
stall a headless run reading from ~/Downloads) and is killed once the file
stops growing (headless Chrome routinely writes the file and never exits).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

W, H = 1280, 640

HTML = """<!doctype html><meta charset="utf-8">
<style>
  @font-face { font-family: x; src: local("Helvetica Neue"); }
  * { box-sizing: border-box; margin: 0; }
  body { width: %(w)spx; height: %(h)spx; background: #0b0d10; color: #e6e9ec;
         font: 400 20px/1.4 ui-sans-serif, -apple-system, "Helvetica Neue", sans-serif;
         padding: 56px 64px; display: flex; flex-direction: column; }
  .brand { color: #f7775a; font-weight: 700; font-size: 26px; letter-spacing: -0.2px; }
  h1 { font-size: 52px; line-height: 1.08; font-weight: 700; letter-spacing: -1.2px;
       margin: 22px 0 0; }
  h1 .q { color: #8a949e; }
  .sub { color: #8a949e; font-size: 21px; margin-top: 14px; }
  .rows { margin-top: 40px; display: grid; grid-template-columns: 200px 1fr 250px;
          row-gap: 14px; align-items: center; font-family: ui-monospace, Menlo, monospace;
          font-size: 19px; }
  .lab { color: #e6e9ec; }
  .track { height: 16px; border-radius: 9px; background: #1b2026; position: relative; }
  .fill { position: absolute; inset: 0 auto 0 0; border-radius: 9px; }
  .val { color: #8a949e; text-align: right; font-size: 18px; }
  .fills { margin-top: 26px; color: #8a949e; font-size: 18px;
           font-family: ui-monospace, Menlo, monospace; }
  .foot { margin-top: auto; display: flex; justify-content: space-between;
          align-items: baseline; color: #5c6670; font-size: 18px;
          font-family: ui-monospace, Menlo, monospace; }
  .foot b { color: #7df0a8; font-weight: 400; }
</style>
<div class="brand">agentburn</div>
<h1>You didn't run out on your<br>average day. <span class="q">You ran out<br>inside one window.</span></h1>
<div class="sub">Claude Code · OpenClaw · Hermes Agent — local, read-only, zero dependencies</div>
<div class="rows">
  <div class="lab">peak 5h window</div>
  <div class="track"><div class="fill" style="width:100%%;background:#f7775a"></div></div>
  <div class="val">555M weighted</div>
  <div class="lab">your median one</div>
  <div class="track"><div class="fill" style="width:19%%;background:#5ab0f7"></div></div>
  <div class="val">104M &nbsp;·&nbsp; 5.4× apart</div>
</div>
<div class="fills">what filled the peak: cache reads 64%% &nbsp;·&nbsp; cache writes 25%% &nbsp;·&nbsp; output 11%%</div>
<div class="foot"><span><b>uvx agentburn limits</b></span><span>github.com/Socialpranker/agentburn</span></div>
""" % {"w": W, "h": H}

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
)


def chrome() -> str:
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    raise SystemExit(
        "no Chrome/Chromium found — install one or render the HTML yourself"
    )


def main() -> None:
    work = tempfile.mkdtemp(prefix="agentburn-social-")
    src = os.path.join(work, "card.html")
    png = os.path.join(work, "social-preview.png")
    with open(src, "w", encoding="utf-8") as f:
        f.write(HTML)

    proc = subprocess.Popen(
        [
            chrome(),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={W},{H}",
            f"--screenshot={png}",
            "--virtual-time-budget=2000",
            f"--user-data-dir={os.path.join(work, 'profile')}",
            f"file://{src}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Headless Chrome writes the file and then keeps running: wait for a stable
    # size rather than for the process to exit.
    last, stable, deadline = -1, 0, time.time() + 60
    while time.time() < deadline and stable < 2:
        time.sleep(0.5)
        size = os.path.getsize(png) if os.path.exists(png) else -1
        stable = stable + 1 if size == last and size > 0 else 0
        last = size
    proc.kill()
    if not os.path.exists(png) or os.path.getsize(png) == 0:
        raise SystemExit("Chrome produced no screenshot")

    dest = os.path.join(os.path.dirname(__file__), "..", "assets", "social-preview.png")
    shutil.copyfile(png, dest)
    shutil.rmtree(work, ignore_errors=True)
    print(f"assets/social-preview.png: {os.path.getsize(dest):,} bytes ({W}×{H})")
    print(
        "Upload it at Settings → General → Social preview (there is no API for this)."
    )


if __name__ == "__main__":
    main()
