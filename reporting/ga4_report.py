#!/usr/bin/env python3
"""Fill the GA4 reporting Google Sheet (weekly + monthly tabs) from GA4.

Runs without any LLM and without the MCP server: it calls the Google
Analytics Data API and the Google Sheets API directly with a service account
(JSON key in the GA4_SA_KEY_JSON environment variable).

Usage (the same words work as a /ga4-report skill argument string):

    python reporting/ga4_report.py [weekly|monthly] [range] [preview]
                                   [overwrite] [refresh[=N]]

    (no words)                  weekly + monthly: fill everything empty up to
                                the newest period
    weekly                      every empty week up to the last completed
                                Mon-Sun week
    weekly 2026-08-03           from the week containing that date to the
                                last completed week
    weekly 2026-08-03..2026-09-27   exactly those weeks
    monthly                     empty/incomplete months up to the current one
    monthly 2026-09             one month;  2026-06..2026-09  a range
    preview                     show the plan and numbers, write nothing
    overwrite                   allow replacing cells that already hold data
    refresh / refresh=3         re-pull the last 1 / 3 completed weeks and
                                rewrite only the cells whose value changed

The current month is always refreshed up to *today* and its header becomes
"Nov (8)"; once the month is over the header goes back to "Nov".
"""
import collections
import datetime as dt
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Channel classification (same rules as agreed in the reporting design)
# --------------------------------------------------------------------------
PAID = r"(cpc|paid|ppc|cpm|^ads?$|promo|sponsor)"
FB = r"(facebook|^fb$|messenger)"
IG = r"(instagram|^ig$)"
AI = (r"(chatgpt|openai|gemini|perplexity|claude\.ai|^claude|copilot|"
      r"deepseek|grok|bard|mistral)")


def split_sm(value):
    """'google / cpc' -> ('google', 'cpc'), lower-cased."""
    parts = value.rsplit(" / ", 1)
    if len(parts) == 2:
        return parts[0].lower(), parts[1].lower()
    return value.lower(), ""


def classify(source_medium):
    """Map a sessionSourceMedium value to a sheet bucket name."""
    s, m = split_sm(source_medium)
    if s == "google" and m == "cpc":
        return "google_cpc"
    if re.search(r"bing|microsoft|msn", s) and re.search(PAID, m):
        return "microsoft_ads"
    if (re.search(FB, s) or re.search(IG, s)) and re.search(PAID, m):
        return "facebook_cpc"
    if "tiktok" in s and re.search(PAID, m):
        return "tiktok"
    if re.search(AI, s) or m == "ai-assistant":
        return "ai"
    if "push" in m:
        return "web_push"
    if "sendpulse" in s and m == "email":
        return "sendpulse"
    if re.search(IG, s) or "instagram" in m:
        return "instagram"
    if re.search(FB, s):
        return "facebook"
    if m == "referral":
        return "referral"
    return "other"


# --------------------------------------------------------------------------
# Dates, weeks, months
# --------------------------------------------------------------------------
def today_kyiv(tz_name):
    override = os.environ.get("REPORT_TODAY")  # for tests: YYYY-MM-DD
    if override:
        return dt.date.fromisoformat(override)
    return dt.datetime.now(ZoneInfo(tz_name)).date()


def last_completed_week_start(today):
    """Monday of the last Mon-Sun week that ended before today."""
    week_end = today - dt.timedelta(days=today.weekday() + 1)
    return week_end - dt.timedelta(days=6)


def week_start(d):
    return d - dt.timedelta(days=d.weekday())


def first_week_start(year):
    """First Monday on or after 1 January (week 1 of the sheet)."""
    d = dt.date(year, 1, 1)
    return d + dt.timedelta(days=(7 - d.weekday()) % 7)


def week_label(ws):
    we = ws + dt.timedelta(days=6)
    if ws.month == we.month:
        return f"{ws:%d}-{we:%d}/{ws:%m}"
    return f"{ws:%d/%m}-{we:%d/%m}"


