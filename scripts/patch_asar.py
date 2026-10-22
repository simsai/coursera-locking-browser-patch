#!/usr/bin/env python3
"""
Coursera Locking Browser 0.6.6 - local patch toolkit.

Patches resources/app.asar in place (with a backup) so the installed browser:

  * skips the forbidden-process and multi-display checks,
  * opens a normal framed/resizable window instead of kiosk/fullscreen/always-on-top,
  * stops the 1s focus/fullscreen enforcement loop,
  * stops swallowing keyboard shortcuts (Alt+Tab, Ctrl+C/V/..., PrintScreen, F11, Alt+F4),
  * lets the window close at any time,
  * allows screen capture,
  * suppresses the page's focus/visibility telemetry (blur/hide events) by injecting a
    guard that runs before any page script.

Everything else is left stock: the exam is still launched only through the Coursera
deep link, the session-transfer request, user-agent and client version are untouched,
and media proctoring (camera/mic/screen) still works normally.

Usage:
    python patch_asar.py                      # auto-detect install, patch in place
    python patch_asar.py --dry-run            # build a patched asar next to this script
    python patch_asar.py --asar path\\app.asar --out path\\app_patched.asar
    python patch_asar.py --no-guard           # skip the focus/visibility guard
    python patch_asar.py --restore            # restore the original from the backup

Only Python 3.8+ is required. No third-party packages.
"""

import argparse
import json
import os
import shutil
import struct
import sys
import tempfile

APP_NAME = "coursera-locking-browser"
CORE_EDITS = [
    # 1. forbidden-process + multi-display checks -> always empty
    (
        't.checkViolations=()=>i(void 0,void 0,void 0,(function*(){const e=[],t=yield p();'
        't&&e.push(t);const r=u();return r&&e.push(r),e}));',
        't.checkViolations=()=>i(void 0,void 0,void 0,(function*(){return[]}));',
    ),
    # 2. window options at creation -> normal window
    (
        'frame:!1,width:r,height:n,minHeight:n,minWidth:r,backgroundColor:"#0056D2",'
        'title:"Coursera Secure Assessment Browser",webPreferences:v,show:!0,kiosk:!0,'
        'fullscreen:!0,movable:!1,autoHideMenuBar:!0,alwaysOnTop:!0',
        'frame:!0,width:r,height:n,minHeight:n,minWidth:r,backgroundColor:"#0056D2",'
        'title:"Coursera Secure Assessment Browser",webPreferences:v,show:!0,kiosk:!1,'
        'fullscreen:!1,movable:!0,autoHideMenuBar:!0,alwaysOnTop:!1',
    ),
    # 3. focus/fullscreen/kiosk enforcer -> no-op
    (
        'const _=e=>{const t=e.isFocused(),r=e.isFullScreen(),n=e.getChildWindows().length>0;'
        '!(t&&r||n)&&(f.info("Window state violation detected:",{focused:t,fullscreen:r}),'
        'e.setAlwaysOnTop(!0),e.setFullScreen(!0),e.focus(),e.setKiosk(!0),e.maximize())}',
        'const _=e=>{}',
    ),
    # 4. accelerator blocker -> no-op
    (
        't.blockAccelerators=e=>{(0,t.computeAccelerators)().forEach((t=>'
        'n.globalShortcut.register(t.accelerator,(()=>{o(e,t.description)}))))}',
        't.blockAccelerators=e=>{}',
    ),
    # 5. close always allowed
    (
        't.canClose=function(){return l||t.isDevelopMode}',
        't.canClose=function(){return!0}',
    ),
    # 5b. canClose() is also part of the deep-link state machine: with it always true,
    #     the "submission-complete" branch was never reached and the app stopped
    #     auto-terminating after submit. Restore the original routing.
    (
        'else if(!_(t.host)||(0,m.canClose)()&&!g.develop){',
        'else if(!_(t.host)){',
    ),
    # 6. content protection off
    (
        'e.setContentProtection(!0),e.setMenuBarVisibility(!1)',
        'e.setContentProtection(!1),e.setMenuBarVisibility(!1)',
    ),
]

GUARD_MARKER = "Focus/visibility guard installed"

SPOOF = (
    "(function(){try{"
    "var d=Document.prototype;"
    "var def=function(o,p,f){try{Object.defineProperty(o,p,{get:f,configurable:true})}catch(e){}};"
    "def(d,'hidden',function(){return false});"
    "def(d,'visibilityState',function(){return 'visible'});"
    "def(d,'webkitHidden',function(){return false});"
    "def(d,'webkitVisibilityState',function(){return 'visible'});"
    "try{Object.defineProperty(Document.prototype,'hasFocus',{value:function(){return true},"
    "configurable:true})}catch(e){}"
    "var orig=EventTarget.prototype.addEventListener;"
    "EventTarget.prototype.addEventListener=function(t,l,o){"
    "if(t==='blur'||t==='visibilitychange'||t==='webkitvisibilitychange')return;"
    "return orig.call(this,t,l,o)};"
    "try{Object.defineProperty(window,'onblur',{get:function(){return null},"
    "set:function(){},configurable:true})}catch(e){}"
    "}catch(e){}})();"
)

GUARD_ANCHOR = "s.menuBarVisible=!1,s.removeMenu(),"
GUARD_CODE = (
    "(function(){try{var _dbg=s.webContents.debugger;_dbg.isAttached()||_dbg.attach(\"1.3\"),"
    "_dbg.sendCommand(\"Page.addScriptToEvaluateOnNewDocument\",{source:SPOOF}),"
    'f.info("%s")}catch(_err){f.info("Focus guard failed:",_err.message)}})(),' % GUARD_MARKER
)
CREATE_ANCHOR = "t.createWindow=e=>{const t=d.screen.getPrimaryDisplay()"


