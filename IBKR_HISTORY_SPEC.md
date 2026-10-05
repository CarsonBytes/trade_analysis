# History-coverage spec — make the P&L chart honest about what is measured vs reconstructed

**Status:** PROPOSAL (no code changed). 2026-10-04. Companion to `IBKR_EXECUTION_SPEC.md`
(order reliability) and `IBKR_CASH_SHIELD_SPEC.md` (cash shield). Addresses the two
history gaps found while backfilling live from IBKR Flex statements.

**User asks that produced this:** *"I still think the spikes at the beginning before 9/4 were
wrongly backfilled…"*, *"paper quant, it's still show only from 4 Sept only… fix"*, and
*"is it really backfill completed? could you help me backfill them via ibkr api?"*

---

## 0. What actually happened, in one table

| | live | paper |
|---|---|---|
| account | `U12991898` | `DUK968178` |
| account type | **live** (`IBKR account`) | **DEMO** (`DU`-prefixed, `is_paper()==True`) |
| IBKR Flex statements exist | ✅ yes | ❌ **no — IBKR issues statements for live accounts only** |
| pre-2026-09-03 equity rows in DB | none | none |
| fix applied | 41 daily NAV rows backfilled | **none possible** |

So live's history was recoverable and has been restored. **Paper's cannot be, from any
authoritative source.** Its "All" period correctly shows only what exists (09-03 → today).

### 0.1 Every IBKR path tested for the paper account (not assumed)

**CORRECTION 2026-10-05 — the original conclusion in this section was WRONG.** The rows below
are retained to record what was actually probed, but the inference drawn from them has been
overturned by direct evidence (see 0.2).

| path | result | evidence |
|---|---|---|
| Flex Web Service, 2026-07-08→09-02, **live query** | returns **`U12991898` only** | 371 KB report; account set = `{U12991898}`; zero `DU…` rows |
| Flex Web Service, 1-year window | `Status: Fail` (window rejected) | probe 2026-10-04 |
| `reqAccountSummary` (TWS) | **current values only** — 0 tags carrying `prev*`/`yesterday`/`history`/`navseries` | probe on `DUK968178` |
| `reqExecutions` (TWS) | **0 fills** from a fresh session | probe on `DUK968178` |
| local DB scan (all tables + cache keys) | **0 rows** pre-2026-09-03 | `equity_history`, `cash_flows`, `paper_trades`, `ib_mirror` all checked |

**What went wrong:** the Flex probe used the **live** Flex query ID. A Flex query is scoped to
the account it was created against, so it can only ever return that one account. Returning
`{U12991898}` therefore says nothing about whether the demo account is statement-eligible. The
error was generalising a per-query scoping property into a per-account ineligibility claim, and
not testing it — the very thing this section's own title promises to avoid.

### 0.2 Paper Flex DOES work — measured, not assumed

The user supplied a **paper-specific** Flex token + query (`1659951`, token held outside the
repo at `C:\Users\Cap\Downloads\token_paper.txt`). Result:

```
"BOF","DUK968178","StatementOfFundsNetLiquidation paper",...
```

Paper's historical NAV **is** available from IBKR, and it has been backfilled:

| window | result |
|---|---|
| Flex `20260601`→`20260930` (query `1659951`) | **89 NAV days**, `2026-05-29 .. 2026-09-30` |
| Flex `20261001`→`20261004` | 2 days; October is not a closed period so coverage is partial |
| backfilled into `equity_history` | 42 rows (`07-07..09-02`) then 27 more (`05-29..07-06`) |
| paper `equity_history` now | **2,362 rows, 101 days, `2026-05-29 .. 2026-10-04`** |
| deposits/withdrawals in window | **none** (`CNAV` `DepositsWithdrawals` = 0 for all 90 days IBKR covered) |

