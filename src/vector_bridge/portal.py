"""Captive portal and on-card documentation for the field access point.

A phone that joins the access point probes a well-known URL to decide whether the
network has internet. We answer every probe with a redirect to the home screen, which
is what makes the phone pop up its "sign in to network" sheet showing our page. Local
DNS (dnsmasq under NetworkManager, see scripts/ap/) already resolves every name to the
Pi, so any request whose Host header is not ours is redirected too.

Documentation is the repo's DOCS directory copied onto the card by the installer and
served as plain pages; nothing is fetched from the internet, there is none.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

AP_ADDRESS = "10.42.0.1"  # NetworkManager shared-mode default, pinned in the connection
HOSTNAME = "vectorbridge"  # http://vectorbridge/ on the AP; http://vectorbridge.local/ via mDNS
DOCS_DIR = Path(os.environ.get("VECTOR_BRIDGE_DOCS", "/opt/vector-bridge/src/DOCS"))

# Connectivity-check paths by platform. Any response other than the expected body
# means "captive" to the client; a redirect to our page is the friendliest answer.
PROBE_PATHS = frozenset({
    "/hotspot-detect.html", "/library/test/success.html",  # iOS, macOS
    "/generate_204", "/gen_204", "/mobile/status.php",  # Android, Chrome OS
    "/connecttest.txt", "/ncsi.txt", "/redirect",  # Windows
    "/canonical.html", "/success.txt",  # Firefox
    "/check_network_status.txt", "/nm-check.txt",  # NetworkManager clients
})

# Hosts that mean "this Pi": the request is not a captive probe and is served normally.
OUR_HOSTS = frozenset({HOSTNAME, f"{HOSTNAME}.local", AP_ADDRESS, "localhost", "127.0.0.1"})
# Home-router DNS suffixes no one can register publicly ("vectorbridge.lan").
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain")


def home_url(host: str = HOSTNAME) -> str:
    return f"http://{host}/"


def redirect_for(path: str, host_header: str | None, our_hosts: frozenset[str] = OUR_HOSTS) -> str | None:
    """Where to send this request, or None to serve it. Pure, so it is easy to test."""
    if path.split("?", 1)[0] in PROBE_PATHS:
        return home_url()
    host = host_name(host_header)
    # "vectorbridge.lan" and the like: a bench router's DNS name for us is still us.
    ours = host in our_hosts or host.startswith(f"{HOSTNAME}.") or _looks_like_ip(host)
    if host and not ours:
        return home_url()
    return None


def host_name(netloc: str | None) -> str:
    """The lowercase name or address in a Host header or URL netloc, port and brackets dropped."""
    try:
        return urlsplit("//" + (netloc or "").strip()).hostname or ""
    except ValueError:
        return ""


def is_ours(host_header: str | None, our_hosts: frozenset[str] = OUR_HOSTS) -> bool:
    """Stricter than `redirect_for`, for writes: a DNS-rebound "vectorbridge.evil.example" is not us."""
    host = host_name(host_header)
    return host in our_hosts or _looks_like_ip(host) or (
        host.startswith(f"{HOSTNAME}.") and host.endswith(LOCAL_SUFFIXES))


def _looks_like_ip(host: str) -> bool:
    # Any literal address reached the Pi by an address the client already knows; serve it.
    return bool(re.fullmatch(r"[0-9.]+|\[[0-9a-f:]+\]|[0-9a-f]*:[0-9a-f:]+", host))


# ---- docs ---------------------------------------------------------------------


def list_docs(docs_dir: Path = DOCS_DIR) -> list[tuple[str, str]]:
    """(slug, title) for every markdown file on the card, title from its first heading."""
    out = []
    if not docs_dir.is_dir():
        return out
    for path in sorted(docs_dir.glob("*.md")):
        first = next((ln for ln in path.read_text(errors="replace").splitlines() if ln.startswith("#")), path.stem)
        out.append((path.stem, first.lstrip("# ").strip() or path.stem))
    return out


def render_doc(slug: str, docs_dir: Path = DOCS_DIR) -> str | None:
    """Minimal markdown to HTML: headings, code fences, lists, paragraphs. None if no such doc."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        return None
    path = docs_dir / f"{slug}.md"
    if not path.is_file():
        return None
    body, in_code, in_list = [], False, False
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("```"):
            body.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            body.append(html.escape(line))
            continue
        if in_list and not re.match(r"\s*[-*] ", line):
            body.append("</ul>")
            in_list = False
        m = re.match(r"(#{1,4})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            body.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif re.match(r"\s*[-*] ", line):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{_inline(line.split(' ', 1)[1] if line.startswith(('-', '*')) else line.strip()[2:])}</li>")
        elif line.startswith("|"):
            body.append(f"<div class=row>{_inline(line.strip('|'))}</div>")
        elif line.strip():
            body.append(f"<p>{_inline(line)}</p>")
    if in_list:
        body.append("</ul>")
    if in_code:
        body.append("</pre>")
    title = next((t for s, t in list_docs(docs_dir) if s == slug), slug)
    return _page(title, "\n".join(body))


def render_index(docs_dir: Path = DOCS_DIR) -> str:
    items = "".join(f'<li><a href="/docs/{s}">{html.escape(t)}</a></li>' for s, t in list_docs(docs_dir))
    return _page("Documentation", f"<p>Everything here is on the card; no internet needed.</p><ul>{items or '<li>no docs on this card</li>'}</ul>")


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(title)} - VectorBridge</title>
<style>
  body {{ margin: 0; padding: 14px; background: #12151a; color: #e7ecf3;
         font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }}
  a {{ color: #5ac8fa; }} pre {{ background: #1b2029; padding: 10px; border-radius: 8px; overflow-x: auto; }}
  code {{ background: #1b2029; padding: 1px 4px; border-radius: 4px; }} .row {{ font-family: monospace; }}
  nav {{ color: #8b97a8; font-size: 13px; margin-bottom: 12px; }}
</style>
<nav><a href="/">Home</a> · <a href="/docs/">Docs</a></nav>
{body}
"""
