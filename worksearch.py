#!/usr/bin/env python3
"""WorkSearch: a local full-text search for a work folder (PDF, Word, Excel, PowerPoint, text, email).

Scans a folder (for example a mounted NAS share), extracts text, OCRs scanned PDF pages with
Tesseract, and serves a search page on http://127.0.0.1:<port>. Everything stays on this computer.

    python3 worksearch.py serve --root "/Volumes/Qnap/Work"
"""
import argparse
import concurrent.futures as cf
import email
import json
import multiprocessing
import os
import platform
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_DIR = Path(__file__).resolve().parent
IS_MAC = platform.system() == 'Darwin'
DATA_DIR = Path(os.environ.get('WORKSEARCH_HOME') or (
    Path.home() / 'Library' / 'Application Support' / 'WorkSearch' if IS_MAC
    else Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'worksearch'))
CONFIG_PATH = DATA_DIR / 'config.json'
DB_PATH = DATA_DIR / 'index.db'
DEFAULTS = {
    'root': '/Volumes/Qnap/Work',
    'port': 8777,
    'rescan_hours': 4,        # how often the folder is re-checked while the app runs
    'ocr_lang': 'eng',        # Tesseract language(s), e.g. "eng+ita"
    'ocr_dpi': 250,
    'ocr_workers': 0,         # 0 = automatic (CPU cores minus 2)
    'max_extract_mb': 400,    # bigger files are indexed by name only
    # Ask AI (OpenRouter, or any OpenAI-compatible API such as Ollama or LM Studio)
    'ai_base_url': 'https://openrouter.ai/api/v1',
    'ai_key': '',             # or set the OPENROUTER_API_KEY environment variable
    'ai_model': 'openrouter/auto',
    'ai_recent': [],          # recently used models, shown in the quick model menu
    'ai_max_pages': 20,       # how many matching pages are sent with each question
}

KINDS = {
    'pdf': {'pdf'},
    'word': {'docx', 'docm', 'doc', 'dotx'},
    'excel': {'xlsx', 'xlsm', 'xls', 'xlsb'},
    'slides': {'pptx', 'pptm', 'ppt'},
    'email': {'eml', 'msg'},
    'text': {'txt', 'md', 'csv', 'tsv', 'log', 'json', 'xml', 'ini', 'cfg', 'yml', 'yaml', 'sql', 'html', 'htm'},
    'image': {'jpg', 'jpeg', 'png', 'tif', 'tiff', 'gif', 'bmp', 'heic'},
    'cad': {'dwg', 'dxf', 'step', 'stp', 'iges', 'igs', 'sldprt', 'sldasm', 'slddrw', 'ipt', 'iam'},
}
EXT_KIND = {e: k for k, exts in KINDS.items() for e in exts}
LEGACY = {'doc', 'xls', 'ppt', 'xlsb', 'msg'}
SKIP_FILES = {'.ds_store', 'thumbs.db', 'desktop.ini'}
TEXT_CHUNK = 2500


