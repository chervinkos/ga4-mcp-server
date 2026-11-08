"""Offline tests: dates, arguments, classification, planning, end-to-end with a
fake Google client. Run:  python -m unittest discover -s reporting -v
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ga4_report as g  # noqa: E402

D = dt.date


class Dates(unittest.TestCase):
    def test_last_completed_week(self):
        # Sunday 8 Nov 2026: that week is not over -> 26/10-01/11
        self.assertEqual(g.last_completed_week_start(D(2026, 11, 8)), D(2026, 10, 26))
        # Monday 9 Nov -> week that ended yesterday
        self.assertEqual(g.last_completed_week_start(D(2026, 11, 9)), D(2026, 11, 2))
        self.assertEqual(g.last_completed_week_start(D(2026, 11, 11)), D(2026, 11, 2))

    def test_week_layout_matches_sheet(self):
        self.assertEqual(g.first_week_start(2026), D(2026, 1, 5))
        self.assertEqual(g.week_col(D(2026, 1, 5), 2026), 2)             # B
        self.assertEqual(g.week_col(D(2026, 6, 1), 2026), 23)            # W
        self.assertEqual(g.col_letter(g.week_col(D(2026, 6, 8), 2026)), "X")
        self.assertEqual(g.col_letter(g.week_col(D(2026, 7, 27), 2026)), "AE")
        self.assertEqual(g.col_letter(g.week_col(D(2026, 8, 3), 2026)), "AF")

    def test_labels_match_existing_sheet_style(self):
        self.assertEqual(g.week_label(D(2026, 1, 5)), "05-11/01")
        self.assertEqual(g.week_label(D(2026, 1, 26)), "26/01-01/02")
        self.assertEqual(g.week_label(D(2026, 6, 8)), "08-14/06")
        self.assertEqual(g.week_label(D(2026, 6, 29)), "29/06-05/07")
        self.assertEqual(g.week_label(D(2026, 7, 27)), "27/07-02/08")

    def test_month_headers(self):
        self.assertEqual(g.header_for("Nov", 2026, 11, D(2026, 11, 8)), "Nov (8)")
        self.assertEqual(g.header_for("Nov", 2026, 11, D(2026, 11, 16)), "Nov (16)")
        self.assertEqual(g.header_for("Oct", 2026, 10, D(2026, 11, 1)), "Oct")
        self.assertEqual(g.header_for("Oct", 2026, 10, D(2026, 10, 31)), "Oct (31)")
        self.assertEqual(g.parse_header("Nov (8)"), ("Nov", 8))
        self.assertEqual(g.parse_header("Nov"), ("Nov", None))
        self.assertEqual(g.month_end(2026, 2), D(2026, 2, 28))
        self.assertEqual(g.month_end(2026, 12), D(2026, 12, 31))


class Args(unittest.TestCase):
    today = D(2026, 11, 8)

    def test_no_args_is_everything(self):
        o = g.parse_args([], self.today)
        self.assertTrue(o.weekly and o.monthly and not o.preview)

    def test_weekly_ranges(self):
        o = g.parse_args(["weekly", "2026-08-05"], self.today)
        self.assertEqual((o.week_from, o.week_to), (D(2026, 8, 3), None))
        o = g.parse_args(["2026-08-03..2026-09-27"], self.today)
        self.assertTrue(o.weekly and not o.monthly)
        self.assertEqual((o.week_from, o.week_to), (D(2026, 8, 3), D(2026, 9, 21)))

    def test_future_week_rejected(self):
        with self.assertRaises(ValueError):
            g.parse_args(["weekly", "2026-11-02"], self.today)   # not completed
        with self.assertRaises(ValueError):
            g.parse_args(["weekly", "bogus"], self.today)

    def test_months_flags(self):
        o = g.parse_args(["monthly", "2026-06..2026-09", "preview", "overwrite",
                          "refresh=3"], self.today)
        self.assertEqual((o.month_from, o.month_to), ((2026, 6), (2026, 9)))
        self.assertTrue(o.preview and o.overwrite)
        self.assertEqual(o.refresh, 3)
        self.assertEqual(g.parse_args(["refresh"], self.today).refresh, 1)


class Classify(unittest.TestCase):
    def test_buckets(self):
        c = g.classify
        self.assertEqual(c("google / cpc"), "google_cpc")
        self.assertEqual(c("google / organic"), "other")
        self.assertEqual(c("(direct) / (none)"), "other")
        self.assertEqual(c("bing / cpc"), "microsoft_ads")
        self.assertEqual(c("bing / organic"), "other")
        self.assertEqual(c("facebook / cpc"), "facebook_cpc")
        self.assertEqual(c("fb / paid"), "facebook_cpc")
        self.assertEqual(c("ig / paid"), "facebook_cpc")
        self.assertEqual(c("Instagram / promo"), "facebook_cpc")
        self.assertEqual(c("m.facebook.com / referral"), "facebook")
        self.assertEqual(c("l.messenger.com / referral"), "facebook")
        self.assertEqual(c("l.instagram.com / referral"), "instagram")
        self.assertEqual(c("ig / social"), "instagram")
        self.assertEqual(c("helena_ua_pl / instagram_post"), "instagram")
        self.assertEqual(c("tiktok / paid"), "tiktok")
        self.assertEqual(c("tiktok.com / referral"), "referral")
        self.assertEqual(c("sendpulse / email"), "sendpulse")
        self.assertEqual(c("sendpulse / push"), "web_push")
        self.assertEqual(c("chatgpt.com / ai-assistant"), "ai")
        self.assertEqual(c("chatgpt.com / (not set)"), "ai")
        self.assertEqual(c("gemini.google.com / referral"), "ai")
        self.assertEqual(c("e.blik.com / referral"), "referral")
        self.assertEqual(c("mail.meta.ua / referral"), "referral")
        self.assertEqual(c("google / ad"), "other")


# ---- planning + end-to-end with a fake Google client -----------------------
def sheet_state(weekly_filled_through, monthly_filled_through):
    cfg = g.load_config()
    wr, mr = cfg["weekly_rows"], cfg["monthly_rows"]
    weekly = [[""] for _ in range(wr["other_formula"])]
    ws = g.first_week_start(2026)
    while ws <= weekly_filled_through:
        c = g.week_col(ws, 2026)
        for r in (wr["label"], wr["sessions"]):
            row = weekly[r - 1]
            row.extend([""] * (c - len(row)))
            row[c - 1] = "x"
        ws += dt.timedelta(days=7)
    monthly = [[""] for _ in range(mr["other_formula"])]
    monthly[0] = [""] + g.MONTH_ABBR
    for m in range(1, monthly_filled_through + 1):
        row = monthly[mr["sessions"] - 1]
        row.extend([""] * (m + 1 - len(row)))
        row[m] = 1000
    return weekly, monthly


class FakeGoogle:
    def __init__(self, weekly, monthly):
        self.weekly, self.monthly = weekly, monthly
        self.written = []
        self.reports = []

    def sheet_get(self, ssid, a1):
        return self.monthly if "weekly" not in a1 else self.weekly

    def sheet_update(self, ssid, updates):
        self.written += updates

    def run_report(self, prop, body):
        self.reports.append(body)
        dims = [d["name"] for d in body["dimensions"]]
        mets = [m["name"] for m in body["metrics"]]
        s, e = (D.fromisoformat(body["dateRanges"][0][k]) for k in ("startDate", "endDate"))
        if dims[0] == "isoWeek":
            keys, d = [], s
            while d <= e:
                keys.append("%02d" % d.isocalendar()[1]); d += dt.timedelta(days=7)
        else:
            keys, d = [], D(s.year, s.month, 1)
            while d <= e:
                keys.append("%04d%02d" % (d.year, d.month))
                d = D(d.year + (d.month == 12), d.month % 12 + 1, 1)
        extra = {"sessionDefaultChannelGroup": ["Organic Search", "Direct", "Referral"],
                 "sessionSourceMedium": ["google / cpc", "e.blik.com / referral",
                                          "chatgpt.com / organic", "sendpulse / email"]}
        out = []
        for k in keys:
            combos = [[]]
            for n in dims[1:]:
                combos = [c + [v] for c in combos for v in extra[n]]
            for c in combos:
                out.append(([k] + c, [10] * len(mets)))
        return out


class Planning(unittest.TestCase):
    def setUp(self):
        self.cfg = g.load_config()

    def test_weekly_fills_gap_to_last_completed(self):
        weekly, _ = sheet_state(D(2026, 7, 27), 8)   # filled to 27/07-02/08
        o = g.parse_args(["weekly"], D(2026, 11, 8))
        targets, refresh = g.plan_weeks(o, weekly, 2026, self.cfg["weekly_rows"], D(2026, 11, 8))
        self.assertEqual(targets[0], D(2026, 8, 3))
        self.assertEqual(targets[-1], D(2026, 10, 26))
        self.assertEqual(len(targets), 13)
        self.assertEqual(refresh, [])

    def test_weekly_refresh_last_two(self):
        weekly, _ = sheet_state(D(2026, 10, 26), 8)
        o = g.parse_args(["weekly", "refresh=2"], D(2026, 11, 8))
        targets, refresh = g.plan_weeks(o, weekly, 2026, self.cfg["weekly_rows"], D(2026, 11, 8))
        self.assertEqual(targets, [])
        self.assertEqual(refresh, [D(2026, 10, 26), D(2026, 10, 19)])

    def test_explicit_filled_week_needs_overwrite(self):
        weekly, _ = sheet_state(D(2026, 10, 26), 8)
        o = g.parse_args(["weekly", "2026-10-19"], D(2026, 11, 8))
        with self.assertRaises(ValueError):
            g.plan_weeks(o, weekly, 2026, self.cfg["weekly_rows"], D(2026, 11, 8))

    def test_monthly_sep_oct_and_current_nov(self):
        _, monthly = sheet_state(D(2026, 7, 27), 8)
        o = g.parse_args(["monthly"], D(2026, 11, 8))
        t = g.plan_months(o, monthly, 2026, self.cfg["monthly_rows"], D(2026, 11, 8))
        self.assertEqual(t, [(9, D(2026, 9, 30)), (10, D(2026, 10, 31)), (11, D(2026, 11, 8))])

    def test_monthly_incomplete_month_is_refreshed_then_finalised(self):
        _, monthly = sheet_state(D(2026, 7, 27), 11)
        monthly[0][10] = "Oct (27)"            # header says Oct was partial
        o = g.parse_args(["monthly"], D(2026, 11, 3))
        t = g.plan_months(o, monthly, 2026, self.cfg["monthly_rows"], D(2026, 11, 3))
        self.assertIn((10, D(2026, 10, 31)), t)
        self.assertIn((11, D(2026, 11, 3)), t)


class EndToEnd(unittest.TestCase):
    def run_it(self, argv, today, wf, mf, preview=True):
        os.environ["REPORT_TODAY"] = today
        weekly, monthly = sheet_state(wf, mf)
        fake = FakeGoogle(weekly, monthly)
        lines = []
        upd = g.run(argv + (["preview"] if preview else []), google=fake, out=lines.append)
        return fake, upd, lines

    def tearDown(self):
        os.environ.pop("REPORT_TODAY", None)

    def test_default_run_today_nov8(self):
        fake, upd, lines = self.run_it([], "2026-11-08", D(2026, 7, 27), 8)
        cells = {a: v[0][0] for a, v in upd}
        # new week columns start at AF (3 Aug) with a label and a formula
        self.assertEqual(cells["'2026 weekly'!AF3"], "03-09/08")
        self.assertTrue(cells["'2026 weekly'!AF43"].startswith("=AF30-SUM(AF31:AF42)"))
        self.assertEqual(cells["'2026 weekly'!AR3"], "26/10-01/11")   # last completed
        self.assertNotIn("'2026 weekly'!AS3", cells)
        # months: Sep, Oct complete -> plain header; Nov partial -> "Nov (8)"
        self.assertEqual(cells["'2026'!J1"], "Sep")
        self.assertEqual(cells["'2026'!K1"], "Oct")
        self.assertEqual(cells["'2026'!L1"], "Nov (8)")
        self.assertEqual(cells["'2026'!L12"], "=L5-(SUM(L6:L11))")
        self.assertEqual(cells["'2026'!L28"], "=L15-SUM(L16:L27)")
        self.assertIn("PREVIEW: nothing written.", lines)
        self.assertEqual(fake.written, [])

    def test_sessions_and_items_are_never_mixed(self):
        fake, _, _ = self.run_it(["monthly"], "2026-11-08", D(2026, 7, 27), 8)
        for body in fake.reports:
            mets = {m["name"] for m in body["metrics"]}
            self.assertFalse("itemsPurchased" in mets and mets & {"sessions", "totalUsers"})

    def test_write_mode_writes(self):
        fake, upd, _ = self.run_it(["weekly"], "2026-11-08", D(2026, 7, 27), 8, preview=False)
        self.assertEqual(len(fake.written), len(upd))
        self.assertGreater(len(upd), 0)

    def test_nothing_to_do(self):
        # weeks filled through last completed one, months through Oct, today Nov 9
        fake, upd, lines = self.run_it(["weekly"], "2026-11-09", D(2026, 11, 2), 11)
        self.assertEqual(upd, [])


if __name__ == "__main__":
    unittest.main()
