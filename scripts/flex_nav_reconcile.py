"""Weekly LIVE equity_history <-> IBKR Flex NAV reconciliation (2026-10-14).

Runs inside quant-dashboard-live-docker (or any env with dashboard.core.store).
ONE Flex SendRequest + ONE GetStatement per run (rate-limit safe: IBKR 1025
"Too many failed attempts" on probing). Window: last ~2 weeks ending yesterday.

Detects flat-placeholder stretches (>=2 consecutive trading days with the
IDENTICAL last NAV -- the signature of a frozen gateway period) and:
  - default: FLAG only, print a report, exit 0
  - --commit: surgically drop only the FROZEN PLACEHOLDER rows of those days and
    insert ONE 3-field EOD row at the true Flex NAV, after backing up under
    store key equity_history_flexrecon_backup. MEASURED rows (any row whose NAV
    differs from the frozen plateau) are always preserved, and a flagged day
    with zero placeholder rows is REFUSED (IBKR_HISTORY_SPEC 3.5).

Flex token is read from FLEX_TOKEN_FILE (default /mnt/c/Users/Cap/Downloads/token.txt
when running in WSL, or C:\\Users\\Cap\\Downloads\\token.txt on Windows). If the file
is missing the script FAILS VISIBLY (exit 1) -- no silent skip, because the whole
point is to notice when the token disappears.

Dry run by default. Idempotent.
"""
import argparse
import csv
import datetime as dt
import io
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from dashboard.core import store

EQ = dt.timezone.utc
BACKUP_KEY = "equity_history_flexrecon_backup"
REQUEST_URL = ("https://ndcdyn.interactivebrokers.com/AccountManagement/"
               "FlexWebService/SendRequest")
STMT_URL = ("https://gdcdyn.interactivebrokers.com/AccountManagement/"
            "FlexWebService/GetStatement")
CCY = "HKD"


def _read_flex_token(path: str) -> tuple[str, str]:
    """Parse token=... and query=... (or bare token then query id) from the file."""
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"FLEX token file not found: {path}. If Downloads was cleaned, "
            "re-download the Flex token and place it there, then re-run.")
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    tok = qid = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"(?i)token\s*=\s*\S+", line):
            tok = re.split(r"=", line, 1)[1].strip()
        elif re.fullmatch(r"(?i)query\s*=\s*\S+", line):
            qid = re.split(r"=", line, 1)[1].strip()
        elif re.fullmatch(r"[0-9A-Fa-f]{16,}", line) and tok is None:
            # bare token line (digits or hex, no key= prefix)
            tok = line
    if not tok or not qid:
        raise ValueError(f"could not parse token+query from {path}")
    return tok, qid


