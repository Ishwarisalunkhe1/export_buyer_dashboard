# Export Buyer Discovery Dashboard

A Flask + SQLite web application built from the supplied hiring-assessment video transcript. The transcript is treated as the primary requirements source. The app turns scattered buyer information into an organized, validated, classified and outreach-ready lead database for an India-based export business working in a mediator-style model (example product: **singing bowls**, example market: **United States**).

## Run locally

```powershell
cd export_buyer_dashboard
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>.

The sample SQLite database is created automatically on first run and seeded with clearly labelled fictional demo leads.

## Requirements covered (from the transcript)

| Video requirement | Where it is implemented |
|---|---|
| Select one product/category and research demand before reaching buyers | Product field used throughout Overview, Discovery, Leads and Outreach |
| Buyer discovery via Google and Google Maps | Generated Google Search and Maps links; country → state → city search ladder; demo discovery candidates |
| Buyer discovery via LinkedIn and Facebook | LinkedIn People/Companies and Facebook search links; manual playbook with title examples (buyer, purchaser, import manager, CEO, owner) |
| Buyer discovery via company websites and business directories | `site:` contact-page search, ImportYeti, Thomas, Alibaba, IndiaMART, Yelp, Yellow Pages, Chamber of Commerce and trade-show/exhibitor directory links |
| Narrow search from country to state to city | Discovery and Playbooks pages with a three-step search ladder (US → Texas → Houston example) |
| Look for importers, wholesalers, retailers, wellness stores, distributors and showrooms | Search intent terms, buyer role field and classification rules |
| Collect buyer name, company name, email, contact number, website, country/state/city and source | Lead form, CSV/XLSX import, SQLite `leads` table, lead detail/edit pages |
| Store data in a database for future reuse | SQLite database with lead storage, import batches and discovery runs |
| Validate email format and filter invalid addresses | Validation engine, bulk validation action, email status filters, outreach skip rule |
| Gemini AI business / individual / uncertain buyer classification | Gemini REST adapter with `GEMINI_API_KEY`, current model default `gemini-3.5-flash-lite`, reason + confidence; labelled rule-based demo fallback |
| Duplicate prevention using email, website and company details | Duplicate checks on add, import and discovery; email normalization, website normalization and company+location matching |
| Gmail personalized outreach | Gmail API send adapter with recipient personalization; demo outbox when credentials are absent |
| Attach product presentation PDF | Attachment selector with generated sample PDF and uploaded PDFs; MIME attachment in Gmail send path |
| Maintain email history | `email_history` table, History page, lead detail history |
| Avoid spam | Buyer-only default, invalid-email skip, one email per lead per day limit and configurable run limit |
| Web interface for uploads, reviewing records, classification and reporting | Leads, Discovery, Outreach, History and Reports pages |
| Manual research methods for non-IT users | Playbooks page with practical steps and source links |
| Reporting | Reports page with source/location/classification/status summaries and CSV export |

## Honest demo boundaries

- **No live scraping or platform API calls.** Google, Maps, LinkedIn, Facebook and directory results are represented as working search links plus a labelled demo discovery run using fictional prospects. Architecture for real adapters is isolated in `services.py`.
- **Gemini**: real classification runs only when `GEMINI_API_KEY` is set. Otherwise, or on API errors/timeouts/invalid responses, the app uses transparent demo rules and labels the method on each lead. Demo results are never presented as Gemini results.
- **Gmail**: real sending runs only with a valid Gmail access token or token file. Otherwise sends are recorded as `demo_sent` and no message leaves the machine.
- **Email validation** is syntax-based by default. Set `EMAIL_VALIDATION_MODE=mx` with `dnspython` installed to add MX lookup.
- **Sample PDF and starter leads** are demonstration aids, not video requirements themselves. They are labelled as Demo in the UI.

## Optional enhancements (implementation aids, not claimed as video requirements)

- CSV/XLSX upload with flexible column aliases.
- CSV export of filtered leads.
- API status page and configuration reference.
- Generated sample product presentation PDF.
- Local dashboard charts.

## Environment variables

```powershell
$env:GEMINI_API_KEY = "your_key"                  # optional: enables real classification
$env:GEMINI_MODEL = "gemini-3.5-flash-lite"       # optional: provider model override
$env:GEMINI_TIMEOUT_SECONDS = "20"                # optional: request timeout in seconds
$env:GMAIL_ACCESS_TOKEN = "token"                 # optional
$env:GMAIL_SENDER_EMAIL = "you@example.com"       # optional
$env:EMAIL_VALIDATION_MODE = "format"             # format or mx
$env:EXPORT_DASHBOARD_SEED = "0"                  # optional: no demo leads
```

The app reads process environment variables directly; it does not load `.env` automatically. If you add a local `.env` loader for convenience, keep the file out of version control — `.gitignore` already ignores `.env`. Never commit an API key.

See the API status page in the app for the full configuration reference.

## Verify real Gemini classification

Real classification needs a valid key in the same shell that starts Flask:

```powershell
cd export_buyer_dashboard
$env:GEMINI_API_KEY = "your_real_key"
$env:GEMINI_MODEL = "gemini-3.5-flash-lite"       # optional; this is the default
$env:GEMINI_TIMEOUT_SECONDS = "20"                # optional
python app.py
```

Then:

1. Open <http://127.0.0.1:5000/leads>. Leave **Use demo rules** unchecked and choose **Classify selected buyers**; or open one lead and click **Classify with Gemini**.
2. The lead detail page shows the stored source (`Gemini AI (gemini-3.5-flash-lite)`), buyer label (business buyer / individual buyer / uncertain), reason, confidence and timestamp. The value is written to SQLite and survives an app restart.
3. If the key is missing, the API errors, times out or returns an unusable response, the source reads `Demo rules (...)` instead. It is never reported as Gemini output.

No `GEMINI_API_KEY` was available in the development shell, so real live calls were not made here. The adapter is covered with mocked HTTP responses in `tests/test_gemini.py`.

## Tests

```powershell
python -m compileall -q .
python -m unittest discover -s tests -v
```

The tests cover duplicate prevention (email and website), add/edit/view/delete, validation/classification, invalid-email outreach exclusion, Gemini success/error/missing-key handling, persistence across a new app instance, file import, demo discovery, outreach history, reports and major web pages.