def default_resources():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    for version in ("app-0.6.6",):
        p = os.path.join(base, "coursera_locking_browser", version, "resources")
        if os.path.isdir(p):
            return p
    root = os.path.join(base, "coursera_locking_browser")
    if os.path.isdir(root):
        for name in sorted(os.listdir(root), reverse=True):
            p = os.path.join(root, name, "resources")
            if os.path.isfile(os.path.join(p, "app.asar")):
                return p
    return None


def read_asar(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 16:
        raise ValueError("not an asar file")
    json_len = struct.unpack("<I", data[12:16])[0]
    header = json.loads(data[16:16 + json_len].decode("utf-8"))
    base = 16 + json_len
    if base % 4:
        base += 4 - (base % 4)
    return header, base, data


def walk(node, base, out):
    for name, meta in node.get("files", {}).items():
        p = os.path.join(base, name)
        if "files" in meta:
            walk(meta, p, out)
        else:
            out.append((meta, p))


def extract(header, base, data, dest):
    entries = []
    walk(header, dest, entries)
    for meta, p in entries:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        off = int(meta["offset"])
        size = meta["size"]
        with open(p, "wb") as f:
            f.write(data[base + off:base + off + size])
    return entries


def repack(header, src_dir, out_path):
    entries = []
    walk(header, src_dir, entries)
    offset = 0
    blobs = []
    for meta, p in entries:
        with open(p, "rb") as f:
            blob = f.read()
        meta["size"] = len(blob)
        meta["offset"] = str(offset)
        offset += len(blob)
        blobs.append(blob)
    js = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    while len(js) % 4:
        js += b" "
    jl = len(js)
    prefix = (struct.pack("<I", 4) + struct.pack("<I", jl + 8) +
              struct.pack("<I", jl + 4) + struct.pack("<I", jl))
    with open(out_path, "wb") as f:
        f.write(prefix + js + b"".join(blobs))


def patch_bundle(path, with_guard=True):
    with open(path, encoding="utf-8") as f:
        src = f.read()

    changed = False
    for old, new in CORE_EDITS:
        if new in src:
            continue  # already applied
        n = src.count(old)
        if n != 1:
            raise SystemExit(
                "ERROR: patch anchor not found exactly once (%d found).\n"
                "This build is probably not Coursera Locking Browser 0.6.6.\n"
                "Anchor: %s..." % (n, old[:70])
            )
        src = src.replace(old, new, 1)
        changed = True

    if with_guard and GUARD_MARKER not in src:
        if CREATE_ANCHOR not in src:
            raise SystemExit("ERROR: createWindow anchor missing")
        src = src.replace(
            CREATE_ANCHOR,
            "t.createWindow=e=>{const SPOOF=" + json.dumps(SPOOF) + ";" + CREATE_ANCHOR[len("t.createWindow=e=>{"):],
            1,
        )
        if GUARD_ANCHOR not in src:
            raise SystemExit("ERROR: guard anchor missing")
        src = src.replace(GUARD_ANCHOR, GUARD_ANCHOR + GUARD_CODE, 1)
        changed = True

    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    return True


def check_bundle(path):
    import subprocess
    node = shutil.which("node")
    if not node:
        return
    r = subprocess.run([node, "--check", path], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("ERROR: patched bundle fails syntax check:\n" + r.stderr[-2000:])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asar", help="path to app.asar (default: auto-detected install)")
    ap.add_argument("--out", help="write patched asar here instead of installing")
    ap.add_argument("--dry-run", action="store_true", help="build patched asar next to this script")
    ap.add_argument("--no-guard", action="store_true", help="skip the focus/visibility guard")
    ap.add_argument("--restore", action="store_true", help="restore app.asar from backup")
    ap.add_argument("--force", action="store_true", help="patch even if markers suggest it is done")
    args = ap.parse_args()

    src = args.asar
    if not src:
        res = default_resources()
        if not res:
            raise SystemExit("Could not find the app install. Use --asar path\\app.asar")
        src = os.path.join(res, "app.asar")

    backup = src + ".orig-0.6.6"

    if args.restore:
        if not os.path.isfile(backup):
            raise SystemExit("No backup found at " + backup)
        shutil.copyfile(backup, src)
        print("Restored:", src)
        return

    if not os.path.isfile(src):
        raise SystemExit("app.asar not found: " + src)

    out = args.out or (os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_patched.asar")
                       if args.dry_run else src)

    print("Input :", src)
    header, base, data = read_asar(src)
    tmp = tempfile.mkdtemp(prefix="clb_patch_")
    try:
        entries = extract(header, base, data, tmp)
        pkg_path = os.path.join(tmp, "package.json")
        if os.path.isfile(pkg_path):
            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.load(f)
            print("Version:", pkg.get("version"), "| Name:", pkg.get("name"))
            if pkg.get("name") != APP_NAME:
                print("WARNING: unexpected package name; continuing anyway")
        bundle = os.path.join(tmp, ".webpack", "main", "index.js")
        if not os.path.isfile(bundle):
            raise SystemExit("ERROR: .webpack/main/index.js not found in archive")
        changed = patch_bundle(bundle, with_guard=not args.no_guard)
        check_bundle(bundle)
        repack(header, tmp, out)
        print("Wrote :", out, "(%d bytes)" % os.path.getsize(out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if out != src:
        print("Dry run complete. Nothing was installed.")
        return

    if changed and not os.path.isfile(backup):
        shutil.copyfile(src, backup)
        print("Backup:", backup)
    print("Patched app.asar installed. Start an assessment from the Coursera website.")
    print("Revert with: python patch_asar.py --restore")


if __name__ == "__main__":
    sys.exit(main())
