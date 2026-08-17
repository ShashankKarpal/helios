# Log to Helios: the voice capture Shortcut

One invocation, zero taps, zero decisions: say what happened, Helios stores it
as a structured event. "Hey Siri, Log to Helios... double espresso at 4pm" and
you are done. The local model parses the line; if the model is down, a rules
fallback still classifies it, and an unclassifiable line is stored as a note
with the raw text preserved. A capture is never dropped.

Everything runs on your own network: the Shortcut POSTs to your Mac over LAN
TLS, the parse happens in LM Studio on the Mac, nothing leaves your machines.

## Build it (about two minutes, once)

1. Open **Shortcuts** on the iPhone, tap **+** for a new shortcut, name it
   **Log to Helios**.
2. Add action: **Dictate Text**. (Language: your dictation language. Stop
   Listening: After Pause.)
3. Add action: **Get Contents of URL** and configure:
   - URL: `https://helios.local:8420/api/quicklog/log`
     (or your Mac's hostname if you changed it)
   - Method: **POST**
   - Request Body: **JSON**, with two fields:
     - `text` (Text): the **Dictated Text** variable
     - `source` (Text): `shortcut`
4. Add action: **Get Dictionary Value**, key `summary`, from **Contents of URL**.
5. Add action: **Show Result** (or **Speak Text**, if you want Siri to read the
   confirmation back) with the **Dictionary Value**.

## Make it frictionless

- Say "Hey Siri, Log to Helios", then speak the line when dictation starts.
- Add the shortcut to the **Action Button** (Settings, Action Button, Shortcut)
  or as a **Home Screen** icon or a **Back Tap** trigger (Settings,
  Accessibility, Touch, Back Tap).
- On Apple Watch, the shortcut appears in the Shortcuts watch app; dictation
  works from the wrist.

## Requirements

- iPhone on the same network as the Mac running heliosd.
- The mkcert root already trusted on the iPhone (you did this for the PWA;
  see SETUP.md). Without it the HTTPS call fails.
- No token needed: the quicklog API is a local read/write surface on your own
  LAN, same as the PWA. The ingest endpoint stays token-guarded.

## Undo

Say **"undo"** (or "undo that") through the same shortcut and the most recent
capture is removed instead of stored; Helios confirms what it removed. A
double-log is also guarded automatically: an identical capture within two
minutes updates the existing entry rather than storing a twin.

## What lands in the store

Each capture becomes one row in `events`: a kind (`caffeine`, `alcohol`,
`med`, `symptom`, `food`, `water`, or `note`), a timestamp (backdated when you
say "90 minutes ago" or "at 4pm"), and a payload holding the item, amount, the
raw dictated text, and which parser handled it. Events feed the correlation
engine and the caffeine/alcohol cutoff finder on the Insights tab.
