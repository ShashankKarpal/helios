# Helios brief check: the 09:05 iPhone alert (retired)

**Retired 2026-10-27 (owner decision).** The owner opens Helios Bridge every
morning, and its widget shows the brief's state, so the alarm is not needed;
this replaces the 2026-10-23 alert channel A decision. To switch the two
automations off on the iPhone (Apple's iOS 27 Shortcuts guide): tap the
shortcut, choose its Time of Day trigger and remove it; the shortcuts
themselves stay. What changed around it:

- `GET /api/brief-check` stays: the MCP freshness tool and `/api/health`
  carry the same line. The brief is now due at 10:00 in the reporting zone
  (was 09:00), so the line says `early: ...` until 10:00 and `late: ...` from
  10:00; the morning step publishes never before 07:30 and at the latest at
  09:30.
- `make_shortcuts.py` and `local.example.json` are kept for history; the
  builder still builds the two files it always built.
- The afternoon pair for trips east (below, under "Not covered") is dropped.

The rest of this page is the design as it ran from 2026-10-23 to 2026-10-27.

Two Shortcuts on the iPhone, built by `make_shortcuts.py` in this folder. At
09:05 **Helios Brief Check** asks the Mac for one line about this morning's
brief and writes it to a small file. At 09:10 **Helios Brief Guard**, which
needs no network, reads the file. Unless today's line says `ok` or `early`, it
shows a notification and sets a Clock alarm two minutes ahead. So the phone
rings when the brief is late, when the Mac answers anything unexpected (an
error page, an old build), and when the Mac does not answer at all (asleep, off
the tailnet, heliosd down). A Mac that cannot be reached never means silence.

## What the Mac answers

`GET /api/brief-check` is an open route (no token), plain text. It reads only
the brief status file, never the store or the local model:

- `ok`: today's complete brief exists (last night's Whoop recovery is in and at
  least one settled number backs it). heliosd publishes it by itself every
  morning (the morning step), so this does not depend on opening the app.
- `early: no complete brief yet; due 09:00 in the reporting zone`: no complete
  brief yet, and it is not 09:00 in the reporting zone. Quiet.
- `late: no complete brief for today; the last complete one is N h old`: past
  09:00 in the reporting zone with no complete brief. The phone rings.

## How the two shortcuts work

**Helios Brief Check** (09:05):

1. Format today's date as `yyyy-MM-dd`.
2. Save the text `pending` to `helios-check.txt` in the Shortcuts folder of
   iCloud Drive, overwriting it.
3. Get Contents of URL: `GET https://<host>/api/brief-check`, no headers.
4. Save `<date> <answer>` to the same file, overwriting it, whatever the
   answer was. For example `2026-10-26 ok`.

If step 3 cannot reach the Mac, the shortcut stops there with an error and the
file keeps `pending`.

**Helios Brief Guard** (09:10, no network):

1. Format today's date the same way and read `helios-check.txt` (no file
   picker; a missing file reads as empty).
2. If the text contains `<today> ok`, stop quietly. Otherwise, if it contains
   `<today> early`, stop quietly.
3. Otherwise: show a notification titled "Helios brief check" that reads "The
   morning check did not pass. Last result: <the line>", and set a Clock alarm
   two minutes later labelled "Helios brief check".

| What happened | The file at 09:10 | The Guard |
|---|---|---|
| Brief ready | `<today> ok` | quiet |
| Before 09:00 in the reporting zone | `<today> early: ...` | quiet |
| Brief late | `<today> late: ...` | notification and alarm |
| Mac asleep, off the tailnet, heliosd down | `pending` | notification and alarm |
| Unexpected answer (an error page) | `<today> <that answer>` | notification and alarm |
| The Check did not run (phone off at 09:05) | yesterday's line | notification and alarm |

Why an alarm as well as a notification: a Focus (Sleep, Do Not Disturb, Work)
holds notifications back, and Shortcuts is a system service on the iPhone, not
an app. On an iPhone running iOS 27 it is not listed under Settings,
Notifications, and it cannot be added to a Focus's allowed apps (seen on the
phone; Apple's pages do not say). Apple's iOS 27 Clock page says an alarm
sounds even in Silent mode and during Do Not Disturb or another Focus. The
notification is kept because it carries the Mac's own words.

## Build and install

On the Mac:

1. Copy `local.example.json` to `local.json` in this folder and set `host` to
   the Mac's Tailscale MagicDNS name and port, the same name the web app uses
   (for example `name.example.ts.net:8420`). git ignores `local.json`; the
   name never goes into a tracked file.
2. Build: `python3 shortcuts/make_shortcuts.py`. It writes
   `Helios Brief Check.unsigned.shortcut` and
   `Helios Brief Guard.unsigned.shortcut` into `shortcuts/build/`, which git
   also ignores (the files carry the name). It refuses to build from a
   missing config, the placeholder, or an address without a port.
3. Sign each file:

   ```
   cd shortcuts/build
   shortcuts sign --input "Helios Brief Check.unsigned.shortcut" --output "Helios Brief Check.shortcut"
   shortcuts sign --input "Helios Brief Guard.unsigned.shortcut" --output "Helios Brief Guard.shortcut"
   ```

   The default mode is `people-who-know-me`; `--mode anyone` also exists.
