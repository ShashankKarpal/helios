# Helios brief check: the 09:05 iPhone alert

Two small Shortcuts and four Time of Day automations. At 09:05 the phone asks
the Mac for one line about this morning's brief. Five minutes later a second
shortcut, which needs no network at all, checks that the first one finished.
So the phone alerts when the brief is late, when the Mac answers with anything
unexpected (an error page, an old build), and when the Mac does not answer at
all (asleep, off the tailnet, heliosd down, a timeout). A Mac that cannot be
reached never means silence. A second pair at 13:05 and 13:10 covers trips east
of the reporting zone, where 09:05 local comes before 09:00 there.

What the Mac answers: `GET /api/brief-check` (open route, no token, plain
text, reads only the brief status file, never the store or the model):

- `ok`: today's complete brief exists (last night's Whoop recovery is in and
  at least one settled number backs it). heliosd publishes it by itself every
  morning (the morning step), so this does not depend on opening the app.
- `early: ...`: no complete brief yet, and it is not yet 09:00 in the
  reporting zone. The phone stays quiet.
- `late: ...`: past 09:00 in the reporting zone with no complete brief. The
  phone alerts with the line.

## Before you start

- Tailscale connected on the iPhone, and the mkcert root trusted (you did this
  for the web app).
- Settings, Notifications, Shortcuts: Allow Notifications on, banners on.
- Settings, Focus: in every Focus that can be on in the morning (Sleep, Do Not
  Disturb, Work), allow notifications from Shortcuts. Otherwise a Focus files
  the alert away without showing it.
- iCloud Drive on for Shortcuts: the two shortcuts share one small file,
  `helios-check.txt`, in iCloud Drive's Shortcuts folder.
- The first time a shortcut reaches the Mac, iOS may ask whether Shortcuts may
  connect to the Mac's name: choose **Always Allow**. "Allow Once" would make
  every unattended run wait for a tap.

## Shortcut 1: Helios Brief Check

In the Shortcuts app, Shortcuts tab, tap **+**, name it **Helios Brief Check**,
then add these actions in this order (search each by name):

1. **Text**: type `pending`.
2. **Save File**: save the **Text** to the Shortcuts folder. Turn **Ask Where
   To Save** off, set the path to `helios-check.txt`, turn **Overwrite If File
   Exists** on.
3. **Get Contents of URL**: URL
   `https://<the Mac's Tailscale MagicDNS name>:8420/api/brief-check`, Method
   **GET**, no headers, no body.
4. **Format Date**: Date **Current Date**, Date Format **Custom**, format
   string `yyyy-MM-dd`.
5. **Save File**: save the **Formatted Date** to the Shortcuts folder, Ask
   Where To Save off, path `helios-check.txt`, Overwrite If File Exists on.
   (The Mac answered, whatever it said: the guard stays quiet.)
6. **If**: input **Contents of URL** (from step 3), condition **is**, text
   `ok`. Leave the If part empty.
7. Inside the **Otherwise** part, add a second **If**: input **Contents of
   URL**, condition **begins with**, text `early`. Leave its If part empty.
8. Inside that second If's **Otherwise** part: **Show Notification**, text
   `Helios brief: ` followed by **Contents of URL**.

If step 3 cannot reach the Mac, the shortcut stops there and the file still
says `pending`: the guard alerts. Any answer other than `ok` or `early...`
reaches step 8 and alerts with the Mac's own words.

## Shortcut 2: Helios Brief Guard

Tap **+**, name it **Helios Brief Guard**:

1. **Get File**: from the Shortcuts folder, turn **Show Document Picker** off,
   path `helios-check.txt`, turn **Error If Not Found** off.
2. **Get Text from Input**: input the **File**.
3. **Format Date**: Current Date, Custom, `yyyy-MM-dd`.
4. **If**: input the **Text** from step 2, condition **is**, the **Formatted
   Date**. Leave the If part empty.
5. Inside the **Otherwise** part: **Show Notification**, text
   `Helios: the brief check did not finish. The Mac did not answer (asleep, off the tailnet, or heliosd down).`

## The four automations

Automation tab, **+**, **Time of Day**, then for each: Repeat **Daily**, **Run
Immediately**, **Notify When Run** off, Next, choose the shortcut.

1. **09:05**: Helios Brief Check.
2. **09:10**: Helios Brief Guard.
3. **13:05**: Helios Brief Check.
4. **13:10**: Helios Brief Guard.

The times are the phone's local time. At home they are 09:05 and 13:05 in the
reporting zone; the 13:05 pair alerts again only if the brief is still not
complete. On a trip east, 09:05 local answers `early` (quiet) and the 13:05
pair is the check. On a day Whoop never scores the night, both pairs alert:
there is no complete morning brief that day.

## Test it once

1. Healthy: Tailscale on. Tap **Helios Brief Check**, then **Helios Brief
   Guard**: nothing appears (before 09:00 in the reporting zone the answer is
   `early`, also quiet).
2. An unexpected answer: in Helios Brief Check, change step 3's URL ending to
   `/api/brief-checkx` and tap it: a notification "Helios brief:" with a short
   "Not Found" message appears. Change the URL back.
3. Unreachable: turn Tailscale off on the iPhone. Tap **Helios Brief Check**:
   Shortcuts shows an error (it cannot reach the Mac). Tap **Helios Brief
   Guard**: the notification "the brief check did not finish" appears. Turn
   Tailscale back on, tap **Helios Brief Check** again, then **Helios Brief
   Guard**: nothing appears.
4. Unattended, once: turn Tailscale off, set the 09:05 automation to two
   minutes from now and the 09:10 one to seven minutes from now, lock the
   phone and wait. The guard's notification must arrive on the lock screen.
   Then set the times back to 09:05 and 09:10 and turn Tailscale on.

What is never covered: a phone that is off, or restarted and not yet unlocked,
at both times.
