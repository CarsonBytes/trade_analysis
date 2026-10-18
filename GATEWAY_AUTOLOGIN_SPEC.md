# Gateway AutoLogin Spec (GATEWAY_AUTOLOGIN)

Status: design **approved 2026-10-18** by user. Two decisions locked in:
restarts move to **05:30 HKT**, and every automated login must **complete silently**
(no console, no OK dialogs -- only the YubiKey touch). Implementation pending
(phases below). Companion docs: `IBKR_GATEWAY_PASSKEY_SPEC.md` (ceremony, passkey
mechanics), `NOTIFICATION_SPEC` (alert ladder conventions).

## 1. Why -- gateway-down retro (Sep 1 -> Oct 18, 2026)

| When | Incident | Root cause |
|---|---|---|
| Sep 1 - Oct 2 | ~10 full logins/week; dark windows **Oct 2 05:30->17:42 (12h)**, Oct 3 05:30->10:16 (4h46m) | no `AutoRestartTime`; 20:00 hard relogin; missed IB-Key push taps; no self-relaunch |
| Oct 3-5 | passkey-only discovered; 105 failed handshakes, 89 "Too many failed login attempts" | SFC HK passkey mandate; `RELOGIN=yes` misread it as missed push |
| Oct 6 | hold switch + auth probe + `RELOGIN=no` | stop the throttle loop |
| Oct 13-14 | native Windows IBC-Live + YubiKey + elevated PIN helper + 2-hop relay | JxBrowser WebAuthn never reaches Windows (no Hello, no QR; UV required) -- human reduced to **touch only** |
| Oct 15 23:24 -> Oct 17 14:52 | **40.5h dark**: passkey prompt raised by logon autostart, no touch for 40h | human away; **watchdog had no gateway checks yet** (`gateway:ibc4001` has paged exactly once in history before today) |
| Oct 16 10:25 | relay dead till Oct 17 (0xC000013A console close) | bare accept loop -> resilient relay (Oct 17) |
| Oct 17 15:28-15:41 | watchdog gateway alert/auto-relaunch layer **built** (direct response to the 40.5h outage) | -- |
| Oct 18 18:39 -> 18:41 | death ~18:39 (churn 18:24-18:38, no hs_err/EventLog crash); **recovered 52s** by the new layer | suspect IBKR Sunday maintenance window (Sun 18:00-19:30 HKT = 06:00-07:30 ET); evidence lost (IBC log overwritten) |
| Oct 18 20:30 | **Sunday weekly cold restart** (announced in config since Oct 14; `verify` codified `sunday-cold-night = full passkey by design`) -> full auth needed | IBKR weekly boundary: first restart after Sunday 01:00 ET requires full login. Chain defect: adoption path waited 18 min without starting the helper (20:32-20:50), session self-exited 20:50:36, re-prompt **recovered 20:55:09** |
| Oct 18 21:04:58 | user closed the login screen -> whole gateway exited (`Normal exit`) | the passkey screen **IS** the main `IBKR Gateway` window (single frame); close = close the app. **Rule: minimize, never close.** Watchdog recovered **21:07:44** (3rd auto-recovery today) |

Lessons: (a) **silence** was the killer (40h dark with zero pages) -- alerting now exists;
(b) every restart after an unexpected death costs a touch unless the session token is reused;
(c) evidence was being overwritten by the next launch (archive-on-login now exists);
(d) the adoption path and modal dialogs wasted 18 min of tonight's ceremony (fixes below).

## 2. Token & restart mechanics (verified from logs, 2026-10-14..18)

- **Autorestart file**: `C:\Jts-Live\ajaninkbfndcbgmelgahgpfcmhmgggpefgeamgbo\autorestart`.
  Written by the gateway only at a healthy scheduled shutdown; consumed and **deleted ~2s
  later** at next start (`launcher.log`: `Reading data from ...` -> `AutoRestartDescriptor
  [autoRestartType=DAILY, sessionId=..., sessionToken=XYZSessionToken[type=TST,...]]` ->
  `Deleting ...`). Content = serialized descriptor incl. the **TST session token** -- it is
  NOT trivial/synthesizable; a real copy must be captured.
