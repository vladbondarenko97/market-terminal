"""Records docs/demo.gif: the three tabs, a scroll through two of them and one console question.

Read-only: it only opens a terminal that is already running. Needs ffmpeg and Playwright's Chromium.
    .venv/bin/python scripts/record_demo.py http://127.0.0.1:8080
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
OUT = Path(__file__).resolve().parent.parent / "docs" / "demo.gif"
SIZE = {"width": 1280, "height": 800}
TABS = ("macro", "arbitrage", "forecast")


def scroll(page, pixels, step=30):
    page.mouse.move(640, 330)
    for _ in range(abs(pixels) // step):
        page.mouse.wheel(0, step if pixels > 0 else -step)
        page.wait_for_timeout(15)


with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport=SIZE, record_video_dir=tmp, record_video_size=SIZE)
    started = time.monotonic()
    page = context.new_page()
    page.goto(URL, wait_until="domcontentloaded")
    for tab in TABS:                                   # load every tab before the part that is kept
        page.click(f"#tab-{tab}")
        page.wait_for_timeout(8000)
    page.click("#tab-macro")
    page.wait_for_timeout(1000)
    skip = time.monotonic() - started                  # everything before this is loading: cut from the GIF

    page.wait_for_timeout(3000)                        # the first screen only: the dark pool panel below shows a paid feed's prints
    page.click("#tab-arbitrage")
    page.wait_for_timeout(1800)
    scroll(page, 600, step=60)
    page.wait_for_timeout(1000)
    page.click("#tab-forecast")
    page.wait_for_timeout(2000)
    scroll(page, 4200, step=70)
    page.wait_for_timeout(1000)
    chip = page.locator("#askSuggest button, #askSuggest .ask-chip").first
    if chip.count():                                   # the console needs a local model; without one this part is skipped
        chip.click()
        page.wait_for_timeout(8000)
    context.close()                                    # writes the video
    video = next(Path(tmp).glob("*.webm"))
    OUT.parent.mkdir(exist_ok=True)
    subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{skip:.2f}", "-i", str(video), "-vf",
                    "fps=6,scale=860:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle",
                    str(OUT)], check=True)
    browser.close()
print(f"{OUT} {OUT.stat().st_size / 1e6:.1f} MB")
