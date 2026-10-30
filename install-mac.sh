#!/bin/bash
# WorkSearch installer for macOS (Apple Silicon or Intel).
#
#   bash install-mac.sh                          # asks a few questions
#   bash install-mac.sh --root "/Volumes/Qnap/Work" --autostart --yes
#
# Installs into your user account:
#   ~/Applications/WorkSearch.app                          the app you double-click
#   ~/Library/Application Support/WorkSearch/app           program files
#   ~/Library/Application Support/WorkSearch/venv          private Python environment
#   ~/Library/LaunchAgents/com.pjcroft.worksearch.plist    (optional) start at login
# Uses Homebrew for Python and Tesseract (OCR).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DATA="$HOME/Library/Application Support/WorkSearch"
APP="$DATA/app"
VENV="$DATA/venv"
BUNDLE="$HOME/Applications/WorkSearch.app"
LABEL="com.pjcroft.worksearch"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
PYFORMULA="python@3.12"

ROOT=""
AUTOSTART=""
YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --autostart) AUTOSTART=1; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  {
  local a
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then echo "$2"; return; fi
  read -r -p "$1 [$2]: " a
  echo "${a:-$2}"
}

[ "$(uname)" = "Darwin" ] || die "This installer is for macOS. On Ubuntu use install-linux.sh."
for f in worksearch.py ui.html icon.svg icon.png requirements.txt; do
  [ -f "$SRC/$f" ] || die "Missing $f next to this installer. Unzip the whole WorkSearch folder and run it from there."
done

# ---------- 1. Homebrew, Python, Tesseract ----------
if ! command -v brew >/dev/null 2>&1; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$b" ] && eval "$("$b" shellenv)" && break
  done
fi
if ! command -v brew >/dev/null 2>&1; then
  warn "Homebrew is needed to install Python and the OCR engine (Tesseract)."
  a="$(ask "Install Homebrew now? It will ask for your Mac password. (y/n)" "n")"
  case "$a" in
    [Yy]*) /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
           for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)" && break; done ;;
    *) die "Install Homebrew from https://brew.sh, then run this installer again." ;;
  esac
  command -v brew >/dev/null 2>&1 || die "Homebrew still isn't available. Open a new Terminal window and run this installer again."
fi
for formula in "$PYFORMULA" tesseract; do
  if brew list --versions "$formula" >/dev/null 2>&1; then
    say "$formula is already installed"
  else
    say "Installing $formula with Homebrew (this can take a few minutes)"
    brew install "$formula" || die "brew install $formula failed."
  fi
done
PY="$(brew --prefix "$PYFORMULA")/bin/python3.12"
[ -x "$PY" ] || die "Could not find Homebrew's Python at $PY"
say "Python: $("$PY" --version 2>&1)   OCR: $(tesseract --version 2>&1 | head -1)"

# ---------- 2. which folder ----------
if [ -z "$ROOT" ]; then
  guess=""
  if [ -f "$DATA/config.json" ]; then
    guess="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("root",""))' "$DATA/config.json" 2>/dev/null || true)"
  fi
  ROOT="$(ask "Folder to search (your QNAP Work folder)" "${guess:-/Volumes/Qnap/Work}")"
fi
ROOT="${ROOT/#\~/$HOME}"
[ -d "$ROOT" ] || warn "$ROOT is not there right now (is the QNAP mounted?). WorkSearch will start indexing once it is."

# ---------- 3. program files + Python environment ----------
say "Installing WorkSearch into $DATA"
mkdir -p "$APP"
cp "$SRC"/worksearch.py "$SRC"/ui.html "$SRC"/icon.svg "$SRC"/icon.png "$SRC"/requirements.txt "$APP"/
[ -f "$SRC/uninstall-mac.sh" ] && cp "$SRC/uninstall-mac.sh" "$APP/"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV" || die "Could not create a Python environment."
fi
"$VENV/bin/python" -m pip install -q --upgrade pip || warn "Could not update pip; continuing"
"$VENV/bin/python" -m pip install -q -r "$APP/requirements.txt" || die "Could not install PyMuPDF (needs internet access)."
"$VENV/bin/python" -c 'import pymupdf' 2>/dev/null || "$VENV/bin/python" -c 'import fitz' || die "PyMuPDF did not install correctly."

"$VENV/bin/python" - "$DATA/config.json" "$ROOT" <<'PY'
import json, sys, os
path, root = sys.argv[1], sys.argv[2]
cfg = {}
if os.path.exists(path):
    try: cfg = json.load(open(path))
    except ValueError: pass
cfg['root'] = root
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(cfg, open(path, 'w'), indent=2)
PY

# ---------- 4. the .app you double-click ----------
say "Creating $BUNDLE"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cat > "$BUNDLE/Contents/MacOS/WorkSearch" <<EOF
#!/bin/bash
# Starts WorkSearch (if it isn't running) and opens the search page in your browser.
exec "$VENV/bin/python" "$APP/worksearch.py" launch
EOF
chmod +x "$BUNDLE/Contents/MacOS/WorkSearch"
cat > "$BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>WorkSearch</string>
  <key>CFBundleDisplayName</key><string>WorkSearch</string>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleExecutable</key><string>WorkSearch</string>
  <key>CFBundleIconFile</key><string>WorkSearch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
ICONSET="$(mktemp -d)/WorkSearch.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "$APP/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null 2>&1 || true
  d=$((s * 2))
  sips -z "$d" "$d" "$APP/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
done
iconutil -c icns "$ICONSET" -o "$BUNDLE/Contents/Resources/WorkSearch.icns" 2>/dev/null || warn "Could not build the app icon (the app still works)."
rm -rf "$(dirname "$ICONSET")"
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREG" ] && "$LSREG" -f "$BUNDLE" >/dev/null 2>&1 || true

# ---------- 5. start at login (optional) ----------
if [ -z "$AUTOSTART" ]; then
  a="$(ask "Run WorkSearch in the background from login, so the index stays up to date? (y/n)" "y")"
  case "$a" in [Yy]*) AUTOSTART=1 ;; *) AUTOSTART=0 ;; esac
fi
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
if [ "$AUTOSTART" = 1 ]; then
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$APP/worksearch.py</string>
    <string>serve</string>
    <string>--no-browser</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DATA/server.log</string>
  <key>StandardErrorPath</key><string>$DATA/server.log</string>
</dict>
</plist>
EOF
  if launchctl bootstrap "gui/$(id -u)" "$AGENT" 2>/dev/null; then
    say "WorkSearch is running in the background and will start again at each login."
  else
    warn "Could not start the background service now; it will start at your next login."
  fi
else
  rm -f "$AGENT"
fi

say "Done. Open WorkSearch from ~/Applications (or Spotlight: type WorkSearch)."
echo "    Folder:  $ROOT"
echo "    Index:   $DATA/index.db"
echo "    Remove:  bash \"$APP/uninstall-mac.sh\""
echo
echo "The first time it reads the QNAP, macOS may ask to allow access to files on a network volume. Click Allow."
