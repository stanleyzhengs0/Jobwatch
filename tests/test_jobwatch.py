"""Offline tests: fake API responses in each ATS's real shape, run the full pipeline twice."""
import io
import json
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jobwatch as jw  # noqa: E402

NOW = jw.NOW
iso = lambda d: (NOW - timedelta(days=d)).isoformat()
ms = lambda d: int((NOW - timedelta(days=d)).timestamp() * 1000)

GREENHOUSE = {"jobs": [
    {"id": 1, "title": "Software Engineer II, Backend", "location": {"name": "New York, NY"},
     "absolute_url": "https://x/1", "first_published": iso(0.5), "updated_at": iso(0.1),
     "content": "&lt;p&gt;The base pay range is &lt;strong&gt;$133,000 - $213,000&lt;/strong&gt; USD.&lt;/p&gt;"},
    {"id": 2, "title": "Senior Software Engineer", "location": {"name": "New York, NY"},
     "absolute_url": "https://x/2", "first_published": iso(0.5)},
    {"id": 3, "title": "Software Engineer", "location": {"name": "London, UK"},
     "absolute_url": "https://x/3", "first_published": iso(0.5)},
    # old posting with a recent edit: should NOT alert (first_published is what counts)
    {"id": 4, "title": "Backend Engineer", "location": {"name": "Remote - US"},
     "absolute_url": "https://x/4", "first_published": iso(40), "updated_at": iso(0.2)},
    {"id": 5, "title": "Full Stack Engineer", "location": {"name": "Remote (US or Canada)"},
     "absolute_url": "https://x/5", "first_published": iso(1)},
    {"id": 6, "title": "Sales Engineer", "location": {"name": "New York, NY"},
     "absolute_url": "https://x/6", "first_published": iso(1)},
    {"id": 7, "title": "Software Engineer, Payment Solutions", "location": {"name": "Jersey City, NJ"},
     "absolute_url": "https://x/7", "first_published": iso(2)},
]}
LEVER = [
    {"id": "a", "text": "Software Engineer - Java", "categories": {"location": "San Francisco",
     "allLocations": ["San Francisco", "New York"]}, "workplaceType": "hybrid",
     "createdAt": ms(1), "hostedUrl": "https://l/a",
     "salaryRange": {"min": 150000, "max": 190000, "currency": "USD", "interval": "per-year-salary"}},
    {"id": "b", "text": "Software Engineer, Platform", "categories": {"location": "Toronto"},
     "workplaceType": "remote", "createdAt": ms(1), "hostedUrl": "https://l/b"},
    {"id": "c", "text": "Staff Backend Engineer", "categories": {"location": "Remote"},
     "workplaceType": "remote", "createdAt": ms(1), "hostedUrl": "https://l/c"},
]
ASHBY = {"jobs": [
    {"id": "p", "title": "Product Engineer", "location": "United States", "isRemote": True,
     "publishedAt": iso(0.2), "jobUrl": "https://a/p", "isListed": True,
     "compensation": {"compensationTierSummary": "$140K – $175K • Offers Equity"}},
    {"id": "q", "title": "Software Engineer", "location": "Bengaluru", "isRemote": True,
     "publishedAt": iso(0.2), "jobUrl": "https://a/q", "isListed": True},
    {"id": "r", "title": "Software Engineer", "location": "New York", "isListed": False,
     "publishedAt": iso(0.2), "jobUrl": "https://a/r"},
]}


def fake_fetch(url):
    if "greenhouse" in url:
        if "/badslug/" in url:
            raise jw.urllib.error.HTTPError(url, 404, "nf", {}, None)
        return json.loads(json.dumps(GREENHOUSE))
    if "lever" in url:
        return json.loads(json.dumps(LEVER))
    if "ashby" in url:
        return json.loads(json.dumps(ASHBY))
    raise AssertionError(url)


CFG = tomllib.loads((Path(jw.__file__).parent / "config.toml").read_text())
CFG["companies"] = [
    {"name": "GH Co", "ats": "greenhouse", "slug": "ghco", "rating": 4.0, "size": "large"},
    {"name": "Lever Co", "ats": "lever", "slug": "leverco", "rating": 4.0, "size": "growth"},
    {"name": "Ashby Co", "ats": "ashby", "slug": "ashbyco", "rating": 3.9, "size": "startup"},
    {"name": "Low Co", "ats": "greenhouse", "slug": "lowco", "rating": 3.2},
    {"name": "Bad Slug", "ats": "greenhouse", "slug": "badslug", "rating": 4.5},
]


def run(state_path, *args):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(jw, "fetch_json", side_effect=fake_fetch) as f, \
         mock.patch.object(jw, "STATE_PATH", state_path), \
         mock.patch.object(jw, "HERE", state_path.parent), \
         mock.patch.object(jw.tomllib, "loads", return_value=CFG), \
         mock.patch.object(jw, "notify_ntfy") as push, \
         mock.patch.object(sys, "argv", ["jobwatch.py", *args]), \
         redirect_stdout(out), redirect_stderr(err):
        jw.main()
    return out.getvalue(), err.getvalue(), push, f