def _get(url: str, params: dict, timeout: int = 90) -> bytes:
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}", headers={"user-agent": "Java"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _flex_fetch(tok: str, qid: str, fd: str, td: str) -> bytes:
    """ONE SendRequest + poll GetStatement. Returns the RAW statement (CSV or XML bytes);
    _parse_nav() / _short_stock_by_day() parse it. Split out 2026-11-21 so one fetch feeds
    both the NAV reconcile and the unintended-short alarm (no second Flex request).

    Uses the CashReport/NetAssetValue section of the Flex activity statement.
    If the statement has no per-day NAV we fall back to the last
    InformationChange/EquitySummaryInReport row per day.
    """
    # SendRequest
    d = {c.tag: (c.text or "").strip() for c in ET.fromstring(
        _get(REQUEST_URL, {"v": "3", "t": tok, "q": qid, "fd": fd, "td": td}))}
    status = d.get("Status", "")
    if status != "Success":
        raise RuntimeError(
            f"Flex SendRequest failed: Status={status} "
            f"code={d.get('ErrorCode')} msg={d.get('ErrorMessage')}")
    ref = d.get("ReferenceCode")
    if not ref:
        raise RuntimeError(f"Flex SendRequest returned no ReferenceCode: {d}")

    # Poll GetStatement (statement generation can lag). Official shape includes
    # the token (docs: ?q=REF&t=TOKEN&v=3). 1003/1004/1019/1021 are "try again
    # shortly" per IBKR's error table; anything else fails visibly.
    xml_bytes = None
    last_err = None
    for attempt in range(12):
        time.sleep(5 if attempt else 2)
        try:
            xml_bytes = _get(STMT_URL, {"v": "3", "q": ref, "t": tok})
        except Exception as e:
            last_err = f"http: {e}"
            continue
        # Queries configured with format=CSV deliver the statement as CSV
        # ("BOF"... "EOF") instead of XML -- that IS the data, not an error.
        if xml_bytes.lstrip().startswith(b'"BOF"'):
            return xml_bytes
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            last_err = f"parse: {e}"  # partial/statement stream -- retry
            continue
        err = root.find(".//ErrorCode")
        if err is not None and (err.text or "").strip():
            code = (err.text or "").strip()
            msg_el = root.find(".//ErrorMessage")
            msg = (msg_el.text or "").strip() if msg_el is not None else ""
            last_err = f"{code}: {msg}"
            if code in ("1003", "1004", "1019", "1021"):
                continue  # not ready / busy / throttled -- keep polling
            raise RuntimeError(f"Flex GetStatement error {code}: {msg}")
        break
    else:
        raise RuntimeError(
            f"Flex statement for ref={ref} never became ready ({last_err})")

    # `root` was parsed in the loop on the success path.
    return xml_bytes


def _parse_nav(data: bytes) -> dict[dt.date, float]:
    """{date: EOD NAV} from a raw statement (XML or CSV)."""
    if data.lstrip().startswith(b"<"):
        return _nav_from_root(ET.fromstring(data))
    return _nav_from_csv(data)


def _flex_eod_nav(tok: str, qid: str, fd: str, td: str) -> dict[dt.date, float]:
    """Back-compat wrapper: fetch + parse NAV."""
    return _parse_nav(_flex_fetch(tok, qid, fd, td))


SHORT_TOLERANCE = 1.0   # base-ccy; anything more negative than this is a real short


def _short_stock_by_day(data: bytes) -> dict[dt.date, float]:
    """{date: StockShort (base ccy, <= 0)} from the NAV section of a raw statement.
    ADDED 2026-11-21 (IBKR_LONG_ONLY_GUARD_SPEC G8): an independent long-only check that
    needs no gateway and no dashboard state. LIVE carried StockShort = -43k..-96k HKD from
    2026-10-30 while the dashboard showed nothing; this would have flagged it on the first
    weekly run. Days without a StockShort column are omitted (cannot tell, not "flat")."""
    out: dict[dt.date, float] = {}
    if data.lstrip().startswith(b"<"):
        for rep in ET.fromstring(data).iter("EquitySummaryInReport"):
            ts = (rep.get("ReportDate") or "").strip()
            v = _f(rep.get("StockShort") if rep.get("StockShort") is not None
                   else rep.get("stockShort"))
            if ts and v is not None:
                try:
                    out[dt.date.fromisoformat(ts[:10])] = v
                except ValueError:
                    continue
        return out
    section = header = None
    for r in csv.reader(io.StringIO(data.decode("utf-8", errors="replace"))):
        if not r:
            continue
        if r[0] == "BOS":
            section, header = (r[2] if len(r) > 2 else None), None
        elif r[0] == "HEADER":
            header = r
        elif r[0] in ("EOS", "EOA", "EOF", "BOA"):
            section = header = None
        elif (r[0] == "DATA" and section and section.startswith("Net Asset Value")
              and header and "StockShort" in header):
            try:
                day = dt.date.fromisoformat(r[header.index("ReportDate")][:10])
                v = _f(r[header.index("StockShort")])
            except (ValueError, IndexError):
                continue
            if v is not None:
                out[day] = v
    return out


