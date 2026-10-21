# Coursera Locking Browser — Local Patch Toolkit

A small, self-contained Python toolkit that patches **Coursera Locking Browser 0.6.6**
(`coursera-locking-browser`) on Windows so the installed app stops enforcing its local
lockdown restrictions — while keeping the official exam flow intact.

No compiled artifacts of Coursera's software are included or redistributed. The tool
edits the copy of `app.asar` already on your machine.

> **Educational / research use.** This project exists to document how a desktop
> "locking browser" enforces restrictions and to demonstrate that those restrictions are
> client-side. Using it may violate Coursera's Terms of Service and your institution's
> academic-integrity rules. You are responsible for how you use it.

---

## What it changes

| Mechanism | Stock behavior | After patch |
|---|---|---|
| Forbidden-process check (`zoom`, `discord`, `teamviewer`, VNC/RDP tools, …) | blocks launch, checked at start + every 10 s | disabled |
| Multi-display check | blocks launch when >1 monitor | disabled |
| Window mode | kiosk + fullscreen + always-on-top + frameless + immovable | normal framed, movable, resizable window |
| Focus/fullscreen enforcement | every 1 s forces focus, fullscreen, kiosk, maximize | disabled |
| Keyboard | global shortcuts swallow Alt+Tab, Ctrl+C/V/X/P, PrintScreen, F11, Alt+F4 | unblocked |
| Window close | prevented until the exam signals completion | always allowed |
| Screen capture | `setContentProtection(true)` hides the window from recorders | disabled |
| Page focus/visibility telemetry | page reports `assignment_learner_focus` / `_visibility` blur/hide events | suppressed by an injected guard |

**Left untouched on purpose**

- Launch still requires the official `coursera-lock://` deep link from the Coursera site.
- The session-transfer request, user-agent (`coursera-locking-browser/0.6.6`) and client
  version are unchanged.
- Camera / microphone / screen proctoring continues to work normally.
- Page analytics other than focus/visibility (e.g. `pasting_detection`, typing events,
  attempt navigation) are **not** suppressed.

---

## Requirements

- Windows 10/11
- Coursera Locking Browser **0.6.6** installed
  (default: `%LOCALAPPDATA%\coursera_locking_browser\app-0.6.6\resources\app.asar`)
- Python 3.8+ (no third-party packages)
- Node.js is optional — used only for a syntax check of the patched bundle

## Quick start

Close the app, then run:

```bat
python scripts\patch_asar.py
```

The script auto-detects the install, backs up the original `app.asar` to
`app.asar.orig-0.6.6`, patches a temporary copy, repacks it, syntax-checks it (if Node is
available) and installs it.

Preview without touching the install:

```bat
python scripts\patch_asar.py --dry-run
```

Patch a specific copy:

```bat
python scripts\patch_asar.py --asar "D:\copy\app.asar" --out "D:\copy\app_patched.asar"
```

Skip the focus/visibility guard (core edits only):

```bat
python scripts\patch_asar.py --no-guard
```

## Verify

1. Start the app from the Coursera assignment page (the **Start** / **Resume** button).
2. The window opens normally — framed, movable, not fullscreen.
3. `%APPDATA%\Coursera Locking Browser\logs\main.log` shows:
   - `No violations found`
   - `Focus/visibility guard installed`
4. Alt+Tab, Ctrl+C/V, PrintScreen and Alt+F4 work.

Double-clicking the exe directly still shows **"Launch From Coursera Required"** — that is
expected and unchanged: the app only starts an exam via the deep link from the website.

## Revert

```bat
python scripts\patch_asar.py --restore
```

or copy `app.asar.orig-0.6.6` over `app.asar` with the app closed.

Re-apply after any reinstall or forced update (a `426` from Coursera means the server wants
a newer client).

## How it works

The entire lock-down logic lives in the app's JavaScript bundle
(`resources/app.asar → .webpack/main/index.js`). Electron fuses in the shipped EXE have
`EnableEmbeddedAsarIntegrityValidation` **disabled**, so the archive can be repacked
without breaking the signature. The script rewrites six exact code sites:

