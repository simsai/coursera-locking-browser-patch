# Reverse-engineering notes — Coursera Locking Browser 0.6.6

Sanitized summary of the analysis that led to the patch. No personal data, tokens,
captures or Coursera source code are included.

## 1. Architecture

- Electron 39 desktop app, packaged with electron-forge + webpack + Squirrel.
- All application logic is JavaScript in `resources/app.asar` →
  `.webpack/main/index.js` (one minified webpack bundle, ~114 KB).
- The renderer is a tiny static "Security Violations Detected" page; the actual exam UI is
  remote code from `coursera.org` loaded into the window.
- Deep linking: the app registers the `coursera-lock://` protocol and only starts an
  assessment when launched with that URL from the website.
- User data / logs: `%APPDATA%\Coursera Locking Browser` (`logs\main.log` is verbose).

## 2. Electron fuses

The shipped EXE contains an Electron fuse wire near the end of the binary with:

| Fuse | State |
|---|---|
| `RunAsNode` | enabled |
| `EnableCookieEncryption` | disabled |
| `EnableNodeOptionsEnvironmentVariable` | enabled |
| `EnableNodeCliInspectArguments` | enabled |
| `EnableEmbeddedAsarIntegrityValidation` | **disabled** |
| `OnlyLoadAppFromAsar` | disabled |
| `LoadBrowserProcessSpecificV8Snapshot` | disabled |
| `GrantFileProtocolExtraPrivileges` | enabled |

Because asar-integrity validation is disabled, `app.asar` can be repacked and the app
still starts. The EXE signature is untouched.

## 3. Local restrictions (all client-side)

1. **Forbidden processes** — regex over running process names (`zoom`, `slack`, `discord`,
   `teams`, `skype`, `whatsapp`, `teamviewer`, `anydesk`, `splashtop`, `logmein`,
   `remotepc`, `zoho`, `isl`, `mstsc`, `rdpclip`, `remoting_host`, `screenconnect`,
   `connectwisecontrol`, `supremo`, `aeroadmin`, `helpwire`, `parallels`, `2xagent`,
   VNC server/viewer variants). Checked at launch and every 10 s.
2. **Multiple displays** — `screen.getAllDisplays().length > 1` blocks launch.
3. **Window lockdown** — kiosk, fullscreen, always-on-top, frameless, immovable; after the
   page signals `submission-start`, a 1 s interval forces focus/fullscreen/kiosk/maximize.
4. **Global shortcut blocker** — swallows Alt+Tab, Ctrl+C/V/X/P/T/R, Ctrl+Shift+R,
   PrintScreen, F11, Alt+F4 (Windows) and shows a "Forbidden Action" dialog.
5. **Close prevention** — window close is cancelled until a `submission-complete` /
   `terminate-browser` deep link arrives. Note: `canClose()` is also consulted by the
   deep-link state machine, so forcing it to `true` without adjusting that routing makes
   the app skip the `submission-complete` termination branch and stay open after submit.
   The patcher updates both sites.
6. **Navigation allowlist** — only `coursera.org` hosts may be navigated to; `window.open`
   is denied and allowed URLs load in the same window.
7. **Content protection** — `setContentProtection(true)` hides the window from screen
   recorders.
8. **Launch gating** — on Windows packaged builds the app waits with "Launch From Coursera
   Required" unless started via the deep link.

None of these checks are reported to Coursera; they only block the exam locally and write
to the local log.

## 4. Server communication

The desktop app itself makes exactly one explicit request:

- `POST https://www.coursera.org/api/rest/v1/locking/transfer`
  body `{token, redirect, proof: "", clientVersion: "0.6.6"}`,
  user-agent `coursera-locking-browser/0.6.6`.
  A `426` response means the client version is too old.

Everything else comes from the exam page running inside the window:

- The page requests camera/mic/screen through the app's media handlers; the app
  auto-grants the first screen source plus loopback audio when the page calls
  `getDisplayMedia`.
- The page batches product/behaviour analytics through Coursera's eventing pipeline and
  sends them with `navigator.sendBeacon` to `/api/rest/v1/eventing/infobatch`.

## 5. Page-side telemetry found in the cached exam bundle

The exam page JavaScript (cached by the app) was inspected for behavioural tracking.
Relevant findings:

- A tracking hook is configured with
  `focusEventName: "assignment_learner_focus"`,
  `visibilityEventName: "assignment_learner_visibility"`.
  It binds `window` `focus`/`blur` and `document` `visibilitychange` and sends actions
  `focus`/`blur` and `show`/`hide` with the user id, assignment id and branch id.
- A separate activity tracker sends `interval` every 60 s plus `blur` on
  `visibilitychange`/`beforeunload` and `submit` on submit.
- Every text input has an `onPaste` handler that sends
  `trackingName: "pasting_detection", action: "paste"` with `clipboardTextLength`,
  `partId`, `partNumber`, `assignment_id` and `branchId`. When the assessment enables
  `enableCopyPasteDeterrenceNotifications`, a visible paste warning is also rendered.
- Keyboard-level events are tracked per input (`..._text_input_typing`, `_backspace`,
  `_focus`, `_blur`) and many `assignment_attempt_*` events exist for save, next,
  previous, submit and file uploads.
- The page queries an `IntegrityCheck` GraphQL service with `TEXTSIMILARITY` and
  `GIBBERISH` checks and can warn that a response looks similar to another learner's
  submission.

Conclusion: focus/visibility loss is reported to the server as analytics, which is why
the guard is included in the patch. Paste and content checks are not suppressed.

## 6. The focus/visibility guard

A script is attached to the exam window through the Chromium DevTools protocol:

```js
webContents.debugger.attach('1.3');
webContents.debugger.sendCommand('Page.addScriptToEvaluateOnNewDocument', { source });
```

`Page.addScriptToEvaluateOnNewDocument` runs before any page script on every document, in
the page's main world. The injected code:

- redefines `Document.prototype.hasFocus` to return `true`,
- redefines `hidden`, `visibilityState` and the `webkit` variants to "visible/not hidden",
- neutralizes `window.onblur`,
- swallows `blur`, `visibilitychange` and `webkitvisibilitychange` listeners.

The page therefore never observes a blur or hide and reports a continuously focused,
visible session. This is the app-level equivalent of the *Always Active Window* browser
extension technique.

## 7. Scope limits

- Media capture (camera/mic/screen) is untouched and still available to the page.
- Paste telemetry and server-side text-similarity checks remain fully active.
- The patch is version-specific to 0.6.6 and validates its code anchors before writing.
- The session transfer, user-agent and client version are left stock so the client looks
  like a normal 0.6.6 install to the server.
