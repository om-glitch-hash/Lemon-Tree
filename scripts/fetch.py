#!/usr/bin/env python3
"""Pull competitor newsroom items into data/news.json and clinical trials into data/trials.json.

News: each company gets a source chosen in this order:
  1. `rss` from companies.yaml, if set
  2. `scrape` from companies.yaml: the newsroom page itself, for newsrooms with no RSS feed
  3. An RSS/Atom feed found on the company's newsroom page or homepage
  4. Common feed paths (/feed, /rss, Q4 and GCS investor-site feeds, ...)
  5. Google News RSS limited to the company's own site (site:domain)
  6. A Google News search on the company name (small companies, or no website listed)

Trials: every asset in the `assets:` section is searched on ClinicalTrials.gov (API v2).
New trials and changes to status, phase or primary completion date are logged.

Feed choices are cached in data/feeds.json and re-checked weekly or when a source breaks.
Source health goes to data/status.json (shown on the dashboard's Sources tab).
"""
from __future__ import annotations

import calendar
import hashlib
import html
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NEWS_FILE = DATA / "news.json"
FEEDS_FILE = DATA / "feeds.json"
STATUS_FILE = DATA / "status.json"
TRIALS_FILE = DATA / "trials.json"

KEEP_DAYS = 365          # drop news items older than this
REDISCOVER_DAYS = 7      # re-check feed choice this often
STALE_DAYS = 120         # an auto-discovered RSS feed whose newest item is older than this is treated as dead
TIMEOUT = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 CI-newsroom-tracker")
COMMON_PATHS = [
    "/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
    "/news/feed", "/news/rss", "/press-releases/rss", "/news-releases/rss",
    "/rss/news-releases.xml", "/rss/pressrelease.aspx",          # GCS / Q4 investor sites
    "/news-releases?format=rss", "/media/press-releases/rss",
    "/en/rss", "/en/news/rss",
]
# Evergreen pages that Google News sometimes returns for site: searches
DEFAULT_EXCLUDE = ["our pipeline", "product development pipeline", "careers", "job search",
                   "contact us", "investor updates", "cookie", "privacy"]

CTGOV = "https://clinicaltrials.gov/api/v2/studies"
TRIAL_FIELDS = ",".join([
    "NCTId", "BriefTitle", "Acronym", "OverallStatus", "Phase", "LeadSponsorName",
    "Condition", "StartDate", "PrimaryCompletionDate", "LastUpdatePostDate",
    "StudyFirstPostDate", "EnrollmentCount", "InterventionName", "InterventionOtherName",
])
TRIAL_WATCH = {"status": "Status", "phase": "Phase", "primary_completion": "Primary completion"}

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "*/*"})

now = datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def get(url: str, **kw) -> requests.Response | None:
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True, **kw)
        if r.status_code < 400:
            return r
    except requests.RequestException:
        pass
    return None


# ---------------------------------------------------------------- news sources

def parse_feed(url: str):
    """Return a parsed feed with entries, or None."""
    r = get(url)
    if not r:
        return None
    ctype = r.headers.get("content-type", "")
    body = r.content
    if "html" in ctype and b"<rss" not in body[:2000] and b"<feed" not in body[:2000]:
        return None
    fp = feedparser.parse(body)
    if fp.entries:
        return fp
    return None


def google_news_url(query: str) -> str:
    return ("https://news.google.com/rss/search?q=" + quote_plus(query)
            + "&hl=en-US&gl=US&ceid=US:en")


def entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


def is_fresh(fp) -> bool:
    times = [t for t in (entry_time(e) for e in fp.entries) if t]
    return bool(times) and max(times) > now - timedelta(days=STALE_DAYS)


def or_terms(words: list[str] | None, limit: int = 12) -> str:
    if not words:
        return ""
    return " (" + " OR ".join(f'"{w}"' for w in words[:limit]) + ")"


def name_query(company: dict) -> str:
    if company.get("search"):
        return company["search"]
    base = re.split(r" \(| / ", company["name"])[0].strip()
    return f'"{base}"' + or_terms(company.get("keywords"))