4. Open the two signed files. Shortcuts on the Mac adds them, and iCloud syncs
   them to the iPhone. If a shortcut of the same name is already there,
   replace it or delete the old one first, then set its trigger again on the
   iPhone (unconfirmed whether a replaced shortcut keeps its trigger).

On the iPhone (Apple's Shortcuts User Guide for iOS 27):

5. Tailscale connected and the mkcert root trusted (done for the web app).
6. For each shortcut: tap it, tap Edit, then Automation. Choose Time of Day,
   set the time and Repeat Daily: **09:05** for Helios Brief Check, **09:10**
   for Helios Brief Guard. Set the triggers on the iPhone itself: Apple says
   automations are specific to a device and sync to other devices turned off.
7. For each shortcut: tap it, tap Edit, then the Info button. Tap Privacy and
   turn on **Allow Running When Locked**. Apple: "The automation will not
   notify you when it's triggered." Without it, iOS asks you to run the
   automation each time.
8. Run Helios Brief Check once by hand. If iOS asks whether the shortcut may
   connect to the Mac's name, choose **Always Allow**; Allow Once would make
   every unattended run wait for a tap.
9. Check the alarm volume once: Settings, Sounds & Haptics, below Alarms and
   Timers. The slider must not be at the bottom.

The times are the phone's local time.

## Test it

Do these in order, with no Focus on until test 4.

1. **Healthy.** Tailscale on. Run Helios Brief Check, then Helios Brief Guard.
   Pass: nothing appears and no alarm is set (before 09:00 in the reporting
   zone the answer is `early`, also quiet).
2. **Unreachable.** Tailscale off. Run Helios Brief Check. Pass: Shortcuts
   shows an error (it cannot find the host). Run Helios Brief Guard. Pass: the
   notification ends "Last result: pending", and an alarm rings about two
   minutes later. Stop it, then delete it (Clock, Alarms, swipe left, Trash).
   Turn Tailscale on, run the Check, then the Guard: nothing.
3. **A wrong answer.** In Helios Brief Check, change the end of the URL to
   `/api/brief-checkx` and run it. An unknown address answers
   `{"detail":"bad or missing X-Helios-Token"}`. Run Helios Brief Guard. Pass:
   the notification shows that line (or "pending" if Shortcuts stops on the
   refusal, unconfirmed which) and the alarm rings. Change the URL back, run
   the Check, then the Guard: nothing.
4. **The proof: unattended, locked, with a Focus on.** Tailscale off, Do Not
   Disturb on. Set the Check's trigger to two minutes from now and the
   Guard's to seven minutes from now, lock the phone and leave it. Pass: the
   alarm rings through Do Not Disturb about nine minutes from now (the
   notification may stay held back until you unlock). Then set the triggers
   back to 09:05 and 09:10, Tailscale on, Do Not Disturb off, delete the alarm.

Proven on an iPhone on iOS 27 in October 2026: test 2 (the host error, "Last
result: pending", the alarm two minutes later), and unattended runs that rang
on two mornings while the Mac slept. Not yet reported: test 4 with a Focus on.

## Not covered

- A phone that is off, or restarted and not yet unlocked, at 09:10
  (unconfirmed whether automations run before the first unlock).
- A trip east of the reporting zone: 09:05 local comes before 09:00 there, so
  the Mac answers `early` (quiet) and nothing checks again that day. An
  afternoon pair would have closed this; dropped with the retirement.
- A day Whoop never scores the night: the Guard rings, because there is no
  complete morning brief that day.
- Alarm volume at the bottom.
- Each failure adds a one-time alarm to Clock. Delete old ones now and then
  (unconfirmed whether iOS removes a one-time alarm after it rings).

## Unconfirmed

Apple's iOS 27 pages do not say these; the tests or a later look decide them.

1. Whether automations run before the first unlock after a restart.
2. Whether a shortcut replaced by an import keeps its trigger.
3. Whether Get Contents of URL continues with the body of a 401 or stops.
4. Whether any action needs its own setting to run unattended (Apple: "You
   also may need to set individual actions to run automatically"; the
   unattended mornings above ran without one).
5. Whether a one-time alarm stays in Clock's list after it rings.

## Apple pages used (iOS 27, read 2026-10-26)

- Set an alarm in Clock on iPhone:
  https://support.apple.com/guide/iphone/set-an-alarm-iph2909d3a74/27/ios/27
- Add automations to Shortcuts on iPhone or iPad:
  https://support.apple.com/guide/shortcuts/add-automations-apdfbdbd7123/10.0/ios/27
- Intro to shortcuts with automations on iPhone or iPad:
  https://support.apple.com/guide/shortcuts/intro-to-automation-shortcuts-apd690170742/10.0/ios/27
- Adjust basic privacy settings in Shortcuts on iPhone and iPad:
  https://support.apple.com/guide/shortcuts/adjust-privacy-settings-apd961a4fc65/10.0/ios/27