1. `checkViolations()` → returns `[]` (process + display checks).
2. `createWindow` options → `frame:true, kiosk:false, fullscreen:false, movable:true,
   alwaysOnTop:false`.
3. The 1-second focus/fullscreen enforcer → no-op.
4. `blockAccelerators()` → no-op.
5. `canClose()` → `true`.
6. `setContentProtection(true)` → `false`.

The optional guard attaches the Chromium DevTools protocol (`1.3`) to the exam window and
calls `Page.addScriptToEvaluateOnNewDocument` with a script that runs **before any page
script** on every document:

- `document.hasFocus()` → `true`
- `visibilityState` / `webkitVisibilityState` → `"visible"`
- `hidden` / `webkitHidden` → `false`
- `window.onblur` neutralized
- `blur` / `visibilitychange` listeners swallowed

That is the same technique browser extensions like *Always Active Window* use, applied at
the app level.

## How this was built

This toolkit was produced by **reverse engineering the installed app** and by **analysing
packet captures** of a live assessment session:

1. **Reverse engineering** — extracted `app.asar` from the installed 0.6.6 client,
   beautified the webpack main bundle and traced every restriction back to its exact code
   site; decoded the Electron fuse wire in the shipped EXE (which showed asar-integrity
   validation is disabled, making repacking possible); and inspected the exam page's
   JavaScript from the client's local HTTP/Code caches to see what the page reports
   (focus/visibility events, paste detection, attempt analytics).
2. **Packet capture analysis** — captured traffic during a real assessment (Wireshark) to
   map the endpoints the client talks to and confirm the network shape: TLS 1.3 to
   Coursera via CloudFront plus analytics/error-reporting services, with no third-party
   proctoring endpoint. The capture also showed that request bodies are encrypted, which
   is why the page-side behaviour was established from the cached JavaScript rather than
   from packet payloads.

Together these established which checks are purely client-side (and therefore patchable),
which are page-side and what they send, and which remain server-side.

## Recommendation: type your answers instead of pasting

If an assessment is reviewed or uses Coursera's integrity features, **type your answers
rather than pasting**. This patch cannot hide paste activity, for four reasons:

1. **Every paste is reported.** Each text and rich-text answer field sends a
   `pasting_detection` event to Coursera containing the pasted text length, question id,
   part number, assignment id, branch id and your user id.
2. **Some assessments warn visibly.** When paste deterrence is enabled, the page shows an
   on-screen warning immediately after a paste.
3. **The pattern is obvious.** Typing produces a stream of per-keystroke events
   (`..._text_input_typing`, `..._backspace`); a paste produces zero keystrokes and one
   event with a large character count — an answer that appears instantly is easy to spot.
4. **Content checks run on the server.** On submission, `IntegrityCheck` compares your text
   with other learners' submissions (text similarity) and runs gibberish detection. That
   happens server-side and no client patch can change the result.

Typing in your own words avoids all four signals. Copying text *out* of the app (e.g. into
notes) is not what these events measure — it is pasting **into** answer fields that is
tracked.

## Limitations and risk

- **Media proctoring is not affected.** If an assessment records camera/mic/screen, that
  still happens.
- **Paste is tracked.** Text inputs send a `pasting_detection` event containing the pasted
  text length, question id and assignment id. Paste is not blocked, but it is visible to
  Coursera's analytics. Some assessments also show a visible "paste deterrence" warning.
- **Text-similarity checks are server-side.** The page queries an `IntegrityCheck` service
  (plagiarism / gibberish) on submitted text; no client patch can change that result.
- **Other analytics remain.** Focus/visibility events are suppressed; typing,
  backspace, save, navigation and submit events are not.
- **Version-locked.** The patch targets 0.6.6 exactly and refuses to run if the code
  anchors are missing. A future release (or a 426 forced update) will overwrite it.
- **No server-side invisibility guarantee.** The client still looks like 0.6.6 and sends
  the normal transfer request, but you accept the risk of account action by using this.

## Files

```
scripts/patch_asar.py   one-command patch / dry-run / restore tool
docs/FINDINGS.md        sanitized reverse-engineering notes
LICENSE                 MIT (applies to this toolkit only)
```

## License

MIT — see `LICENSE`. No Coursera code, binaries or assets are distributed here.
