# PoshSmart — Poshmark Suit & Blazer Monitor

A zero-cost, serverless GitHub Actions agent that watches 63 designer labels on
Poshmark, filters for your sizes, and emails an HTML digest every two days.

---

## How it works

1. **Scrape** — every 2 days the workflow wakes up and calls Poshmark's mobile
   app API for each designer in the list, filtering results to sizes
   38, 38R/L/S, 40, 40R, Large, and L.
2. **Diff** — the run compares the current listings against a cached snapshot
   from the previous run and flags new IDs.
3. **Email** — an HTML digest (new listings highlighted at the top, all listings
   below) is sent via SendGrid to both recipients.
4. **Persist** — the current snapshot is stored in `state/listings.json` and
   cached between GitHub Actions runs using `actions/cache`.

**Cost:** $0. GitHub Actions free tier (2 000 minutes / month) and SendGrid
free tier (100 emails / day) are both far more than enough.

---

## One-time setup

### 1 — Create a private GitHub repository

If you haven't already, push this code to a **private** repository on GitHub.
Using a private repo keeps the state cache and workflow logs out of public view.

```bash
git init
git remote add origin git@github.com:YOUR_USERNAME/YOUR_REPO.git
git add .
git commit -m "Initial commit"
git push -u origin main
```

---

### 2 — Verify a sender address in SendGrid

SendGrid requires every *From* address to be verified before it will send mail.

1. Sign in at https://app.sendgrid.com
2. Go to **Settings → Sender Authentication**.
3. Under **Single Sender Verification**, click **Verify a Single Sender**.
4. Fill in your name and the email address you want to send *from* (this becomes
   your `FROM_EMAIL` secret — it does **not** have to be a domain you own, just
   a real inbox you control).
5. Click **Create**, then open the verification email SendGrid sends and click
   **Verify Single Sender**.

> **Do not** set up full Domain Authentication — Single Sender Verification is
> enough and takes one minute.

---

### 3 — Create a SendGrid API key

1. In SendGrid, go to **Settings → API Keys → Create API Key**.
2. Name it (e.g. `poshmark-monitor`).
3. Select **Restricted Access**.
4. Expand **Mail Send** and set it to **Full Access**.
5. Leave everything else at *No Access*.
6. Click **Create & View**, and **copy the key now** — SendGrid will never show
   it again.

---

### 4 — Add GitHub repository secrets

In your GitHub repository go to **Settings → Secrets and variables → Actions →
New repository secret** and add two secrets:

| Secret name        | Value                                                    |
|--------------------|----------------------------------------------------------|
| `SENDGRID_API_KEY` | The API key you copied in step 3                         |
| `FROM_EMAIL`       | The verified sender address from step 2                  |

---

### 5 — Trigger a manual test run

1. Go to the **Actions** tab in your GitHub repository.
2. Select the **Poshmark Monitor** workflow in the left sidebar.
3. Click **Run workflow → Run workflow**.
4. Wait ~5–15 minutes for the run to finish; check the logs for any errors.
5. Both recipients (`travis.a.hees@gmail.com` and `oliviapierce101@gmail.com`)
   should receive a digest email. On the first run every listing will be marked
   **NEW** because there is no previous state to diff against.

---

## Cron schedule

The workflow runs on this schedule (defined in `.github/workflows/poshmark.yml`):

```yaml
schedule:
  - cron: "0 8 */2 * *"   # every 2 days at 08:00 UTC
```

To change the frequency, edit the cron expression:

| Frequency            | Cron expression    |
|----------------------|--------------------|
| Every day            | `0 8 * * *`        |
| Every 2 days (default) | `0 8 */2 * *`  |
| Every 3 days         | `0 8 */3 * *`      |
| Every week (Mondays) | `0 8 * * 1`        |

After editing, commit and push — GitHub picks it up automatically.

---

## Designers tracked (63 labels)

Anderson & Sheppard · Henry Poole · Huntsman · Cifonelli · Rubinacci ·
Caraceni · A Caraceni · Dege & Skinner · Edward Sexton · Stefano Ricci ·
Kiton · Brioni · Isaia · Oxxford · Cesare Attolini · Attolini · Canali ·
Corneliani · Richard Anderson · Kathryn Sargent · Richard James ·
Brunello Cucinelli · Tom Ford · Giorgio Armani · Ralph Lauren Purple Label ·
Saint Laurent · Dior · Berluti · Hermès · Gucci · Prada ·
Ermenegildo Zegna · Zegna · Boglioli · Lardini · Borelio · Sartorio Napoli ·
Belvest · Caruso · Samuelsohn · Hickey Freeman · Ravazzolo · Coppley ·
Lubiam · L.B.M. 1911 · Pal Zileri · Stile Latino · Raffaele Caruso ·
Southwick · H. Freeman & Son · Jack Victor · Chester Barrie ·
Gieves & Hawkes · Ede & Ravenscroft · Barneys New York · Bergdorf Goodman ·
Neiman Marcus · Saks Fifth Avenue · Paul Stuart · Palm Beach ·
Norman Hilton · Aquascutum · Dunhill

---

## Sizes filtered

`38`, `38R`, `38L`, `38S`, `40`, `40R`, `Large`, `L`

Matching is case-insensitive and checks both the listing's `size` field and
its `title`. My reference measurements: shoulder 18.5–19″, chest 40″,
jacket at button 34″, sleeve 23″, waist 33″.

---

## Architecture notes

### Scraping approach

**Primary** — Poshmark mobile app API (`GET https://api.poshmark.com/api/posts/search`)
with iOS app headers. Returns clean JSON; each item includes `id`, `title`,
`brand`, `size`, `price_amount.val` (cents), `pictures[0].url_small`,
`creator_username`, and `condition`.

**Fallback** — if the mobile API returns a non-200 status or an empty `data`
array for a given designer, the scraper fetches the Poshmark web search page
and tries two sub-strategies:

1. Extract the embedded `__NEXT_DATA__` JSON blob (Next.js server-side render).
2. Parse visible `.card` / tile HTML elements with BeautifulSoup.

**Per-designer error handling** — if both methods fail for a designer, the
failure is logged and the run continues with the next designer. A single
failure never aborts the whole run.

### Known environment limitation

During development, Poshmark returned **403 "Host not in allowlist"** from the
cloud server used to build this — a Cloudflare-based block of datacenter IP
ranges. GitHub Actions runners use Microsoft Azure IP ranges which may or may
not be affected. If you see the same 403 in your Actions runs, options include:

- Adding a `HTTPS_PROXY` env var pointing to a residential proxy (adds cost).
- Running the scraper locally with secrets set in your shell.
- Contacting Poshmark about API access.

### State caching strategy

`actions/cache@v4` uses a unique `run_id`-suffixed primary key with a
prefix-based `restore-keys` fallback:

```yaml
key: poshmark-state-${{ runner.os }}-${{ github.run_id }}
restore-keys: |
  poshmark-state-${{ runner.os }}-
```

This ensures:
- **Restore**: always loads the most recent previous state.
- **Save**: always writes a new cache entry so the latest state is never stale.

---

## Local development

```bash
# Install dependencies
pip install -r requirements.txt

# Set secrets in your shell
export SENDGRID_API_KEY="SG.xxxx"
export FROM_EMAIL="you@example.com"

# Run
python scraper.py
```

The script writes `state/listings.json` after each successful run.
Delete that file to simulate a first run where all listings appear as new.
