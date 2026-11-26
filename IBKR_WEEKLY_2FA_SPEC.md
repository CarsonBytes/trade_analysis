# IBKR Weekly 2FA Spec (WEEKLY_2FA)

Status: **Phase 0 + the weekly window implemented and deployed 2026-11-21 16:50 HKT** (user:
"stick with HKT time zone display and logs, do spec and verify"). See §10 for what shipped,
how it was verified, and what is still open. The first planned touch is
**Sun 2026-11-22 20:35 HKT**, with the reminder at 20:25.

Evidence and per-event classification: `RETRO_2FA_2026-11-21.md`. 26 passkey touches in 31 days:
4 were IBKR's weekly re-auth, and 22 were unplanned.

Builds on and partly supersedes:
- `GATEWAY_AUTOLOGIN_SPEC.md`: §3 (the 05:30 decision) is re-opened as decision D1. §4 (silent
  completion) and §5 Phase 2 (staged token, page ladder) are re-scoped here.
- `IBKR_GATEWAY_PASSKEY_SPEC.md`
- HANDOFF "MIN-2FA spec (2026-10-17 afternoon)": P5 (keep the Sunday cold restart) is reversed.

## 0. Target

| Metric | Today (Oct 22 → Nov 21) | Target |
|---|---|---|
| Planned touches/week | ~1 (4 in 31 d) | **exactly 1**, at a time the user chose, with one reminder |
| Unplanned touches/week | ~5 (22 in 31 d) | **0 caused by us**. IBKR-caused ones are measured and reported, never silent |
| Live LIVE-account login attempts from anything other than the Windows gateway | 44 (Docker) | **0** |
| Gateway dark during US RTH without a page reaching the user | yes (Nov 6, Nov 20, today's suppressed page) | never |
| Human actions per touch | touch + OK boxes + sometimes 14 h of modal | touch only |

**Honest caveat.** 7 of the 22 unplanned touches came from IBKR rejecting the daily restart
token (`security tokens ... have expired (routinely)`, RETRO §3.2). If that turns out to be IBKR
policy rather than a side effect of competing logins, the achievable floor is about
**1 planned + ~1.5 IBKR-forced per week**, and no client-side design can reach 1/week. Phase 0
measures this before we promise anything (§6, §7 gate G1).

## 1. What IBKR forces (hard limits, do not design against them)

1. **Every full authentication needs a human touch.** Passkey only, UV required, hybrid
   disabled. The SFC HK mandate applies. Proven Oct 3–13 (`IBKR_GATEWAY_PASSKEY_SPEC.md`).
2. **Weekly re-authentication around the Sunday server reset.** Observed:
   - `Re-login is required` on Sun Oct 18 18:38, Nov 1 16:54 (repeated 6× to 17:29), Nov 8 17:18 HKT
   - the Sunday daily restart turned into a full shutdown (Nov 8 20:26)

   The IBC user guide puts the boundary at Sunday 01:00 ET. Observed kicks came 13–17 h after
   it. One Sunday (Nov 15) the post-boundary 20:30 HKT restart still accepted the token, so the
   exact trigger is IBKR's, not a clock rule.
3. **One session per username.** A second login for the same user competes. Examples:
   `Existing session detected` (Nov 3 00:20), and collisions of concurrent passkey challenges
   (RETRO §3.3).
4. **The gateway must restart or log off daily.** The session token survives only a
   *healthy scheduled* restart. The `autorestart` file is written at the scheduled shutdown and
   consumed about 2 s later.
5. **Failed-login throttle** (`Too many failed login attempts`, Oct 3–5 and Nov 19).
6. **Unknown, possibly IBKR:** daily-token rejections at warm restart (6 of 17), plus one
   transient `Server disconnected, please try again`.

Everything else in RETRO §1 is ours.

## 2. Mechanics: which events keep the session and which force a touch

| Event | Session kept? | Touch? | Evidence | Lever |
|---|---|---|---|---|
| Daily warm restart (`AutoRestartTime`, gateway clock = HKT) | yes, if IBKR accepts the TST | no (10/17) / **yes** (7/17 rejected or transient) | launcher.log `NS_AUTH_START initialTokenType=TST` → `Login has completed` vs `Authorization failed` | §3.6 |
| First restart after IBKR's Sunday kick | no | **yes: the planned touch** | IBC `Re-login is required`, Nov 8 `Shutdown progress` | §3.1 |
| IBC `ColdRestartTime` (Windows clock!) | no | **yes, always** | Nov 1 20:30, Nov 9 / Nov 16 12:30 HKT | §3.3 removes it |
| Windows reboot / shutdown / power loss / long sleep | no | **yes** | 14 events, 11 touches | §3.5 |
| Docker / WSL restart (`Docker Daily Restart`, deploys) | Windows gateway untouched | no for the Windows process, **but it restarts the Docker LIVE gateway, which logs into the same account** | `docker logs quant-ibgateway-live-docker` | §3.2 |
| Dashboard container recreate | yes | no | API clients reconnect via relay | none needed |
| API relay death (4003) | yes | no (data path only) | Oct 16–17 | relay watchdog (exists) |
| Closing the gateway login/main window | no | **yes** | Oct 18 21:04, Oct 26 20:02 | rule: minimize, never close (exists) |
| User IBKR mobile/web login, same username | unknown | possibly via token expiry (H1) | Nov 3 `Existing session detected` | experiment E2 |
| Windows timezone change | yes (JVM keeps HKT) | no by itself, but it **moves IBC's cold restart and every local-clock script** | RETRO §3.1 | §3.4 |

## 3. Design

### 3.1 One planned weekly touch: "the weekly window"

The weekly touch is scheduled by **us** at a fixed UTC instant `T_touch`. It is not left to
wherever IBKR's kick or IBC's cold restart happens to land.

- `T_touch` is stored as **UTC** plus a display timezone, in one file:
  `C:\IBC-Live\weekly-touch.json`, e.g. `{"weekday":"Sun","utc":"12:30","display_tz":"Asia/Hong_Kong"}`.
  Default: Sunday 12:30Z (Sun 20:30 HKT = Sun 07:30 EST = Sun 04:30 PST). See D1.
- Constraint: `T_touch` must fall after IBKR's Sunday kick (latest observed 18:40 HKT = 10:40Z)
  and before Monday US open (22:30 HKT winter / 21:30 summer). Valid range: **Sun 11:00Z →
  Mon 13:00Z**.
- **Sunday hold (kick → T_touch).** When IBKR logs the session out on Sunday before `T_touch`,
  nothing launches a login: no watchdog auto-login, and IBC's auto `Re-login` click is
  neutralised (see below). The gateway sits logged out and silent. US markets are closed. The
  watchdog logs `weekly-hold` instead of paging.
- **At T_touch − 10 min: one reminder.** Toast + one Telegram `critical` on key
  `weekly-touch:reminder`: "Weekly YubiKey touch at 20:30 HKT (07:30 your time). Key plugged in
  directly, not via hub."
- **At T_touch:** `weekly-touch.ps1` runs. It is a new scheduled task with a UTC-anchored trigger.
  - If the gateway is logged out → launch `ibkr-live-login.ps1 -Unattended`. The helper enters
    the PIN; the human touches.
  - If the gateway is **logged in but has not done a full auth since the Sunday boundary** →
    *conditional cold restart*: stop IBC cleanly and launch the login. This replaces IBC's
    `ColdRestartTime` (D2), so the touch happens now, when expected, not mid-week.
  - If a full auth already happened since the boundary (e.g. the user touched after a reboot
    on Sunday) → skip. The week is satisfied; log `weekly-touch: already satisfied at <ts>`.
- **Deadline ladder:**
  - T+15 min: re-page with the state from `passkey-helper.log` ("PIN entered, touch now" vs
    "no Security dialog").
  - Then every 30 min.
  - At Mon 12:00Z (2.5 h before the winter open), escalate the text to "US open in 2h30, gateway
    logged out".
- **IBC `Re-login is required` handling.** IBC auto-clicks Re-login (Nov 1: 6 cycles in 35 min).
  Each click starts a 2FA that times out unanswered and burns failed-login budget. During the
  Sunday hold, the watchdog must not add launches. For IBC itself, try first
  `ReloginAfterSecondFactorAuthenticationTimeout=no` (already set) plus
  `SecondFactorAuthenticationExitInterval`. If IBC still re-clicks, accept it (bounded, Sunday
  only) and document it. Do not patch IBC.

### 3.2 P0: Remove the second LIVE gateway (Docker)

Problem: `quant-ibgateway-live-docker` (`restart: unless-stopped`) starts on every WSL boot and
logs into U12991898: 44 attempts since Oct 22, collisions with real prompts, a throttle hit, and
possibly the token rejections.

1. `docker update --restart=no quant-ibgateway-live-docker && docker stop quant-ibgateway-live-docker`.
   Recreate the hold file `/home/cap/.container-hold/quant-ibgateway-live-docker` with reason
   `parked: windows gateway is live (WEEKLY_2FA)`.
2. `docker-compose.live.yml` `ib-gateway-live`: `restart: "no"`, and **remove the LIVE
   credentials** from its environment (or point `TWS_USERID` at a non-existent placeholder). Even
   a deliberate `--with-gateway` deploy must not log into live without an explicit edit.
3. Preferred end state (D3): `docker rm` the container. Keep the compose block commented, with a
   pointer to this spec.
4. `infra-watchdog.sh` / `gateway-login-watchdog.sh` / `gateway-relogin.sh`: assert they never
   `docker start`/`restart` the live gateway while `LIVE_GATEWAY_MODE=windows`. The code reads
   that way today, but put it under test.
5. Verify: after a `wsl --shutdown` cycle, `docker logs quant-ibgateway-live-docker --since 10m`
   shows **no** `Login attempt` and the container state is `exited`/absent.

### 3.3 P0: Remove IBC's `ColdRestartTime`

- `C:\IBC-Live\config.ini:643 ColdRestartTime=` (empty). The weekly full auth comes from IBKR
  (§1.2) or from the conditional cold in §3.1. Removing it also removes the Windows-clock
  dependency that put the touch on Monday 12:30 HKT.
- Update `restart-observe.ps1:107` and `restart-verify.ps1`, which grep and assert the
  `Gateway will be cold restarted at` sentence, and the `test-min2fa.ps1` fixtures. The
  `sunday-cold-night` exemption becomes `weekly-window` keyed off `weekly-touch.json`.

### 3.4 P0: Make everything timezone-proof

The gateway JVM runs in HKT. IBC's pre-start logic, Task Scheduler "local" triggers, PowerShell
`Get-Date` comparisons and the log stamps all follow the Windows clock, which changed 4 times.

- **Decision D4:** pin Windows to one timezone (simplest), **or** make every script
  timezone-agnostic. Recommended: both. Scripts compute in UTC
  (`[DateTime]::UtcNow`); log lines carry an explicit offset
  (`yyyy-MM-dd HH:mm:ss zzz`); scheduled tasks use UTC-anchored triggers
  (`-At` with `Kind=Utc` / "Synchronize across time zones"). The observe/verify tasks happen to
  be UTC-anchored already, but their scripts are not: `restart-observe.ps1` picks its phase from
  the local clock "20:00–20:31", which is why it fails under PST.
- The watchdog and login script stamp logs with offset. Archive folder names use UTC
  (`yyyyMMddTHHmmssZ`).
- Test: run the suite with the timezone temporarily switched (in a test VM or with an
  injected clock). Do not tzutil the live box.

### 3.5 P1: Stop the box going down

| Trigger (Oct 22 → Nov 21) | Count | Control |
|---|---|---|
| Unexpected power loss (Kernel-Power 41) | 5 | **D5:** a UPS (a small line-interactive unit covers brownouts and short outages); check the PSU/BIOS logs; look at the 41 records' `BugcheckCode`/`PowerButtonTimestamp` to tell power loss from a hang/reset |
| Start-menu restart by the user | 7 | **Reboot ritual** (see below) |
| Windows Update restart (notification click) | 1 | Active hours 05:00–23:59; WU deferral; patch only in the weekly window (the reboot then coincides with the planned touch) |
| Sleep / hibernate / fast-startup shutdown | 4 | `powercfg /change standby-timeout-ac 0`, `hibernate-timeout-ac 0`, `powercfg /h off` (also kills fast startup). Elevated, one-time, user decision D6 |

**Reboot ritual** (behavioural, supported by tooling):
- Reboot only inside the weekly window, right **before** `T_touch`.
- Otherwise use `safe-reboot.ps1`. It shows "This will cost a YubiKey touch now and the gateway
  is dark until you touch. Continue?" and records an `unplanned-touch: reboot` entry in the
  ledger. That is the only deliberate modal in the design, because a human is present by
  definition.
- If experiment E3 succeeds, the script parks the token first, and the reboot becomes zero-touch.

### 3.6 P1: Daily token rejections: measure, retry, then decide

1. **Measure** (Phase 0, no behaviour change). The ledger (§3.8) records every warm restart's
   outcome from launcher.log: `TST accepted` / `rejected: expired (routinely)` /
   `transient: Server disconnected`. Do this for two weeks **after §3.2 removes the Docker
   gateway.**
2. **Gate G1:**
   - rejections drop to ≤1 per 2 weeks → H1 (competing logins) confirmed; done.
   - otherwise → experiments E1/E2.
3. **Transient retry** (independent of G1). When a warm restart fails with `Server disconnected,
   please try again` (the token was not rejected), relaunch once **within 60 s** with the same
   autorestart descriptor. It is consumed and deleted, so this needs the staged copy (E3) or an
   IBC restart that keeps the TST. Until E3 exists, it is a page, not an automatic action.
4. **E1: a dedicated API username.** IBKR lets an account holder add a second user to the same
   account. The gateway uses user B. The human's mobile/web/TWS logins use user A, so they can
   never compete with or expire the gateway's session. User B still needs a passkey: register
   the same YubiKey as a second credential (FIDO2 resident keys, fw 5.4.3 holds 25). Cost: an
   IBKR form, possibly market-data subscriptions per user. **D7.**
5. **E2: does a mobile/web login expire the gateway token?** For one week the user logs their
   IBKR mobile/web logins in a note; correlate with rejections. Cheap; needs no change.
6. **E3: staged token** (GATEWAY_AUTOLOGIN Phase 2.2, re-scoped). Facts:
   - A full login's `sessionId`/TST is reused by every following warm restart
     (`6adb4a2b.0028` on Oct 24, 25 and 26).
   - A copy captured in the ~2 s window could therefore relaunch after a crash or reboot without
     2FA, **if** IBKR still honours the session server-side.

   Safety rules:
   - DPAPI-encrypt the copy (`ProtectedData`, CurrentUser), ACL it to the user, delete it after
     7 days or at the next full auth, and never log its bytes.
   - Use it only on an unexpected-death or reboot relaunch.
   - On rejection, fall through to the normal 2FA, so there is no regression.

   Test on a weekday with the human present: kill IBC 1 h after a warm restart, restore, observe.
   It addresses 11 of 22 unplanned touches (power + reboot), so it is the biggest lever. It is
   also the most invasive (it handles a bearer token), so it is gated on D8.

### 3.7 P0: The unattended login path never blocks

These are the GATEWAY_AUTOLOGIN §4 rules, re-scoped. Implement them in `ibkr-live-login.ps1`
with a new switch `-Unattended`, which the watchdog, autostart and `weekly-touch.ps1` always pass:
- **No MessageBox at all under `-Unattended`.** Terminal states become toast + log + exit. Today
  the `Say()` at give-up keeps the process alive, and the watchdog's
  `Test-LoginInProgress` then suppresses re-prompts. That is today's outage state.
- **"Abandoned instance" (IBC alive, no prompt)** is not abandoned. It is almost always a warm
  restart whose token IBKR rejected (the `Gateway` message box, `LoginState LOGGED_OUT`).
  Unattended behaviour: read the IBC/launcher log tail.
  - `Authorization failed` / `LOGGED_OUT` → kill the IBC tree and start a fresh login
    immediately. A touch is needed anyway; waiting hours on OKCancel only adds darkness
    (14 h, 13.6 h, 10.5 h, 2.5 h in RETRO §3.5).
  - `Starting Gateway` less than 5 min ago with no auth outcome yet → a restart is in progress;
    wait, don't touch.
  - Only a human-invoked run (desktop shortcut) may ask, and the text must say "this costs a
    YubiKey touch".
- **Adoption** (a prompt is already pending) also starts the elevated helper (GATEWAY_AUTOLOGIN
  §4.5).
- **Archive right before every IBC launch**, not at script start. The autostart path archives
  too. That closes the evidence gaps (the Nov 15 and Nov 20 sessions were lost).
- **Watchdog `Start-LiveLogin`** launches hidden (`-WindowStyle Hidden`) with `-Unattended`.
- **Concurrency:** one login at a time per box. Keep the existing guards. The autostart and the
  watchdog must use the same named mutex, because on Nov 6, Nov 19 and Nov 21 they launched
  overlapping logins.
- **Throttle guard:** at most 3 IBC launches per 30 min. After that, page "IBKR throttle risk"
  and freeze for 30 min (Nov 19: 7 launches in 22 min).

### 3.8 Detection and alerting

- **Touch ledger** `C:\IBC-Live\Logs\touch-ledger.jsonl`. One line per full auth: UTC time,
  weekday, trigger, planned/unplanned, minutes dark, RTH overlap. Trigger is derived
  automatically from the evidence chain RETRO used:
  - Kernel-Power 41 → `power`
  - User32 1074 → `reboot`
  - 42/107 → `sleep`
  - `Re-login is required` → `ibkr-weekly`
  - `Authorization failed: ... expired` → `token-rejected`
  - weekly window → `planned`
  - anything else → `unknown`

  Written by a small `touch-ledger.ps1` run after every successful login. It is the single
  source for the metrics in §0.
- **Weekly report:** Monday 01:00Z push (`error` level = digest), e.g. "Week N: 1 planned
  touch, 0 unplanned; 7/7 warm restarts accepted". Any unplanned touch in the week →
  `critical` with its trigger.
- **Gateway pages bypass the global cap.** In `gateway-push.sh`, keys `gateway:*` and
  `weekly-touch:*` get their own 20/day budget, separate from `legacy` and other keys. Today's
  page was `suppressed-cap` because `D:\claude\task-heartbeat-check.ps1` used up the shared
  10/day. It uses the legacy single-argument call (critical, key `legacy`) and paged every
  10 min about RustDesk/router heartbeats (RETRO §2.5). *(Implemented §10.)*
- **Legacy callers get a real key and level.** `task-heartbeat-check.ps1`, and any other
  single-argument caller, must call `gateway-push.sh <level> infra:heartbeat:<task> <msg>`. A
  missing heartbeat for a non-trading task (RustDesk, router restart) is `error` (digest), not
  `critical`. Afterwards, `gateway-push.sh` should log a warning to the audit trail whenever the
  1-arg form is used, so new legacy callers are visible. **Not done.** It is outside this repo
  (`D:\claude\`), and the user owns it.
- **Page ladder** (GATEWAY_AUTOLOGIN Phase 2.3): page at grace expiry, re-page every 15 min
  with helper state, freeze relaunches after 2 h and page hourly, then a recovery page. Within
  the Sunday hold, no pages until `T_touch`.
- **Self-audit replacement.** Retire `restart-observe.ps1`/`restart-verify.ps1` (failing
  silently since Oct 17). Replace them with a daily "warm-restart verdict" computed from
  launcher.log after `AutoRestartTime + 10 min` (UTC trigger), written to the ledger. A missing
  verdict (task didn't run or crashed) is itself a page. **A task that fails must page.**
  Add `LastTaskResult != 0` checks for all `\IBKR\*` tasks to the watchdog's hourly pass.

## 4. Weekly touch: when, in the user's own time

`T_touch` candidates. HKT is the gateway clock; also shown in the zones this box has used.

| Option | HKT | EST (now) | PST | Notes |
|---|---|---|---|---|
| A (default) | Sun 20:30 | Sun 07:30 | Sun 04:30 | Same moment as today's daily restart. Comfortably after IBKR's kick |
| B (GATEWAY_AUTOLOGIN choice) | Mon 05:30 | Sun 16:30 | Sun 13:30 | Wake-up touch in HK, Sunday afternoon in the US. Needs `AutoRestartTime=05:30 AM` |
| C | Mon 09:00 | Sun 20:00 | Sun 17:00 | Latest "evening" in the US. 13 h before the HK-time US open |
| D | user-defined | — | — | Any instant Sun 11:00Z → Mon 13:00Z |

The daily `AutoRestartTime` should stay outside US RTH (21:30/22:30 → 05:00 HKT). It does not
need to equal `T_touch` once the conditional cold exists.

## 5. Tests

Extend `scripts\windows\test-min2fa.ps1`, and rename it `test-weekly2fa.ps1` with a shim:
1. Static: `config.ini` has an empty `ColdRestartTime`, and `AutoRestartTime` is in the allowed
   window.
2. Static: no `MessageBox`/`Read-Host` reachable under `-Unattended` (AST scan of
   `ibkr-live-login.ps1`).
3. Static: watchdog `Start-LiveLogin` passes `-WindowStyle Hidden` and `-Unattended`.
4. Static: every `\IBKR\*` time trigger is UTC-anchored. No script compares
   `(Get-Date).Hour` against a literal.
5. Docker: `docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' quant-ibgateway-live-docker`
   is `no` (or the container is absent). The hold file exists. The compose service has no live
   credentials.
6. Fixture: the "abandoned" classifier on archived logs. `20261103_203157` and
   `20261120_043159` → `token-rejected → relogin now`; `20261024_053205` (restart in progress
   at 05:32) → `wait`.
7. Fixture: ledger trigger derivation on the RETRO timeline. All 26 RETRO §1 rows must classify
   as the RETRO did (golden file).
8. Fixture: weekly-window decision table: logged-out → login; logged-in and no full auth since
   the boundary → conditional cold; already satisfied → skip; reboot inside the hold → no
   page until `T_touch`.
9. Fixture: `gateway-push.sh` with `legacy` at cap, then a `gateway:ibc4001 critical` → `sent`
   (extend `gateway_push_test.sh`'s level matrix).
10. Timezone: run 4/6/8 with an injected clock in HKT, PST and EST. Results must be identical.
11. Throttle guard: a 4th launch in 30 min is refused and pages once.
12. `uv run pytest dashboard/tests -q` stays green. No dashboard code changes are expected.

## 6. Rollout

**Phase 0: stop the self-inflicted ones (1 evening, human present, outside RTH)**
1. §3.2 Docker gateway: restart policy `no`, stop, hold file. Verify with a `wsl --shutdown` cycle.
2. §3.3 `ColdRestartTime=` empty. The edit takes effect at the next IBC start. Do it right
   before the weekly touch so the new config loads with that login.
3. §3.7 `-Unattended` path + watchdog hidden launch + shared mutex + throttle guard.
4. §3.8 gateway cap exemption in `gateway-push.sh`. Sync the deployed WSL copy.
5. Suite green.

**Phase 1: measure (2 weeks)**
6. Touch ledger + weekly report + failing-task pages. §3.4 UTC conversion of the scripts the
   ledger depends on.
7. D4 pin timezone. D5/D6 power (user).
8. Gate G1 on token rejections. E2 note-taking runs in parallel.

**Phase 2: the weekly window**
9. `weekly-touch.json`, `weekly-touch.ps1` (UTC task), the Sunday hold in the watchdog, the
   reminder and deadline ladder, the conditional cold.
10. First planned touch with the user present. Check the ledger entry is `planned`.

**Phase 3: levers that depend on decisions/experiments**
11. E1 (dedicated API user) if G1 fails. E3 (staged token) if D8 = yes. `safe-reboot.ps1`.

Rollback for each phase: revert the file. `config.ini` edits keep a `.bak-YYYYMMDD` next to them,
per the precedent in `StartIBC.bat.bak-20261013`.

## 7. Acceptance criteria

1. **Zero Docker live logins.** `docker logs quant-ibgateway-live-docker` shows no new `Login
   attempt` across 2 weeks, including after reboots and Docker daily restarts.
2. **No cold restart.** No `Gateway will be cold restarted at` and no `exiting via [File, Close]`
   in any IBC log except the conditional cold at `T_touch`.
3. **Ledger complete.** Every full auth has a ledger line. The weekly report arrives every Monday.
   Each missing week pages.
4. **Planned touch.** For 3 consecutive weeks, the ledger shows exactly **1** `planned` touch
   inside `T_touch ± 30 min`.
5. **Unplanned touches.** 0 with trigger in {`cold`, `docker`, `manual-close`, `sleep`}. Every
   `reboot`/`power`/`token-rejected` touch is paged within 3 min and reported weekly.
6. **No blocking.** No login process outlives its terminal log line by more than 5 s under
   `-Unattended`. No modal unless a human started the script.
7. **Alerting.** A gateway-down page is delivered even when another key has hit the global cap
   (test 9 + one live injection).
8. **Timezone-proof.** Suite test 10 passes. A real timezone change on the box changes nothing
   in the ledger or schedule. Verify opportunistically; do not tzutil on purpose.
9. **Gate G1 recorded.** The token-rejection rate after Phase 0 is written into this spec,
   with the E1/E3 decision that follows.

## 8. Open decisions for the user

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | When is the weekly touch (`T_touch`)? | A Sun 20:30 HKT / B Mon 05:30 HKT / C Mon 09:00 HKT / custom (§4) | Depends on where you are now. The box spent 17 days on PST and is on EST today. Pick the slot that is a waking hour **where you will be most weeks** |
| D2 | If IBKR has not forced the weekly re-auth by `T_touch`, do we force it (conditional cold)? | yes (predictable, may spend a touch IBKR didn't strictly need) / no (risk of a surprise mid-week forced re-auth) | **yes** |
| D3 | Docker live gateway: stop + restart=no + hold, or delete the container? | stop / `docker rm` | **delete** after Phase 0 has run for a week |
| D4 | Pin the Windows timezone? Which one? Who ran `tzutil` on Nov 4 and Nov 21? | HKT / your local / UTC | Pin to the zone you read logs in. Scripts go UTC regardless |
| D5 | UPS for the gateway PC (5 power losses in 3 weeks) | buy / investigate first | investigate the 41 records first, then buy |
| D6 | Disable sleep/hibernate/fast startup on AC (elevated `powercfg`) | yes / no | **yes** |
| D7 | Add a dedicated IBKR API username for the gateway (E1) | yes / only if G1 fails | **only if G1 fails** |
| D8 | Allow a DPAPI-encrypted staged session token on disk for zero-touch reboot/crash recovery (E3) | yes / no | yes, experiment only, with the §3.6 safety rules. The biggest remaining lever (11 of 22) |
| D9 | Reboot ritual: reboot only in the weekly window, or accept 1 touch per ad-hoc reboot | ritual / accept | ritual, with `safe-reboot.ps1` as the reminder |
| D10 | Is 1 planned + measured IBKR-forced touches acceptable if G1 shows the rejections are IBKR policy? | accept / pursue E1/E3 harder | decide at G1 |

## 9. Rejected / out of scope

- **Remote touch / USB-IP** (GATEWAY_AUTOLOGIN §7). This doesn't reduce touches; it only moves
  where you touch. Revisit if D1 can't find a slot you are reliably at the PC for.
- **Software authenticator or relaying the challenge.** Rejected Oct 6 and still rejected: both
  factors on one box, or tampering with the broker's auth client.
- **Longer watchdog grace.** It would not save a touch. Rejected tokens are final at second 4
  (RETRO §3.2), and 10/10 accepted restarts finished within 2.5 min.
- **Disabling the daily restart.** IBKR forces a daily restart or logoff (§1.4).
- **Windows auto-logon** (GATEWAY_AUTOLOGIN §7). It doesn't help: every reboot needs a full
  auth anyway. Only E3 changes that.

## 10. Implementation record (2026-11-21, deployed 16:50 HKT)

The user decided **D4 = HKT**: every log line and every schedule is HKT. It is computed from
UTC (`[DateTime]::UtcNow + 8h`), so it no longer matters what the Windows timezone is. The
Windows clock itself was **not** changed; it is still EST, set by an unknown `tzutil` run.

Other decisions taken with the spec's recommendations, all changeable:
- **D1 = A:** Sun **20:35** HKT. That is 5 min after the gateway's own 20:30 daily restart, so
  the two don't collide. Edit `C:\IBC-Live\weekly-touch.json` to move it.
- **Daily restart moved to 05:30 HKT (user decision, 2026-11-21 ~19:20 HKT, relayed from the
  main session: "Move to 05:30", because the 20:30 restart prompt is annoying).** This is the
  `AutoRestartTime` half of option B and the GATEWAY_AUTOLOGIN Phase 1 migration.
  `C:\IBC-Live\config.ini:609` `AutoRestartTime=05:45 AM` (05:45, not 05:30: see the router
  bullet; backup `config.ini.bak-20261121-0530`); `ColdRestartTime` stays empty. It takes effect
  at the next gateway start (the weekly touch, Sun Nov 22 20:35, unless you log in earlier). The **weekly touch was NOT moved** to Mon 05:30: under B the planned 2FA prompt
  would fire while you are asleep and time out. It stays Sun 20:35 until you choose a slot
  (A Sun 20:35 / C Mon 09:00 / custom).
  - **Router collision → user chose gateway 05:45 (2026-11-21 ~19:40 HKT).** The router reboot
    stays 05:30 HKT (`Xiaomi Router Restart Daily 0530`, UTC-anchored); same-minute router + warm
    restart cost 2 touches in October (RETRO §3.2 H3). So `AutoRestartTime=05:45 AM`, 15 min after
    the router starts (longest logged outage 102 s). **First restart: Mon 2026-11-23 05:45 HKT.**
    Suite check `router reboot >= 10 min away from the gateway daily restart` guards it.
  - Night risk: a token rejection at 05:30 now starts an unattended 2FA prompt while you sleep;
    it times out and the watchdog retries within the launch budget (3 per 30 min). The market
    is closed until 21:30/22:30 HKT, so a night quiet window (no auto-prompt until morning) is a
    candidate follow-up; not implemented.
- **D2 = yes** (conditional cold restart).
- **D3:** stopped and parked, not deleted.

### What shipped

| Spec | Change | Where |
|---|---|---|
| §3.2 | Docker LIVE gateway stopped, `restart=no`, hold file written. Compose: `restart: "no"` and `.env.live` removed, so it cannot log into LIVE even on `--with-gateway` | container `quant-ibgateway-live-docker`; `docker-compose.live.yml` |
| §3.3 | `ColdRestartTime=` (empty). Backup at `C:\IBC-Live\config.ini.bak-20261121` | `C:\IBC-Live\config.ini` |
| §3.4 (HKT) | Gateway JVM gets `-Duser.timezone=Asia/Hong_Kong`, so IBC and launcher logs are HKT from the next IBC start. Backup `.bak-20261121` | `C:\Jts\ibgateway\1047\ibgateway.vmoptions` |
| §3.4 (HKT) | Shared helpers: HKT clock and logs, IBC state classifier, archive-before-launch, login mutex, launch budget, weekly window, touch ledger | `scripts/windows/ibkr-common.ps1` (new) |
| §3.7 | Login script rewritten (see below) | `scripts/windows/ibkr-live-login.ps1` |
| §3.1 | Logon autostart rewritten (see below) | `scripts/windows/ibkr-live-autostart.ps1` |
| §3.1 | Weekly touch: `reminder` / `touch` (skip, conditional cold, or login) / `report`; idempotent per week; `-DryRun` | `scripts/windows/weekly-touch.ps1` (new), `C:\IBC-Live\weekly-touch.json` |
| §3.1/3.7/3.8 | Watchdog changes (see below) | `watchdog.ps1` (restarted 16:48 HKT) |
| §3.8 | Gateway and weekly-touch keys get their own 20/day cap (today's page was eaten by the shared cap). Cap day is the HKT day. `GATEWAY_PUSH_DRYRUN` test hook. Synced to `/home/cap/quant/scripts/` | `scripts/gateway-push.sh` |
| §3.8 | `\IBKR\Restart Observe` / `\IBKR\Restart Verify` **disabled** (failing silently since Oct 17). They are replaced by the ledger and the task-failure page | Task Scheduler |
| §5 | New suite. `test-min2fa.ps1` is marked superseded | `scripts/windows/test-weekly2fa.ps1` |
| §8 | (added 19:30 HKT, user decisions) Daily `AutoRestartTime` 08:30 PM → **05:45 AM** (05:30 requested, then 05:45 to clear the 05:30 router reboot); weekly touch unchanged. Suite +2 checks: router reboot ≥ 10 min from the daily restart, router guard dry run. Watchdog liveness check no longer needs a fresh log line (the watchdog logs on state changes only); it checks CPU across 45 s. **46/46** | `C:\IBC-Live\config.ini`, `scripts/windows/test-weekly2fa.ps1`, `watchdog.ps1` (comment only) |
| §3.7/3.8 | (added Tue 2026-11-24 ~13:00 HKT, RETRO_2FA_2026-11-24 F2–F4, user go-ahead) **Gateway-down policy** `Get-GatewayDownAction` in `ibkr-common.ps1`, called by the watchdog every cycle past grace, outside the weekend hold. **F2 key absent:** no login launch; page once; poll `VID_1050` every 20 s cycle; launch the moment the key appears (even in the quiet window, no page). The login script no longer pages on a missing key. The weekly reminder checks the key ("KEY NOT PLUGGED IN"). The weekly touch does not launch without the key (`nokey-wait`: page, the watchdog launches on insert) and never stops a working session without it (`nokey-keep`). **F3 pages:** one per down incident, then only when a checkpoint (`repageAt`, default 08:00/12:00/18:00/21:30 HKT) is crossed. Re-prompts keep their cadence (5 min for 2 h, then hourly) but do not page. Watchdog-launched logins do not page on the launch budget. If the weekly touch already paged for the incident, that counts as the page. **F4 quiet window** `quietStart`–`quietEnd` (default 05:00–08:00 HKT, US market shut): no prompt, no page; at 08:00 one prompt plus one page. Defaults are in `Get-WeeklyConfig` and overridable in `weekly-touch.json`. Replays: the Nov 22–23 key-absent night goes from ~20 pages + 24 launches to 5 pages + 1 launch (on insert); the Nov 24 05:45 rejection goes from a 05:47 prompt to 08:00. Suite +8 checks: **54/54**. **The running watchdog (pid 19220, started Sun 18:27) still has the old code until it is restarted**; the login and weekly-touch changes are live now | `scripts/windows/ibkr-common.ps1`, `watchdog.ps1`, `scripts/windows/ibkr-live-login.ps1`, `scripts/windows/weekly-touch.ps1`, `scripts/windows/test-weekly2fa.ps1` |
| §3.7 | (added Thu 2026-11-26 ~21:30 HKT, user request "independent of Claude") **No dependence on the Claude desktop app.** Processes started from a Claude Code shell join that session's Windows job and died when the app quit (Wed 25 16:41: watchdog + LIVE gateway, extra touch). `Test-UnderClaude` (job members or ancestors include `claude.exe`); the watchdog, the login and the autostart hand over to on-demand Task Scheduler tasks and exit (`\IBKR\Quant Watchdog Start`, `\IBKR\Live Login`, `\IBKR\Live Auto-Start`). A 120 s loop brake per task. Watchdog moved under Task Scheduler 21:25. Suite +4: **58/58** | `scripts/windows/ibkr-common.ps1`, `watchdog.ps1`, `scripts/windows/ibkr-live-login.ps1`, `scripts/windows/ibkr-live-autostart.ps1`, `scripts/windows/ibkr-live-login-task.vbs` (new), `scripts/windows/setup-claude-independent-launchers.ps1` (new), `scripts/windows/test-weekly2fa.ps1` |
| §3.4 | (added 19:15 HKT, user request) Router reboot guard uses HKT from UTC, not the Windows clock. Since Nov 4 it had been rebooting at 04:30 HKT = 15:30 New York (in RTH); back to 05:30 HKT. Both router logs stamped `... HKT`. `/dryrun` + `/hkt:` test hooks. Outside this repo; backups `*.bak-20261121`. RETRO §3.2 H3: the router reboot colliding with the old 05:30 warm restart caused 2 of the 7 "token rejected" touches | `D:\claude\xiaomi-router-reboot.vbs`, `.ps1` (Write-Log only) |

Login script (`ibkr-live-login.ps1`):
- `-Unattended` mode: never a MessageBox, and the process always exits.
- One login run per box (mutex), shared with autostart and the weekly touch.
- An existing IBC instance is classified before anything is done to it:
  - `starting`: wait, never kill.
  - `prompt-pending`: adopt it **and** start the helper.
  - `token-rejected` / `transient` / `logged-out`: clear it and log in at once. No hours-long
    OKCancel.
  - `unknown` with 4001 up: never killed unattended.
- At most 3 IBC launches per 30 min.
- Logs are archived right before each launch.
- Every successful login updates the touch ledger.

Logon autostart (`ibkr-live-autostart.ps1`):
- Respects the **weekend hold** (Sat 06:00 → Sun 20:35 HKT): no login, a toast instead.
- Takes the login mutex and the launch budget, archives before launch, writes the ledger.
- No modals.

Watchdog (`watchdog.ps1`):
- HKT logs.
- Login launches are hidden and `-Unattended`.
- **Weekend hold:** no login, digest-only.
- Records the down cause to `gateway-down.state.json`.
- Dispatches the weekly phases, with catch-up if the box was off at the minute.
- Ledger scan every ~5 min.
- After 2 h down: hourly re-prompts only.
- Pages hourly for any failing `\IBKR\*` task.

New files at runtime (all HKT):
- `C:\IBC-Live\Logs\touch-ledger.jsonl`: one line per `full-auth` (with `cause`, `planned`,
  `week`, `rth`), `token-login`, or `warm-restart` verdict.
- `weekly-touch.log`, `weekly-touch.state.json`, `ibc-launches.txt`, `gateway-down.state.json`.

### Verification (2026-11-21)

- `test-weekly2fa.ps1`: **44/44 PASS** after the watchdog restart. 43/44 before it; the only
  failure was the old watchdog still logging in the local clock. The suite covers:
  - parse of all 7 scripts
  - config / vmoptions / compose / Docker state
  - AST proof that the only MessageBox sits in `Ask()`, which returns early under
    `-Unattended`
  - classifier on 4 real archived incidents (Nov 3 + Nov 20 token rejections → relogin;
    Oct 24 slow restart → wait; today → logged-out)
  - ledger parsing of real logs (Nov 19 full auth, Nov 9 full + token login, Nov 3 rejected /
    Nov 9 accepted warm restarts)
  - planned-vs-unplanned and idempotency
  - 9 injected-clock weekly-window cases
  - HKT clock independent of the Windows TZ (box is on EST)
  - launch budget, cross-process login lock
  - push matrix 11/11 including the cap exemption
  - `weekly-touch -DryRun` changes nothing
  - watchdog alive with HKT stamps
- **Live:**
  - The new watchdog saw the down gateway and recorded `cause=logged-out`. Because of the
    weekend hold it logged `no login, no page; weekly touch at 2026-11-22 20:35:00 HKT`, and
    the audit trail shows an `error`-level `gateway:hold` (digest), not a page.
  - The real login script, run while the lock was held, exited 3 `REJECTED` and touched
    nothing.
  - Dry runs of reminder and report logged the right messages.
  - The stale MessageBox process from this morning's old login script (pid 20444) was closed.
    It had been keeping the old watchdog in "login in progress" mode.
- **Not verified live (needs a touch):** the full unattended login after a classifier
  `logged-out`, and the real `touch` phase. **First real run: Sun 2026-11-22 20:25 reminder →
  20:35 prompt.** Check afterwards:
  - `weekly-touch.log` shows `decision=login`
  - `live-login.log` shows `existing IBC-Live state: logged-out` → `old session cleared` →
    `login confirmed`
  - `touch-ledger.jsonl` has a `full-auth` with `cause=weekly-touch planned=true`
  - IBC and launcher logs now stamp `Starting Gateway` in HKT (vmoptions)

### Still open

- **G1 measurement (2 weeks):** watch `warm-restart` verdicts in the ledger now that Docker is
  parked. Then decide E1/E2.
- E3 staged token (D8), `safe-reboot.ps1` (D9), power plan (D6), UPS (D5): user decisions,
  not done.
- Transient-retry (§3.6.3): not done (needs E3).
- Page text with helper state (§3.8 page ladder detail): pages are still the plain
  "re-launching" text.
- No history backfill: the ledger starts empty on 2026-11-21. The Oct 22 → Nov 21 history is
  in `RETRO_2FA_2026-11-21.md`.
- IBC still auto-clicks "Re-login" on IBKR's Sunday kick (bounded, Sunday only). This is
  accepted per §3.1.
- Windows timezone left on EST. Pin it with `tzutil /s "China Standard Time"` if you want the
  Windows clock itself in HKT; nothing depends on it any more.
- Nothing is committed.