- With the file: IBC logs `authentication will not be required`, java starts with
  `restart=<dir>, authToken=TST` -> **no 2FA**. Without it: `full authentication will be
  required`, `restart=, authToken=null` -> passkey ceremony.
- **Warm** = daily `AutoRestartTime` restart with the file (token reuse, promptless).
  **Cold** = `ColdRestartTime` (config.ini:643, currently `20:30`): forced full auth; the
  sentence `Gateway will be cold restarted at <date> <time>` is grepped by
  `restart-observe.ps1:107` and asserted by `restart-verify.ps1` (cold night = skip
  token_finding).
- **Weekly boundary** (IBC userguide): full login on the first restart after **Sunday
  01:00 ET**. Timezone math (HKT = ET+12 in EDT):
  - 20:30 schedule: Sun 20:30 HKT = Sun 08:30 ET -> past boundary -> weekly touch lands
    **Sunday evening, human present, zero dark** (today's pattern), but interrupts evenings.
  - 05:30 schedule (chosen): Sat/Sun 05:30 HKT = Fri/Sat 17:30/18:30 ET -> pre-boundary,
    warm/token-only; **Mon 05:30 HKT = Sun 17:30 ET -> weekly touch lands at wake-up**.
    Market opens 21:30 HKT -> a 05:30-to-wake dark window has zero trading exposure.
- **Ceremony**: chooser "安全性金鑰 Security key" -> PIN (elevated `\IBKR\Passkey Helper`
  task) -> human touch. UV required, hybrid transport disabled -> USB key + PIN only.
  A late touch always completes (IBC left running by design, `KeepAlive` default).

## 3. Decision -- restarts move to 05:30 HKT (approved 2026-10-18)

Rationale: no evening prompt; the weekly touch becomes a wake-up touch; daily warm
restarts stay promptless; no market-hours risk. Consequences locked in:
- This week's boundary already satisfied by today's full logins (18:41/20:54/21:07).
- Config takes effect at the next gateway/chain start (see Phase 1.4) -> first migrated
  warm restart Tue 2026-10-20 05:30; **first weekly touch under the new schedule:
  Mon 2026-10-26 05:30**.
- Open point (Phase 1.1): `ColdRestartTime` day semantics. If IBC derives "next Sunday",
  Sunday 05:30 is pre-boundary and a cold there spends a touch IBKR did not require;
  preferred: cold aligned to the **first post-boundary restart (Mon 05:30)**, else accept
  a Sunday-morning cold (one touch/week either way). Resolve from config comments/IBC
  userguide before editing.

## 4. Silent completion -- no windows, no OK clicks (requirement added 2026-10-18)

Current offenders (user must click OK after every run today):
- `scripts/windows/ibkr-live-login.ps1:75` `Say()` = modal `MessageBox` on **every**
  terminal state: success (lines 109/121/127/191/198/229/236), timeout/failure
  (133/141/156/247/251). The adoption modal (121) even blocks the watch-loop: the loop
  at 123 only starts after the user clicks OK.
- `watchdog.ps1:132` `Start-LiveLogin` launches `powershell.exe` **without
  `-WindowStyle Hidden`** (unlike `Show-Toast` at :122) -> a visible console sits through
  the whole ceremony.
- `ibkr-live-autostart.ps1:14` already states the house rule: *"No MessageBoxes anywhere
  -- runs hidden at every logon and must never block the desktop."*

Rules to implement:
1. **Terminal-state reports become toasts + log** (reuse `scripts/windows/toast.ps1`,
   auto-expiring): success, adopted, gave-up/timeout, already-logged-in. Zero clicks.
2. **Modal dialogs survive ONLY where the flow blocks on the human's decision**:
   abandoned-session OKCancel (137), stale Security windows OKCancel (151), USB-hub
   warning OKCancel (161). Never on the happy path.
3. Every console launch is hidden: `Start-LiveLogin` gains `-WindowStyle Hidden`
   (shortcut/VBS paths are already hidden via `wscript`).
4. Invariants (add to `test-min2fa.ps1`): no `Read-Host` in the login chain (holds
   today); no `MessageBox` on success paths; watchdog login launches are Hidden.
5. Adoption branch also **starts the elevated helper** (today's 18-min dead wait: prompt
   adopted at 20:32 but nobody clicked the chooser/entered PIN until the session died).
   Helper is safe if the dialog is already handled -- it no-ops after 60s (precedent
   2026-10-15 23:27 "no Security dialog for this attempt -- nothing done").

Acceptance: a full watchdog-triggered or manual login ends with **only** the YubiKey
touch as user input; no console/MessageBox remains >3s after the terminal log line.

## 5. Implementation plan

**Phase 1 -- 05:30 migration**
1. Read `C:\IBC-Live\config.ini` ~595-660 (AutoRestartTime/ColdRestartTime comments) and
   resolve the cold-day open point (spec S3).
2. Edit `AutoRestartTime=08:30 PM -> 05:30 AM` (+ ColdRestartTime per step 1).
3. Re-anchor every 20:30-driven piece: `restart-observe.ps1` pre-window `20:00-20:31` ->
   `05:00-05:31`; scheduled observe (20:29/20:34/20:38/20:43) and verify (20:50) tasks ->
   05:29/05:34/05:38/05:43 + 05:50; `restart-verify.ps1` cold-night day logic and expected
   cold sentence (`2026/10/25 20:30` record no longer exists); `test-min2fa.ps1` fixtures
   (~L157, ~L166) and `sunday-cold-night` skip; `autorestart-check` writer; HANDOFF notes.
4. Verify config re-read cadence: after the next chain restart, the IBC settings dump
   must show `AutoRestartTime=05:30 AM` in the live IBC log.
5. While re-registering tasks: root-cause tonight's misses -- observe task `lastRun` stuck
   at 2026-10-17 and no verify run at 20:50 tonight.

**Phase 2 -- autologin hardening**
1. Silent-completion fixes (spec S4) incl. adoption->helper.
2. **Staged 2FA token for crash restarts** (token preservation): capture a real
   `autorestart` file via a 1s-poll window `05:29:40-05:31:30` (2s consume window makes a
   20s poll useless) -> `C:\IBC-Live\Logs\staged\autorestart.<date>`. On an
   unexpected-death relaunch (watchdog/manual, before launching IBC): if staged copy is
   <=7d old, place it at the settings path first -> IBC says "authentication will not be
   required" -> java reuses the TST -> **zero-touch crash recovery while IBKR still holds
   the session**; if IBKR rejects, the normal 2FA flow proceeds (no regression). Scheduled
   warm restarts keep the existing write/consume chain untouched.
3. Page ladder (anti-silence, anti-storm): page at grace expiry (exists) -> re-page every
   15min while down with elapsed + state (read `passkey-helper.log` to distinguish
   "PIN entered -- touch now" vs "waiting for PIN") -> after 2h freeze re-launching and
   page hourly "needs human" -> recovery page (exists, `gateway:recovered`).
4. Death-archive coverage: `Archive-IbcLogs` dedup is 120s -- confirm every launch path
   snapshots before overwrite (the Oct 18 18:39 cause was lost before this existed).
5. Maintenance annotation: pages during Sun 18:00-19:30 HKT append "(IBKR Sunday
   maintenance window likely)".

**Phase 3 -- validation**
- `test-min2fa.ps1` all green (incl. new S4 invariants); observe/verify dry-runs with
  `-NoPush`; restart the watchdog via the Startup VBS to load changes; watch Mon 20:30
  chain (last old-schedule cycle) and Tue 05:30 first migrated warm restart
  (expect `autorestart file found ... authentication will not be required`, no prompt).

## 6. Success metrics

MTTR p50 <3 min with a human present (Oct 18: 52s/25min/3min -- middle case is the
adoption defect fixed by S4.5); zero un-paged darkness >30 min; zero IBKR-throttled
login incidents; **1 human action per week (touch), 0 post-login clicks**.

## 7. P2 open

- **Remote touch**: USB/IP (`usbipd` + Tailscale) to touch the key from another device --
  the only path to zero local presence. No software alternative exists (UV-required,
  hybrid disabled -- proven Oct 6/13).
- **Unattended reboot**: auto-start tasks are AtLogOn/interactive; a reboot without a
  user logon leaves the gateway dark until logon (Windows auto-logon = security
  tradeoff, user decision).
- **Close-screen decoupling**: not configurable (auth frame is the main window);
  behavioral rule only (S1) + watchdog recovery as the net.
