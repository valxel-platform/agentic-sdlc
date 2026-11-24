"""Re-render the capability-layer images from their HTML sources.

    pip install playwright && playwright install chromium
    python docs/architecture/render_layers.py

Edit capability-layers.html (names only) or capability-layers-detailed.html (with what each
capability does), then run this; it writes the PNG beside each source at twice the page's size.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
PAGES = {"capability-layers.html": 1240, "capability-layers-detailed.html": 1800}

with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, width in PAGES.items():
        page = browser.new_page(viewport={"width": width, "height": 1200}, device_scale_factor=2)
        page.goto((HERE / name).resolve().as_uri())
        page.wait_for_timeout(1500)  # the web fonts
        page.locator("#poster").screenshot(path=str(HERE / name.replace(".html", ".png")))
        print("wrote", name.replace(".html", ".png"))
    browser.close()
