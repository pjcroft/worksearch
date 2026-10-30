#!/usr/bin/env bash
# WorkSearch installer for Ubuntu (and other Debian-based desktops).
#
#   bash install-linux.sh                     # asks a few questions
#   bash install-linux.sh --root /mnt/qnap/Work --autostart --yes
#
# Installs into your home folder (no system-wide changes except apt packages):
#   ~/.local/share/worksearch/app      program files
#   ~/.local/share/worksearch/venv     private Python environment
#   ~/.local/bin/worksearch            command to start it
#   ~/.local/share/applications/worksearch.desktop   app menu entry
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/worksearch"
APP="$DATA/app"
VENV="$DATA/venv"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

ROOT=""
AUTOSTART=""
YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --autostart) AUTOSTART=1; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { # ask "question" default -> echoes answer
  local a
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then echo "$2"; return; fi
  read -r -p "$1 [$2]: " a
  echo "${a:-$2}"
}

for f in worksearch.py ui.html icon.svg icon.png requirements.txt; do
  [ -f "$SRC/$f" ] || die "Missing $f next to this installer. Unzip the whole WorkSearch folder and run it from there."
done

# ---------- 1. system packages ----------
need=()
command -v python3 >/dev/null || need+=(python3)
python3 -c 'import ensurepip, venv' 2>/dev/null || need+=(python3-venv)
command -v tesseract >/dev/null || need+=(tesseract-ocr)
command -v xdg-open >/dev/null || need+=(xdg-utils)
if [ ${#need[@]} -gt 0 ]; then
  say "Installing system packages: ${need[*]}"
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get update -q || warn "apt-get update failed; trying the install anyway"
  $SUDO apt-get install -y -q "${need[@]}" || die "Could not install ${need[*]}. Install them with apt and run this again."
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 3.9 or newer is needed."
command -v tesseract >/dev/null && say "OCR: $(tesseract --version 2>&1 | head -1)"

# ---------- 2. which folder ----------
if [ -z "$ROOT" ]; then
  guess=""
  if [ -f "$DATA/config.json" ]; then
    guess="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("root",""))' "$DATA/config.json" 2>/dev/null || true)"
  fi
  if [ -z "$guess" ]; then
    for c in /mnt/qnap/Work /mnt/Qnap/Work /media/"$USER"/*/Work /run/user/"$(id -u)"/gvfs/smb-share:*/Work "$HOME/Work"; do
      [ -d "$c" ] && { guess="$c"; break; }
    done
  fi
  ROOT="$(ask "Folder to search (your mounted QNAP Work folder)" "${guess:-/mnt/qnap/Work}")"
fi
ROOT="${ROOT/#\~/$HOME}"
[ -d "$ROOT" ] || warn "$ROOT is not there right now. WorkSearch will start indexing once it is mounted."

# ---------- 3. program files + Python environment ----------
say "Installing WorkSearch into $DATA"
mkdir -p "$APP" "$BIN" "$APPS"
cp "$SRC"/worksearch.py "$SRC"/ui.html "$SRC"/icon.svg "$SRC"/icon.png "$SRC"/requirements.txt "$APP"/
[ -f "$SRC/uninstall-linux.sh" ] && cp "$SRC/uninstall-linux.sh" "$APP/"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || die "Could not create a Python environment (is python3-venv installed?)"
fi
"$VENV/bin/python" -m pip install -q --upgrade pip || warn "Could not update pip; continuing"
"$VENV/bin/python" -m pip install -q -r "$APP/requirements.txt" || die "Could not install PyMuPDF (needs internet access)."
"$VENV/bin/python" -c 'import pymupdf' 2>/dev/null || "$VENV/bin/python" -c 'import fitz' || die "PyMuPDF did not install correctly."

# save the folder choice
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

# ---------- 4. launcher + menu entry ----------
cat > "$BIN/worksearch" <<EOF
#!/usr/bin/env bash
# Starts WorkSearch (if it isn't running) and opens the search page.
exec "$VENV/bin/python" "$APP/worksearch.py" "\${@:-launch}"
EOF
chmod +x "$BIN/worksearch"

cat > "$APPS/worksearch.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=WorkSearch
GenericName=File Search
Comment=Search the contents of your work files
Exec=$BIN/worksearch launch
Icon=$APP/icon.svg
Terminal=false
Categories=Utility;
Keywords=search;pdf;documents;files;
StartupNotify=false
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

# ---------- 5. start at login (optional) ----------
if [ -z "$AUTOSTART" ]; then
  a="$(ask "Start WorkSearch in the background when you log in, so the index stays up to date? (y/n)" "y")"
  case "$a" in [Yy]*) AUTOSTART=1 ;; *) AUTOSTART=0 ;; esac
fi
if [ "$AUTOSTART" = 1 ]; then
  mkdir -p "$AUTOSTART_DIR"
  cat > "$AUTOSTART_DIR/worksearch.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=WorkSearch (background)
Comment=Keeps the WorkSearch index up to date
Exec=$BIN/worksearch launch --no-browser
Icon=$APP/icon.svg
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=20
EOF
  say "WorkSearch will start in the background when you log in."
else
  rm -f "$AUTOSTART_DIR/worksearch.desktop"
fi

say "Done. Open WorkSearch from your app menu, or run: worksearch"
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not on your PATH yet; log out and back in, or run $BIN/worksearch" ;; esac
echo "    Folder:  $ROOT"
echo "    Index:   $DATA/index.db"
echo "    Remove:  bash $APP/uninstall-linux.sh"