def short_alarm(short_by_day: dict[dt.date, float]) -> tuple[dt.date, float] | None:
    """PURE: (latest day, StockShort) when the LATEST reported day holds shorts, else None.
    Uses the latest day only -- a short that was already covered is history, not an alarm."""
    if not short_by_day:
        return None
    day = max(short_by_day)
    v = short_by_day[day]
    return (day, v) if v < -SHORT_TOLERANCE else None


def _nav_from_root(root: ET.Element) -> dict[dt.date, float]:
    """Extract {date: EOD NAV} from a parsed Flex statement."""
    nav_by_day: dict[dt.date, float] = {}
    # Primary: EquitySummaryInReport rows (BaseCurrencySummary / NetAssetValue).
    for rep in root.iter("EquitySummaryInReport"):
        ts = (rep.get("ReportDate") or "").strip()
        if not ts:
            continue
        try:
            day = dt.date.fromisoformat(ts[:10])
        except ValueError:
            continue
        # Prefer BaseCurrencySummary cash+NIV, else any row with Cash and IBEquities.
        cash = _f(rep.get("Cash"))
        ibeq = _f(rep.get("IBEquities"))
        total = _f(rep.get("Total"))
        if total is not None:
            nav_by_day[day] = total
        elif cash is not None and ibeq is not None:
            nav_by_day[day] = cash + ibeq

    # Fallback: NetAssetValue in CashReport / AccountInformation.
    if not nav_by_day:
        for el in root.iter("NetAssetValue"):
            # Often nested under ChangeInNAV with Date.
            parent = None  # ElementTree has no parent; scan Date-bearing ancestors via iter.
        for cin in root.iter("ChangeInNAV"):
            ts = (cin.get("EndingNAV") or "").strip()
            # ChangeInNAV has StartingNAV/EndingNAV as numbers, not dates; skip.
            _ = ts
        for ai in root.iter("AccountInformation"):
            pass  # no per-day NAV here

    if not nav_by_day:
        raise RuntimeError(
            "Flex statement parsed but no EquitySummaryInReport NetAssetValue rows found")
    return nav_by_day


def _nav_from_csv(data: bytes) -> dict[dt.date, float]:
    """Flex CSV statement (queries configured with format=CSV).

    Walks BOS/HEADER/DATA/EOS records; the 'Net Asset Value' section's
    ReportDate + Total columns give {date: EOD NAV}. Duplicate rows (same
    report date emitted twice) carry identical values -- last one wins.
    """
    nav_by_day: dict[dt.date, float] = {}
    section: str | None = None
    header: list[str] | None = None
    text = data.decode("utf-8", errors="replace")
    if not text.strip().endswith('"EOF"'):
        raise RuntimeError("CSV statement truncated (missing EOF record) -- refetch")
    for r in csv.reader(io.StringIO(text)):
        if not r:
            continue
        if r[0] == "BOS":
            section = r[2] if len(r) > 2 else None
            header = None
        elif r[0] == "HEADER":
            header = r
        elif r[0] in ("EOS", "EOA", "EOF", "BOA"):
            section = None
            header = None
        elif r[0] == "DATA" and section and section.startswith("Net Asset Value") and header:
            try:
                day = dt.date.fromisoformat(r[header.index("ReportDate")][:10])
                nav_by_day[day] = float(r[header.index("Total")])
            except (ValueError, IndexError):
                continue
    if not nav_by_day:
        raise RuntimeError(
            "Flex CSV statement parsed but no Net Asset Value DATA rows found")
    return nav_by_day


def _f(s) -> float | None:
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _find_flat_stretches(hist: list, min_days: int = 2) -> list[tuple[dt.date, dt.date, float, int]]:
    """Consecutive trading days (>=min_days) whose LAST row NAV is IDENTICAL.

    Returns [(start_date, end_date, frozen_nav, n_days), ...].
    Weekend/holiday gaps are fine: we compare trading days in sequence.
    """
    by_day: dict[dt.date, list] = {}
    for row in hist:
        d = dt.datetime.fromtimestamp(row[0], EQ).date()
        by_day.setdefault(d, []).append(row)
    trading_days = sorted(by_day)
    stretches = []
    i = 0
    while i < len(trading_days):
        nav_i = by_day[trading_days[i]][-1][1]
        j = i
        while (j + 1 < len(trading_days)
               and by_day[trading_days[j + 1]][-1][1] == nav_i):
            j += 1
        n = j - i + 1
        if n >= min_days:
            stretches.append((trading_days[i], trading_days[j], nav_i, n))
        i = j + 1
    return stretches