def kind_of(ext):
    return EXT_KIND.get(ext, 'other')


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def log(*a):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(DATA_DIR / 'worksearch.log', 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def load_fitz():
    try:
        import pymupdf as fitz  # PyMuPDF 1.24+
    except ImportError:
        fitz = load_fitz()
    return fitz


def find_tesseract():
    for c in (shutil.which('tesseract'), '/opt/homebrew/bin/tesseract', '/usr/local/bin/tesseract'):
        if c and os.path.exists(c):
            return c
    return None


def candidate_roots():
    """Places a work folder is likely to be: mounted drives and network shares, plus the home folder."""
    homes = [str(Path.home())]
    roots = []
    if IS_MAC:
        bases = ['/Volumes']
    else:
        user = os.environ.get('USER') or Path.home().name
        bases = ['/mnt', f'/media/{user}', f'/run/media/{user}', f'/run/user/{os.getuid()}/gvfs']
    for b in bases:
        try:
            for e in sorted(os.scandir(b), key=lambda x: x.name.lower()):
                if not e.name.startswith('.') and e.is_dir():
                    roots.append(e.path)
        except OSError:
            pass
    return roots + homes


def describe_root_problem(root):
    """Why can't the folder be read? Returns a message that says what to do, or '' if it is fine."""
    try:
        with os.scandir(root):
            return ''
    except FileNotFoundError:
        where = [os.path.basename(r) for r in candidate_roots() if r != str(Path.home())]
        hint = f' Drives that are mounted right now: {", ".join(where)}.' if where else ' No network drives are mounted right now.'
        return f'Folder not found: {root}. Is the NAS mounted?{hint} Use "Change folder" to pick the right one.'
    except NotADirectoryError:
        return f'{root} is a file, not a folder. Use "Change folder" to pick a folder.'
    except PermissionError:
        if IS_MAC:
            py = os.path.realpath(sys.executable)
            return (f'macOS is not letting WorkSearch read {root}. Open System Settings > Privacy & Security > '
                    f'Full Disk Access, add the program {py} (use Cmd+Shift+G in the file picker to type its path), '
                    f'then quit and reopen WorkSearch.')
        return f'Permission denied reading {root}. Check that your user can open this folder.'
    except OSError as ex:
        return f'Could not read {root}: {ex.strerror or ex}'


def browse(path):
    """List the sub-folders of a folder, for the folder picker."""
    path = os.path.expanduser(path or '')
    if not path or not os.path.isdir(path):
        path = str(Path.home())
    path = os.path.normpath(path)
    out = {'path': path, 'parent': os.path.dirname(path) if path != '/' else None, 'dirs': [], 'volumes': candidate_roots()}
    try:
        names = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir() and not e.name.startswith(('.', '@', '$', '#')):
                        names.append(e.name)
                except OSError:
                    continue
        out['dirs'] = sorted(names, key=str.lower)[:1000]
        out['truncated'] = len(names) > 1000
    except OSError as ex:
        out['error'] = describe_root_problem(path) or str(ex)
    return out


# ----------------------------------------------------------------------------------------
# Text extraction. Each extractor returns a list of (page, label, text, needs_ocr).
# ----------------------------------------------------------------------------------------
class Reject(Exception):
    """File can't be read (password protected, damaged...)."""


def chunk_lines(lines, limit=TEXT_CHUNK):
    out, cur, n = [], [], 0
    for ln in lines:
        cur.append(ln)
        n += len(ln) + 1
        if n >= limit:
            out.append('\n'.join(cur)); cur, n = [], 0
    if cur:
        out.append('\n'.join(cur))
    return out


def local(tag):
    return tag.rsplit('}', 1)[-1]


def para_texts(root):
    """Text of every paragraph (a:p / w:p) below root."""
    out = []
    for p in root.iter():
        if local(p.tag) == 'p':
            t = ''.join(x.text or '' for x in p.iter() if local(x.tag) == 't')
            t = re.sub(r'\s+', ' ', t).strip()
            if t:
                out.append(t)
    return out


def resolve(base, target):
    if target.startswith('/'):
        return target[1:]
    parts = base.split('/')[:-1]
    for seg in target.split('/'):
        if seg == '..':
            if parts:
                parts.pop()
        elif seg and seg != '.':
            parts.append(seg)
    return '/'.join(parts)


def read_rels(z, part):
    d, _, name = part.rpartition('/')
    rp = (d + '/' if d else '') + '_rels/' + name + '.rels'
    out = {}
    if rp in z.namelist():
        for r in ET.fromstring(z.read(rp)):
            if r.get('TargetMode') != 'External':
                out[r.get('Id')] = (r.get('Type', ''), resolve(part, r.get('Target', '')))
    return out


def extract_pdf(path):
    fitz = load_fitz()
    try:
        doc = fitz.open(path, filetype='pdf')
    except Exception:
        raise Reject('damaged or not a valid PDF')
    try:
        if doc.needs_pass:
            raise Reject('password protected')
        out = []
        for i in range(doc.page_count):
            try:
                page = doc[i]
                t = page.get_text('text') or ''
                need = len(t.strip()) < 40 and bool(page.get_images(full=False))
            except Exception:
                t, need = '', False
            out.append((i + 1, f'Page {i + 1}', t, need))
        return out
    finally:
        doc.close()


def extract_docx(path):
    with zipfile.ZipFile(path) as z:
        if 'word/document.xml' not in z.namelist():
            raise Reject('not a Word file')
        paras = para_texts(ET.fromstring(z.read('word/document.xml')))
    return [(i + 1, f'Section {i + 1}', c, False) for i, c in enumerate(chunk_lines(paras))]


def extract_pptx(path):
    out = []
    with zipfile.ZipFile(path) as z:
        pres = 'ppt/presentation.xml'
        if pres not in z.namelist():
            raise Reject('not a PowerPoint file')
        prel = read_rels(z, pres)
        ns_r = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
        order = [e.get(ns_r) for e in ET.fromstring(z.read(pres)).iter() if local(e.tag) == 'sldId']
        for n, rid in enumerate(order, 1):
            if rid not in prel:
                continue
            spath = prel[rid][1]
            if spath not in z.namelist():
                continue
            lines = para_texts(ET.fromstring(z.read(spath)))
            for typ, tgt in read_rels(z, spath).values():
                if typ.endswith('/notesSlide') and tgt in z.namelist():
                    lines += ['Notes: ' + t for t in para_texts(ET.fromstring(z.read(tgt)))]
            out.append((n, f'Slide {n}', '\n'.join(lines), False))
    return out


def extract_xlsx(path):
    out, budget = [], 6_000_000
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if 'xl/workbook.xml' not in names:
            raise Reject('not an Excel file')
        sst = []
        if 'xl/sharedStrings.xml' in names:
            for si in ET.fromstring(z.read('xl/sharedStrings.xml')):
                sst.append(''.join(t.text or '' for t in si.iter() if local(t.tag) == 't'))
        wrel = read_rels(z, 'xl/workbook.xml')
        ns_r = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
        page = 0
        for sh in ET.fromstring(z.read('xl/workbook.xml')).iter():
            if local(sh.tag) != 'sheet' or sh.get(ns_r) not in wrel:
                continue
            sname, spath = sh.get('name'), wrel[sh.get(ns_r)][1]
            if spath not in names:
                continue
            rows, rowno = [], 0
            with z.open(spath) as f:
                for _, el in ET.iterparse(f, events=('end',)):
                    if local(el.tag) != 'row':
                        continue
                    vals = []
                    for c in el:
                        t, v = c.get('t'), None
                        for ch in c:
                            if local(ch.tag) == 'v':
                                v = ch.text
                            elif local(ch.tag) == 'is':
                                v = ''.join(x.text or '' for x in ch.iter() if local(x.tag) == 't')
                        if v is None or v == '':
                            continue
                        if t == 's':
                            try:
                                v = sst[int(v)]
                            except (ValueError, IndexError):
                                continue
                        vals.append(v)
                    if vals:
                        rowno += 1
                        rows.append(' | '.join(vals))
                    el.clear()
                    if rowno > 200_000:
                        break
            for i, c in enumerate(chunk_lines(rows, 4000)):
                page += 1
                budget -= len(c)
                out.append((page, f'Sheet "{sname}" part {i + 1}', c, False))
            if budget < 0:
                break
    return out


def extract_text(path):
    with open(path, 'rb') as f:
        raw = f.read(5_000_000)
    text = raw.decode('utf-8', errors='replace')
    if path.lower().endswith(('.html', '.htm', '.xml')):
        text = re.sub(r'<[^>]+>', ' ', text)
    return [(i + 1, f'Part {i + 1}', c, False) for i, c in enumerate(chunk_lines(text.splitlines()))]


def extract_eml(path):
    with open(path, 'rb') as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    head = '\n'.join(f'{h}: {msg[h]}' for h in ('Subject', 'From', 'To', 'Cc', 'Date') if msg[h])
    body = msg.get_body(preferencelist=('plain', 'html'))
    text = body.get_content() if body else ''
    if body and body.get_content_type() == 'text/html':
        text = re.sub(r'<[^>]+>', ' ', text)
    atts = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    if atts:
        head += '\nAttachments: ' + ', '.join(atts)
    return [(1, 'Message', head + '\n\n' + text, False)]


def extractor_for(ext):
    if ext == 'pdf':
        return extract_pdf
    if ext in ('docx', 'docm', 'dotx'):
        return extract_docx
    if ext in ('pptx', 'pptm'):
        return extract_pptx
    if ext in ('xlsx', 'xlsm'):
        return extract_xlsx
    if ext == 'eml':
        return extract_eml
    if ext in KINDS['text']:
        return extract_text
    return None


# ----------------------------------------------------------------------------------------
# OCR (runs in worker processes)
# ----------------------------------------------------------------------------------------
def _worker_init(parent_pid):
    """OCR workers quit on their own if WorkSearch stops, however it was stopped."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    def watch():
        while True:
            time.sleep(3)
            if os.getppid() != parent_pid:
                os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def ocr_page(path, page_no, lang, dpi, tess):
    fitz = load_fitz()
    doc = fitz.open(path)
    try:
        pix = doc[page_no - 1].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        png = pix.tobytes('png')
    finally:
        doc.close()
    env = dict(os.environ, OMP_THREAD_LIMIT='1')  # one thread per worker; the pool provides the parallelism
    r = subprocess.run([tess, 'stdin', 'stdout', '-l', lang, '--psm', '3'], input=png,
                       capture_output=True, timeout=300, env=env)
    return r.stdout.decode('utf-8', errors='replace')


# ----------------------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------------------
SCHEMA_VERSION = 2  # bump when the index layout changes; older indexes are rebuilt automatically
SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, rel TEXT, name TEXT, ext TEXT, kind TEXT,
  size INTEGER, mtime REAL, indexed REAL, pages INTEGER DEFAULT 0, status TEXT, note TEXT,
  ocr_left INTEGER DEFAULT 0, row_lo INTEGER, row_hi INTEGER);
CREATE INDEX IF NOT EXISTS files_kind ON files(kind);
CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
  name, rel, body, file_id UNINDEXED, page UNINDEXED, label UNINDEXED,
  tokenize='unicode61 remove_diacritics 2', prefix='3');
CREATE TABLE IF NOT EXISTS ocr_queue(file_id INTEGER, page INTEGER, row_id INTEGER, PRIMARY KEY(file_id, page));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=NORMAL')
    return c


def init_db():
    c = connect()
    ver = c.execute('PRAGMA user_version').fetchone()[0]
    if ver != SCHEMA_VERSION:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='files'").fetchone():
            log(f'index format changed (v{ver} -> v{SCHEMA_VERSION}); rebuilding the index')
        c.executescript('DROP TABLE IF EXISTS docs; DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS ocr_queue;')
    c.executescript(SCHEMA)
    c.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    c.commit()
    c.close()


def word_root(w):
    """Crude word stem so 'supplies', 'supplied' and 'supply' find each other."""
    w = w.lower()
    for suf in ('ies', 'ied', 'ing', 'ed', 'es', 's', 'y'):
        if w.endswith(suf) and not (suf == 's' and w.endswith('ss')) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def fts_query(q):
    """Turn what the user typed into an FTS5 query. Every word must match; the last word also matches as a
    prefix (search as you type); "quoted text" must match as a phrase."""
    toks = re.findall(r'"[^"]+"|[^\s"]+', q)
    parts = []
    for i, t in enumerate(toks):
        quoted = t.startswith('"')
        words = re.findall(r'\w+', t.lower())
        if not words:
            continue
        if quoted or len(words) > 1:  # phrase, or something like C5889-01
            parts.append('"' + ' '.join(words) + '"')
            continue
        w = words[0]
        last = i == len(toks) - 1 and not q.endswith(' ')
        root = word_root(w)
        if last and len(w) >= 3:
            parts.append(f'("{w}"*' + (f' OR "{root}"*)' if root != w else ')'))
        elif root != w:
            parts.append(f'("{w}" OR "{root}"*)')
        else:
            parts.append(f'"{w}"')
    return ' AND '.join(parts)


# ----------------------------------------------------------------------------------------
# Indexer: folder scan + OCR queue
# ----------------------------------------------------------------------------------------
class Indexer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = os.path.normpath(os.path.expanduser(cfg['root']))
        self.state = {'phase': 'idle', 'done': 0, 'total': 0, 'current': '', 'last_scan': None, 'error': '', 'root_ok': True}
        self.wlock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.abort = threading.Event()      # set while the folder is being switched: running scan stops early
        self.scan_lock = threading.Lock()   # only one scan (or folder switch) at a time
        self.tess = find_tesseract()
        self.ocr_errors = 0

    # ---- folder walking ----
    def walk(self):
        self.walk_errors = 0
        out, stack = [], [self.root]
        while stack:
            if self.abort.is_set():
                return None
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        n = e.name
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if n[0] in '@.#' or n.startswith('$'):
                                    continue
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                if n.startswith(('.', '~$')) or n.lower() in SKIP_FILES:
                                    continue
                                st = e.stat()
                                out.append((e.path, st.st_size, st.st_mtime))
                        except OSError:
                            continue
            except OSError as ex:
                self.walk_errors += 1
                log('cannot list', d, ex)
            self.state.update(phase='listing', done=len(out), current=d)
        return out

    def scan(self):
        with self.scan_lock:
            self._scan()

    def _scan(self):
        st = self.state
        st.update(error='', done=0, total=0)
        problem = describe_root_problem(self.root)
        if problem:
            st.update(phase='idle', root_ok=False, error=problem)
            return
        entries = self.walk()
        if entries is None:  # folder was switched while listing
            st.update(phase='idle', current='')
            return
        conn = connect()
        try:
            existing = {r['path']: r for r in conn.execute('SELECT id,path,size,mtime,status FROM files')}
            if not entries and existing:
                st.update(phase='idle', root_ok=False, error='The folder looks empty or unreachable. The index was not changed.')
                return
            st['root_ok'] = True
            seen = {p for p, _, _ in entries}
            # if any folder could not be listed (NAS hiccup), don't drop anything from the index this time
            gone = [] if self.walk_errors else [r['id'] for p, r in existing.items() if p not in seen]
            for fid in gone:
                self.delete_file(conn, fid)
            todo = []
            for p, size, mtime in entries:
                old = existing.get(p)
                if old and old['size'] == size and abs(old['mtime'] - mtime) <= 2:
                    continue
                todo.append((p, size, mtime))
            todo.sort(key=lambda t: -t[2])  # newest first
            st.update(phase='reading', done=0, total=len(todo))
            for i, (p, size, mtime) in enumerate(todo, 1):
                if self.stop.is_set() or self.abort.is_set():
                    break
                st.update(done=i, current=os.path.relpath(p, self.root))
                try:
                    self.index_file(conn, p, size, mtime)
                except Exception as ex:  # one bad file must not stop the scan
                    log('index failed', p, ex)
            if self.abort.is_set():
                return
            conn.execute("INSERT OR REPLACE INTO meta VALUES('last_scan', ?)", (str(time.time()),))
            conn.commit()
            st['last_scan'] = time.time()
            log(f'scan finished: {len(entries)} files, {len(todo)} read, {len(gone)} removed')
        finally:
            conn.close()
            st.update(phase='idle', current='')

    def delete_file(self, conn, fid):
        with self.wlock:
            r = conn.execute('SELECT row_lo,row_hi FROM files WHERE id=?', (fid,)).fetchone()
            if r and r['row_lo'] is not None:
                conn.execute('DELETE FROM docs WHERE rowid BETWEEN ? AND ?', (r['row_lo'], r['row_hi']))
            conn.execute('DELETE FROM ocr_queue WHERE file_id=?', (fid,))
            conn.execute('DELETE FROM files WHERE id=?', (fid,))
            conn.commit()

    def index_file(self, conn, path, size, mtime):
        rel = os.path.relpath(path, self.root).replace(os.sep, '/')
        name = os.path.basename(path)
        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        kind = kind_of(ext)
        rows, status, note = [], 'ok', ''
        ex = extractor_for(ext)
        if size > self.cfg['max_extract_mb'] * 1024 * 1024 and ex:
            status, note = 'name-only', 'file too large to read; only the name is searchable'
        elif ex:
            try:
                rows = ex(path)
            except Reject as e:
                status, note = 'error', str(e)
            except Exception as e:
                status, note = 'error', f'{type(e).__name__}: {e}'[:200]
        elif ext in LEGACY:
            status, note = 'name-only', 'old file format; only the name is searchable (save as .docx/.xlsx/.pptx)'
        else:
            status = 'name-only'
        if not rows:
            rows = [(0, 'File name', '', False)]
        with self.wlock:
            old = conn.execute('SELECT id,row_lo,row_hi FROM files WHERE path=?', (path,)).fetchone()
            if old:
                if old['row_lo'] is not None:
                    conn.execute('DELETE FROM docs WHERE rowid BETWEEN ? AND ?', (old['row_lo'], old['row_hi']))
                conn.execute('DELETE FROM ocr_queue WHERE file_id=?', (old['id'],))
                conn.execute('UPDATE files SET rel=?,name=?,ext=?,kind=?,size=?,mtime=?,indexed=?,status=?,note=? WHERE id=?',
                             (rel, name, ext, kind, size, mtime, time.time(), status, note, old['id']))
                fid = old['id']
            else:
                fid = conn.execute('INSERT INTO files(path,rel,name,ext,kind,size,mtime,indexed,status,note) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                   (path, rel, name, ext, kind, size, mtime, time.time(), status, note)).lastrowid
            lo = hi = None
            ocr = []
            for page, label, text, need in rows:
                rid = conn.execute('INSERT INTO docs(name,rel,body,file_id,page,label) VALUES(?,?,?,?,?,?)',
                                   (name, rel, text, fid, page, label)).lastrowid
                lo = rid if lo is None else lo
                hi = rid
                if need:
                    ocr.append((fid, page, rid))
            if ocr and self.tess:
                conn.executemany('INSERT OR REPLACE INTO ocr_queue VALUES(?,?,?)', ocr)
            conn.execute('UPDATE files SET pages=?,row_lo=?,row_hi=?,ocr_left=? WHERE id=?',
                         (len(rows), lo, hi, len(ocr) if self.tess else 0, fid))
            conn.commit()

    def set_root(self, new_root):
        """Switch to another folder. The old index is discarded (it describes the old folder). Returns an error or ''."""
        new_root = os.path.normpath(os.path.expanduser(new_root.strip()))
        if not os.path.isabs(new_root):
            return 'Please give a full path, such as /Volumes/Qnap/Work.'
        if new_root == '/':
            return 'Pick a specific folder, not the whole disk.'
        problem = describe_root_problem(new_root)
        if problem:
            return problem
        if new_root == self.root:
            self.wake.set()
            return ''
        self.abort.set()                 # ask a running scan to stop...
        with self.scan_lock:             # ...and wait until it has
            with self.wlock:
                conn = connect()
                try:
                    conn.execute('DELETE FROM docs')
                    conn.execute('DELETE FROM files')
                    conn.execute('DELETE FROM ocr_queue')
                    conn.execute("DELETE FROM meta WHERE k='last_scan'")
                    conn.commit()
                finally:
                    conn.close()
            self.root = new_root
            self.cfg['root'] = new_root
            save_config(self.cfg)
            self.state.update(phase='idle', done=0, total=0, current='', last_scan=None, error='', root_ok=True)
            self.abort.clear()
        log(f'folder changed to {new_root}')
        self.wake.set()
        return ''

    def new_pool(self, workers):
        return cf.ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn'),
                                      initializer=_worker_init, initargs=(os.getpid(),))

    def shutdown(self):
        self.stop.set()
        self.wake.set()
        pool = getattr(self, 'pool', None)
        if pool:
            for proc in list(getattr(pool, '_processes', {}).values()):
                proc.terminate()
            pool.shutdown(wait=False, cancel_futures=True)

    # ---- background loops ----
    def scan_loop(self):
        init_db()
        while not self.stop.is_set():
            try:
                self.scan()
            except Exception as ex:
                log('scan error', ex)
                self.state.update(phase='idle', error=str(ex))
            # if the NAS isn't mounted yet (common right after login), look again in a minute
            self.wake.wait(60 if not self.state['root_ok'] else self.cfg['rescan_hours'] * 3600)
            self.wake.clear()

    def ocr_loop(self):
        if not self.tess:
            log('Tesseract not found: scanned PDFs will not be OCRed. On a Mac: brew install tesseract')
            return
        workers = self.cfg['ocr_workers'] or max(1, (os.cpu_count() or 4) - 2)
        pool = self.pool = self.new_pool(workers)
        conn = connect()
        log(f'OCR ready ({self.tess}, {workers} workers)')
        while not self.stop.is_set():
            rows = conn.execute('SELECT q.file_id,q.page,q.row_id,f.path FROM ocr_queue q JOIN files f ON f.id=q.file_id '
                                'ORDER BY q.file_id LIMIT ?', (workers * 6,)).fetchall()
            if not rows:
                self.state['ocr_active'] = False
                self.stop.wait(4)
                continue
            self.state['ocr_active'] = True
            futs = {pool.submit(ocr_page, r['path'], r['page'], self.cfg['ocr_lang'], self.cfg['ocr_dpi'], self.tess): r for r in rows}
            for fut in cf.as_completed(futs):
                r = futs[fut]
                try:
                    text = fut.result()
                except Exception as ex:
                    text = ''
                    self.ocr_errors += 1
                    log('OCR failed', r['path'], 'page', r['page'], type(ex).__name__, ex)
                    if isinstance(ex, cf.process.BrokenProcessPool):
                        pool = self.pool = self.new_pool(workers)
                with self.wlock:
                    # still wanted? (the file may have been changed and re-queued while this page was being read)
                    if conn.execute('SELECT 1 FROM ocr_queue WHERE file_id=? AND page=? AND row_id=?',
                                    (r['file_id'], r['page'], r['row_id'])).fetchone():
                        if text.strip():
                            conn.execute('UPDATE docs SET body=? WHERE rowid=?', (text, r['row_id']))
                        conn.execute('DELETE FROM ocr_queue WHERE file_id=? AND page=?', (r['file_id'], r['page']))
                        conn.execute('UPDATE files SET ocr_left=MAX(0,ocr_left-1) WHERE id=?', (r['file_id'],))
                        conn.commit()


# ----------------------------------------------------------------------------------------
# Search + web server
# ----------------------------------------------------------------------------------------
def highlight_terms(q):
    """Regexes that roughly match each search word, including simple word endings (supply ~ supplied)."""
    return [re.compile(r'(?<!\w)' + re.escape(word_root(w)) + r'\w*', re.I) for w in dict.fromkeys(re.findall(r'\w+', q.lower()))]


def make_snippet(body, pats, width=220):
    """Short excerpt around the best cluster of matches; matches are wrapped in chr(1) ... chr(2). None if no match."""
    spans = sorted((m.start(), m.end(), i) for i, p in enumerate(pats) for m in p.finditer(body, 0, 200_000))[:400]
    if not spans:
        return None
    # centre on the shortest stretch of text that contains every search word found on the page
    need, counts, j, best = len({i for _, _, i in spans}), {}, 0, None
    for a, b, i in spans:
        counts[i] = counts.get(i, 0) + 1
        while len(counts) == need:
            if best is None or b - spans[j][0] < best[0]:
                best = (b - spans[j][0], spans[j][0])
            k = spans[j][2]
            counts[k] -= 1
            if not counts[k]:
                del counts[k]
            j += 1
    best = best[1]
    lo = max(0, best - 60)
    lo = body.rfind(' ', 0, lo) + 1 if lo else 0
    hi = min(len(body), lo + width + 60)
    out, pos = [], lo
    for a, b, _ in spans:
        if a < pos or b > hi:
            continue
        out += [body[pos:a], '\x01', body[a:b], '\x02']
        pos = b
    out.append(body[pos:hi])
    text = re.sub(r'\s+', ' ', ''.join(out)).strip()
    return ('… ' if lo else '') + text + (' …' if hi < len(body) else '')


def filters(kinds, folder):
    """SQL conditions on the files table (alias f) for the file-type buttons and the folder menu."""
    kinds = [k for k in (kinds or '').split(',') if k]
    where, args = [], []
    if kinds:
        where.append('f.kind IN (%s)' % ','.join('?' * len(kinds)))
        args += kinds
    if folder:
        where.append("(f.rel LIKE ? ESCAPE '\\')")
        args.append(folder.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%')
    return where, args, (' AND ' + ' AND '.join(where)) if where else ''


def search(conn, q, kinds, folder, sort, limit, offset):
    where, args, extra = filters(kinds, folder)
    fq = fts_query(q)
    if not fq:
        sql = f'SELECT f.* FROM files f WHERE 1=1 {extra} ORDER BY f.mtime DESC LIMIT ? OFFSET ?'
        rows = conn.execute(sql, args + [limit, offset]).fetchall()
        total = conn.execute(f'SELECT count(*) FROM files f WHERE 1=1 {extra}', args).fetchone()[0]
        return {'total': total, 'results': [file_dict(r, []) for r in rows], 'recent': True}
    # rank pages; join the files table only when a filter needs it (much faster without)
    join = 'JOIN files f ON f.id=d.file_id' if where else ''
    sql = (f"SELECT d.rowid AS rid, d.file_id, d.page, d.label, bm25(docs,8.0,3.0,1.0) AS score "
           f"FROM docs d {join} WHERE docs MATCH ? {extra} ORDER BY score LIMIT 1500")
    try:
        hits = conn.execute(sql, [fq] + args).fetchall()
    except sqlite3.OperationalError as ex:
        return {'total': 0, 'results': [], 'error': str(ex)}
    groups = {}
    for h in hits:  # best first, so the first row per file sets the file's rank
        rows = groups.setdefault(h['file_id'], [])
        if len(rows) < 6:
            rows.append(h)
    ids = list(groups)
    meta = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute('SELECT * FROM files WHERE id IN (%s)' % ','.join('?' * len(part)), part):
            meta[r['id']] = r
    ordered = [i for i in ids if i in meta]
    if sort == 'newest':
        ordered.sort(key=lambda i: -meta[i]['mtime'])
    pats = highlight_terms(q)
    results = []
    for fid in ordered[offset:offset + limit]:
        shown, first = [], None
        for h in groups[fid]:
            body = conn.execute('SELECT body FROM docs WHERE rowid=?', (h['rid'],)).fetchone()['body'] or ''
            snip = make_snippet(body, pats)
            if snip and len(shown) < 3:
                shown.append({'page': h['page'], 'label': h['label'], 'snip': snip})
            elif not snip and body.strip() and (first is None or h['page'] < first['page']):
                first = {'page': h['page'], 'label': h['label'], 'snip': re.sub(r'\s+', ' ', body[:260]).strip() + ' …'}
        if not shown and first:  # matched on the file or folder name only: show the start of the file
            shown = [first]
        results.append(file_dict(meta[fid], shown))
    return {'total': len(ordered), 'capped': len(hits) >= 1500, 'results': results}


def file_dict(r, hits):
    return {'id': r['id'], 'name': r['name'], 'rel': r['rel'], 'dir': os.path.dirname(r['rel']), 'kind': r['kind'], 'ext': r['ext'],
            'size': r['size'], 'mtime': r['mtime'], 'status': r['status'], 'note': r['note'], 'ocr_left': r['ocr_left'], 'hits': hits}


def status_dict(ix, conn):
    f = conn.execute("SELECT count(*) n, coalesce(sum(pages),0) p, coalesce(sum(ocr_left),0) o, "
                     "sum(status='error') e, sum(status='name-only') nm FROM files").fetchone()
    return {'files': f['n'], 'pages': f['p'], 'ocr_left': f['o'], 'errors': f['e'] or 0, 'name_only': f['nm'] or 0,
            'root': ix.root, 'tesseract': bool(ix.tess), 'platform': 'mac' if IS_MAC else 'linux', **ix.state}


# ----------------------------------------------------------------------------------------
# Ask AI: answers questions from the indexed files through OpenRouter (or any OpenAI-compatible API)
# ----------------------------------------------------------------------------------------
class AIError(Exception):
    pass


def ai_key(cfg):
    return (cfg.get('ai_key') or os.environ.get('OPENROUTER_API_KEY') or '').strip()


def ai_request(cfg, path, payload=None, timeout=120):
    headers = {'Content-Type': 'application/json', 'HTTP-Referer': 'http://127.0.0.1', 'X-Title': 'WorkSearch'}
    if ai_key(cfg):
        headers['Authorization'] = 'Bearer ' + ai_key(cfg)
    req = urllib.request.Request(cfg['ai_base_url'].rstrip('/') + path, headers=headers,
                                 data=None if payload is None else json.dumps(payload).encode())
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as ex:
        try:
            msg = json.loads(ex.read()).get('error', {})
            msg = msg.get('message') if isinstance(msg, dict) else str(msg)
        except Exception:
            msg = ''
        hint = {401: ' Check the API key in AI settings.', 402: ' Your OpenRouter credit may have run out.',
                404: ' The model may no longer exist; pick another in AI settings.'}.get(ex.code, '')
        raise AIError(f'The AI service said: {msg or ex.reason} (HTTP {ex.code}).{hint}')
    except (urllib.error.URLError, OSError) as ex:
        raise AIError(f'Could not reach the AI service at {cfg["ai_base_url"]}: {getattr(ex, "reason", ex)}')


_models_cache = {'at': 0, 'url': '', 'list': []}


def ai_models(cfg):
    """Models the service offers, with prices in dollars per million tokens (cached for an hour)."""
    c = _models_cache
    if c['list'] and c['url'] == cfg['ai_base_url'] and time.time() - c['at'] < 3600:
        return c['list']
    with ai_request(cfg, '/models', timeout=30) as r:
        data = json.loads(r.read()).get('data') or []
    def price(m, k):
        try:
            return round(float((m.get('pricing') or {})[k]) * 1e6, 4)
        except (KeyError, TypeError, ValueError):
            return None
    out = [{'id': m['id'], 'name': m.get('name') or m['id'], 'ctx': m.get('context_length'),
            'in': price(m, 'prompt'), 'out': price(m, 'completion'), 'created': m.get('created') or 0}
           for m in data if m.get('id')]
    out.sort(key=lambda m: -m['created'])
    c.update(at=time.time(), url=cfg['ai_base_url'], list=out)
    return out


def ai_chat(cfg, messages, max_tokens=None, stream=False):
    payload = {'model': cfg['ai_model'] or 'openrouter/auto', 'messages': messages, 'stream': stream}
    if max_tokens:
        payload['max_tokens'] = max_tokens
    if 'openrouter.ai' in cfg['ai_base_url']:
        payload['usage'] = {'include': True}  # OpenRouter then reports the cost of the request
    return ai_request(cfg, '/chat/completions', payload, timeout=300)


STOPWORDS = set('''a an and are as at be by can could did do does for from had has have how i in is it its me my of on or
our that the their there these this to was we were what when where which who why will with would you your about any all
find show tell give list please file files document documents'''.split())

PLAN_PROMPT = """You turn a question into keyword searches for a full-text search engine over someone's work files \
(quotes, orders, invoices, manuals, drawings, emails, spreadsheets, reports). A search only matches pages that contain \
ALL of its words, so keep each search to 1-3 distinctive words. Cover likely synonyms, part numbers, company names, \
abbreviations and alternative spellings with separate searches. Put the most specific search first. Reply with JSON \
only, no other text: {"searches": ["...", "..."]} with 3 to 6 searches."""

ANSWER_PROMPT = """You answer questions about the user's own work files, using the numbered excerpts below, which come \
from a search of those files. Cite the excerpts you rely on with their numbers in square brackets, like [3] or [2][5], \
right after the fact they support. If the excerpts don't contain the answer, say so plainly and suggest other words to \
search for. Mention the file name when it helps the user find the document. Be concise: short paragraphs or bullet \
lists. Today's date is {today}."""


def plan_searches(cfg, question, history):
    """Ask the model for keyword searches. Returns (searches, cost)."""
    msgs = [{'role': 'system', 'content': PLAN_PROMPT}]
    for t in history:
        msgs += [{'role': 'user', 'content': t['q']}, {'role': 'assistant', 'content': t['a'][:1500]}]
    msgs.append({'role': 'user', 'content': 'Question: ' + question})
    cost, searches = 0.0, []
    try:
        with ai_chat(cfg, msgs, max_tokens=2000) as r:
            j = json.loads(r.read())
        cost = float((j.get('usage') or {}).get('cost') or 0)
        text = ((j.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        m = re.search(r'\{.*\}', text, re.S)
        searches = [str(x) for x in json.loads(m.group(0)).get('searches', [])] if m else []
    except (AIError, ValueError, AttributeError, TypeError) as ex:
        log('AI search planning failed, using the question words:', ex)
    words = [w for w in re.findall(r'\w+', question.lower()) if w not in STOPWORDS and len(w) > 2]
    if words:
        searches.append(' '.join(words[:3]))
        searches += sorted(words, key=len, reverse=True)[:3]  # broad single-word fallbacks, used last
    return list(dict.fromkeys(s.strip() for s in searches if s.strip()))[:9], cost


def excerpt(body, pats, n=3500):
    body = re.sub(r'[ \t]+', ' ', body)
    if len(body) <= n:
        return body.strip()
    pos = min((m.start() for p in pats for m in [p.search(body)] if m), default=0)
    lo = max(0, min(pos - 800, len(body) - n))
    return ('… ' if lo else '') + body[lo:lo + n].strip() + ' …'


def retrieve(conn, searches, kinds, folder, max_pages):
    """Best pages across all searches, taking turns between searches so each one contributes."""
    where, args, extra = filters(kinds, folder)
    join = 'JOIN files f ON f.id=d.file_id' if where else ''
    lists = []
    for s in searches:
        fq = fts_query(s + ' ')  # trailing space: whole words (plus simple endings), not prefixes
        if not fq:
            continue
        try:
            lists.append(conn.execute(f"SELECT d.rowid AS rid, d.file_id, d.page, d.label FROM docs d {join} "
                                      f"WHERE docs MATCH ? {extra} ORDER BY bm25(docs,8.0,3.0,1.0) LIMIT 40",
                                      [fq] + args).fetchall())
        except sqlite3.OperationalError:
            continue
    picked, seen, per_file = [], set(), {}
    for depth in range(40):
        for rows in lists:
            if depth < len(rows) and len(picked) < max_pages:
                h = rows[depth]
                if h['rid'] in seen or per_file.get(h['file_id'], 0) >= 3:
                    continue
                seen.add(h['rid'])
                per_file[h['file_id']] = per_file.get(h['file_id'], 0) + 1
                picked.append(h)
    return picked


def build_sources(conn, hits, pats, chars=3500):
    meta = {}
    for fid in {h['file_id'] for h in hits}:
        r = conn.execute('SELECT * FROM files WHERE id=?', (fid,)).fetchone()
        if r:
            meta[fid] = r
    sources = []
    for h in hits:
        if h['file_id'] not in meta:
            continue
        body = conn.execute('SELECT body FROM docs WHERE rowid=?', (h['rid'],)).fetchone()['body'] or ''
        text = excerpt(body, pats, chars) if body.strip() else '(no readable text on this page; matched by file or folder name)'
        snip = make_snippet(body, pats) or re.sub(r'\s+', ' ', body[:260]).strip()
        d = file_dict(meta[h['file_id']], [{'page': h['page'], 'label': h['label'], 'snip': snip}] if snip else [])
        d.update(n=len(sources) + 1, text=text, label=h['label'] or '')
        sources.append(d)
    return sources


def file_sources(conn, file_id, max_chars=80000):
    """All pages of one file (up to a limit), for 'Summarise'."""
    f = conn.execute('SELECT * FROM files WHERE id=?', (file_id,)).fetchone()
    if not f:
        return []
    out, used = [], 0
    for r in conn.execute('SELECT page, label, body FROM docs WHERE file_id=? ORDER BY page', (file_id,)):
        body = re.sub(r'[ \t]+', ' ', r['body'] or '').strip()
        if not body or used >= max_chars:
            continue
        body = body[:max_chars - used]
        used += len(body)
        d = file_dict(f, [{'page': r['page'], 'label': r['label'], 'snip': body[:260] + ' …'}])
        d.update(n=len(out) + 1, text=body, label=r['label'] or '')
        out.append(d)
    return out


def ask(cfg, conn, body, emit):
    """Answer a question, sending progress as dicts to emit(): status, sources, delta (answer text), done, error."""
    question = str(body.get('q') or '').strip()
    history = [{'q': str(t.get('q', ''))[:2000], 'a': str(t.get('a', ''))[:4000]}
               for t in (body.get('history') or [])[-3:] if isinstance(t, dict)]
    if not ai_key(cfg) and 'openrouter.ai' in cfg['ai_base_url']:
        return emit({'type': 'error', 'error': 'Add your OpenRouter API key in AI settings first.'})
    cost = 0.0
    if body.get('file_id'):
        history = []  # a summary stands on its own
        emit({'type': 'status', 'text': 'Reading the file…'})
        sources = file_sources(conn, int(body['file_id']))
        question = question or 'Summarise this document: what it is, who it involves, the key facts, figures and dates.'
        if not sources:
            return emit({'type': 'error', 'error': 'This file has no readable text to summarise.'})
    else:
        if not question:
            return emit({'type': 'error', 'error': 'Type a question first.'})
        emit({'type': 'status', 'text': 'Working out what to search for…'})
        searches, cost = plan_searches(cfg, question, history)
        emit({'type': 'status', 'text': 'Searching: ' + ' · '.join(searches[:6])})
        pats = highlight_terms(' '.join(searches))
        hits = retrieve(conn, searches, body.get('type', ''), body.get('folder', ''), int(cfg.get('ai_max_pages') or 20))
        sources = build_sources(conn, hits, pats)
        if not sources:
            return emit({'type': 'error', 'error': 'No matching pages were found for: ' + ', '.join(searches)
                         + '. Try different words, or check the file-type and folder filters.'})
    emit({'type': 'sources', 'sources': [{k: v for k, v in s.items() if k != 'text'} for s in sources]})
    emit({'type': 'status', 'text': f'Reading {len(sources)} page{"s" if len(sources) != 1 else ""} with {cfg["ai_model"]}…'})
    ctx = '\n\n'.join(f"[{s['n']}] File: {s['rel']}" + (f" ({s['label']})" if s['label'] else '')
                      + f" · modified {time.strftime('%Y-%m-%d', time.localtime(s['mtime']))}\n{s['text']}" for s in sources)
    msgs = [{'role': 'system', 'content': ANSWER_PROMPT.format(today=time.strftime('%Y-%m-%d'))}]
    for t in history:
        msgs += [{'role': 'user', 'content': t['q']}, {'role': 'assistant', 'content': t['a']}]
    msgs.append({'role': 'user', 'content': f'Excerpts:\n\n{ctx}\n\nQuestion: {question}'})
    model, usage = cfg['ai_model'], {}
    with ai_chat(cfg, msgs, stream=True) as r:
        for raw in r:
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue  # blank lines and ": OPENROUTER PROCESSING" keep-alives
            data = line[5:].strip()
            if data == '[DONE]':
                break
            try:
                j = json.loads(data)
            except ValueError:
                continue
            if j.get('error'):
                e = j['error']
                raise AIError('The AI service said: ' + (e.get('message') if isinstance(e, dict) else str(e)))
            model = j.get('model') or model
            usage = j.get('usage') or usage
            for ch in j.get('choices') or []:
                t = (ch.get('delta') or {}).get('content')
                if t:
                    emit({'type': 'delta', 'text': t})
    cost += float(usage.get('cost') or 0)
    emit({'type': 'done', 'model': model, 'cost': cost if cost else None,
          'tokens': (usage.get('prompt_tokens') or 0) + (usage.get('completion_tokens') or 0)})


def ai_settings(cfg):
    k = ai_key(cfg)
    return {'has_key': bool(k), 'key_hint': ('…' + k[-4:]) if len(k) > 8 else ('set' if k else ''),
            'key_from_env': bool(k) and not cfg.get('ai_key'), 'model': cfg['ai_model'],
            'recent': cfg.get('ai_recent') or [], 'base_url': cfg['ai_base_url'],
            'needs_key': 'openrouter.ai' in cfg['ai_base_url']}


def set_ai_settings(cfg, body):
    if 'key' in body:
        cfg['ai_key'] = str(body['key'] or '').strip()
    if body.get('base_url'):
        u = str(body['base_url']).strip()
        if not re.match(r'https?://', u):
            raise AIError('The API address must start with http:// or https://')
        cfg['ai_base_url'] = u
    if body.get('model'):
        m = str(body['model']).strip()
        recent = [cfg.get('ai_model')] + (cfg.get('ai_recent') or [])  # keep the model being replaced in the quick menu
        cfg['ai_model'] = m
        cfg['ai_recent'] = list(dict.fromkeys(x for x in [m] + recent if x))[:8]
    save_config(cfg)
    return ai_settings(cfg)


class Handler(BaseHTTPRequestHandler):
    server_version = 'WorkSearch'
    ix = None
    token = ''
    port = 0

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header('Content-Type', ctype + ('; charset=utf-8' if ctype.startswith('text') else ''))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return self.headers.get('Host', '') in (f'127.0.0.1:{self.port}', f'localhost:{self.port}')

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {'error': 'bad host'})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == '/':
            html = (APP_DIR / 'ui.html').read_text().replace('__TOKEN__', self.token)
            return self._send(200, html, 'text/html')
        conn = connect()
        try:
            if u.path == '/api/search':
                res = search(conn, q.get('q', ''), q.get('type', ''), q.get('folder', ''), q.get('sort', 'relevance'),
                             min(int(q.get('limit', 40)), 100), int(q.get('offset', 0)))
                return self._send(200, res)
            if u.path == '/api/status':
                return self._send(200, status_dict(self.ix, conn))
            if u.path == '/api/folders':
                cols = conn.execute("SELECT substr(rel,1,instr(rel||'/','/')-1) AS top, count(*) n FROM files "
                                    "WHERE instr(rel,'/')>0 GROUP BY top ORDER BY top COLLATE NOCASE").fetchall()
                return self._send(200, [{'name': r['top'], 'n': r['n']} for r in cols])
            if u.path == '/api/browse':
                return self._send(200, browse(q.get('path', '')))
            if u.path == '/api/ai/settings':
                return self._send(200, ai_settings(self.ix.cfg))
            if u.path == '/api/ai/models':
                try:
                    return self._send(200, {'models': ai_models(self.ix.cfg), 'current': self.ix.cfg['ai_model']})
                except (AIError, ValueError) as ex:
                    return self._send(502, {'error': str(ex)})
            if u.path == '/api/problems':
                rows = conn.execute("SELECT id,rel,status,note FROM files WHERE status IN ('error','name-only') "
                                    "ORDER BY status, rel LIMIT 500").fetchall()
                return self._send(200, [dict(r) for r in rows])
        finally:
            conn.close()
        self._send(404, {'error': 'not found'})

    def _ask(self, body):
        """Stream the answer as newline-delimited JSON so the page can show it as it is written."""
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.close_connection = True
        def emit(obj):
            self.wfile.write((json.dumps(obj) + '\n').encode())
            self.wfile.flush()
        conn = connect()
        try:
            ask(self.ix.cfg, conn, body, emit)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page stopped listening (new question or closed tab)
        except AIError as ex:
            try:
                emit({'type': 'error', 'error': str(ex)})
            except OSError:
                pass
        except Exception as ex:
            log('Ask AI failed:', repr(ex))
            try:
                emit({'type': 'error', 'error': f'Something went wrong: {ex}'})
            except OSError:
                pass
        finally:
            conn.close()

    def do_POST(self):
        if not self._host_ok() or self.headers.get('X-WS-Token') != self.token:
            return self._send(403, {'error': 'forbidden'})
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            body = {}
        if self.path == '/api/setroot':
            err = self.ix.set_root(str(body.get('path', '')))
            return self._send(400 if err else 200, {'error': err} if err else {'ok': True, 'root': self.ix.root})
        if self.path == '/api/quit':
            self._send(200, {'ok': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path == '/api/ai/settings':
            try:
                return self._send(200, set_ai_settings(self.ix.cfg, body))
            except AIError as ex:
                return self._send(400, {'error': str(ex)})
        if self.path == '/api/ask':
            return self._ask(body)
        if self.path == '/api/rescan':
            self.ix.wake.set()
            return self._send(200, {'ok': True})
        if self.path == '/api/open':
            conn = connect()
            try:
                r = conn.execute('SELECT path FROM files WHERE id=?', (body.get('id'),)).fetchone()
            finally:
                conn.close()
            if not r or not os.path.exists(r['path']):
                return self._send(404, {'error': 'File not found. Is the NAS mounted?'})
            reveal = body.get('mode') == 'reveal'
            if IS_MAC:
                cmd = ['open', '-R', r['path']] if reveal else ['open', r['path']]
            elif reveal and shutil.which('nautilus'):
                cmd = ['nautilus', '--select', r['path']]
            else:
                cmd = ['xdg-open', os.path.dirname(r['path']) if reveal else r['path']]
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as ex:
                return self._send(500, {'error': f'Could not open: {ex}'})
            return self._send(200, {'ok': True, 'path': r['path']})
        self._send(404, {'error': 'not found'})


def open_url(url):
    try:
        subprocess.Popen(['open', url] if IS_MAC else ['xdg-open', url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        print('Open', url, 'in your browser.')


def serve(cfg, open_browser=True):
    init_db()
    ix = Indexer(cfg)
    Handler.ix, Handler.token, Handler.port = ix, secrets.token_urlsafe(24), cfg['port']
    threading.Thread(target=ix.scan_loop, daemon=True).start()
    threading.Thread(target=ix.ocr_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(('127.0.0.1', cfg['port']), Handler)
    url = f"http://127.0.0.1:{cfg['port']}/"
    log(f'WorkSearch running at {url}  (folder: {ix.root})')
    if open_browser:
        threading.Timer(1.0, lambda: open_url(url)).start()
    def on_signal(signum, frame):  # logout, kill, launchctl stop: shut down cleanly
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ix.shutdown()
        httpd.server_close()
        log('WorkSearch stopped')


def server_running(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=2) as r:
            return 'files' in json.loads(r.read())
    except Exception:
        return False


def tell_user(msg):
    print(msg)
    try:
        if IS_MAC:
            subprocess.run(['osascript', '-e', f'display alert "WorkSearch" message {json.dumps(msg)}'], timeout=120)
        elif shutil.which('zenity'):
            subprocess.run(['zenity', '--error', '--title=WorkSearch', '--text=' + msg], timeout=120)
        elif shutil.which('notify-send'):
            subprocess.run(['notify-send', 'WorkSearch', msg], timeout=10)
    except Exception:
        pass


def launch(cfg, open_browser=True):
    """Start the server in the background if needed, then open the search page."""
    url = f"http://127.0.0.1:{cfg['port']}/"
    if not server_running(cfg['port']):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = open(DATA_DIR / 'server.log', 'a')
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve', '--no-browser'], stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(APP_DIR))
        for _ in range(60):
            time.sleep(0.5)
            if server_running(cfg['port']):
                break
        else:
            tell_user(f"WorkSearch could not start. Port {cfg['port']} may be in use by another program. "
                      f"Details are in {DATA_DIR / 'server.log'}")
            return 1
    if open_browser:
        open_url(url)
    return 0


def main():
    ap = argparse.ArgumentParser(description='Local full-text search for a work folder.')
    sub = ap.add_subparsers(dest='cmd')
    for name in ('serve', 'scan', 'launch'):
        sp = sub.add_parser(name)
        sp.add_argument('--root', help='folder to index (saved for next time)')
        sp.add_argument('--port', type=int)
        sp.add_argument('--no-browser', action='store_true')
    sub.add_parser('status')
    args = ap.parse_args()
    cfg = load_config()
    if getattr(args, 'root', None):
        cfg['root'] = args.root
    if getattr(args, 'port', None):
        cfg['port'] = args.port
    if args.cmd in ('serve', 'scan', 'launch') and (getattr(args, 'root', None) or getattr(args, 'port', None)):
        save_config(cfg)
    if args.cmd == 'launch':
        sys.exit(launch(cfg, open_browser=not args.no_browser))
    if args.cmd == 'scan':
        init_db()
        ix = Indexer(cfg)
        ix.scan()
        print(ix.state)
    elif args.cmd == 'status':
        init_db()
        c = connect()
        print(status_dict(Indexer(cfg), c))
    else:
        serve(cfg, open_browser=not getattr(args, 'no_browser', False))


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