# ---- newsroom page scraping (for newsrooms without RSS)

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
MON3 = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
DATE_PATTERNS = [
    (re.compile(r"\b(20\d\d)[-/.](\d{1,2})[-/.](\d{1,2})\b"), "ymd"),
    (re.compile(rf"\b({MONTHS}|{MON3})\.? (\d{{1,2}}),? (20\d\d)\b"), "mdy_name"),
    (re.compile(rf"\b(\d{{1,2}}) ({MONTHS}|{MON3})\.?,? (20\d\d)\b"), "dmy_name"),
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d\d)\b"), "mdy_dots"),      # Pfizer: 11.18.2026
]
MONTH_NUM = {m.lower()[:3]: i for i, m in enumerate(MONTHS.split("|"), 1)}


def find_date(text: str) -> datetime | None:
    for rx, kind in DATE_PATTERNS:
        m = rx.search(text or "")
        if not m:
            continue
        try:
            if kind == "ymd":
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
            elif kind == "mdy_name":
                mo, d, y = MONTH_NUM[m[1].lower()[:3]], int(m[2]), int(m[3])
            elif kind == "dmy_name":
                d, mo, y = int(m[1]), MONTH_NUM[m[2].lower()[:3]], int(m[3])
            else:
                mo, d, y = int(m[1]), int(m[2]), int(m[3])
            dt = datetime(y, mo, d, 12, 0, tzinfo=timezone.utc)
            if dt <= now + timedelta(days=2):
                return dt
        except (ValueError, KeyError):
            continue
    return None


def slug_title(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1] if path else url
    slug = re.sub(r"\.(html?|aspx|php)$", "", slug)
    slug = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}$", "", slug)
    return re.sub(r"[-_]+", " ", slug).strip().capitalize()


def jsonld_articles(soup: BeautifulSoup, base: str) -> list[dict]:
    out = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(x in ("NewsArticle", "Article", "PressRelease", "BlogPosting") for x in types) \
                    and o.get("url") and (o.get("headline") or o.get("name")):
                dt = None
                if o.get("datePublished"):
                    try:
                        dt = datetime.fromisoformat(str(o["datePublished"]).replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        dt = find_date(str(o["datePublished"]))
                out.append({"title": o.get("headline") or o.get("name"), "summary": o.get("description", ""),
                            "link": urljoin(base, o["url"]), "published": dt, "outlet": ""})
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(s.string or s.get_text() or "null"))
        except (ValueError, TypeError):
            continue
    return out


def scrape_page(cfg: dict) -> list[dict]:
    """Read press-release links (title + date) straight from a newsroom page."""
    if isinstance(cfg, str):
        cfg = {"url": cfg}
    url = cfg["url"]
    r = get(url)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")

    found = jsonld_articles(soup, r.url)
    if len(found) >= 3:
        return found[:40]

    custom = cfg.get("pattern")
    pattern = re.compile(custom or r"(news|press|release|media)[^?#]*/[^/?#]{12,}", re.I)
    entries, seen = [], set()
    for a in soup.find_all("a", href=True):
        link = re.sub(r"(%20)+$", "", urljoin(r.url, a["href"].strip()).strip())
        if not pattern.search(link) or link.rstrip("/") == url.rstrip("/") or link in seen:
            continue
        if not custom and "?" in link:          # filter/tag links, not articles
            continue
        inner = a.find(["h1", "h2", "h3", "h4", "h5", "h6"])      # card-style links wrap a heading
        title = re.sub(r"\s+", " ", (inner or a).get_text(" ")).strip()
        node, date = a, find_date(link) or (find_date(a.get_text(" ", strip=True)[:300]) if inner else None)
        for _ in range(5):                       # look around the link for a heading and a date
            node = node.parent
            if node is None:
                break
            if len(title) < 15:
                h = node.find(["h2", "h3", "h4", "h5"])
                if h:
                    title = re.sub(r"\s+", " ", h.get_text(" ")).strip()
            if not date:
                date = find_date(node.get_text(" ", strip=True)[:400])
            if len(title) >= 15 and date:
                break
        if len(title) < 15:
            title = slug_title(link)
        if len(title) < 15 or re.fullmatch(r"(read|learn|view|see) more.*", title, re.I):
            continue
        seen.add(link)
        entries.append({"title": title, "summary": "", "link": link, "published": date, "outlet": ""})
    return entries[:40]


def feed_entries(fp) -> list[dict]:
    out = []
    for e in fp.entries[:60]:
        src = e["source"].get("title", "") if isinstance(e.get("source"), dict) else ""
        out.append({"title": e.get("title", ""), "summary": e.get("summary", "") or e.get("description", ""),
                    "link": e.get("link", ""), "published": entry_time(e), "outlet": src})
    return out


def fetch_entries(choice: dict) -> list[dict] | None:
    if choice.get("scrape"):
        entries = scrape_page(choice["scrape"])
        return entries or None
    fp = parse_feed(choice["feed"])
    return feed_entries(fp) if fp else None


def discover(company: dict) -> dict:
    """Pick the best working news source for a company, in priority order."""
    site = company.get("site")
    if company.get("rss"):
        return {"feed": company["rss"], "method": "newsroom rss (configured)", "filter": True}
    if company.get("scrape"):
        cfg = company["scrape"]
        url = cfg if isinstance(cfg, str) else cfg["url"]
        if scrape_page(cfg):
            return {"feed": url, "scrape": cfg, "method": "newsroom page (scraped)", "filter": True}

    if site:
        parsed = urlparse(site)
        base = f"{parsed.scheme}://{parsed.netloc}"
        pages = [site] if site.rstrip("/") == base else [site, base]
        candidates: list[str] = []
        for page in pages:
            r = get(page)
            if not r:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
                t = (link.get("type") or "").lower()
                if "rss" in t or "atom" in t:
                    candidates.append(urljoin(r.url, link.get("href", "")))
            for a in soup.find_all("a", href=True):
                h = a["href"]
                if re.search(r"(rss|/feed\b|\.xml$|atom)", h, re.I) and "sitemap" not in h.lower():
                    candidates.append(urljoin(r.url, h))
        site_path = site.rstrip("/")
        candidates += [site_path + p for p in ("/feed", "/rss", "/feed/")]
        candidates += [base + p for p in COMMON_PATHS]

        seen = set()
        for c in candidates:
            if c in seen or c.startswith(("mailto:", "javascript:")):
                continue
            seen.add(c)
            if re.search(r"comments/feed|/tag/|/author/|/category/", c):
                continue
            fp = parse_feed(c)
            if fp and is_fresh(fp):          # skip dead feeds (e.g. an old blog feed)
                return {"feed": c, "method": "newsroom rss", "filter": True}

        domain = parsed.netloc.removeprefix("www.")
        parts = domain.split(".")
        if len(parts) > 2 and parts[0] in {"ir", "investor", "investors", "news", "media"}:
            domain = ".".join(parts[1:])
        gn = google_news_url(f"site:{domain}" + or_terms(company.get("keywords")))
        if parse_feed(gn):
            return {"feed": gn, "method": "newsroom via Google News (site:)", "filter": False}
        # Small companies' own sites often aren't indexed by Google News: search the
        # company name instead, which picks up their wire releases (GlobeNewswire etc.)
        return {"feed": google_news_url(name_query(company)),
                "method": "news search (newsroom not indexed)", "filter": False}

    return {"feed": google_news_url(name_query(company)),
            "method": "news search (no newsroom found)", "filter": False}