_SHORT_FOUND = False


def _exit(code: int) -> None:
    """Exit, but never report success while the statement shows a short (cron log: exit=2)."""
    sys.exit(2 if _SHORT_FOUND and code == 0 else code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="actually patch flat stretches (default: flag only)")
    ap.add_argument("--token-file",
                    default=os.environ.get(
                        "FLEX_TOKEN_FILE",
                        "/mnt/c/Users/Cap/Downloads/token.txt"
                        if sys.platform != "win32" else
                        r"C:\Users\Cap\Downloads\token.txt"),
                    help="path to IBKR Flex token file (token= / query= lines)")
    ap.add_argument("--days", type=int, default=14,
                    help="look-back window in calendar days (default 14)")
    ap.add_argument("--statement-file",
                    help="saved Flex statement XML to use instead of network fetch "
                         "(offline mode: no token/SendRequest needed)")
    args = ap.parse_args()

    print("===== LIVE weekly Flex NAV reconciliation =====")
    if args.statement_file:
        print(f"  statement file: {args.statement_file} (offline mode -- no Flex request)")
        try:
            with open(args.statement_file, "rb") as fh:
                data = fh.read()
            flex_nav = _parse_nav(data)
        except (OSError, ET.ParseError, RuntimeError) as e:
            print(f"  !! cannot load statement file: {e}")
            sys.exit(1)
    else:
        tok, qid = _read_flex_token(args.token_file)
        print(f"  token file: {args.token_file}")
        print(f"  query id:   {qid}")

        today = dt.datetime.now(EQ).date()
        fd = (today - dt.timedelta(days=args.days)).strftime("%Y%m%d")
        td = (today - dt.timedelta(days=1)).strftime("%Y%m%d")  # exclude today (incomplete)
        print(f"  Flex window: {fd} .. {td}  (ONE SendRequest)")

        try:
            data = _flex_fetch(tok, qid, fd, td)
            flex_nav = _parse_nav(data)
        except Exception as e:
            print(f"  !! Flex fetch FAILED: {e}")
            print("  (IBKR may be rate-limiting -- wait and re-run later, do NOT probe)")
            sys.exit(1)
    print(f"  Flex EOD NAV rows: {len(flex_nav)}")
    for day in sorted(flex_nav):
        print(f"    {day}  {flex_nav[day]:>14,.2f}")

    # G8 (2026-11-21): independent long-only check from the same statement.
    global _SHORT_FOUND
    alarm = short_alarm(_short_stock_by_day(data))
    if alarm:
        _SHORT_FOUND = True
        day, v = alarm
        msg = (f"Flex: UNINTENDED SHORT -- IBKR statement shows StockShort {v:,.0f} (base ccy) "
               f"on {day}; this strategy is long-only. Close the shorts during US RTH "
               f"(RETRO_2026-11-21.md section 6).")
        print(f"\n  !! {msg}")
        try:
            from dashboard.core import notable_events
            notable_events.record(msg, level="error", force_push=True)
        except Exception as e:                     # noqa: BLE001 -- still fails the run below
            print(f"  !! could not record/push the alarm: {e}")
    else:
        print("\n  long-only check: no short stock on the latest statement day")

    hist, _ = store.cache_get("equity_history")
    hist = hist or []
    if not hist:
        print("  no equity_history -- nothing to reconcile")
        _exit(0)
    print(f"\n  equity_history: {len(hist)} rows, "
          f"{dt.datetime.fromtimestamp(hist[0][0], EQ).date()} .. "
          f"{dt.datetime.fromtimestamp(hist[-1][0], EQ).date()}")

    stretches = _find_flat_stretches(hist, min_days=2)
    if not stretches:
        print("\n  no flat-placeholder stretches found -- equity_history looks healthy")
        _exit(0)

    print(f"\n  flat-placeholder stretches detected ({len(stretches)}):")
    target_days: set[dt.date] = set()
    frozen_by_day: dict[dt.date, float] = {}
    for start, end, nav, n in stretches:
        print(f"    {start} .. {end}  ({n} days)  frozen NAV={nav:,.2f}")
        d = start
        while d <= end:
            target_days.add(d)
            frozen_by_day[d] = nav
            d += dt.timedelta(days=1)

    # Cross-check against Flex: only patch days where Flex has a DIFFERENT NAV.
    patchable: dict[dt.date, float] = {}
    for day in sorted(target_days):
        if day.weekday() >= 5:  # skip weekends
            continue
        if day not in flex_nav:
            print(f"    {day}: no Flex NAV (weekend/holiday or not yet generated) -- skip")
            continue
        # What is equity_history's last NAV for that day?
        rows = [r for r in hist
                if dt.datetime.fromtimestamp(r[0], EQ).date() == day]
        last = rows[-1][1] if rows else None
        if last == flex_nav[day]:
            print(f"    {day}: already matches Flex ({flex_nav[day]:,.2f}) -- ok")
            continue
        frozen = frozen_by_day.get(day)
        ph = [r for r in rows if r[1] == frozen]
        meas = [r for r in rows if r[1] != frozen]
        if not ph:
            print(f"    {day}: REFUSING -- {len(meas)} MEASURED row(s), no placeholder rows "
                  f"(spec 3.5: never overwrite measured history) -- skip")
            continue
        patchable[day] = flex_nav[day]
        print(f"    {day}: PLACEHOLDER last={last} vs Flex={flex_nav[day]:,.2f} -> PATCH "
              f"({len(ph)} placeholder row(s) dropped, {len(meas)} measured row(s) kept)")

    if not patchable:
        print("\n  nothing to patch -- all flagged days already match Flex")
        _exit(0)

    print(f"\n  plan: patch {len(patchable)} day(s) with 3-field EOD rows at true Flex NAV")
    if not args.commit:
        print("  DRY RUN -- nothing written. Pass --commit to apply.")
        return

    # Build new series: drop only placeholder rows on patchable days, keep
    # measured rows even inside a mixed day, insert one EOD row per day.
    kept = []
    for r in hist:
        d = dt.datetime.fromtimestamp(r[0], EQ).date()
        if d not in patchable:
            kept.append(r)
        elif r[1] != frozen_by_day.get(d):
            kept.append(r)  # MEASURED row on a mixed day -- preserve (spec 3.5)
    new_rows = []
    for day, nav in sorted(patchable.items()):
        existing = [r for r in hist
                    if dt.datetime.fromtimestamp(r[0], EQ).date() == day]
        ts = (existing[-1][0] if existing else
              int(dt.datetime(day.year, day.month, day.day, 23, 59, 59,
                              tzinfo=EQ).timestamp()))
        new_rows.append([ts, nav, CCY])  # 3-field: hist_cash_gpv -> (None, None)
    merged = kept + new_rows
    merged.sort(key=lambda r: r[0])

    store.cache_set(BACKUP_KEY, hist)
    print(f"  backup saved under store key {BACKUP_KEY}")
    store.cache_set("equity_history", merged)

    check, _ = store.cache_get("equity_history")
    check = check or []
    print(f"  verify: equity_history now {len(check)} rows")
    for day, nav in sorted(patchable.items()):
        rows = [r for r in check
                if dt.datetime.fromtimestamp(r[0], EQ).date() == day]
        last = rows[-1][1] if rows else None
        flag = "OK" if last == nav else "MISMATCH"
        print(f"    {day}: last={last} expected={nav} -> {flag}")


if __name__ == "__main__":
    main()
    _exit(0)
