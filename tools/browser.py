"""
tools/browser.py — Playwright Browser Suite
Gives Lumina a persistent, real browser for JS-rendered pages, clicking,
typing, form filling, and screenshots. Complements (not replaces) web.py.

Requires: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

# Import deferred to ensure_running() below (where it's actually first
# used) rather than at module level. core.agent imports this module
# unconditionally at startup, so a top-level `from playwright.sync_api
# import ...` meant playwright had to be installed just to import
# core.agent/core.headless at all -- not just to actually drive a
# browser. Broke test collection in CI, which deliberately runs against
# a minimal dependency set (pytest/requests/platformdirs only, no
# playwright/PySide6) for exactly this kind of backend-logic test suite.
# TYPE_CHECKING import + `from __future__ import annotations` keeps the
# type hints below working for static analysis without needing the
# names bound at runtime.
if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright

import config

logger = logging.getLogger(__name__)

# Screenshot output directory — FE-04-class fix: was Path.home() / "lumina" /
# "browser_screenshots", the identical hardcoded-folder-name bug as
# tools/projects.py (works by accident only if the clone happens to be named
# exactly ~/lumina, breaks for any other clone location/name).
#
# Anchored under memory/ (config.BASE_DIR-relative, gitignored), matching the
# existing convention for runtime-only data like sandbox_tmp/ and lumina.db —
# deliberately NOT the same folder as the repo-tracked browser_screenshots/
# at the project root (the promo screenshots kept per FE-10). Putting live
# runtime screenshots in that tracked folder would risk them getting swept
# into a future `git add .` alongside the promo images.
SCREENSHOT_DIR = Path(config.BASE_DIR) / "memory" / "browser_screenshots"


class BrowserManager:
    """
    Persistent Playwright browser instance that lives for the Lumina session.
    Lazy-initialized on first use. Auto-restarts if the browser crashes.
    Headless by default; set LUMINA_BROWSER_HEADLESS=0 to show the window.
    """

    def __init__(self):
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self, fn):
        """Submit fn to the browser-owning thread and block until done."""
        return self._executor.submit(fn).result()

    def _headless(self) -> bool:
        return os.environ.get("LUMINA_BROWSER_HEADLESS", "1") != "0"

    def ensure_running(self) -> None:
        """Lazy init + crash recovery. Safe to call before every tool."""
        try:
            # Quick liveness check — if page is gone, restart
            if self._page is not None:
                _ = self._page.url  # will throw if context is closed
            if self._playwright is not None and self._browser is not None and self._page is not None:
                return
        except Exception:
            logger.warning("Browser context dead — restarting.")
            self._playwright = None
            self._browser = None
            self._page = None

        logger.info("Starting Playwright browser (headless=%s)", self._headless())
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless())
        context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._page = context.new_page()
        logger.info("Browser ready.")

    def _page_text(self) -> str:
        """Extract visible text from the current page (strips scripts/styles)."""
        return self._page.evaluate("""() => {
            const clone = document.cloneNode(true);
            for (const el of clone.querySelectorAll('script, style, noscript, svg')) {
                el.remove();
            }
            return (document.body?.innerText || clone.textContent || '').trim();
        }""")

    # ── Public API ────────────────────────────────────────────────────────────

    def navigate(self, url: str, timeout: int = 30000) -> dict:
        """Navigate to a URL. Returns title + visible text + final URL."""
        def _do():
            self.ensure_running()
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                title = self._page.title()
                text = self._page_text()
                final_url = self._page.url
                return {
                    "success": True,
                    "title": title,
                    "url": final_url,
                    "text": text[:8000],  # cap at 8k chars — model context is finite
                    "truncated": len(text) > 8000,
                }
            except Exception as e:
                return {"success": False, "error": str(e)}
        return self._run(_do)

    def click(self, selector: str, timeout: int = 10000) -> dict:
        """Click an element by CSS selector or text. Waits for it to appear."""
        def _do():
            self.ensure_running()
            try:
                self._page.click(selector, timeout=timeout)
                return {"success": True, "message": f"Clicked: {selector}"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return self._run(_do)

    def type_text(self, selector: str, text: str, clear_first: bool = True) -> dict:
        """Type into a field. Clears existing content first by default."""
        def _do():
            self.ensure_running()
            try:
                self._page.click(selector)
                if clear_first:
                    self._page.fill(selector, "")
                self._page.type(selector, text)
                return {"success": True, "message": f"Typed into: {selector}"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return self._run(_do)

    def screenshot(self) -> dict:
        """
        Capture the current page. Returns both:
          - path: absolute path to saved PNG
          - base64: base64-encoded PNG (ready for image block when landmine is disarmed)
        """
        def _do():
            self.ensure_running()
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = SCREENSHOT_DIR / f"screenshot_{timestamp}.png"
                self._page.screenshot(path=str(path), full_page=False)
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                return {
                    "success": True,
                    "path": str(path),
                    "base64": b64,
                    "url": self._page.url,
                }
            except Exception as e:
                return {"success": False, "error": str(e)}
        return self._run(_do)

    def extract(self, selector: str) -> dict:
        """Extract inner text from a specific element by CSS selector."""
        def _do():
            self.ensure_running()
            try:
                element = self._page.query_selector(selector)
                if element is None:
                    return {"success": False, "error": f"No element found for: {selector}"}
                content = element.inner_text()
                return {"success": True, "selector": selector, "content": content}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return self._run(_do)

    def scroll(self, direction: str = "down", amount: int = 500) -> dict:
        """Scroll the page. direction: 'up' | 'down'. amount: pixels."""
        def _do():
            self.ensure_running()
            try:
                delta = amount if direction == "down" else -amount
                self._page.evaluate(f"window.scrollBy(0, {delta})")
                return {"success": True, "direction": direction, "amount": amount}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return self._run(_do)

    def get_links(self) -> dict:
        """Return all unique links on the current page with their text."""
        def _do():
            self.ensure_running()
            try:
                links = self._page.evaluate("""() => {
                    const seen = new Set();
                    const results = [];
                    for (const a of document.querySelectorAll('a[href]')) {
                        const href = a.href;
                        if (!seen.has(href) && href.startsWith('http')) {
                            seen.add(href);
                            results.push({ url: href, text: a.innerText.trim().slice(0, 100) });
                        }
                    }
                    return results.slice(0, 50);  // cap at 50 links
                }""")
                return {"success": True, "url": self._page.url, "links": links, "count": len(links)}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return self._run(_do)

    def current_url(self) -> dict:
        """Return the current page URL and title."""
        def _do():
            self.ensure_running()
            try:
                return {"success": True, "url": self._page.url, "title": self._page.title()}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return self._run(_do)

    def close(self) -> dict:
        """Close the browser and clean up Playwright resources."""
        def _do():
            try:
                if self._browser:
                    self._browser.close()
                if self._playwright:
                    self._playwright.stop()
            except Exception as e:
                logger.warning("Error closing browser: %s", e)
            finally:
                self._playwright = None
                self._browser = None
                self._page = None
            return {"success": True, "message": "Browser closed."}
        result = self._run(_do)
        self._executor.shutdown(wait=False)
        return result


# ── Singleton ─────────────────────────────────────────────────────────────────
browser_manager = BrowserManager()


# ── Tool Registration ─────────────────────────────────────────────────────────

def register_browser_tools(registry) -> None:

    registry.register(
        "browser_navigate",
        lambda url, timeout=30000: browser_manager.navigate(url, timeout=timeout),
        (
            "Navigate the browser to a URL and return the page title and visible text. "
            "Use for JS-rendered pages, single-page apps, or sites that block simple HTTP requests. "
            "For plain static pages a quick fetch, prefer web_fetch instead."
        ),
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL including https://"},
                "timeout": {"type": "integer", "description": "Timeout in milliseconds (default 30000)"},
            },
            "required": ["url"],
        },
    )

    registry.register(
        "browser_click",
        lambda selector, timeout=10000: browser_manager.click(selector, timeout=timeout),
        (
            "Click an element on the current page by CSS selector (e.g. '#submit-btn', "
            "'.nav-link', 'button[type=submit]') or by visible text using Playwright's "
            "text selector (e.g. 'text=Sign In')."
        ),
        {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector or Playwright text selector"},
                "timeout": {"type": "integer", "description": "Timeout in ms (default 10000)"},
            },
            "required": ["selector"],
        },
    )

    registry.register(
        "browser_type",
        lambda selector, text, clear_first=True: browser_manager.type_text(selector, text, clear_first=clear_first),
        "Type text into an input field on the current page. Clears the field first by default.",
        {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the input field"},
                "text": {"type": "string", "description": "Text to type"},
                "clear_first": {"type": "boolean", "description": "Clear existing content first (default true)"},
            },
            "required": ["selector", "text"],
        },
    )

    registry.register(
        "browser_screenshot",
        lambda **_: browser_manager.screenshot(),
        (
            "Take a screenshot of the current browser page. "
            "Returns the file path and base64-encoded image data."
        ),
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )

    registry.register(
        "browser_extract",
        lambda selector: browser_manager.extract(selector),
        "Extract the text content of a specific element on the current page by CSS selector.",
        {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the element to extract"},
            },
            "required": ["selector"],
        },
    )

    registry.register(
        "browser_scroll",
        lambda direction="down", amount=500: browser_manager.scroll(direction, amount),
        "Scroll the current page up or down by a given number of pixels.",
        {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
                "amount": {"type": "integer", "description": "Pixels to scroll (default 500)"},
            },
            "required": ["direction"],
        },
    )

    registry.register(
        "browser_get_links",
        lambda **_: browser_manager.get_links(),
        "Get all unique links on the current page with their anchor text. Returns up to 50 links.",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )

    registry.register(
        "browser_current_url",
        lambda **_: browser_manager.current_url(),
        "Return the current page URL and title. Useful to confirm navigation succeeded.",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )

    registry.register(
        "browser_close",
        lambda **_: browser_manager.close(),
        "Close the browser session and free resources. Use when done with a browsing task.",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )