#!/usr/bin/env python3
"""
jobwatch: get pinged within the hour when a company you like posts a mid-level SWE role.

Polls the public job-board APIs that most tech companies post through
(Greenhouse, Lever, Ashby), filters by title / level / location / freshness,
skips anything already seen (including reposts under a new ID), and sends a
push notification via ntfy (and optionally an email digest).

Standard library only. Python 3.11+.

Usage:
    python jobwatch.py              # normal run (sends notifications, updates seen.json)
    python jobwatch.py --dry-run    # print matches, don't notify, don't save state
    python jobwatch.py --list       # print every currently-open matching role (ignores seen/age)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.toml"
STATE_PATH = HERE / "seen.json"
USER_AGENT = "jobwatch/1.0 (personal job alert script)"
NOW = datetime.now(timezone.utc)
DEADLINE = time.time() + float(os.environ.get("JOBWATCH_BUDGET_SECONDS", 25 * 60))  # stop fetching after this


# --------------------------------------------------------------------------- data


@dataclass
class Job:
    id: str            # globally unique: "<ats>:<slug>:<job id>"
    company: str
    title: str
    location: str
    url: str
    posted: datetime | None
    remote: bool = False
    pay: tuple[int, int] | None = None   # annual base range in USD, if the posting lists one
    size: str = "large"                  # startup | growth | large (from config)
    rating: float | None = None
    desc: str = ""                       # posting text, used to read the years-of-experience requirement
    stretch: bool = False                # senior title, but the posting asks for few years
    fit: int = 0                         # resume-fit score (see [fit] in config)

    @property
    def years(self) -> float | None:
        return parse_years(self.desc)

    def pay_text(self) -> str:
        if not self.pay:
            return ""
        lo, hi = self.pay
        k = lambda n: f"${round(n / 1000)}K"
        return k(lo) if lo == hi else f"{k(lo)}–{k(hi)}"

    @property
    def repost_key(self) -> str:
        """Same company + same title + same location = probably the same role re-posted."""
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
        return f"{norm(self.company)}|{norm(self.title)}|{norm(self.location)}"

    def age_text(self) -> str:
        if not self.posted:
            return "posting date unknown"
        hours = (NOW - self.posted).total_seconds() / 3600
        if hours < 1:
            return "posted <1h ago"
        if hours < 48:
            return f"posted {int(hours)}h ago"
        return f"posted {int(hours // 24)}d ago"


# --------------------------------------------------------------------------- fetching


def _open_json(req: urllib.request.Request, retries: int = 3):
    """Open a request and parse JSON; retry timeouts, 5xx, and 429 rate limits with backoff."""
    for attempt in range(retries + 1):
        if time.time() > DEADLINE:
            raise TimeoutError("run time budget used up; skipping the rest of this board")
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if attempt == retries or not (e.code == 429 or e.code >= 500):
                raise
            try:
                wait = int(e.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0
            time.sleep(min(wait or 5 * (attempt + 1), 30))
        except (TimeoutError, urllib.error.URLError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_json(url: str):
    return _open_json(urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}))


def post_json(url: str, payload: dict):
    return _open_json(urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"},
    ))


def parse_time(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):  # Lever uses epoch milliseconds
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s?[kK])")


def parse_pay(text: str | None) -> tuple[int, int] | None:
    """Pull an annual USD salary range like '$133,000 - $213,000' or '$150K – $200K' out of text."""
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(text)))
    vals = []
    for m in _MONEY.finditer(text):
        raw = m.group(1).replace(",", "").replace(" ", "")
        n = float(raw[:-1]) * 1000 if raw[-1] in "kK" else float(raw)
        if 40_000 <= n <= 700_000:   # plausible annual base salary; skips hourly, stipends, etc.
            vals.append(int(n))
        if len(vals) == 2:
            break
    if not vals:
        return None
    return (min(vals), max(vals))


_YEARS = re.compile(
    r"(?<![\d$.,])(\d{1,2}(?:\.\d)?)\s*(?:\+|(?:-|–|to)\s*\d{1,2})?\s*\+?\s*(?:or more\s+)?years?",
    re.I,
)


def parse_years(text: str | None) -> float | None:
    """Smallest 'N+ years' experience requirement in the posting, or None if it doesn't say."""
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(text)))
    found = []
    for m in _YEARS.finditer(text):
        window = text[m.start(): m.end() + 80].lower()
        if "experience" in window or "industry" in window or "professional" in window:
            n = float(m.group(1))
            if 0 < n <= 15:
                found.append(n)
    return min(found) if found else None


def fetch_greenhouse(company: str, slug: str, filters=None) -> list[Job]:
    host = "boards-api.greenhouse.io"
    if slug.startswith("eu:"):          # EU-hosted boards (job-boards.eu.greenhouse.io)
        host, slug = "boards-api.eu.greenhouse.io", slug[3:]
    data = fetch_json(f"https://{host}/v1/boards/{slug}/jobs?content=true")
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") or ""
        jobs.append(Job(
            id=f"gh:{slug}:{j['id']}",
            company=company,
            title=j.get("title", "").strip(),
            location=loc,
            url=j.get("absolute_url", ""),
            # first_published = original publish date; updated_at changes on every edit
            posted=parse_time(j.get("first_published")) or parse_time(j.get("updated_at")),
            remote="remote" in loc.lower(),
            pay=parse_pay(j.get("content")),
            desc=j.get("content") or "",
        ))
    return jobs


def fetch_lever(company: str, slug: str, filters=None) -> list[Job]:
    data = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = [cats.get("location") or ""] + [l for l in (cats.get("allLocations") or []) if l]
        loc = " / ".join(dict.fromkeys(l for l in locs if l))
        jobs.append(Job(
            id=f"lever:{slug}:{j['id']}",
            company=company,
            title=(j.get("text") or "").strip(),
            location=loc,
            url=j.get("hostedUrl", ""),
            posted=parse_time(j.get("createdAt")),
            remote=(j.get("workplaceType") == "remote") or "remote" in loc.lower(),
            pay=_lever_pay(j),
            desc=" ".join([j.get("descriptionPlain") or "", j.get("additionalPlain") or ""]
                          + [(l.get("content") or "") for l in (j.get("lists") or []) if isinstance(l, dict)]),
        ))
    return jobs


def _lever_pay(j: dict) -> tuple[int, int] | None:
    sr = j.get("salaryRange") or {}
    if sr.get("min") and (sr.get("currency") in (None, "USD")) and sr.get("interval", "per-year-salary") \
            in ("per-year-salary", None):
        return (int(sr["min"]), int(sr.get("max") or sr["min"]))
    return parse_pay((j.get("additionalPlain") or "") + " " + (j.get("descriptionPlain") or ""))


def fetch_ashby(company: str, slug: str, filters=None) -> list[Job]:
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [
            (s.get("location") or "") for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)
        ]
        loc = " / ".join(dict.fromkeys(l for l in locs if l))
        jobs.append(Job(
            id=f"ashby:{slug}:{j['id']}",
            company=company,
            title=(j.get("title") or "").strip(),
            location=loc,
            url=j.get("jobUrl", ""),
            posted=parse_time(j.get("publishedAt")),
            remote=bool(j.get("isRemote")) or (j.get("workplaceType") or "").lower() == "remote"
            or "remote" in loc.lower(),
            pay=parse_pay(((j.get("compensation") or {}).get("compensationTierSummary")
                           or (j.get("compensation") or {}).get("scrapeableCompensationSalarySummary")
                           or j.get("descriptionPlain"))),
            desc=j.get("descriptionPlain") or j.get("descriptionHtml") or "",
        ))
    return jobs


def _workday_posted(text: str | None) -> datetime | None:
    """Workday gives 'Posted Today', 'Posted Yesterday', 'Posted 3 Days Ago', 'Posted 30+ Days Ago'."""
    if not text:
        return None
    t = text.lower()
    if "today" in t:
        return NOW
    if "yesterday" in t:
        return NOW - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return NOW - timedelta(days=int(m.group(1)) + (1 if "+" in t else 0))
    return None


_WORKDAY_SLOTS = threading.Semaphore(3)   # Workday rate-limits when too many sites are hit at once


def fetch_workday(company: str, slug: str, filters=None, max_pages: int = 10, max_details: int = 200) -> list[Job]:
    """slug = 'tenant/wdN/Site', from a URL like https://tenant.wdN.myworkdayjobs.com/Site/job/..."""
    with _WORKDAY_SLOTS:
        return _fetch_workday(company, slug, filters, max_pages, max_details)


def _fetch_workday(company, slug, filters, max_pages, max_details) -> list[Job]:
    tenant, wd, site = slug.split("/", 2)
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}"
    # Workday search is keyword-based, so run a few searches and merge them.
    postings, seen_paths = [], set()
    for query in ("software engineer", "developer", "full stack", "backend", "frontend"):
        offset, total = 0, None
        for _ in range(max_pages):
            data = post_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": query})
            page = data.get("jobPostings") or []
            if total is None:
                total = data.get("total") or 0
            for p in page:
                if p.get("externalPath") not in seen_paths:
                    seen_paths.add(p.get("externalPath"))
                    postings.append(p)
            offset += 20
            if not page or offset >= total:
                break

    jobs, details = [], 0
    for p in postings:
        path = p.get("externalPath") or ""
        job = Job(
            id=f"workday:{tenant}:{path.rsplit('_', 1)[-1] if '_' in path else path}",
            company=company,
            title=(p.get("title") or "").strip(),
            location=p.get("locationsText") or "",
            url=f"{host}/{site}{path}",
            posted=_workday_posted(p.get("postedOn")),
        )
        # The list view says "3 Locations" and has no pay; open the posting for title matches only.
        if filters is not None and filters.title_candidate(job) and details < max_details:
            details += 1
            try:
                info = fetch_json(f"{api}{path}").get("jobPostingInfo") or {}
                locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
                job.location = " / ".join(dict.fromkeys(l for l in locs if l)) or job.location
                job.remote = "remote" in (info.get("remoteType") or "").lower() or "remote" in job.location.lower()
                job.pay = parse_pay(info.get("jobDescription"))
                job.desc = info.get("jobDescription") or ""
                job.posted = parse_time(info.get("startDate")) or job.posted
            except Exception:
                pass
        jobs.append(job)
    return jobs


def fetch_smartrecruiters(company: str, slug: str, filters=None, max_pages: int = 10, max_details: int = 150) -> list[Job]:
    """slug = the company identifier from jobs.smartrecruiters.com/<Identifier>/... (case-sensitive)."""
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    postings, offset = [], 0
    for _ in range(max_pages):
        data = fetch_json(f"{base}?limit=100&offset={offset}")
        page = data.get("content") or []
        postings.extend(page)
        offset += 100
        if not page or offset >= (data.get("totalFound") or 0):
            break

    jobs, details = [], 0
    for p in postings:
        loc = p.get("location") or {}
        country = (loc.get("country") or "").lower()
        place = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
        location = f"{place}, {'United States' if country == 'us' else country.upper()}".strip(", ")
        if loc.get("remote"):
            location = f"Remote / {location}"
        job = Job(
            id=f"sr:{slug}:{p.get('id')}",
            company=company,
            title=(p.get("name") or "").strip(),
            location=location,
            url=f"https://jobs.smartrecruiters.com/{slug}/{p.get('id')}",
            posted=parse_time(p.get("releasedDate")),
            remote=bool(loc.get("remote")),
        )
        if filters is not None and filters.title_candidate(job) and details < max_details:
            details += 1
            try:
                sections = ((fetch_json(f"{base}/{p.get('id')}").get("jobAd") or {}).get("sections") or {})
                job.desc = " ".join((v or {}).get("text") or "" for v in sections.values() if isinstance(v, dict))
                job.pay = parse_pay(job.desc)
            except Exception:
                pass
        jobs.append(job)
    return jobs


def fetch_eightfold(company: str, slug: str, filters=None, max_details: int = 150) -> list[Job]:
    """slug = 'tenant|domain', e.g. 'aexp|aexp.com' for https://aexp.eightfold.ai/careers?domain=aexp.com"""
    tenant, domain = slug.split("|", 1)
    host = tenant if "." in tenant else f"{tenant}.eightfold.ai"   # some companies use their own domain
    base = f"https://{host}/api/apply/v2/jobs"
    postings, seen = [], set()
    for query in ("software engineer", "developer", "full stack"):
        start = 0
        for _ in range(15):
            q = urllib.parse.urlencode({"domain": domain, "start": start, "num": 50, "query": query, "sort_by": "relevance"})
            data = fetch_json(f"{base}?{q}")
            page = data.get("positions") or []
            for p in page:
                if p.get("id") not in seen:
                    seen.add(p.get("id"))
                    postings.append(p)
            start += len(page)
            if not page or start >= (data.get("count") or 0):
                break

    jobs, details = [], 0
    for p in postings:
        locs = p.get("locations") or [p.get("location") or ""]
        loc = " / ".join(dict.fromkeys(l for l in locs if l))
        wlo = (p.get("work_location_option") or "").lower()
        job = Job(
            id=f"eightfold:{tenant}:{p.get('id')}",
            company=company,
            title=(p.get("name") or "").strip(),
            location=loc,
            url=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p.get('id')}?domain={domain}",
            posted=parse_time(p.get("t_create")) if not isinstance(p.get("t_create"), (int, float))
                   else datetime.fromtimestamp(p["t_create"], tz=timezone.utc),
            remote="remote" in wlo or "remote" in loc.lower(),
        )
        if filters is not None and filters.title_candidate(job) and details < max_details:
            details += 1
            try:
                d = fetch_json(f"{base}/{p.get('id')}?domain={urllib.parse.quote(domain)}")
                job.desc = d.get("job_description") or ""
                job.pay = parse_pay(job.desc)
            except Exception:
                pass
        jobs.append(job)
    return jobs


def fetch_oracle(company: str, slug: str, filters=None, max_details: int = 150) -> list[Job]:
    """Oracle Recruiting Cloud. slug = 'host/siteNumber', from a URL like
    https://<host>/hcmUI/CandidateExperience/en/sites/<siteNumber>/job/<id>"""
    host, site = slug.split("/", 1)
    api = f"https://{host}/hcmRestApi/resources/latest"
    reqs, seen = [], set()
    for query in ("software engineer", "developer"):
        offset = 0
        for _ in range(12):
            finder = (f'findReqs;siteNumber={site},limit=25,offset={offset},'
                      f'keyword="{query}",sortBy=POSTING_DATES_DESC')
            url = (f"{api}/recruitingCEJobRequisitions?onlyData=true"
                   f"&expand=requisitionList.secondaryLocations&finder={urllib.parse.quote(finder, safe='=;,')}")
            items = (fetch_json(url).get("items") or [{}])
            page = items[0].get("requisitionList") or []
            total = items[0].get("TotalJobsCount") or 0
            for r in page:
                if r.get("Id") not in seen:
                    seen.add(r.get("Id"))
                    reqs.append(r)
            offset += 25
            if not page or offset >= total:
                break

    jobs, details = [], 0
    for r in reqs:
        locs = [r.get("PrimaryLocation") or ""] + [
            (l.get("Name") or "") for l in (r.get("secondaryLocations") or []) if isinstance(l, dict)]
        loc = " / ".join(dict.fromkeys(l for l in locs if l))
        job = Job(
            id=f"oracle:{host}:{r.get('Id')}",
            company=company,
            title=(r.get("Title") or "").strip(),
            location=loc,
            url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r.get('Id')}",
            posted=parse_time(r.get("PostedDate")),
            remote="remote" in (r.get("WorkplaceType") or "").lower() or "remote" in loc.lower(),
        )
        if filters is not None and filters.title_candidate(job) and details < max_details:
            details += 1
            try:
                finder = f'ById;Id="{r.get("Id")}",siteNumber={site}'
                d = (fetch_json(f"{api}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                                f"&finder={urllib.parse.quote(finder, safe='=;,')}").get("items") or [{}])[0]
                job.desc = " ".join(d.get(k) or "" for k in
                                    ("ExternalDescriptionStr", "ExternalQualificationsStr", "ExternalResponsibilitiesStr"))
                job.pay = parse_pay(job.desc)
            except Exception:
                pass
        jobs.append(job)
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workday": fetch_workday, "smartrecruiters": fetch_smartrecruiters,
            "eightfold": fetch_eightfold, "oracle": fetch_oracle}


# --------------------------------------------------------------------------- filtering


def compile_any(patterns: list[str]) -> re.Pattern | None:
    patterns = [p for p in patterns if p]
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None


class Filters:
    def __init__(self, cfg: dict):
        f = cfg["filters"]
        self.title_include = compile_any(f["title_include"])
        self.title_exclude = compile_any(f["title_exclude"])
        self.loc_local = compile_any(f["locations_local"])
        self.allow_remote = f.get("allow_remote_us", True)
        self.loc_us = compile_any(f.get("us_markers", []))
        self.loc_non_us = compile_any(f["non_us_markers"])
        self.loc_exclude = compile_any(f.get("locations_exclude", []))
        self.max_age = timedelta(days=f.get("max_age_days", 3))
        self.stretch_title = compile_any(f.get("stretch_titles", []))
        self.stretch_max_years = f.get("stretch_max_years", 3)
        self.max_years = f.get("max_years_required", 4)

    def title_candidate(self, job: Job) -> bool:
        """Engineering title that isn't ruled out outright (senior titles still need a years check)."""
        if self.title_include and not self.title_include.search(job.title):
            return False
        return not (self.title_exclude and self.title_exclude.search(job.title))

    def title_ok(self, job: Job) -> bool:
        """Title alone is enough: a candidate that isn't a 'stretch' (senior) title."""
        return self.title_candidate(job) and not (self.stretch_title and self.stretch_title.search(job.title))

    def level_ok(self, job: Job) -> bool:
        """Use the posting's experience requirement: allow senior titles that ask for few years,
        drop non-senior titles that ask for a lot."""
        if not self.title_candidate(job):
            return False
        yrs = job.years
        if self.stretch_title and self.stretch_title.search(job.title):
            job.stretch = yrs is not None and yrs <= self.stretch_max_years
            return job.stretch
        return yrs is None or yrs <= self.max_years

    def location_ok(self, job: Job) -> bool:
        loc = job.location
        # "Remote / Europe / Poland / UK": every named place is outside the US, so bare "Remote" means non-US.
        if (self.loc_non_us and self.loc_non_us.search(loc)
                and not (self.loc_us and self.loc_us.search(loc))
                and not (self.loc_local and self.loc_local.search(loc))):
            return False
        # A role can list several locations ("New York / London"); one acceptable one is enough.
        for part in re.split(r"\s*(?:/|;|\||\bor\b)\s*", loc):
            if self.loc_exclude and self.loc_exclude.search(part):
                continue
            if self.loc_local and self.loc_local.search(part):
                return True
            if self.allow_remote and (job.remote or "remote" in part.lower()):
                is_non_us = bool(self.loc_non_us and self.loc_non_us.search(part))
                is_us = bool(self.loc_us and self.loc_us.search(part))
                if is_us or not is_non_us:
                    return True
        return False

    def fresh(self, job: Job) -> bool:
        return job.posted is None or (NOW - job.posted) <= self.max_age

    def matches(self, job: Job) -> bool:
        return self.level_ok(job) and self.location_ok(job)


# --------------------------------------------------------------------------- state


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        state.setdefault("ids", {})
        state.setdefault("repost_keys", {})
        return state
    return {"ids": {}, "repost_keys": {}, "first_run": True}


def save_state(state: dict, keep_days: int = 90) -> None:
    cutoff = (NOW - timedelta(days=keep_days)).isoformat()
    for k in ("ids", "repost_keys"):
        state[k] = {key: ts for key, ts in state[k].items() if ts >= cutoff}
    state.pop("first_run", None)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- notifying


def notify_ntfy(jobs: list[Job], overflow: int, cfg_topic: str | None = None) -> None:
    topic = os.environ.get("NTFY_TOPIC") or cfg_topic
    if not topic:
        print("NTFY_TOPIC not set; skipping push notifications")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    for job in jobs:
        size = {"startup": "startup", "growth": "growth-stage"}.get(job.size, "")
        body = f"{job.location or 'location n/a'}\n{job.pay_text() or 'pay not listed'} · {job.age_text()}"
        if size or job.rating:
            body += f"\n{size + ' · ' if size else ''}Glassdoor {job.rating}"
        _post(f"{server}/{topic}", body, {
            "Title": f"{job.company}: {job.title}"[:200],
            "Click": job.url,
            "Tags": "briefcase",
        })
    if overflow:
        _post(f"{server}/{topic}", f"{overflow} more new matches; check the email digest or run --list.",
              {"Title": "jobwatch: more matches", "Tags": "mag"})


def _post(url: str, body: str, headers: dict) -> None:
    safe = {k: v.encode("latin-1", "replace").decode("latin-1") for k, v in headers.items()}
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=safe, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.URLError as e:
        print(f"  ! notification failed: {e}", file=sys.stderr)


def notify_email(jobs: list[Job]) -> None:
    host, user, pw, to = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO"))
    if not (host and user and pw and to):
        return
    msg = EmailMessage()
    msg["Subject"] = f"jobwatch: {len(jobs)} new SWE role{'s' if len(jobs) != 1 else ''}"
    msg["From"], msg["To"] = user, to
    lines = [f"[{j.size}] {j.company} (Glassdoor {j.rating}): {j.title}\n  {describe(j)}\n  {j.url}\n"
             for j in jobs]
    msg.set_content("\n".join(lines))
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", 465)), timeout=20) as s:
        s.login(user, pw)
        s.send_message(msg)


# --------------------------------------------------------------------------- main


def _fetch_company(c: dict, filters: Filters):
    fetch = FETCHERS.get(c["ats"])
    if not fetch:
        return c, None, f"{c['name']}: unknown ats '{c['ats']}'"
    try:
        return c, fetch(c["name"], c["slug"], filters=filters), None
    except urllib.error.HTTPError as e:
        hint = " (board slug is probably wrong; check the company's careers page URL)" if e.code == 404 else ""
        return c, None, f"{c['name']}: HTTP {e.code}{hint}"
    except Exception as e:  # network blips shouldn't kill the whole run
        return c, None, f"{c['name']}: {type(e).__name__}: {e}"


def collect(cfg: dict, filters: Filters):
    """Fetch every active company's board once (in parallel). Returns matches, errors, and all jobs per company."""
    from concurrent.futures import ThreadPoolExecutor
    min_rating = cfg.get("min_rating", 0)
    active = [c for c in cfg["companies"] if c.get("rating") is None or c["rating"] >= min_rating]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda c: _fetch_company(c, filters), active))
    matches, errors, by_company = [], [], []
    for c, jobs, err in results:
        if err:
            errors.append(err)
            continue
        hits = [j for j in jobs if filters.matches(j)]
        for j in jobs:
            j.size, j.rating = c.get("size", "large"), c.get("rating")
        print(f"{c['name']:<20} {len(jobs):>4} open, {len(hits):>3} match")
        matches.extend(hits)
        by_company.append((c, jobs))
    return matches, errors, by_company


def score_fit(job: Job, fit_cfg: dict) -> int:
    text = job.title + " " + re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(job.desc or "")))
    return sum(w for pat, w in fit_cfg.get("keywords", []) if re.search(pat, text, re.I))


def best_fits_report(matches: list[Job], fit_cfg: dict) -> str:
    days = fit_cfg.get("recent_days", 21)
    recent = [j for j in matches if j.posted and (NOW - j.posted).days <= days]
    recent.sort(key=lambda j: (-j.fit, -(j.posted.timestamp())))
    out = [f"# Best resume fits posted in the last {days} days ({len(recent)} roles), highest fit first\n"]
    for j in recent[:60]:
        out.append(f"- {j.company}: {j.title} | {describe(j)}\n  {j.url}")
    return "\n".join(out) + "\n"


SIZE_ORDER = {"startup": 0, "growth": 1, "large": 2}


def rank(job: Job):
    """Smaller companies first, then higher posted pay, then higher rating, then newest."""
    return (
        SIZE_ORDER.get(job.size, 2),
        -(job.pay[1] if job.pay else 0),
        -(job.rating or 0),
        -(job.posted.timestamp() if job.posted else NOW.timestamp()),
    )


def describe(job: Job) -> str:
    bits = [f"fit {job.fit}", job.location or "location n/a"] if job.fit else [job.location or "location n/a"]
    if job.pay:
        bits.append(job.pay_text())
    if job.years:
        bits.append(f"asks {job.years:g}+ yrs" + (" (stretch: senior title)" if job.stretch else ""))
    bits.append(job.age_text())
    return " | ".join(bits)


def rejected_report(by_company, filters: Filters) -> str:
    """Every engineering-ish role that was filtered out, and why (for tuning the filters)."""
    out = []
    for c, jobs in by_company:
        rows = []
        for j in jobs:
            if not re.search(r"engineer|developer", j.title, re.I) or filters.matches(j):
                continue
            if filters.title_exclude and filters.title_exclude.search(j.title):
                why = "title excluded: " + filters.title_exclude.search(j.title).group(0)
            elif not filters.title_candidate(j):
                why = "title not in include list"
            elif not filters.level_ok(j):
                why = f"experience: asks {j.years:g}+ yrs" if j.years else "senior title, years not stated"
            else:
                why = "location"
            rows.append(f"  - {j.title} | {j.location} | {why}")
        out.append(f"## {c['name']}: {len(rows)} engineering roles rejected")
        out.extend(rows[:60])
    return "\n".join(out) + "\n"


def open_roles_report(matches: list[Job]) -> str:
    return "".join(f"- [{j.size}] {j.company}: {j.title} | {describe(j)}\n  {j.url}\n"
                   for j in sorted(matches, key=rank))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't notify or save state")
    ap.add_argument("--list", action="store_true", help="show all current matches, ignoring seen/age")
    ap.add_argument("--rejected", action="store_true", help="show engineering roles the filters rejected, and why")
    args = ap.parse_args()

    cfg = tomllib.loads(CONFIG_PATH.read_text())
    filters = Filters(cfg)

    matches, errors, by_company = collect(cfg, filters)
    for j in matches:
        j.fit = score_fit(j, cfg.get("fit", {}))

    if args.rejected:
        print(rejected_report(by_company, filters), end="")
        return 0
    if args.list:
        print(open_roles_report(matches), end="")
        for e in errors:
            print(f"! {e}", file=sys.stderr)
        return 0
    if not args.dry_run:
        (HERE / "open_roles.txt").write_text(open_roles_report(matches))
        (HERE / "rejected_roles.txt").write_text(rejected_report(by_company, filters))
        (HERE / "best_fits.txt").write_text(best_fits_report(matches, cfg.get("fit", {})))

    state = load_state()
    stamp = NOW.isoformat()
    new: list[Job] = []
    for j in matches:
        already_seen = j.id in state["ids"]
        is_repost = (not already_seen) and j.repost_key in state["repost_keys"]
        # Refresh timestamps for everything still open, so long-open roles never expire from state.
        state["ids"][j.id] = stamp
        state["repost_keys"][j.repost_key] = stamp
        if already_seen:
            continue
        if is_repost:
            continue  # same company + title + location seen before under another ID: a repost
        if not filters.fresh(j):
            continue  # new to us, but the posting itself is old
        if j.posted is None and state.get("first_run"):
            continue  # on the very first run, undated postings are probably old
        new.append(j)

    new.sort(key=rank)
    cap = cfg.get("max_pushes_per_run", 8)
    print(f"\n{len(new)} new match(es)")
    for j in new:
        print(f"  + [{j.size}] {j.company}: {j.title} | {describe(j)}\n    {j.url}")
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)

    if args.dry_run:
        return 0
    if new:
        notify_ntfy(new[:cap], max(0, len(new) - cap), cfg.get("ntfy_topic"))
        try:
            notify_email(new)
        except Exception as e:
            print(f"  ! email failed: {e}", file=sys.stderr)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
