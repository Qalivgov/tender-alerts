#!/usr/bin/env python3
"""Daily digest of active tenders on the PPRA Punjab e-procurement portal.

Reads every page of the Active Procurement list, keeps the tenders that match
the keyword groups in config.json, and emails the ones that are new since the
last run plus the ones about to close. The same digest, with the full list of
open matches, is written to docs/index.html, which GitHub Pages serves as the
team's shared page.

    python3 tender_alerts.py              normal daily run
    python3 tender_alerts.py --dry-run    fetch and write the page only;
                                          no email, nothing marked as seen
"""

import argparse
import datetime as dt
import html
import http.cookiejar
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "state.json"
PAGE_FILE = HERE / "docs" / "index.html"
ENV_FILE = HERE / ".env"

# The portal's server drops the connection on the cipher suites most clients
# pick by default (ECDHE-RSA-AES256-SHA384 and the ECDHE GCM suites), so offer
# only ones it handles.
CIPHERS = "ECDHE-RSA-AES128-SHA256:AES256-GCM-SHA384:AES128-GCM-SHA256:AES256-SHA256"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_PAGES = 60
FORGET_AFTER_DAYS = 45

ROW_RE = re.compile(r'<tr class="rg(?:Alt)?Row".*?</tr>', re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
HREF_RE = re.compile(r'href="?([^"\s>]+)', re.I)
INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
ATTR_RE = re.compile(r'([\w:-]+)="([^"]*)"')
CURRENT_PAGE_RE = re.compile(r'class="rgCurrentPage"[^>]*><span>(\d+)</span>')
TOTALS_RE = re.compile(r"<strong>(\d+)</strong> items in <strong>(\d+)</strong> pages")


def log(message):
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {message}", flush=True)


# ---------------------------------------------------------------- fetching

def make_opener():
    context = ssl.create_default_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(CIPHERS)
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def fetch(opener, url, data=None, attempts=3):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Referer"] = url
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with opener.open(request, timeout=90) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as error:
            if attempt == attempts:
                raise
            log(f"fetch failed ({error}); retrying")
            time.sleep(5 * attempt)


def cell_text(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def absolute(base, href):
    return urllib.parse.urljoin(base, urllib.parse.quote(html.unescape(href), safe="/:?=&%#"))


def parse_date(text):
    try:
        return dt.datetime.strptime(text, "%d %b %Y").date().isoformat()
    except ValueError:
        return ""


def parse_rows(page, base):
    tenders = []
    for row in ROW_RE.findall(page):
        cells = CELL_RE.findall(row)
        if len(cells) < 9:
            continue
        detail = HREF_RE.search(cells[1])
        notice = HREF_RE.search(cells[7])
        bidding = HREF_RE.search(cells[8])
        title = re.sub(r"\(\s*View Tender Detail\s*\)", "", cell_text(cells[1])).strip()
        tender = {
            "type": cell_text(cells[0]),
            "title": title,
            "category": cell_text(cells[2]),
            "published": parse_date(cell_text(cells[3])),
            "closes": parse_date(cell_text(cells[4])),
            "department": cell_text(cells[5]),
            "status": cell_text(cells[6]),
            "notice_url": absolute(base, notice.group(1)) if notice else "",
            "bidding_url": absolute(base, bidding.group(1)) if bidding else "",
            "detail_url": absolute(base, detail.group(1)) if detail else "",
        }
        tender["key"] = (
            urllib.parse.urlparse(tender["notice_url"]).path
            or "|".join((tender["type"], tender["title"], tender["department"], tender["published"]))
        )
        tenders.append(tender)
    return tenders


def form_fields(page):
    """Hidden fields to post back, plus the Next Page button name if it is enabled."""
    fields, next_button = {}, None
    for tag in INPUT_RE.findall(page):
        attrs = dict(ATTR_RE.findall(tag))
        if attrs.get("type") == "hidden" and attrs.get("name"):
            fields[html.unescape(attrs["name"])] = html.unescape(attrs.get("value", ""))
        elif attrs.get("title") == "Next Page" and "onclick" not in attrs:
            next_button = html.unescape(attrs["name"])
    return fields, next_button


def current_page(page):
    match = CURRENT_PAGE_RE.search(page)
    return int(match.group(1)) if match else 1


def fetch_all_tenders(url):
    opener = make_opener()
    page = fetch(opener, url)
    tenders = parse_rows(page, url)
    if not tenders:
        raise RuntimeError("no tender rows found on the first page; the portal layout may have changed")
    totals = TOTALS_RE.search(page)
    expected = int(totals.group(1)) if totals else None

    for _ in range(MAX_PAGES):
        fields, next_button = form_fields(page)
        if not next_button:
            break
        fields[next_button] = " "
        before = current_page(page)
        time.sleep(1)
        page = fetch(opener, url, data=urllib.parse.urlencode(fields).encode())
        if current_page(page) <= before:
            raise RuntimeError(f"the portal did not advance past page {before}")
        rows = parse_rows(page, url)
        if not rows:
            raise RuntimeError(f"page {current_page(page)} came back without tenders")
        tenders.extend(rows)

    unique = list({tender["key"]: tender for tender in tenders}.values())
    log(f"read {len(unique)} tenders from {current_page(page)} pages (portal reports {expected})")
    if expected and len(unique) < expected * 0.9:
        raise RuntimeError(f"only read {len(unique)} of {expected} tenders; not trusting this run")
    return unique


# --------------------------------------------------------------- filtering

def keyword_regex(keyword):
    """Whole-word match. A trailing * allows any ending; ALL-CAPS keywords are case-sensitive."""
    stem = keyword.rstrip("*")
    body = r"\s+".join(re.escape(part) for part in stem.split())
    tail = "[A-Za-z]*" if keyword.endswith("*") else ""
    return re.compile(
        rf"(?<![A-Za-z0-9]){body}{tail}(?![A-Za-z0-9])",
        0 if stem.isupper() else re.I,
    )


def build_matcher(config):
    groups = [
        (
            group["name"],
            [keyword_regex(keyword) for keyword in group.get("keywords", [])],
            [department.lower() for department in group.get("departments", [])],
        )
        for group in config["groups"]
    ]
    ignore = [re.compile(re.escape(phrase), re.I) for phrase in config.get("ignore_phrases", [])]
    skip = [phrase.lower() for phrase in config.get("skip_titles_containing", [])]

    def match(tender):
        title = tender["title"]
        if any(phrase in title.lower() for phrase in skip):
            return None
        for pattern in ignore:
            title = pattern.sub(" ", title)
        for name, keywords, _ in groups:
            if any(keyword.search(title) for keyword in keywords):
                return name
        for name, _, departments in groups:
            if any(department in tender["department"].lower() for department in departments):
                return name
        return None

    return match


# ------------------------------------------------------------------- state

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def load_env(config):
    """SMTP settings from config's env_file (if any), then .env here, then the environment."""
    env = {}
    files = [Path(config["env_file"]).expanduser()] if config.get("env_file") else []
    for path in files + [ENV_FILE]:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            env[name.strip()] = value
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


# --------------------------------------------------------------- rendering

def days_left(tender, today):
    if not tender["closes"]:
        return None
    return (dt.date.fromisoformat(tender["closes"]) - today).days


def closes_label(tender, today):
    if not tender["closes"]:
        return "Closing date not listed", ""
    date = dt.date.fromisoformat(tender["closes"])
    left = (date - today).days
    when = (
        "closes today" if left == 0
        else "closes tomorrow" if left == 1
        else f"{left} days left" if left > 1
        else "closing date passed"
    )
    return f"Closes {date:%d %b %Y}", when


def by_closing(tenders):
    return sorted(tenders, key=lambda tender: (tender["closes"] or "9999", tender["title"].lower()))


def render_html(sections, today, heading, summary):
    esc = html.escape
    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:680px;margin:0 auto;padding:16px;color:#1a1a1a;background:#ffffff">',
        f'<h1 style="font-size:20px;margin:0 0 4px">{esc(heading)}</h1>',
        f'<p style="font-size:13px;color:#666666;margin:0 0 8px">{esc(summary)}</p>',
    ]
    for title, tenders, note in sections:
        parts.append(
            f'<h2 style="font-size:15px;margin:24px 0 0;padding-bottom:6px;'
            f'border-bottom:2px solid #1a1a1a">{esc(title)} ({len(tenders)})</h2>'
        )
        if not tenders:
            parts.append(f'<p style="font-size:13px;color:#666666;margin:10px 0">{esc(note)}</p>')
        for tender in tenders:
            date_text, when = closes_label(tender, today)
            left = days_left(tender, today)
            urgent = left is not None and left <= 2
            tag = ""
            if tender["type"] and tender["type"] != "Tender Notice":
                tag = (
                    ' <span style="font-size:11px;font-weight:600;color:#7a4b00;background:#fff1d6;'
                    f'padding:1px 6px;border-radius:3px;white-space:nowrap">{esc(tender["type"])}</span>'
                )
            links = []
            if tender["notice_url"]:
                links.append(f'<a href="{esc(tender["notice_url"])}" style="color:#0b57d0">Tender notice</a>')
            if tender["bidding_url"]:
                links.append(f'<a href="{esc(tender["bidding_url"])}" style="color:#0b57d0">Bidding document</a>')
            if tender["detail_url"]:
                links.append(f'<a href="{esc(tender["detail_url"])}" style="color:#0b57d0">Original tender</a>')
            parts.append('<div style="padding:12px 0;border-bottom:1px solid #e4e4e4">')
            parts.append(
                f'<div style="font-size:15px;font-weight:600;line-height:1.35">{esc(tender["title"])}{tag}</div>'
            )
            parts.append(
                f'<div style="font-size:13px;color:#555555;margin-top:3px">{esc(tender["department"])}</div>'
            )
            parts.append(
                f'<div style="font-size:13px;margin-top:5px"><strong>{esc(date_text)}</strong>'
                + (f' <span style="color:{"#b3261e" if urgent else "#666666"}">&middot; {esc(when)}</span>' if when else "")
                + "</div>"
            )
            if tender["status"]:
                parts.append(
                    f'<div style="font-size:12px;color:#b3261e;margin-top:4px">PPRA flag: {esc(tender["status"])}</div>'
                )
            if links:
                parts.append(f'<div style="font-size:13px;margin-top:6px">{" &nbsp;|&nbsp; ".join(links)}</div>')
            parts.append("</div>")
    parts.append(
        '<p style="font-size:12px;color:#888888;margin-top:24px">Source: '
        '<a href="https://eproc.punjab.gov.pk/ActiveTenders.aspx" style="color:#888888">'
        "PPRA Punjab Active Procurement</a>. Keywords are in config.json.</p></div>"
    )
    return "\n".join(parts)


def render_text(sections, today, heading, summary):
    lines = [heading, summary]
    for title, tenders, note in sections:
        lines += ["", f"{title} ({len(tenders)})", "-" * 40]
        if not tenders:
            lines.append(note)
        for tender in tenders:
            date_text, when = closes_label(tender, today)
            kind = "" if tender["type"] in ("", "Tender Notice") else f" [{tender['type']}]"
            lines.append(f"{tender['title']}{kind}")
            lines.append(f"  {tender['department']}")
            lines.append(f"  {date_text}" + (f" ({when})" if when else ""))
            if tender["notice_url"]:
                lines.append(f"  Tender notice: {tender['notice_url']}")
            if tender["bidding_url"]:
                lines.append(f"  Bidding document: {tender['bidding_url']}")
            lines.append("")
    return "\n".join(lines)


def page_wrap(body, title):
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{html.escape(title)}</title></head>"
        f'<body style="margin:0;background:#ffffff">\n{body}\n</body></html>\n'
    )


