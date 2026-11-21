# DigiCrawler: Onboarding

DigiCrawler takes an **Excel list of websites** and gives you back the same list with the **phone numbers and email addresses** found on each website.

- [Part 1: For managers (no coding needed)](#part-1-for-managers-no-coding-needed)
- [Part 2: For developers](#part-2-for-developers)

---

## Part 1: For managers (no coding needed)

### One-time setup (about 10 minutes)

1. Install **Docker Desktop** from https://www.docker.com/products/docker-desktop/ and keep the default options.
2. Restart your computer if the installer asks you to.
3. Open **Docker Desktop** once and wait until the bottom-left corner says **Engine running**.
4. Get the `site-contact-scraper` folder from your developer and put it anywhere, for example on your Desktop.

### Every time you use it

| Step | What to do                                                                                                                                                                                                                                          |
| ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | Open **Docker Desktop** and wait for **Engine running**.                                                                                                                                                                                            |
| 2    | Open the `site-contact-scraper` folder and **double-click `start.bat`**. A black window appears. The first time it takes a few minutes; after that, a few seconds.                                                                                  |
| 3    | Your browser opens **http://localhost:3003**. If it doesn't, type that address into your browser yourself.                                                                                                                                          |
| 4    | **Drop your Excel file** onto the page (or click the big box to browse).                                                                                                                                                                            |
| 5    | **Check the preview.** The page shows which column it thinks holds the websites, plus the first few addresses. If it picked the wrong column, click the right one.                                                                                  |
| 6    | Pick the **country** where most of the businesses are. It's only a fallback: each website's own country is detected automatically where possible (see below). Leave **Analytics mode** off for a quick contact list, or turn it on for every detail (see below). |
| 7    | Click **Start checking**. You'll see a live count of emails and numbers, the websites just checked, and the time left. You can close the tab; reopening the page brings you back to the check. **Download results so far** gives you an Excel file of everything found up to now. Need to stop early? Click **Stop and keep results**. |
| 8    | On the results screen, **search** or filter the list, **click any email or number to copy it**, or use **Copy emails / Copy mobiles** to copy a whole list. Click **Download Excel** to save the file. If some websites couldn't be opened (or you stopped early), **Check N websites again** retries just those and keeps everything already found. |
| 9    | When you're finished for the day, **double-click `stop.bat`**.                                                                                                                                                                                      |

> On a Mac, open Terminal in the folder and run `./start.sh` and `./stop.sh` instead of the `.bat` files.

### Nothing gets lost

Every website's result is saved on this computer the moment it's found, so a long check (even 5,000 websites) is safe:

- **You refresh or close the page**: open http://localhost:3003 again. It goes straight back to your check.
- **The internet drops, or the laptop goes to sleep**: the check **pauses** and the page shows **Waiting for internet**. When the connection is back it carries on by itself. Websites aren't marked as failed because of the outage; any website that was being checked at that moment is simply checked again.
- **The computer restarts or switches off, or you run `stop.bat` in the middle**: start DigiCrawler again (`start.bat`). The check **carries on where it left off**. Websites already done aren't checked twice. If the page was open, it reconnects by itself.
- **You lost track of a check**: the upload page lists **Your recent checks** (the last 7 days), with **Open** and **Download** buttons.

Tip: in Docker Desktop, go to *Settings → General* and turn on **Start Docker Desktop when you sign in**. Then, after a restart, DigiCrawler starts by itself and an unfinished check carries on without you doing anything.

### How your Excel file should look

- The **first row** holds the column names.
- One column contains the websites. Calling it **Website** is easiest, but `URL`, `Domain` or `Site` also work.
- Websites can be written any way you like: `acme.com`, `www.acme.com` or `https://www.acme.com`.
- Other columns, such as company name or city, are kept unchanged in the result.
- The limit is 5,000 rows per file. For more, split the list into several files.

On the upload page, click **Download a sample file** for a ready-made template (also in `examples/sample.xlsx`).

| Company       | Website                      | City   |
| ------------- | ---------------------------- | ------ |
| Acme Plumbing | acmeplumbing.com             | Austin |
| Beta Dental   | https://www.betadental.co.uk | London |

### Analytics mode: simple list or every detail

The **Analytics mode** switch is on the screen where you check the file, before you click **Start checking**. It is **off every time** you choose a new file. It decides how the websites are checked and what's on the results screen and in the Excel file.

- **Off (the normal setting, quicker):** each website is checked only until a contact turns up. The homepage is read first. If it already shows an email *and* a mobile, nothing else is opened. Otherwise the contact pages are opened, and the about/services pages and the hidden browser are used only while no email or mobile has been found at all. The result is a clean contact list. It only includes businesses where **at least one email or one mobile number** was found. Websites that failed or had nothing are left out. Next to your original columns (for example Business, Website, Type), it adds only `business_name`, `state`, `country`, `email_1`, `email_2`, … and `mobile_1`, `mobile_2`, …. Landline numbers aren't included.
- **On (slower):** every planned page is opened (up to 6 per website). The result has every row of your file, including failed websites, with all the columns below (`website_final`, `address`, landline `phone_…` numbers, `source_pages` and `status`). Use it to see why a website gave nothing, or to double-check a result. The file is named `<your file>_analytics.xlsx`.

### What the result columns mean

With Analytics mode on, the result has your original columns plus these:

| Column           | Meaning                                                                                                                                                                                                                                                                                                     |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `business_name`  | The business's name, read from the website itself (its business details for Google, site name, page title, logo or the "© 2024 …" line at the bottom). If the site gives no name, it's made from the address (`joes-pizza.in` → `Joes Pizza`), so double-check those. Blank when the website doesn't exist. |
| `website_final`  | The address the website actually ended up at, after any redirects                                                                                                                                                                                                                                           |
| `address`        | The business's **postal address** as written on the website, in one cell, e.g. `12 MG Road, Pune, Maharashtra 411001, India`. Phone numbers and emails next to it are left out. If a site lists several offices, the first one in the site's own country is used. Blank when the site shows no address. |
| `state`          | The state (or province / UK nation) from the business's **postal address** on the website, e.g. `Maharashtra`, `New York`, `Ontario`, `Scotland`. Blank when the site shows no address with a state.                                                                                                       |
| `country`        | The country from the same postal address, e.g. `India`, `United States`. Blank when the site shows no address. The website's ending (`.in`) or phone numbers are **not** used for this column.                                                                                                              |
| `email_1`, `email_2`, … | One email per column. The business's own domain comes first. There are as many columns as the row with the most emails needs: if one website has 3 emails, every row gets `email_1` to `email_3` (blank where a site has fewer).                                                                   |
| `mobile_1`, `mobile_2`, … | One **mobile or WhatsApp number** per column (e.g. `+91 98450 43210`), with the same "as many as needed" rule.                                                                                                                                                                                    |
| `phone_1`, `phone_2`, …   | One **telephone (landline) number** per column. Mobiles aren't repeated here.                                                                                                                                                                                                                     |
| `source_pages`   | The pages where the contacts were found, so you can double-check them                                                                                                                                                                                                                                       |
| `status`         | `ok` = contacts found · `no_contacts` = the site opened, but nothing was listed · `error: …` = the site couldn't be opened or blocked us · `skipped: …` = no valid website in that row, or the site doesn't allow automated visits · `skipped: stopped before this website was checked` = you clicked Stop · `skipped: not checked yet` = in a "results so far" file, the check hadn't reached this website yet |

### Where it looks, and what it picks up

- **Pages:** the homepage plus the site's **Contact, About and Services** pages (up to 6 pages per website). Pages like `/contact`, `/contact-us`, `/about`, `/about-us` and `/services` are tried even if the menu doesn't link to them. Blog posts, products and news pages are ignored.
- **Contacts:** numbers and emails written in **normal text** (paragraphs, footers, tables) as well as "click to call / click to email" links, WhatsApp links and hidden business info that Google reads.
- **Numbers kept:** only mobile, WhatsApp and telephone (landline) numbers.
- **Left out on purpose:** fax numbers; toll-free and call-centre numbers (`1800…`, `1-800…`, `0800…`, India `1860…`, UK `084…`/`087…`/`09…`); dummy numbers such as `98765 43210` or `1234567890`; example emails such as `you@example.com`; and anything inside the website's code.
- **Mobile or not:** decided from each country's official numbering plan. In the **US and Canada**, mobiles and landlines look the same, so a number is only listed as mobile if the site labels it (for example "Cell:", "Mobile:" or a WhatsApp link). Otherwise it goes in the `phone_…` columns.
- **State and country:** read only from a postal address on the site, such as its business details for Google or an address like `Pune, Maharashtra 411001`, `Austin, TX 78701`, `Toronto, ON M5V 3L9`, `Sydney NSW 2000` or `London NW1 6XE`. If a site lists several offices, the location mentioned most often wins.
- **Address:** taken from the site's business details for Google first, then an address block on the page, then an address recognised by its postcode (with the street lines just above it), and last a line labelled "Address:" / "Our Address" / "Registered Office:". That last step is what finds addresses in countries without a recognised postcode, such as the UAE. An address written only inside an image or a map can't be read.
- **Country of each website:** a number like `212-254-2246` means different things in different countries. Each website's country is worked out from its address ending (`.in`, `.co.uk`…) or from what's on the page (a "New York, NY 10002" address, "Mumbai 400069, India", numbers written with `+1` or `+91`…). Your chosen country is only used when a site gives no clue.

### Common questions

- **"Docker Desktop is not running"**: open Docker Desktop, wait for _Engine running_, then double-click `start.bat` again.
- **The page doesn't open**: wait 30 seconds and refresh. If it still doesn't load, double-click `start.bat` again.
- **What does "403" / "Blocked" mean? Is it robots.txt?** No, those are two different things:
  - `skipped: … (robots.txt)` → the website's robots.txt file **asks** tools not to visit, and we respect that. Nothing was requested.
  - `error: website blocked automated visits (403)` → we asked for the page and the website's security service (Cloudflare, Akamai…) **refused** because the visit didn't look like a person. When this happens, Contact Finder **automatically opens the site in a real (hidden) Chrome browser**. It keeps cookies between pages, waits for "Checking your browser…" screens to finish, and scrolls down so footers load. Rows rescued this way show a **via browser** tag on the results page.
  - `error: website blocked automated visits, even in a browser` → the site refused the real browser too. Open it yourself to copy the details.
- **Some rows say `error`**: the website may be down or the address may be wrong. Check that row's address in your browser, or click **Check N websites again** on the results screen to retry only those.
- **The page says "Waiting for internet" but my internet works**: your network may block the connection test. Ask the developer to set `ONLINE_CHECK_HOSTS` (see Settings).
- **The page says "Waiting for DigiCrawler to start"**: the app isn't running yet (for example, just after a restart). Open Docker Desktop and double-click `start.bat`. Your check is saved and opens by itself.
- **The results say "Needs attention"**: something stopped the check, most often a full disk. Free up some space and click **Carry on checking**. Nothing found so far is lost.
- **A row says `no_contacts` but I can see a phone number on the site**: the browser fallback already looked at the site the way a visitor sees it. The number is most likely shown as a picture, or only after clicking a button or filling a form.
- **How long does it take?** Roughly 1–2 minutes per 100 websites when sites answer normally. Sites that need the browser take longer (about 15–60 seconds each, 3 at a time).

---

## Part 2: For developers

### What's inside

```
Browser ──► nginx (port 3003) ──► /            → web/index.html (static upload page)
                              └─► /api/*       → app (FastAPI, port 8000) ──► scraper
```

- **app**: Python 3.12, FastAPI. Uses `httpx` for async fetching, `BeautifulSoup` for parsing, `phonenumbers` for validation and `openpyxl` for Excel.
- **nginx**: serves the page and proxies `/api`. The upload limit is 20 MB.
- **data/**: mounted into the container at `/data`. Each upload and its result are kept in `data/jobs/<id>/` and deleted automatically after 7 days. This folder is the app's only real state (see *How checks survive restarts*).

Code map (`src/contact_scraper/`):

| File                | Responsibility                                                                            |
| ------------------- | ----------------------------------------------------------------------------------------- |
| `extract.py`        | Pure functions: emails, phones, JSON-LD, contact-page links                               |
| `address.py`        | Pure functions: the postal address, plus state and country from it (schema.org or text)  |
| `fetcher.py`        | HTTP client, retries, robots.txt, headless-browser sessions (Playwright)                  |
| `crawler.py`        | One site: homepage + contact/about/services pages, country detection, block-page handling |
| `runner.py`         | All rows in parallel, with progress callbacks (shared by CLI + API); pauses while offline |
| `netguard.py`       | Tells "the internet is down" apart from "this website is down"                           |
| `jobstore.py`       | Crash-safe job files on disk (`job.json`, `plan.json`, `results.jsonl`)                   |
| `excel_io.py`       | Read the input, detect the URL column, write the output                                   |
| `api.py` / `cli.py` | Web API and command-line entry points                                                     |

### How checks survive restarts

The disk is the source of truth; memory is only a cache so the page gets quick answers. Each job folder `data/jobs/<id>/` holds:

| File            | Written                                    | Holds                                                                       |
| --------------- | ------------------------------------------ | --------------------------------------------------------------------------- |
| `input.xlsx`    | on upload                                  | the uploaded file, untouched (`upload.json` keeps its name for the preview) |
| `plan.json`     | once, when the check starts                | the website and name of every input row                                     |
| `job.json`      | atomically (temp file + rename) on changes | settings, status, counters, time spent                                      |
| `results.jsonl` | appended + `fsync` for every website       | one result per line; `{"reset": [...]}` lines come from "Check again"       |
| `output.xlsx`   | atomically, at the end                     | the finished file                                                           |

- A result goes to disk **before** the page is told about it. If the power goes mid-write, only the last line can be cut short; it's dropped when the file is read (and the file repaired), and that one website is checked again.
- When the app starts, it reads every job folder. Unfinished jobs carry on with the websites that have no saved result, so nothing is checked twice. Finished jobs are read from disk the first time they're opened. A damaged folder is skipped with a log line and doesn't stop the app.
- Shutting the app down (`stop.bat`, `docker compose down`, Ctrl+C) is **not** "Stop": the job stays `running` on disk and carries on at the next start. Only the Stop button marks it `cancelled`.
- **Internet outages** (`netguard.py`): a website's result is kept only after a connection test that *started after the website finished* succeeds. The test connects to a few big hosts (DNS + TCP; `ONLINE_CHECK_HOSTS`). If it fails, the run pauses (the job's `offline` flag, shown on the page), tests again every 5 s, and re-checks the websites that were in progress. An "address not found" result also gets one more try 2 s later, in case it was a short DNS hiccup.
- If saving fails (for example, the disk is full), the job ends as `error` with a plain message. Everything saved is still downloadable, and `POST /api/jobs/{id}/retry` carries on.

API additions: `GET /api/jobs` (recent checks), `POST /api/jobs/{id}/retry` (redo failed and unchecked websites), and `GET /api/jobs/{id}/download` while running (results so far, unchecked rows marked `skipped: not checked yet`).

### Run with Docker (recommended)

```bash
cp .env.example .env              # first time only (start.bat does this for you)
docker compose up -d --build      # build + start → http://localhost:3003
docker compose logs -f app        # watch logs
docker compose ps                 # check status
docker compose down               # stop
```

### Command line (CLI)

Put the Excel file in the `data/` folder, then:

```bash
docker compose run --rm app contact-scraper /data/input.xlsx -o /data/output.xlsx --region IN
```

Options:

| Flag                       | Default                 | Meaning                                                                                        |
| -------------------------- | ----------------------- | ---------------------------------------------------------------------------------------------- |
| `-o, --output`             | `<input>_contacts.xlsx` | Output file                                                                                    |
| `-c, --url-column`         | auto-detect             | Name of the website column                                                                     |
| `-r, --region`             | `REGION` from `.env`    | Fallback country for numbers without a `+` code, when a site gives no clue (`US`, `IN`, `GB`…) |
| `--concurrency`            | 20                      | Sites checked in parallel                                                                      |
| `--max-pages`              | 6                       | Pages opened per site                                                                          |
| `--browser / --no-browser` | `WITH_BROWSER` (on)     | Automatic headless-browser fallback                                                            |
| `--ignore-robots`          | off                     | Skip robots.txt checks                                                                         |
| `--analytics/--simple`     | `--analytics`           | `--simple` = Analytics mode off: the quicker check and the file with only rows with an email or mobile |
| `-v`                       | off                     | Verbose logs                                                                                   |

### Run locally without Docker

```bash
python -m venv .venv
.venv\Scripts\activate            # Mac/Linux: source .venv/bin/activate
pip install -e ".[dev]"

pytest                                                        # run tests (no internet needed)
contact-scraper examples/sample.xlsx -o data/out.xlsx         # CLI
uvicorn contact_scraper.api:app --reload --port 8000          # API → http://localhost:8000/api/docs
```

The upload page in `web/` expects nginx in front of the API, so use Docker to test the full UI.

### Settings (`.env`)

| Variable              | Default | Meaning                                                         |
| --------------------- | ------- | --------------------------------------------------------------- |
| `REGION`              | `US`    | Fallback country, preselected on the page                       |
| `CONCURRENCY`         | `20`    | Sites checked in parallel                                       |
| `MAX_PAGES`           | `6`     | Pages per site (homepage + contact/about/services)              |
| `TIMEOUT`             | `15`    | Seconds before a site is treated as unreachable                 |
| `RESPECT_ROBOTS`      | `true`  | Honour robots.txt                                               |
| `WITH_BROWSER`        | `true`  | Automatic headless-Chrome fallback (see below)                  |
| `BROWSER_CONCURRENCY` | `3`     | Sites using the browser at the same time (~150–300 MB RAM each) |
| `ONLINE_CHECK`        | `true`  | Pause while the internet is down instead of marking sites failed |
| `ONLINE_CHECK_HOSTS`  | `www.google.com:443,one.one.one.one:443,www.microsoft.com:443` | Hosts for the connection test (any one answering = online) |
| `PORT`                | `3003`  | Port on your computer                                           |

After changing `.env`, run `docker compose up -d --build`, or double-click `start.bat` again.

**Automatic browser fallback (on by default).** Every site is first read over plain HTTP, which is fast and light. Headless Chromium (Playwright) takes over automatically when:

| Situation                                                                                | What the browser does                                                                |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Homepage refused (401/403/429/503) or an "Access denied" / "Checking your browser…" page | Opens the whole site (homepage + contact/about/services) in the browser              |
| Nothing found over plain HTTP (JavaScript-built site, lazy-loaded footer)                | Looks at the whole site again in the browser, skipping pages already known to be 404 |
| Contacts found, but some contact pages were refused                                      | Opens only those pages in the browser and merges the results                         |

Each site gets one browser context, so cookies (consent, passed bot checks) carry over between its pages. "Checking your browser" screens get up to 15 s to finish on their own. Pages are scrolled to the bottom for lazy-loaded footers, and images, fonts and video are skipped for speed. Time limits are separate: 8× `TIMEOUT` for plain HTTP, and 12× `TIMEOUT` for the browser, counted from when a browser slot is free. Whatever plain HTTP found is kept if the browser fails. In `crawler.py` the result's `method` is `http`, `browser` or `http+browser`.

What it deliberately does **not** do: solve CAPTCHAs, disguise the automated browser, or rotate proxies. If a site still refuses a real browser, the row says `blocked automated visits, even in a browser`.

In the live test (26 sites) the fallback raised sites with contacts from 10 to 18, recovering Lenskart, Taj Hotels, Fabindia, Bettys, Fortnum & Mason and Zingerman's. Powell's still refused. Set `WITH_BROWSER=false` for the light image (~285 MB instead of ~2 GB).

### Troubleshooting

| Problem                                                                        | Fix                                                                                                  |
| ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| `port is already allocated`                                                    | Change `PORT` in `.env` (e.g. `8090`) and start again                                                |
| Page loads but uploads fail                                                    | `docker compose logs app`; check that the app container is `healthy` with `docker compose ps`        |
| `DLL load failed … Application Control policy` when running locally on Windows | Your PC blocks some compiled Python packages. Use Docker instead.                                    |
| Many `error: website took too long` rows                                       | Lower `CONCURRENCY` (e.g. `8`) or raise `TIMEOUT`                                                    |
| CLI says `/data/input.xlsx does not exist` in **Git Bash**                     | Git Bash rewrites `/data/...` paths. Prefix the command with `MSYS_NO_PATHCONV=1`, or use PowerShell |
| Page stuck on "Waiting for internet" while the internet works                  | A firewall blocks the connection test. Set `ONLINE_CHECK_HOSTS` to hosts you can reach, or `ONLINE_CHECK=false` |
| Need to start fresh                                                            | `docker compose down`, then delete `data/jobs/` (this also forgets unfinished checks)                |