def col_letter(n):
    """1 -> A, 27 -> AA."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def week_col(ws, year):
    """Sheet column number (B=2) of the week starting on ws."""
    return 2 + (ws - first_week_start(year)).days // 7


def month_end(year, month):
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def parse_header(text):
    """'Nov (8)' -> ('Nov', 8);  'Nov' -> ('Nov', None)."""
    m = re.match(r"^\s*([A-Za-z]+)\s*(?:\(\s*(\d+)\s*\))?\s*$", text or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2)) if m.group(2) else None


def header_for(base, year, month, today):
    """Plain name once the month is over, else 'Nov (8)' (today's day)."""
    if month_end(year, month) < today:
        return base
    return f"{base} ({today.day})"


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------
class Options:
    def __init__(self):
        self.weekly = False
        self.monthly = False
        self.week_from = None      # Monday dates, inclusive
        self.week_to = None
        self.month_from = None     # (year, month)
        self.month_to = None
        self.preview = False
        self.overwrite = False
        self.refresh = 0           # number of completed weeks to re-pull


def _parse_ym(text):
    y, m = text.split("-")
    return int(y), int(m)


def parse_args(tokens, today):
    o = Options()
    ranges = []
    for t in tokens:
        tl = t.strip().lower()
        if not tl:
            continue
        if tl == "weekly":
            o.weekly = True
        elif tl == "monthly":
            o.monthly = True
        elif tl == "preview":
            o.preview = True
        elif tl == "overwrite":
            o.overwrite = True
        elif tl == "refresh" or tl.startswith("refresh="):
            o.refresh = int(tl.split("=", 1)[1]) if "=" in tl else 1
        elif re.fullmatch(r"\d{4}-\d{2}(-\d{2})?(\.\.\d{4}-\d{2}(-\d{2})?)?", tl):
            ranges.append(tl)
        else:
            raise ValueError(
                f"Unrecognised argument '{t}'. Use: weekly|monthly, a range "
                "(2026-09, 2026-06..2026-09, 2026-08-03, 2026-08-03..2026-09-27), "
                "preview, overwrite, refresh[=N].")
    if len(ranges) > 1:
        raise ValueError("Give at most one range.")
    if ranges:
        a, _, b = ranges[0].partition("..")
        is_day = len(a) == 10
        if b and (len(b) == 10) != is_day:
            raise ValueError("Range ends must both be dates or both be months.")
        if is_day:
            if o.monthly and not o.weekly:
                raise ValueError("A date range is a weekly range.")
            o.weekly, o.monthly = True, False
            o.week_from = week_start(dt.date.fromisoformat(a))
            o.week_to = week_start(dt.date.fromisoformat(b)) if b else None
        else:
            if o.weekly and not o.monthly:
                raise ValueError("A month range is a monthly range.")
            o.weekly, o.monthly = False, True
            o.month_from = _parse_ym(a)
            o.month_to = _parse_ym(b) if b else None
    if not (o.weekly or o.monthly):
        o.weekly = o.monthly = True
    last_ok = last_completed_week_start(today)
    if o.week_to and o.week_to > last_ok:
        raise ValueError(
            f"Week {week_label(o.week_to)} is not completed yet; the last "
            f"completed week is {week_label(last_ok)}.")
    if o.week_from and o.week_from > last_ok:
        raise ValueError(
            f"Week {week_label(o.week_from)} is not completed yet; the last "
            f"completed week is {week_label(last_ok)}.")
    return o


# --------------------------------------------------------------------------
# Sheet state helpers (values come from the Sheets API as list-of-lists)
# --------------------------------------------------------------------------
def cell(grid, row, col):
    """1-based row/col lookup that tolerates ragged / short rows."""
    try:
        v = grid[row - 1][col - 1]
    except IndexError:
        return ""
    return "" if v is None else v


def to_number(v):
    if isinstance(v, (int, float)):
        return v
    s = str(v).replace(",", "").replace(" ", "").strip()
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def plan_weeks(opts, grid, year, rows, today):
    """Return (targets, refresh_targets): lists of week-start dates."""
    last_ok = last_completed_week_start(today)
    first = first_week_start(year)
    if last_ok.year > year:  # past year's tab: stop at the end of that year
        last_ok = week_start(dt.date(year, 12, 31))
    filled = lambda ws: cell(grid, rows["sessions"], week_col(ws, year)) != ""
    targets = []
    if opts.week_from:
        ws = max(opts.week_from, first)
        end = opts.week_to or last_ok
        while ws <= end:
            if ws.year == year or ws == week_start(dt.date(year, 12, 31)):
                if filled(ws) and not opts.overwrite:
                    raise ValueError(
                        f"Week {week_label(ws)} already has data. "
                        "Add 'overwrite' to replace it.")
                targets.append(ws)
            ws += dt.timedelta(days=7)
    else:
        ws = first
        while ws <= last_ok:
            if not filled(ws):
                targets.append(ws)
            ws += dt.timedelta(days=7)
    refresh = []
    for i in range(opts.refresh):
        ws = last_ok - dt.timedelta(days=7 * i)
        if ws >= first and ws not in targets and filled(ws):
            refresh.append(ws)
    return targets, refresh


def plan_months(opts, grid, year, rows, today):
    """Return list of (month_number, range_end_date) to (re)fill."""
    targets = []
    lo = opts.month_from
    hi = (opts.month_to or opts.month_from) if lo else None
    for m in range(1, 13):
        start = dt.date(year, m, 1)
        if start > today:
            break
        if lo and not (lo <= (year, m) <= hi):
            continue
        end = min(month_end(year, m), today)
        base, shown = parse_header(cell(grid, rows["header"], m + 1))
        filled = cell(grid, rows["sessions"], m + 1) != ""
        incomplete = month_end(year, m) >= today or shown is not None
        if opts.month_from:
            if filled and not incomplete and not opts.overwrite:
                raise ValueError(
                    f"{MONTH_ABBR[m - 1]} {year} is complete and already has "
                    "data. Add 'overwrite' to replace it.")
            targets.append((m, end))
        elif (not filled) or incomplete:
            targets.append((m, end))
    return targets


# --------------------------------------------------------------------------
# Google API access (service account)
# --------------------------------------------------------------------------
class Google:
    SCOPES = ["https://www.googleapis.com/auth/analytics.readonly",
              "https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self, key_json):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        info = json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=self.SCOPES)
        self.session = AuthorizedSession(creds)
        self.email = info.get("client_email", "?")

    def _call(self, method, url, **kw):
        r = self.session.request(method, url, timeout=120, **kw)
        if r.status_code >= 400:
            raise RuntimeError(
                f"{method} {url.split('?')[0]} -> {r.status_code}: {r.text[:600]}")
        return r.json()

    def run_report(self, property_id, body):
        """Run a GA4 report; page through all rows. Returns [(dims, mets)]."""
        url = (f"https://analyticsdata.googleapis.com/v1beta/properties/"
               f"{property_id}:runReport")
        out, offset = [], 0
        while True:
            data = self._call("POST", url, json={**body, "limit": 100000,
                                                 "offset": offset})
            for r in data.get("rows", []):
                out.append(([d["value"] for d in r["dimensionValues"]],
                            [int(float(m["value"])) for m in r["metricValues"]]))
            offset += len(data.get("rows", []))
            if offset >= data.get("rowCount", 0) or not data.get("rows"):
                return out

    def sheet_get(self, spreadsheet_id, a1):
        from urllib.parse import quote
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
               f"/values/{quote(a1)}?valueRenderOption=UNFORMATTED_VALUE")
        return self._call("GET", url).get("values", [])

    def sheet_update(self, spreadsheet_id, updates):
        """updates: [(a1_range, [[value]])] written as USER_ENTERED."""
        if not updates:
            return
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
               "/values:batchUpdate")
        self._call("POST", url, json={
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": r, "values": v} for r, v in updates]})


# --------------------------------------------------------------------------
# Fetching + computing the numbers for a set of periods
# --------------------------------------------------------------------------
def _key(dim, raw):
    return int(raw)  # isoWeek "02" -> 2, yearMonth "202609" -> 202609


def fetch(google, property_id, dim, start, end, with_items):
    """Return {period_key: {row_name: value}} for the given date range.

    dim is 'isoWeek' or 'yearMonth'. Sessions-type and items-type metrics are
    requested in separate reports: mixing them makes GA4 count only sessions
    that have item events.
    """
    dr = [{"startDate": start.isoformat(), "endDate": end.isoformat()}]
    rep = lambda dims, mets, **kw: google.run_report(property_id, {
        "dateRanges": dr, "dimensions": [{"name": n} for n in dims],
        "metrics": [{"name": n} for n in mets], **kw})

    totals = {_key(dim, d[0]): m for d, m in rep([dim], ["sessions", "totalUsers"])}
    cg = collections.defaultdict(dict)
    for d, m in rep([dim, "sessionDefaultChannelGroup"], ["sessions"]):
        cg[_key(dim, d[0])][d[1]] = m[0]
    buckets = collections.defaultdict(collections.Counter)
    for d, m in rep([dim, "sessionSourceMedium"], ["sessions"]):
        buckets[_key(dim, d[0])][classify(d[1])] += m[0]
    overlap = collections.defaultdict(collections.Counter)  # in Organic Search
    for d, m in rep([dim, "sessionSourceMedium", "sessionDefaultChannelGroup"],
                    ["sessions"]):
        if d[2] == "Organic Search":
            c = classify(d[1])
            if c in ("referral", "ai"):
                overlap[_key(dim, d[0])][c] += m[0]
    view = {}
    for d, m in rep([dim], ["eventCount"], dimensionFilter={"filter": {
            "fieldName": "eventName",
            "stringFilter": {"matchType": "EXACT", "value": "view_item"}}}):
        view[_key(dim, d[0])] = m[0]

    items_total, items_cg = {}, collections.defaultdict(dict)
    items_b = collections.defaultdict(collections.Counter)
    if with_items:
        for d, m in rep([dim], ["itemsPurchased"]):
            items_total[_key(dim, d[0])] = m[0]
        for d, m in rep([dim, "sessionSourceMedium", "sessionDefaultChannelGroup"],
                        ["itemsPurchased"]):
            k = _key(dim, d[0])
            items_cg[k][d[2]] = items_cg[k].get(d[2], 0) + m[0]
            c = classify(d[1])
            if c == "referral" and d[2] != "Referral":
                c = "other"
            items_b[k][c] += m[0]

    out = {}
    for k, (sessions, users) in totals.items():
        b, o = buckets[k], overlap[k]
        v = {"users": users, "sessions": sessions,
             "organic": cg[k].get("Organic Search", 0),
             "direct": cg[k].get("Direct", 0),
             "google_cpc": b["google_cpc"], "microsoft_ads": b["microsoft_ads"],
             "facebook_cpc": b["facebook_cpc"], "facebook": b["facebook"],
             "instagram": b["instagram"], "tiktok": b["tiktok"],
             "referral": b["referral"] - o["referral"],
             "sendpulse": b["sendpulse"], "web_push": b["web_push"],
             "ai": b["ai"] - o["ai"], "view_item": view.get(k, 0)}
        if with_items:
            ib = items_b[k]
            v.update({"items_all": items_total.get(k, 0),
                      "items_organic": items_cg[k].get("Organic Search", 0),
                      "items_direct": items_cg[k].get("Direct", 0),
                      "items_google_cpc": ib["google_cpc"],
                      "items_facebook_cpc": ib["facebook_cpc"],
                      "items_referral": ib["referral"],
                      "items_email": ib["sendpulse"]})
        out[k] = v
    return out


# --------------------------------------------------------------------------
# Building sheet updates
# --------------------------------------------------------------------------
WEEKLY_VALUE_ROWS = ["view_item", "users", "sessions", "organic", "direct",
                     "google_cpc", "microsoft_ads", "facebook_cpc", "facebook",
                     "instagram", "tiktok", "referral", "sendpulse",
                     "web_push", "ai"]
MONTHLY_VALUE_ROWS = (["view_item", "items_all", "items_organic", "items_direct",
                       "items_google_cpc", "items_facebook_cpc", "items_referral",
                       "items_email", "users", "sessions", "organic", "direct",
                       "google_cpc", "microsoft_ads", "facebook_cpc", "facebook",
                       "instagram", "tiktok", "referral", "sendpulse",
                       "web_push", "ai"])


def build_week_updates(tab, rows, year, ws, values, grid, only_changed=False):
    """Updates for one week column. Returns (updates, diffs)."""
    c = week_col(ws, year)
    L = col_letter(c)
    updates, diffs = [], []
    if not only_changed:
        updates.append((f"'{tab}'!{L}{rows['label']}", [[week_label(ws)]]))
    for name in WEEKLY_VALUE_ROWS:
        new = values[name]
        old = cell(grid, rows[name], c)
        if only_changed:
            if to_number(old) == new:
                continue
            diffs.append((week_label(ws), name, old, new))
        updates.append((f"'{tab}'!{L}{rows[name]}", [[new]]))
    if not only_changed:  # formulas are only written for brand-new columns
        r = rows
        updates.append((f"'{tab}'!{L}{r['other_formula']}",
                        [[f"={L}{r['sessions']}-SUM({L}{r['organic']}:{L}{r['ai']})"]]))
    return updates, diffs


def build_month_updates(tab, rows, year, month, values, grid, today):
    c = month + 1
    L = col_letter(c)
    r = rows
    base, _ = parse_header(cell(grid, r["header"], c))
    base = base or MONTH_ABBR[month - 1]
    updates = [(f"'{tab}'!{L}{r['header']}",
                [[header_for(base, year, month, today)]])]
    for name in MONTHLY_VALUE_ROWS:
        updates.append((f"'{tab}'!{L}{r[name]}", [[values[name]]]))
    updates.append((f"'{tab}'!{L}{r['items_other_formula']}",
                    [[f"={L}{r['items_all']}-(SUM({L}{r['items_organic']}:{L}{r['items_email']}))"]]))
    updates.append((f"'{tab}'!{L}{r['other_formula']}",
                    [[f"={L}{r['sessions']}-SUM({L}{r['organic']}:{L}{r['ai']})"]]))
    return updates


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def fmt_updates(updates):
    return "\n".join(f"  {a} <- {v[0][0]}" for a, v in updates)


def run(argv, google=None, out=print):
    cfg = load_config()
    today = today_kyiv(cfg["timezone"])
    opts = parse_args(argv, today)
    ssid, prop = cfg["spreadsheet_id"], cfg["property_id"]
    if google is None:
        key = os.environ.get("GA4_SA_KEY_JSON")
        if not key:
            raise RuntimeError("GA4_SA_KEY_JSON is not set.")
        google = Google(key)
    out(f"Today (Europe/Kyiv): {today}  | mode: "
        f"{'weekly' if opts.weekly else ''} {'monthly' if opts.monthly else ''}"
        f"{' | PREVIEW' if opts.preview else ''}")
    # In January the previous year's December/last weeks still need closing.
    years = [today.year - 1, today.year] if today.month == 1 else [today.year]
    if opts.month_from:
        years = sorted({y for y, _ in (opts.month_from,
                                        opts.month_to or opts.month_from)})
    all_updates, all_diffs = [], []
    for year in years:
        u, d = run_year(cfg, google, opts, today, year, ssid, prop, out)
        all_updates += u
        all_diffs += d

    if all_diffs:
        out("Refresh changes (week, row, before -> after):")
        for w, n, old, new in all_diffs:
            out(f"  {w}  {n}: {old} -> {new}")
    out(f"{len(all_updates)} cell updates planned.")
    if opts.preview:
        out(fmt_updates(all_updates))
        out("PREVIEW: nothing written.")
        return all_updates
    google.sheet_update(ssid, all_updates)
    out("Written." if all_updates else "Nothing to write.")
    return all_updates


def read_tab(google, ssid, tab, last_col, last_row):
    try:
        return google.sheet_get(ssid, f"'{tab}'!A1:{last_col}{last_row}")
    except RuntimeError as exc:
        if "Unable to parse range" in str(exc) or "not found" in str(exc).lower():
            raise ValueError(f"Tab '{tab}' not found - duplicate last year's "
                             "template tab and name it exactly so.") from exc
        raise


def run_year(cfg, google, opts, today, year, ssid, prop, out):
    all_updates, all_diffs = [], []

    if opts.weekly:
        tab = cfg["weekly_tab"].format(year=year)
        rows = cfg["weekly_rows"]
        grid = read_tab(google, ssid, tab, "BE", rows["other_formula"])
        targets, refresh = plan_weeks(opts, grid, year, rows, today)
        out(f"Weekly: new weeks = {[week_label(w) for w in targets] or 'none'}"
            f"; refresh = {[week_label(w) for w in refresh] or 'none'}")
        every = sorted(targets + refresh)
        if every:
            data = fetch(google, prop, "isoWeek", every[0],
                         every[-1] + dt.timedelta(days=6), with_items=False)
            for ws in targets:
                v = data.get(ws.isocalendar()[1])
                if v is None:
                    raise RuntimeError(f"GA4 returned no data for {week_label(ws)}")
                u, _ = build_week_updates(tab, rows, year, ws, v, grid)
                all_updates += u
            for ws in refresh:
                v = data.get(ws.isocalendar()[1])
                if v is None:
                    continue
                u, d = build_week_updates(tab, rows, year, ws, v, grid,
                                          only_changed=True)
                all_updates += u
                all_diffs += d

    if opts.monthly:
        tab = cfg["monthly_tab"].format(year=year)
        rows = cfg["monthly_rows"]
        grid = read_tab(google, ssid, tab, "M", rows["other_formula"])
        targets = plan_months(opts, grid, year, rows, today)
        out("Monthly: " + (", ".join(
            f"{MONTH_ABBR[m - 1]} (to {e:%d.%m})" for m, e in targets) or "none"))
        if targets:
            start = dt.date(year, targets[0][0], 1)
            end = targets[-1][1]
            data = fetch(google, prop, "yearMonth", start, end, with_items=True)
            for m, _ in targets:
                v = data.get(year * 100 + m)
                if v is None:
                    raise RuntimeError(f"GA4 returned no data for month {m}")
                all_updates += build_month_updates(tab, rows, year, m, v, grid, today)
    return all_updates, all_diffs


if __name__ == "__main__":
    try:
        run(sys.argv[1:] if len(sys.argv) > 1 else [])
    except Exception as exc:  # non-zero exit -> GitHub sends a failure e-mail
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