def clean(text: str, limit: int = 320) -> str:
    text = BeautifulSoup(html.unescape(text or ""), "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def matches(keywords: list[str] | None, text: str) -> bool:
    if not keywords:
        return True
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def process(company: dict, cache: dict) -> tuple[list[dict], dict]:
    name = company["name"]
    cached = cache.get(name)
    sig = json.dumps([company.get(k) for k in ("site", "rss", "scrape", "search", "keywords")])
    fresh = (cached and cached.get("sig") == sig
             and datetime.fromisoformat(cached["checked"].replace("Z", "+00:00"))
             > now - timedelta(days=REDISCOVER_DAYS))
    reused = bool(fresh)
    choice = dict(cached) if fresh else discover(company)

    entries = fetch_entries(choice)
    if entries is None and reused:            # cached source broke: rediscover once
        choice, reused = discover(company), False
        entries = fetch_entries(choice)

    choice.update({"sig": sig, "checked": choice["checked"] if reused else iso(now)})
    cache[name] = choice

    status = {"company": name, "site": company.get("site"), "feed": choice["feed"],
              "method": choice["method"], "ok": entries is not None, "items": 0,
              "latest": None, "verify": bool(company.get("verify")),
              "checked": iso(now)}
    items = []
    if entries is None:
        status["error"] = "page unreadable or no links found" if choice.get("scrape") else "feed unreachable or empty"
        return items, status

    exclude = [x.lower() for x in (company.get("exclude") or []) + DEFAULT_EXCLUDE]
    for e in entries:
        title = clean(e["title"], 300)
        summary = clean(e["summary"])
        if not title or any(x in title.lower() for x in exclude):
            continue
        # Google News queries already include the keywords; filter the company's own feeds/pages here
        if choice.get("filter", True) and not matches(company.get("keywords"), f"{title} {summary}"):
            continue
        published = e["published"]
        if published and published < now - timedelta(days=KEEP_DAYS):
            continue
        items.append({
            "id": hashlib.sha1(f"{name}|{title.lower()}".encode()).hexdigest()[:16],
            "company": name,
            "title": title,
            "summary": summary if summary.lower() != title.lower() else "",
            "link": e["link"],
            "published": iso(published) if published else None,
            "outlet": e["outlet"],
            "method": choice["method"],
        })
    status["items"] = len(items)
    dates = [i["published"] for i in items if i["published"]]
    status["latest"] = max(dates) if dates else None
    return items, status


# ---------------------------------------------------------------- clinical trials

def ctgov_search(terms: list[str]) -> list[dict] | None:
    """All studies whose intervention matches any of the terms. None if the API failed."""
    query = " OR ".join(f'"{t}"' if " " in t else t for t in terms)
    studies, token = [], None
    for _ in range(5):                                   # up to 5 pages of 100
        params = {"query.intr": query, "pageSize": 100, "fields": TRIAL_FIELDS, "format": "json"}
        if token:
            params["pageToken"] = token
        r = get(CTGOV, params=params, headers={"Accept": "application/json"})
        if not r:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        studies += data.get("studies", [])
        token = data.get("nextPageToken")
        if not token:
            break
    return studies


def norm_trial(study: dict) -> dict:
    p = study.get("protocolSection", {})
    ident, stat = p.get("identificationModule", {}), p.get("statusModule", {})
    design, spons = p.get("designModule", {}), p.get("sponsorCollaboratorsModule", {})
    phases = design.get("phases") or []
    nice_phase = "/".join(x.replace("EARLY_PHASE1", "Early Ph1").replace("PHASE", "Ph") for x in phases) or "N/A"
    return {
        "nct": ident.get("nctId"),
        "title": ident.get("briefTitle", ""),
        "acronym": ident.get("acronym", ""),
        "status": stat.get("overallStatus", "").replace("_", " ").capitalize(),
        "phase": nice_phase,
        "sponsor": spons.get("leadSponsor", {}).get("name", ""),
        "conditions": p.get("conditionsModule", {}).get("conditions", [])[:6],
        "start": stat.get("startDateStruct", {}).get("date"),
        "primary_completion": stat.get("primaryCompletionDateStruct", {}).get("date"),
        "first_posted": stat.get("studyFirstPostDateStruct", {}).get("date"),
        "last_update": stat.get("lastUpdatePostDateStruct", {}).get("date"),
        "enrollment": design.get("enrollmentInfo", {}).get("count"),
    }


def update_trials(assets: list[dict], stamp: str) -> dict:
    old = load_json(TRIALS_FILE, {"trials": {}})
    trials: dict = old.get("trials", {})
    first_run = not trials
    asset_status, found_now = {}, {}

    def run(a):
        return a, ctgov_search(a.get("terms") or [a["name"]])

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, assets))

    for a, studies in results:
        ok = studies is not None
        asset_status[a["name"]] = {"company": a.get("company"), "terms": a.get("terms") or [a["name"]],
                                   "ok": ok, "count": len(studies or []), "checked": stamp}
        if not ok:
            # API failed for this asset: keep its trials as they were
            for t in trials.values():
                if a["name"] in t.get("assets", []):
                    found_now.setdefault(t["nct"], set()).add(a["name"])
            continue
        for s in studies:
            t = norm_trial(s)
            if not t["nct"]:
                continue
            found_now.setdefault(t["nct"], set()).add(a["name"])
            prev = trials.get(t["nct"])
            if prev is None:
                t.update({"first_seen": None if first_run else stamp,
                          "changed": None if first_run else stamp,
                          "events": [] if first_run else [{"date": stamp, "type": "new", "text": "New trial listed"}]})
                trials[t["nct"]] = t
                continue
            events = prev.get("events", [])
            for field, label in TRIAL_WATCH.items():
                if t[field] and prev.get(field) and t[field] != prev.get(field):
                    events.append({"date": stamp, "type": field, "text": f"{label}: {prev.get(field)} → {t[field]}"})
                    prev["changed"] = stamp
            for k, v in t.items():
                prev[k] = v
            prev["events"] = events[-10:]

    for nct, names in found_now.items():
        trials[nct]["assets"] = sorted(names)
    for nct, t in trials.items():
        t["listed"] = nct in found_now          # False = no longer returned by the search

    ok = sum(1 for s in asset_status.values() if s["ok"])
    out = {"updated": stamp, "assets": asset_status, "trials": trials}
    TRIALS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": ok, "total": len(asset_status), "trials": len(trials)}


