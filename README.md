# jobwatch

Get a phone notification within about an hour of a company you like posting a mid-level SWE role, so you're among the first applicants instead of the 100th.

Every hour it:
1. Pulls open roles straight from each company's job board (Greenhouse / Lever / Ashby). No LinkedIn and no stale aggregator listings.
2. Keeps only SWE II / mid-level backend, full-stack, frontend, and platform titles in NYC/NJ or remote-US.
3. Skips anything first published more than 3 days ago, plus reposts of a role it has already seen.
4. Ranks matches **smaller companies first, then by posted salary** (pulled from the posting when listed), then by Glassdoor rating.
5. Pushes each new match to your phone with pay, location, and rating; tap the notification to open the application.

It only watches companies rated **3.8+ on Glassdoor** (`config.toml`). It runs free on GitHub Actions and needs no dependencies.

## Setup (about 10 minutes)

**1. Phone notifications.** Install the free **ntfy** app (iOS/Android). Tap **+** and subscribe to a topic with a long, random name, e.g. `jobs-7f3k9q2xw81mzp`. Anyone who knows the name can read it, so don't use something guessable.

**2. Repo.** Create a new **private** GitHub repo and upload everything in this folder, including the hidden `.github/` folder:
```bash
cd jobwatch
git init && git add -A && git commit -m "jobwatch"
git branch -M main
git remote add origin git@github.com:<you>/jobwatch.git
git push -u origin main
```

**3. Secret.** In the repo: Settings → Secrets and variables → Actions → New repository secret, named `NTFY_TOPIC`, with your topic name as the value. Never put the topic in the code; this repo is public.

**4. Test it.** Go to the Actions tab → **jobwatch** → **Run workflow**. Open the run log: you'll see each company's open-role count and any match. Matches from the last 3 days come through as notifications right away.
- If a company shows `HTTP 404 ... slug is probably wrong`, open that company's careers page, click any job, and copy the board name from the URL (e.g. `job-boards.greenhouse.io/<slug>/jobs/123`) into `config.toml`.

After that it runs every hour on its own.

### Optional: email digest
Add these secrets to also get one email per run that has matches (Gmail needs an [app password](https://myaccount.google.com/apppasswords)):
`SMTP_HOST=smtp.gmail.com`, `SMTP_USER=you@gmail.com`, `SMTP_PASS=<app password>`, `EMAIL_TO=you@gmail.com`

## Tuning

Everything lives in `config.toml`:
- **Add a company:** copy a `[[companies]]` block and fill in the name, ATS, slug, Glassdoor rating, and `size` (startup / growth / large). Companies under `min_rating` are skipped automatically.
- **Banks and big insurers** (JPMorgan, Capital One, etc.) mostly post through Workday, which this doesn't read yet.
- **Titles:** `title_include` / `title_exclude` (regex). Mobile, ML, senior, staff, and similar titles are excluded by default.
- **Locations:** `locations_local` for commutable cities; remote roles must be US-eligible.
- **Freshness:** `max_age_days` (default 3).

Good places to find more companies: Built In NYC, YC's Work at a Startup, the monthly HN "Who is hiring" thread, and recent Series A–C funding news. Check the Glassdoor rating before adding one.

## Running locally
```bash
python3 jobwatch.py --list      # every current match, ignoring age and seen state
python3 jobwatch.py --dry-run   # what a real run would alert on, without sending or saving
python3 tests/test_jobwatch.py  # offline tests
```

## How "already seen" works
`seen.json` (committed back by the workflow) records every matching job ID plus a company + title + location key. A brand-new ID with the same key is treated as a repost and stays silent. On the first run it alerts only on roles published in the last 3 days, so you won't get a flood of old postings.
