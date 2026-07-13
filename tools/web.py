"""
Web Tools — search, fetch, wikipedia.
Uses duckduckgo-search library (pip install ddgs).
Falls back to direct HTML scraping if library unavailable.
"""

import requests
import re
import warnings
import ipaddress
import socket
from urllib.parse import quote_plus, urlparse, urljoin
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Suppress duckduckgo_search rename warning
warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")

PDF_EXTENSIONS = ('.pdf', '.PDF')
SKIP_DOMAINS = ('twitter.com', 'x.com', 'facebook.com', 'instagram.com')


def _is_pdf(url: str) -> bool:
    return any(url.split('?')[0].endswith(ext) for ext in PDF_EXTENSIONS)


def _arxiv_abstract(url: str) -> str:
    """Convert arXiv PDF URL to abstract URL."""
    return url.replace('/pdf/', '/abs/').replace('.pdf', '')


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web. Returns titles, URLs, and snippets. Use get_website to read a specific result."""
    # Try ddgs library first
    try:
        from ddgs import DDGS
        results = list(DDGS().text(query, max_results=max_results))
        if results:
            return _format_results(results)
    except ImportError:
        pass
    except Exception:
        pass

    # Try old duckduckgo_search package
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if results:
            return _format_results(results)
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: scrape DDG HTML directly
    return _ddg_html_search(query, max_results)


def _format_results(results: list) -> str:
    lines = []
    for r in results:
        url = r.get('href') or r.get('url', '')
        title = r.get('title', 'No title')
        snippet = r.get('body') or r.get('snippet', '')

        # Flag PDFs so model knows not to fetch them directly
        if _is_pdf(url):
            if 'arxiv.org/pdf' in url:
                abstract_url = _arxiv_abstract(url)
                lines.append(f"- {title}")
                lines.append(f"  [PDF — use abstract instead: {abstract_url}]")
            else:
                lines.append(f"- {title}")
                lines.append(f"  [PDF file — may not be readable: {url}]")
        else:
            lines.append(f"- {title}")
            lines.append(f"  {url}")

        if snippet:
            lines.append(f"  {snippet[:200]}")
    return "\n".join(lines)


def _ddg_html_search(query: str, max_results: int = 5) -> str:
    """Scrape DuckDuckGo HTML search results."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()

        results = []
        # Parse result links and snippets
        blocks = re.findall(
            r'class="result__title".*?href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</span>',
            resp.text, re.DOTALL
        )
        for raw_url, title, snippet in blocks[:max_results]:
            # Extract actual URL from DDG redirect
            actual_url = raw_url
            if "uddg=" in raw_url:
                from urllib.parse import unquote, urlparse, parse_qs
                qs = parse_qs(urlparse(raw_url).query)
                if "uddg" in qs:
                    actual_url = unquote(qs["uddg"][0])
            title_clean = re.sub(r'<[^>]+>', '', title).strip()
            snippet_clean = re.sub(r'<[^>]+>', '', snippet).strip()[:200]
            results.append({"title": title_clean, "href": actual_url, "body": snippet_clean})

        if results:
            return _format_results(results)

        # Bare URL fallback
        urls = re.findall(r'href="(https?://[^"&]+)"', resp.text)
        filtered = list(dict.fromkeys(
            u for u in urls if "duckduckgo.com" not in u and "duck.com" not in u
        ))[:max_results]
        return "\n".join(f"- {u}" for u in filtered) if filtered else "No results found."

    except Exception as e:
        return f"[Search error: {e}]"


def _is_safe_public_url(url: str) -> bool:
    """FE-08: reject URLs that resolve to private/loopback/link-local/reserved
    addresses. Without this, a Discord-Safe stranger could ask get_website to
    fetch http://localhost:8080/v1/models or http://192.168.1.1/ and get the
    response body handed back to them — service fingerprinting and content
    disclosure of unauthenticated local services, from a stranger, via chat.

    Applies to ALL callers, owner included — registry.call() has no session/
    owner context threaded through to individual tool functions today, so a
    real owner-vs-guest split here would mean a much bigger dispatch-layer
    refactor than this warrants. Owner loses nothing real: terminal/sandbox
    already cover any legitimate local-network fetch need.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        # Resolve to an actual IP — blocks literal private IPs AND hostnames
        # that resolve to one (e.g. a DNS record pointing at 127.0.0.1 or a
        # LAN address), not just a string-match on "localhost".
        addr = socket.gethostbyname(host)
        ip = ipaddress.ip_address(addr)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
        return True
    except Exception:
        return False  # can't resolve/parse it -> fail closed, don't fetch


def _safe_get(url: str, **kwargs):
    """requests.get() with manual redirect-hop validation. requests follows
    redirects by default (FE-08's own note) — without this, a public URL
    that 302s to a private one would sail straight past _is_safe_public_url
    and fetch the private target anyway."""
    if not _is_safe_public_url(url):
        raise ValueError(f"refusing to fetch — resolves to a private/internal address: {url}")

    resp = requests.get(url, allow_redirects=False, **kwargs)
    hops = 0
    while resp.is_redirect and hops < 5:
        location = resp.headers.get("Location")
        if not location:
            break
        url = urljoin(url, location)
        if not _is_safe_public_url(url):
            raise ValueError(f"refusing to follow redirect — target resolves to a private/internal address: {url}")
        resp = requests.get(url, allow_redirects=False, **kwargs)
        hops += 1
    return resp


def get_website(url: str, max_chars: int = 3000) -> str:
    """Fetch readable text from a URL. Do not use on PDF URLs."""
    if _is_pdf(url):
        if 'arxiv.org/pdf' in url:
            return f"[PDF detected. Try the abstract page instead: {_arxiv_abstract(url)}]"
        return f"[PDF file — cannot extract text. URL: {url}]"

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
        }
        resp = _safe_get(url, timeout=15, headers=headers)
        resp.raise_for_status()

        text = resp.text
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<nav[^>]*>.*?</nav>', '', text, flags=re.DOTALL)
        text = re.sub(r'<footer[^>]*>.*?</footer>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&[a-zA-Z]+;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        return text[:max_chars] + ("..." if len(text) > max_chars else "")
    except requests.exceptions.HTTPError as e:
        return f"[HTTP error {e.response.status_code}: {url}]"
    except Exception as e:
        return f"[Fetch error: {e}]"


def get_wikipedia(topic: str) -> str:
    """Get Wikipedia summary for a topic."""
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote_plus(topic)}"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Lumina/1.0"})
        if resp.status_code == 404:
            return f"No Wikipedia article found for '{topic}'."
        data = resp.json()
        extract = data.get("extract", "No content.")
        page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
        return f"{extract}\n\nSource: {page_url}"
    except Exception as e:
        return f"[Wikipedia error: {e}]"


def register_web_tools(registry):
    registry.register(
        "web_search", web_search,
        "Search the web. Returns titles, URLs, snippets. Call get_website only if you need full page content.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5}
            },
            "required": ["query"]
        }
    )

    registry.register(
        "get_website", get_website,
        "Fetch text content from a URL. Do not use on PDF URLs.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer", "default": 3000}
            },
            "required": ["url"]
        }
    )

    registry.register(
        "get_wikipedia", get_wikipedia,
        "Get Wikipedia summary for a topic.",
        {
            "type": "object",
            "properties": {
                "topic": {"type": "string"}
            },
            "required": ["topic"]
        }
    )
