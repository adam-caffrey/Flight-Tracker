# Fare Board — Flight Price Bot

Checks Google Flights daily for as many routes as you like, and emails you
when a price drops at or below the limit you set for that route. Manage
routes ("trackers") through a small web page instead of editing files by
hand.

## Pieces
- **`trackers.json`** — the list of trackers (route, months, price limit,
  filters). This is what both the checker and the admin UI read/write.
- **`flight_checker.py`** — runs through every tracker, queries Google
  Flights, emails you if anything's under the price limit for that tracker.
  Also emails you separately if it looks like the scraper itself broke.
- **`.github/workflows/check_flights.yml`** — runs the checker once a day,
  free, via GitHub Actions.
- **`docs/index.html`** — the admin page (list, add, edit, pause, delete
  trackers), hosted free via GitHub Pages.

**Caveat:** this scrapes Google Flights unofficially (no API key, via the
`fast-flights` library). It can break if Google changes their site — that's
exactly what the breakage-alert email below is for.

## Setup

### 1. Push to GitHub
Create a repo and push all these files, keeping the folder structure as-is
(`.github/workflows/...` and `docs/...` matter).

### 2. Turn on GitHub Pages for the admin UI
Repo → **Settings → Pages** → under "Build and deployment", set **Source**
to "Deploy from a branch", branch `main`, folder `/docs`. Save. GitHub gives
you a URL like `https://yourname.github.io/flight-bot/` — that's your admin
page.

### 3. Create a Personal Access Token for the admin page
The admin page edits `trackers.json` in your repo directly via GitHub's API,
so it needs a token:
- GitHub → **Settings → Developer settings → Personal access tokens →
  Fine-grained tokens → Generate new token**.
- Restrict it to **only this repository**.
- Under **Permissions**, grant **Contents: Read and write**. Nothing else.
- Copy the token. You'll paste it into the admin page each time you use it —
  it's kept in memory in the browser tab only, never saved anywhere, so
  you'll need to re-enter it each visit.

### 4. Set up email alerts (Gmail — free, no new signup)
1. Turn on 2-Step Verification if it isn't already: https://myaccount.google.com/security
2. Generate an app password: https://myaccount.google.com/apppasswords — name it "flight bot" and copy the 16-character code (your normal Gmail password won't work for this).
3. Add these as **repo Secrets** (Settings → Secrets and variables → Actions):
   - `SMTP_HOST` → `smtp.gmail.com`
   - `SMTP_PORT` → `587`
   - `SMTP_USER` → your full Gmail address
   - `SMTP_PASS` → the app password from step 2
   - `ALERT_TO` → your Gmail address (or wherever you want alerts sent)

Free Gmail allows 500 emails/day, far more than this bot will ever send.

### 5. Manage your trackers
Open your GitHub Pages URL, enter your repo owner/name and the token, click
**Load trackers**, then add/edit/pause/delete as needed. Every change
commits straight to `trackers.json` in your repo.

Tick **"Remember on this device"** if you don't want to re-paste the token
each visit — it's saved in that browser's local storage only (never sent
anywhere but GitHub's API, never synced). Leave it unticked on a shared or
public computer, and use **"Forget saved token"** to clear it anytime.

### 6. Test the checker manually
Repo → **Actions** tab → "Daily flight price check" → **Run workflow**.
Check the logs, and check your inbox.

It then runs automatically every day (default 07:00 UTC — edit the `cron`
line in `.github/workflows/check_flights.yml` to change the time).

## The two kinds of email you'll get
- **"✈️ N flight(s) found under your price limit"** — good news, one or more
  trackers found a match.
- **"⚠️ Flight price bot may be broken"** — sent instead if most of a day's
  queries errored out rather than legitimately returning "no flights found".
  This is your signal to check the Actions log and see if `fast-flights`
  needs updating.

## Day-of-week and time-of-day filters
Each tracker can now say things like "leave Dublin Friday after 6pm, come
back from Paris Sunday after 5pm":
- **Depart/return days** — pick specific weekdays in the admin UI (or leave
  blank for any day). This is applied *before* querying, so a 6-month
  Friday-out/Sunday-back search is a couple dozen queries, not a couple
  hundred.
- **Min/max nights** — for a fixed weekend length, set both to the same
  number (e.g. 2 and 2 for a Fri→Sun trip). For a flexible range, set them
  apart (e.g. 1–4 nights) and every weekday-matching combination in that
  range gets checked.
- **Depart/return after/before** — a real time (`HH:MM`, 24h), checked
  against the actual first-leg departure time in the search results. Since
  Google Flights results don't support this as a query parameter, matching
  itineraries are queried normally and then filtered by their real times.

## Scope and rate-limiting
Each date combination checked = one request. Weekday filters cut this down
a lot — a 6-month Friday/Sunday search is roughly 26 requests instead of
~180. A reasonable starting point: a handful of trackers, `REQUEST_DELAY_SECONDS`
of 3+.

## Local testing
```bash
pip install -r requirements.txt
export SMTP_HOST=... SMTP_PORT=587 SMTP_USER=... SMTP_PASS=... ALERT_TO=...
python flight_checker.py
```