class PipelineTest(unittest.TestCase):
    def test_first_and_second_run(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "seen.json"
            out, err, push, fetch = run(state)
            alerted = {j.id for j in push.call_args.args[0]}
            self.assertEqual(alerted, {
                "gh:ghco:1",       # SWE II, NYC, fresh
                "gh:ghco:5",       # remote US-or-Canada
                "gh:ghco:7",       # Jersey City; 'Payment Solutions' isn't a solutions-engineer role
                "lever:leverco:a", # SF + New York
                "ashby:ashbyco:p", # remote United States
            })
            # low-rated company never fetched
            self.assertFalse(any("lowco" in c.args[0] for c in fetch.call_args_list))
            self.assertIn("slug is probably wrong", err)
            self.assertTrue(state.exists())

            # second run: nothing new
            out, err, push, _ = run(state)
            push.assert_not_called()
            self.assertIn("0 new match", out)

    def test_ranking_smaller_and_higher_paid_first(self):
        with tempfile.TemporaryDirectory() as d:
            _, _, push, _ = run(Path(d) / "seen.json")
            jobs = push.call_args.args[0]
            self.assertEqual([j.id for j in jobs][:3], ["ashby:ashbyco:p", "lever:leverco:a", "gh:ghco:1"])
            self.assertEqual(jobs[0].pay, (140000, 175000))
            self.assertEqual(jobs[1].pay, (150000, 190000))
            self.assertEqual(jobs[2].pay, (133000, 213000))  # large co: first among large because it lists pay

    def test_repost_under_new_id_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "seen.json"
            run(state)
            GREENHOUSE["jobs"].append({"id": 99, "title": "Software Engineer II, Backend",
                                       "location": {"name": "New York, NY"},
                                       "absolute_url": "https://x/99", "first_published": iso(0.1)})
            GREENHOUSE["jobs"].append({"id": 100, "title": "Software Engineer II, Payments",
                                       "location": {"name": "New York, NY"},
                                       "absolute_url": "https://x/100", "first_published": iso(0.1)})
            try:
                _, _, push, _ = run(state)
                self.assertEqual([j.id for j in push.call_args.args[0]], ["gh:ghco:100"])
            finally:
                del GREENHOUSE["jobs"][-2:]

    def test_list_mode(self):
        with tempfile.TemporaryDirectory() as d:
            out, _, push, _ = run(Path(d) / "seen.json", "--list")
            push.assert_not_called()
            self.assertIn("Backend Engineer | Remote - US | posted 40d ago", out)  # list ignores age


class PayParseTest(unittest.TestCase):
    def test_formats(self):
        P = jw.parse_pay
        self.assertEqual(P("$133,000 - $213,000"), (133000, 213000))
        self.assertEqual(P("$150K – $200K • Offers Equity"), (150000, 200000))
        self.assertEqual(P("pay: $165k-$200k plus equity"), (165000, 200000))
        self.assertEqual(P("$45/hour, $1,500 stipend, salary $120,000 to $140,000"), (120000, 140000))
        self.assertIsNone(P("competitive salary"))
        self.assertIsNone(P(None))


class TitleTest(unittest.TestCase):
    def test_frontend_included(self):
        f = jw.Filters(CFG)
        ok = lambda t: f.title_ok(jw.Job("x", "c", t, "NY", "", None))
        self.assertTrue(ok("Frontend Engineer II"))
        self.assertTrue(ok("Software Engineer, Front-End (React)"))
        self.assertFalse(ok("Senior Frontend Engineer"))

    def test_remote_europe_rejected(self):
        f = jw.Filters(CFG)
        loc = lambda l: f.location_ok(jw.Job("x", "c", "Software Engineer", l, "", None, remote=True))
        self.assertFalse(loc("Remote / Europe / Poland / United Kingdom"))
        self.assertTrue(loc("Remote / Ireland / Europe / United States / Poland"))
        self.assertTrue(loc("Remote, Global"))
        self.assertTrue(loc("San Francisco, California / New York, New York"))


class LevelTest(unittest.TestCase):
    def test_experience_rules(self):
        f = jw.Filters(CFG)
        J = lambda t, d: jw.Job("x", "c", t, "New York, NY", "", None, desc=d)
        stretch = J("Senior Software Engineer, Payments", "<p>3+ years of professional experience</p>")
        self.assertTrue(f.matches(stretch) and stretch.stretch)
        self.assertFalse(f.matches(J("Senior Software Engineer", "6+ years of experience")))
        self.assertFalse(f.matches(J("Senior Software Engineer", "no years stated")))
        self.assertFalse(f.matches(J("Software Engineer, Platform", "7+ years of industry experience")))
        self.assertTrue(f.matches(J("Software Engineer II", "1.5+ years of experience")))
        self.assertTrue(f.matches(J("Software Engineer II", "")))
        self.assertFalse(f.matches(J("Staff Software Engineer", "2+ years of experience")))


class WorkdayTest(unittest.TestCase):
    def test_workday_fetch(self):
        listing = {"total": 3, "jobPostings": [
            {"title": "Software Engineer II", "externalPath": "/job/New-York-NY/Software-Engineer-II_R100",
             "locationsText": "2 Locations", "postedOn": "Posted 2 Days Ago"},
            {"title": "Senior Software Engineer", "externalPath": "/job/New-York-NY/Senior-SWE_R101",
             "locationsText": "New York, NY", "postedOn": "Posted Today"},
            {"title": "Software Engineer", "externalPath": "/job/Bangalore/SWE_R102",
             "locationsText": "Bangalore, India", "postedOn": "Posted 30+ Days Ago"},
        ]}
        detail = {"jobPostingInfo": {"location": "McLean, VA", "additionalLocations": ["New York, NY"],
                  "jobDescription": "&lt;p&gt;New York base salary: $130,000 - $150,000&lt;/p&gt;",
                  "startDate": iso(2)[:10]}}
        f = jw.Filters(CFG)
        with mock.patch.object(jw, "post_json", return_value=listing), \
             mock.patch.object(jw, "fetch_json", return_value=detail) as det:
            jobs = jw.fetch_workday("Big Co", "bigco/wd5/External_Careers", filters=f)
        self.assertEqual(len(jobs), 3)
        a = jobs[0]
        self.assertEqual(a.id, "workday:bigco:R100")
        self.assertEqual(a.url, "https://bigco.wd5.myworkdayjobs.com/External_Careers/job/New-York-NY/Software-Engineer-II_R100")
        self.assertEqual(a.location, "McLean, VA / New York, NY")
        self.assertEqual(a.pay, (130000, 150000))
        self.assertTrue(f.matches(a) and f.fresh(a))
        self.assertFalse(f.matches(jobs[1]))       # senior
        self.assertEqual(det.call_count, 3)        # details fetched for all engineering titles, incl. senior (years check)


class SmartRecruitersTest(unittest.TestCase):
    def test_fetch(self):
        listing = {"totalFound": 2, "content": [
            {"id": "744000001", "name": "Software Engineer II", "releasedDate": iso(1),
             "location": {"city": "New York", "region": "NY", "country": "us", "remote": False}},
            {"id": "744000002", "name": "Software Engineer", "releasedDate": iso(1),
             "location": {"city": "Krakow", "country": "pl", "remote": False}},
        ]}
        detail = {"jobAd": {"sections": {"qualifications": {"title": "Q", "text": "<li>2+ years of experience</li>"},
                                         "additionalInformation": {"text": "Salary: $140,000 - $170,000"}}}}
        f = jw.Filters(CFG)
        with mock.patch.object(jw, "fetch_json", side_effect=lambda u: listing if "offset=" in u else detail):
            jobs = jw.fetch_smartrecruiters("SR Co", "SRCo", filters=f)
        a, b = jobs
        self.assertEqual(a.url, "https://jobs.smartrecruiters.com/SRCo/744000001")
        self.assertEqual(a.location, "New York, NY, United States")
        self.assertEqual(a.pay, (140000, 170000))
        self.assertEqual(a.years, 2)
        self.assertTrue(f.matches(a))
        self.assertFalse(f.matches(b))


class EightfoldOracleTest(unittest.TestCase):
    def test_eightfold(self):
        listing = {"count": 1, "positions": [{"id": 111, "name": "Engineer II - Payments", "locations": ["New York, NY, United States"],
                   "t_create": int((NOW - jw.timedelta(days=1)).timestamp()), "canonicalPositionUrl": "https://x/111",
                   "work_location_option": "hybrid"}]}
        detail = {"job_description": "<p>2+ years of experience. Salary $120,000 - $150,000</p>"}
        f = jw.Filters(CFG)
        with mock.patch.object(jw, "fetch_json", side_effect=lambda u: listing if "query=" in u else detail):
            (j,) = jw.fetch_eightfold("Amex", "aexp|aexp.com", filters=f)
        self.assertEqual((j.url, j.pay, j.years), ("https://x/111", (120000, 150000), 2))
        self.assertTrue(f.matches(j) and f.fresh(j))

    def test_oracle(self):
        listing = {"items": [{"TotalJobsCount": 1, "requisitionList": [
            {"Id": "R9", "Title": "Software Engineer", "PostedDate": iso(2)[:10],
             "PrimaryLocation": "Jersey City, NJ, United States", "secondaryLocations": []}]}]}
        detail = {"items": [{"ExternalDescriptionStr": "Java services", "ExternalQualificationsStr": "3+ years of experience"}]}
        f = jw.Filters(CFG)
        with mock.patch.object(jw, "fetch_json", side_effect=lambda u: listing if "findReqs" in u else detail):
            (j,) = jw.fetch_oracle("DTCC", "ebxr.fa.us2.oraclecloud.com/CX_1", filters=f)
        self.assertEqual(j.url, "https://ebxr.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R9")
        self.assertEqual(j.years, 3)
        self.assertTrue(f.matches(j))


if __name__ == "__main__":
    unittest.main(verbosity=2)