# ------------------------------------------------------------------- email

def send_email(env, recipients, subject, text_body, html_body):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = env.get("MAIL_FROM") or env["SMTP_USER"]
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    host, port = env["SMTP_HOST"], int(env.get("SMTP_PORT") or 465)
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=60, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        server.starttls(context=context)
    with server:
        server.login(env["SMTP_USER"], env["SMTP_PASS"])
        server.send_message(message, to_addrs=recipients)


def email_settings(config):
    env = load_env(config)
    recipients = config.get("email_to") or env.get("MAIL_TO") or env.get("SMTP_USER") or ""
    recipients = [address.strip() for address in recipients.split(",") if address.strip()]
    ready = all(env.get(name) for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")) and recipients
    return env, recipients, bool(ready)


# -------------------------------------------------------------------- main

def run(dry_run):
    config = load_json(CONFIG_FILE, None)
    if config is None:
        raise RuntimeError(f"missing {CONFIG_FILE.name}")
    now = dt.datetime.now(ZoneInfo(config.get("timezone", "Asia/Karachi")))
    today = now.date()
    stamp = today.isoformat()
    match = build_matcher(config)

    matched = []
    for tender in fetch_all_tenders(config["url"]):
        group = match(tender)
        if group:
            tender["group"] = group
            matched.append(tender)

    state = load_json(STATE_FILE, {"seen": {}})
    seen = state["seen"]
    # "New" means first seen today, so a second run on the same day shows the same list.
    new = [tender for tender in matched if seen.get(tender["key"], {}).get("first", stamp) == stamp]
    new_keys = {tender["key"] for tender in new}
    soon_days = config.get("closing_soon_days", 5)
    closing_soon = [
        tender for tender in matched
        if tender["key"] not in new_keys
        and days_left(tender, today) is not None
        and 0 <= days_left(tender, today) <= soon_days
    ]

    group_names = [group["name"] for group in config["groups"]]
    new_sections = [
        (f"New: {name}", by_closing([t for t in new if t["group"] == name]), "Nothing new in this group.")
        for name in group_names
    ]
    soon_section = (
        f"Closing within {soon_days} days",
        by_closing(closing_soon),
        "Nothing you have already seen is about to close.",
    )
    heading = f"PPRA Punjab tenders, {today:%d %b %Y}"
    summary = (
        f"{len(new)} new today, {len(closing_soon)} closing soon, {len(matched)} open in total. "
        f"Updated {now:%d %b %Y, %H:%M} Pakistan time."
    )
    log(summary)

    full_sections = new_sections + [soon_section] + [
        (f"All open: {name}", by_closing([t for t in matched if t["group"] == name]), "None open.")
        for name in group_names
    ]
    PAGE_FILE.parent.mkdir(exist_ok=True)
    PAGE_FILE.write_text(
        page_wrap(render_html(full_sections, today, heading, summary), heading), encoding="utf-8"
    )
    log(f"wrote {PAGE_FILE.relative_to(HERE)}")

    if dry_run:
        log("dry run: no email sent, nothing marked as seen")
        return

    email_sections = new_sections + [soon_section]
    env, recipients, ready = email_settings(config)
    if not ready:
        log("email is not set up (no SMTP settings); skipping email")
    elif state.get("last_emailed") == stamp:
        log("already emailed today; skipping email")
    elif not new and not config.get("send_when_nothing_new", True):
        log("nothing new; skipping email")
    else:
        subject = f"PPRA tenders {today:%d %b}: {len(new)} new, {len(closing_soon)} closing soon"
        send_email(
            env, recipients, subject,
            render_text(email_sections, today, heading, summary),
            render_html(email_sections, today, heading, summary),
        )
        log(f"emailed {', '.join(recipients)}")
        state["last_emailed"] = stamp

    for tender in matched:
        seen.setdefault(tender["key"], {"first": stamp})["last"] = stamp
    cutoff = (today - dt.timedelta(days=FORGET_AFTER_DAYS)).isoformat()
    state["seen"] = {key: dates for key, dates in seen.items() if dates["last"] >= cutoff}
    state["last_run"] = now.isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="write the page only; no email, no state change")
    args = parser.parse_args()
    try:
        run(args.dry_run)
    except Exception as error:
        log(f"FAILED: {error}")
        if not args.dry_run:
            try:
                env, recipients, ready = email_settings(load_json(CONFIG_FILE, {}) or {})
                if ready:
                    body = f"The daily PPRA tender check could not finish:\n\n{error}\n"
                    send_email(env, recipients, "PPRA tender alert failed today", body, f"<pre>{html.escape(body)}</pre>")
            except Exception as mail_error:
                log(f"could not send the failure email either: {mail_error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
