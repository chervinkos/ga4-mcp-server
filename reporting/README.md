# GA4 → Google Sheet reporting

`ga4_report.py` fills the **2026** (monthly) and **2026 weekly** tabs of the
*GA4 Reporting Template* sheet straight from the GA4 Data API. It needs no
LLM, no MCP server and no Render: it signs in with a service account.

## One-time setup (already done unless noted)
1. Service account created in the Google Cloud project; **Google Analytics
   Data API** and **Google Sheets API** enabled there.
2. Service account email added as **Viewer** on the GA4 property (298408448)
   and as **Editor** on the sheet.
3. Its JSON key stored as the **`GA4_SA_KEY_JSON`** repository secret
   (Settings → Secrets and variables → Actions).
4. Each new year needs tabs named `2027` and `2027 weekly` (duplicate the
   template tabs and keep the row layout).

## Arguments
`[weekly|monthly] [range] [preview] [overwrite] [refresh[=N]]`

| Arguments | Meaning |
|---|---|
| *(none)* | weekly + monthly, fill everything empty up to the newest period |
| `weekly` | every empty week up to the last completed Mon–Sun week |
| `weekly 2026-08-03` | from that week to the last completed week |
| `weekly 2026-08-03..2026-09-27` | exactly those weeks |
| `monthly` | empty or incomplete months up to the current one |
| `monthly 2026-09` / `2026-06..2026-09` | that month / those months |
| `preview` | print the plan and numbers, write nothing |
| `overwrite` | allow replacing cells that already hold data |
| `refresh` / `refresh=3` | re-pull the last 1 / 3 completed weeks, rewrite only changed cells and list before → after |

Rules: the current month is filled up to *today* and its header becomes
`Nov (8)`; when the month is over the header returns to plain `Nov`. Months
with a `(n)` in the header are refreshed on the next monthly run. "Other"
rows are formulas and are extended to new columns.

## Running
* **Schedule** (`.github/workflows/ga4-report.yml`): Mondays 10:00 and the 1st
  of each month 10:00, Europe/Kyiv. Needs the workflow merged to the default
  branch. A failed run sends GitHub's failure e-mail.
* **By hand**: Actions → *GA4 report* → *Run workflow* → type the arguments.
* **Locally**: `GA4_SA_KEY_JSON='<json>' python reporting/ga4_report.py preview`

## Tests
`python -m unittest discover -s reporting -v` — offline; covers dates, argument
parsing, classification, planning and a full run against a fake Google client.

## Definitions
Channel rules live in `classify()`; sheet row numbers in `config.json`.
Sessions and items-purchased are always requested in separate reports, because
mixing them makes GA4 count only sessions with item events.