# ---------------------------------------------------------------- main

def main() -> int:
    DATA.mkdir(exist_ok=True)
    config = yaml.safe_load((ROOT / "companies.yaml").read_text(encoding="utf-8"))
    companies = config["companies"]
    assets = config.get("assets") or []
    cache = load_json(FEEDS_FILE, {})
    old = load_json(NEWS_FILE, {"items": []})
    existing = {i["id"]: i for i in old.get("items", [])}
    first_run = not existing

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda c: process(c, cache), companies))

    stamp = iso(now)
    new_count = 0
    for items, _ in results:
        for it in items:
            if it["id"] in existing:
                # keep first_seen; refresh link/summary in case they changed
                existing[it["id"]].update({k: it[k] for k in ("link", "summary", "published") if it[k]})
            else:
                it["first_seen"] = None if first_run else stamp   # backfill isn't "new"
                existing[it["id"]] = it
                new_count += 0 if first_run else 1

    cutoff = iso(now - timedelta(days=KEEP_DAYS))
    known = {c["name"] for c in companies}
    items = [i for i in existing.values()
             if i["company"] in known and (i["published"] or i.get("first_seen") or stamp) >= cutoff]
    items.sort(key=lambda i: i["published"] or i.get("first_seen") or "", reverse=True)

    meta = {c["name"]: {"site": c.get("site"), "lists": c.get("lists", []),
                        "keywords": c.get("keywords")} for c in companies}
    asset_meta = [{"name": a["name"], "company": a.get("company"), "terms": a.get("terms") or [a["name"]]}
                  for a in assets]
    NEWS_FILE.write_text(json.dumps({"updated": stamp, "companies": meta, "assets": asset_meta,
                                     "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    FEEDS_FILE.write_text(json.dumps(cache, indent=1), encoding="utf-8")

    trial_status = None
    if assets:
        try:
            trial_status = update_trials(assets, stamp)
        except Exception as exc:                      # never let trials break the news update
            trial_status = {"ok": 0, "total": len(assets), "error": str(exc)[:200]}

    STATUS_FILE.write_text(json.dumps({"updated": stamp, "sources": [s for _, s in results],
                                       "trials": trial_status}, indent=1), encoding="utf-8")
    ok = sum(1 for _, s in results if s["ok"])
    print(f"{ok}/{len(results)} news sources OK, {len(items)} items, {new_count} new")
    if trial_status:
        print(f"trials: {trial_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