Cross-check against paper's independently-recorded data (20 days from `09-03`, recorded from
the live broker feed) agreed to **0.239% mean / 1.490% max** — the residual is sampling-time
skew (the dashboard's last sample of a day is not the close), consistent with live's 0.072%.

**What this means for §0.1's conclusion:** it is void. Paper NAV is a *measurement* from IBKR,
not a §3 *reconstruction*. §3 is now only a fallback for days Flex has not yet generated.
The practical lesson is recorded in `docs/NOTIFICATION_SPEC.md`'s sibling rule: a negative
result from a scoped probe is not evidence of absence.


## 1. Three bugs this work exposed (all fixed 2026-10-04, all in this file's scope)

**B1 — midnight-stamped NAV created fake one-day spikes.** An IBKR statement NAV is an
**end-of-day** snapshot; the backfill stamped every row `00:00:00`. Deposits landed *during*
the day (the 07-08 deposit at `00:01:44`), and `deposit_adjusted_series()` only nets a flow
into rows at/after its timestamp — so each deposit surfaced as a single-day profit jump
(7/08 **+10,040**, 7/27 **+30,930**, 8/11 **+62,404**). Fixed by re-stamping to `23:59:59`.

**B2 — backfill silently displaced the inception anchor.** `with_inception()` prepends the
0.00 zero-reference only when `inception_ts < hist[0][0]`. A seeded row landing 1 second later
made the anchor vanish, so `base0` became a *real* NAV sample (10,040 — which already
contains that day's 10,000 deposit) while `net_flows` still subtracted the same deposit.
The Total P&L card read **−6,724.50 (−2.59%)** instead of **+3,315.50**. Fixed by moving the
anchor to `1783468799`, plus a regression test.

**B3 — the chart axis was index-spaced and period-re-based.** `xAxis: {type: "category"}`
spaces points by *index*, so the 41 daily pre-tracking points were crushed into **1.7% of the
plot width** and read as an empty gap. Separately, each period re-based the series on its own
first point, so 1W showed "0 → +351" and 1M "0 → +1,198" while true cumulative P&L was
+3,276 for both. Fixed with a **time axis** + `[ts_ms, value]` pairs, a **global** zero
reference, and ECharts **LTTB** downsampling (2,300+ 10-min samples rendered as a solid band).

## 2. The remaining gap, stated honestly

Paper's chart starts 2026-09-03 because **equity tracking began then**. Paper *does* have
pre-9/3 trading evidence — 26 closed journal trades (from 2026-06-24) and 24 `ib_mirror`
rows — but **no NAV and no deposits**, so there is no anchor to reconstruct from.

A journal-derived curve would be a **reconstruction, not a measurement**: it captures realized
`R × risk_money` only, and silently omits unrealized marks, SGOV, interest and FX. It would
not tie to the broker and would be non-comparable with live's broker-measured history.

## 3. Design: explicit data provenance

The core idea — **never let a chart or a stat mix measured and derived data silently.**
Three tiers, tagged per series:

| tier | meaning | styling | counts toward stats? |
|---|---|---|---|
| `MEASURED` | read from the broker (or IBKR Flex) | solid line | **yes** |
| `RECONSTRUCTED` | derived from journal P&L | dashed + labelled | **no** |
| `UNKNOWN` | no data exists | coverage banner only | n/a |

**3.1 Storage — reconstructed data is physically separated.** New store key
`equity_history_reconstructed`, holding `[ts, value, ccy, note]`. It is **never merged into
`equity_history`**, so it cannot contaminate `deposit_adjusted_series`, the drawdown peak,
the Total P&L card, or `pnl_crosscheck`. This is the same reasoning that already put
`equity_inception` outside `equity_history` (see `paper.with_inception`'s docstring).

**3.2 Chart — overlay, not merge.** `RECONSTRUCTED` points draw as a **dashed** line with a
`markLine` label at the seam reading `reconstructed from journal (not broker-verified)`, and a
legend entry. The `MEASURED` line keeps its current styling. The seam is placed at the first
`MEASURED` row and the two are not joined by a solid segment.

**3.3 Stats — reconstructed rows never enter a number.** `nl`, `base0`, `net_flows`,
`total_pl`, `capital_base`, `current_drawdown_pct`, `pnl_crosscheck` all keep reading
`equity_history` only. A new **"Tracked since"** field surfaces the coverage start so the UI can
say *"measured 09-03 → today"* rather than implying all-time coverage.

**3.4 Coverage banner (both accounts, always).** A single line under the chart heading:
`History: measured <first date> → <last date> · <n> points`. When a selected period extends
before the measured start, the banner says so explicitly. This alone would have prevented both
this confusion and the 9/3-gap report.

**3.5 Prevent recurrence on live (the part that actually matters).** The live gap existed for
weeks because nothing ever backfilled it. Spec a **one-shot + periodic Flex backfill**:
- a `dashboard/ops/flex_backfill.py` runbook tool: token from an env var / local file (never
  committed), `--from/--to`, **dry-run by default**, refuses to overwrite `MEASURED` rows,
  writes `MEASURED` rows stamped at `23:59:59`, and **asserts `inception_ts < first_row_ts`
  before committing** (the B2 guard);
- scheduled monthly against live **and** paper — each with its own Flex query ID, since a query
  is scoped to one account (corrected 2026-10-05; the old "demo accounts return nothing" note
  was an artefact of probing paper with the live query).

## 4. Verification

| id | check | pass condition |
|---|---|---|
| H-1 | Backfill idempotence | re-running changes 0 rows |
| H-2 | Anchor guard | a backfill whose first row would displace the inception is **rejected** |
| H-3 | Spike guard | after any backfill, no day-over-day P&L move > 20,000 HKD (deposit-sized) |
| H-4 | Period consistency | 1W/1M/3M/All report the **same** P&L at any shared timestamp |
| H-5 | Provenance isolation | `total_pl` and `pnl_crosscheck` identical with/without reconstructed rows |
| H-6 | Card/chart agreement | `card total_pl == adj[-1]` on both accounts |
| H-7 | Coverage banner | rendered on both; states the measured start date |

## 5. Build order

1. **Coverage banner + `Tracked since`** (§3.4, §3.3) — small, no new data, immediately
   removes the ambiguity the user hit. Ship first.
2. **`flex_backfill.py` as a repo tool** (§3.5) with the H-1/H-2/H-3 guards — makes the live
   backfill repeatable instead of a one-off shell script.
3. **Provenance tiering** (§3.1–3.3) — separation + dashed overlay, **only if** the user
   elects a reconstructed paper curve.
4. **Paper reconstruction** (§2) — gated on that decision; not started without it.

## 6. Open questions

- **Q1** Reconstruct paper's pre-09-03 curve from the journal (dashed, excluded from stats),
  or leave the gap and show the coverage banner? *This is the decision item — everything else
  is buildable without it.*
- **Q2** If reconstructed: should it use `realized_r × risk_money`, or the paper-side fill
  prices for unrealized marks too (more faithful, more assumptions)?
- **Q3** Monthly scheduled Flex backfill on live — acceptable to store the token on this host,
  and should it be IP-restricted in the IBKR portal?

## 7. What this does NOT change

`equity_history` remains the single source of truth for every stat. No strategy, sizing, risk
or execution code is touched. Deposit detection, the cross-check monitor and the cash-shield
maths all keep operating on measured data only.