"""
Knowledge library: documents the AI can search (the search_knowledge tool).

Put Ubiquiti help articles, UniFi release notes, vendor PDFs, your own notes and
diagrams in data/knowledge/ (or add them in the app). They are read, cut into
passages of about 1,200 characters, and indexed on this computer:
  * keyword search (BM25), always: no extra software, works offline;
  * meaning search, optional: an embedding model on your Local AI server (Ollama's
    nomic-embed-text, for example) turns each passage into a vector, so a question
    finds passages that say the same thing in other words. Results of both are merged
    (reciprocal rank fusion). If that server is unreachable, keyword search carries on.
Nothing is sent anywhere except, for meaning search, the passages and questions to
your own Local AI server. Files stay in data/knowledge/; the index is
data/knowledge_index.json and is rebuilt for any file that changes.

Supported: Markdown, plain text, HTML (saved web pages) and PDF.

Authority: every document gets a tier from where it came from (Ubiquiti official,
standards, government guidance, your own, other vendors, expert commentary, community),
which weights search results and is shown to the AI so a blog never overrides Ubiquiti
or an RFC.

Licence: every document also gets a licence class. Only "shareable" ones (RFCs under the
IETF Trust's terms; NIST/CISA publications, which are US Government works) go into an
exported library, with a NOTICE crediting each. Ubiquiti, IEEE, vendor and blog material
is for your own reference and is never exported. The index and its vectors are never
exported either: they hold text from every document.
"""

import html
import io
import json
import logging
import math
import os
import re
import threading
import time
import warnings
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from html.parser import HTMLParser

import config_store as cfg

TYPES = {".md": "Markdown", ".markdown": "Markdown", ".txt": "text", ".text": "text", ".rst": "text",
         ".html": "web page", ".htm": "web page", ".pdf": "PDF"}
MAX_FILE = 30_000_000            # bytes per document
MAX_FETCH = 8_000_000            # bytes per web page fetched by URL
CHUNK, MIN_CHUNK = 1200, 200     # characters per passage
PASSAGE_SHOWN = 1500             # characters of each passage the AI sees
EMBED_BATCH = 32
_STOP = set("""a an and are as at be but by can do does for from has have how i if in into is it its of on or
that the their then there these this to was what when where which who why will with you your""".split())

_lock = threading.RLock()
_cache = {"path": None, "index": None, "bm25": None}
_job = {"running": False, "phase": None, "done": 0, "total": 0, "error": None, "finished_at": None}


class KnowledgeError(ValueError):          # the server shows ValueErrors to the user as they are
    pass


# ── paths and settings ────────────────────────────────────────────────────────
def folder():
    return os.path.join(cfg.DATA_DIR, "knowledge")


def _index_path():
    return os.path.join(cfg.DATA_DIR, "knowledge_index.json")


def _sources_path():
    return os.path.join(cfg.DATA_DIR, "knowledge_sources.json")


def sources():
    """{file name: {"source": url, "from": origin, ...}} for imported and downloaded documents."""
    try:
        with open(_sources_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_sources(m):
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    tmp = _sources_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
    os.replace(tmp, _sources_path())


def record_source(name, **info):
    with _lock:
        m = sources()
        m[name] = dict(m.get(name) or {}, **info)
        save_sources(m)


# key: (label shown to you and the AI, search weight, rank: 1 = most authoritative)
AUTHORITY = {
    "ubiquiti":  ("Ubiquiti official", 1.0, 1),
    "standard":  ("Standard (IETF, IEEE, Wi-Fi Alliance)", 0.92, 2),
    "yours":     ("Your own document", 0.95, 2),
    "guidance":  ("Government guidance (NIST, CISA)", 0.86, 3),
    "vendor":    ("Other vendor's documentation", 0.72, 4),
    "other":     ("Other web page", 0.62, 5),
    "expert":    ("Expert commentary", 0.62, 5),
    "community": ("Community post", 0.55, 6),
}
_HOST_TIERS = [
    (r"(^|\.)(ui\.com|ubnt\.com|ubiquiti\.com)$", "ubiquiti"),
    (r"(^|\.)(rfc-editor\.org|ietf\.org|iana\.org|ieee\.org|wi-fi\.org)$", "standard"),
    (r"(^|\.)(nist\.gov|cisa\.gov|nsa\.gov|ncsc\.gov\.uk)$", "guidance"),
    (r"(^|\.)(cisco\.com|meraki\.com|hpe\.com|arubanetworks\.com|juniper\.net|fortinet\.com|paloaltonetworks\.com|"
     r"mikrotik\.com|ruckusnetworks\.com|commscope\.com|extremenetworks\.com|tp-link\.com|netgear\.com|"
     r"microsoft\.com|apple\.com|google\.com)$", "vendor"),
    (r"(^|\.)(ekahau\.com|packetpushers\.net|ipspace\.net|wlanpros\.com|semfionetworks\.com|revolutionwifi\.net|"
     r"cloudflare\.com)$", "expert"),
    (r"(^|\.)(reddit\.com|stackexchange\.com|serverfault\.com|superuser\.com|stackoverflow\.com)$", "community"),
]


def authority_of(info):
    """info: {"source": url, "from": origin} -> tier key."""
    origin, src = info.get("from"), info.get("source") or ""
    if origin in ("ui-help", "ui-docs"):
        return "ubiquiti"
    if origin == "rfc":
        return "standard"
    if origin == "security":
        return "guidance"
    u = urllib.parse.urlparse(src)
    host = (u.hostname or "").lower()
    if host == "community.ui.com":                    # official release notes vs ordinary posts
        return "ubiquiti" if u.path.startswith("/releases") or "/rss/releases" in u.path else "community"
    for rx, tier in _HOST_TIERS:
        if re.search(rx, host):
            return tier
    return "other" if host else "yours"


def licence_of(info, tier):
    """-> (class, why). Only "shareable" documents go into an exported library."""
    origin = info.get("from")
    host = (urllib.parse.urlparse(info.get("source") or "").hostname or "").lower()
    if origin == "rfc" or re.search(r"(^|\.)(rfc-editor\.org)$", host):
        return "shareable", ("IETF Trust Legal Provisions: RFCs may be copied and shared in full, unmodified, "
                             "with their copyright notice.")
    if origin == "security" or re.search(r"(^|\.)(nist\.gov|cisa\.gov)$", host):
        return "shareable", ("US Government work, not subject to copyright in the US (17 U.S.C. 105). Keep the "
                             "attribution; a few documents quote others' material, which keeps its own copyright.")
    if tier == "yours":
        return "yours", "Your own document: share it only if you have the right to."
    if tier == "ubiquiti":
        return "personal", "© Ubiquiti Inc.: downloaded for your own reference. Don't redistribute."
    if re.search(r"(^|\.)ieee\.org$", host):
        return "personal", "© IEEE: IEEE restricts copying and redistribution of its standards."
    return "personal", "Copyrighted by its publisher: for your own reference."


def _info(name, idx_entry, manifest):
    return dict({"source": (idx_entry or {}).get("source")}, **(manifest.get(name) or {}))


def embed_model():
    return (cfg.get_setting("KNOWLEDGE_EMBED_MODEL") or "").strip()


def _embed_endpoint():
    url = (cfg.get_setting("LOCAL_AI_URL") or "").strip()
    if not url:
        return None, None
    from providers import normalize_base_url
    return normalize_base_url(url), cfg.provider_key("local")


# ── reading documents ─────────────────────────────────────────────────────────
class _HTMLText(HTMLParser):
    """Visible text of a web page, with headings kept as Markdown-style '#' lines."""
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "svg", "form", "aside", "button"}
    BLOCK = {"p", "div", "li", "tr", "br", "section", "article", "table", "ul", "ol", "pre", "blockquote", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self._in_title, self._heading = [], 0, "", False, None
        self._title_done = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif re.fullmatch(r"h[1-4]", tag):
            self._heading = int(tag[1])
            self.out.append("\n\n" + "#" * self._heading + " ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self._in_title = False
            self._title_done = self._title_done or bool(self.title.strip())    # the first title wins
        elif re.fullmatch(r"h[1-4]", tag):
            self._heading = None
            self.out.append("\n")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            if not self._title_done:
                self.title += data
        elif not self.skip:
            self.out.append(data if self._heading is None else data.replace("\n", " "))


def _html_to_text(raw):
    p = _HTMLText()
    p.feed(raw)
    text = re.sub(r"[ \t]+", " ", "".join(p.out))
    return html.unescape(p.title).strip(), re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def extract(path):
    """-> (title, [(section, text)]). Sections are headings (Markdown/HTML) or pages (PDF)."""
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    if ext == ".pdf":
        logging.getLogger("pypdf").setLevel(logging.ERROR)     # it reports every odd font; nobody needs that
        warnings.filterwarnings("ignore", module="pypdf")
        try:
            from pypdf import PdfReader
        except ImportError:
            raise KnowledgeError("PDF support isn't installed yet. Close the app and start it again so the launcher installs it.")
        try:
            reader = PdfReader(path)
            title = ((reader.metadata or {}).get("/Title") or "").strip() if reader.metadata else ""
            pages = [(f"page {i + 1}", (p.extract_text() or "").strip()) for i, p in enumerate(reader.pages)]
        except Exception as e:
            raise KnowledgeError(f"Couldn't read this PDF ({type(e).__name__}).")
        pages = [p for p in pages if p[1]]
        if not pages:
            raise KnowledgeError("This PDF has no text to read (a scanned image?).")
        return title or name, pages
    text = _read_text(path)
    title = ""
    if ext in (".html", ".htm"):
        title, text = _html_to_text(text)
    return title or name, _sections(text)


def _sections(text):
    """Split Markdown-ish text at headings. -> [(heading path, body)]"""
    out, path, buf = [], [], []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            out.append((" › ".join(path), body))
        buf.clear()
    for line in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(.+?)\s*#*$", line)
        if m:
            flush()
            level = len(m.group(1))
            path[:] = path[:level - 1] + [m.group(2).strip()]
        else:
            buf.append(line)
    flush()
    return out or [("", text.strip())]


def chunk(sections):
    """[(section, text)] -> [(section, passage)] of about CHUNK characters, cut at paragraphs,
    then sentences. A passage keeps the heading it came from."""
    out = []
    for section, text in sections:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        cur = ""
        for p in paras:
            pieces = [p] if len(p) <= CHUNK else re.split(r"(?<=[.!?])\s+", p)
            for piece in pieces:
                while len(piece) > CHUNK:                        # one enormous sentence or table row
                    if cur:
                        out.append((section, cur))
                        cur = ""
                    out.append((section, piece[:CHUNK]))
                    piece = piece[CHUNK:]
                if cur and len(cur) + len(piece) + 2 > CHUNK:
                    out.append((section, cur))
                    cur = ""
                cur = f"{cur}\n\n{piece}" if cur else piece
        if cur:
            if out and out[-1][0] == section and len(cur) < MIN_CHUNK and len(out[-1][1]) + len(cur) < CHUNK * 1.5:
                out[-1] = (section, out[-1][1] + "\n\n" + cur)
            else:
                out.append((section, cur))
    return out


# ── keyword search (BM25) ─────────────────────────────────────────────────────
def tokens(text):
    """Words, keeping model numbers whole ('802.11ax', 'u6-pro', 'udm-se') and also their parts."""
    out = []
    for t in re.findall(r"[a-z0-9]+(?:[._\-/][a-z0-9]+)*", text.lower()):
        if t in _STOP:
            continue
        out.append(t)
        if re.search(r"[._\-/]", t):
            out += [p for p in re.split(r"[._\-/]", t) if p and p not in _STOP]
    return out


class _BM25:
    def __init__(self, chunks, k1=1.4, b=0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokens(f"{c['title']} {c['section']} {c['section']} {c['text']}")) for c in chunks]
        self.len = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 1
        df = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query):
        q = [t for t in tokens(query) if t in self.idf]
        out = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for t in q:
                f = d.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            out.append(s)
        return out


# ── the index ─────────────────────────────────────────────────────────────────
def _empty():
    return {"version": 1, "files": {}, "chunks": [], "embed_model": None}


def _load():
    path = _index_path()
    if _cache["path"] == path and _cache["index"] is not None:
        return _cache["index"]
    try:
        with open(path, encoding="utf-8") as f:
            idx = json.load(f)
        if idx.get("version") != 1:
            idx = _empty()
    except (OSError, ValueError):
        idx = _empty()
    _cache.update(path=path, index=idx, bm25=None)
    return idx


def _save(idx):
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    tmp = _index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, separators=(",", ":"))
    os.replace(tmp, _index_path())


def reset_cache():
    with _lock:
        _cache.update(path=None, index=None, bm25=None)


def _sig(path):
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}"


def _files():
    d = folder()
    if not os.path.isdir(d):
        return {}
    return {n: os.path.join(d, n) for n in sorted(os.listdir(d))
            if os.path.isfile(os.path.join(d, n)) and os.path.splitext(n)[1].lower() in TYPES}


_refresh_lock = threading.Lock()       # one reader at a time; others don't wait for it
_reading = {"running": False, "done": 0, "total": 0}


def _read_one(name, path, sig):
    """Read one document into passages, outside the index lock. -> (index entry, passages)"""
    entry = {"sig": sig, "title": name, "chunks": 0, "error": None, "type": TYPES[os.path.splitext(name)[1].lower()]}
    passages = []
    try:
        if os.path.getsize(path) > MAX_FILE:
            raise KnowledgeError(f"Too big (over {MAX_FILE // 1_000_000} MB).")
        title, sections = extract(path)
        parts = chunk(sections)
        entry.update(title=title[:200], chunks=len(parts))
        if name.lower().endswith((".html", ".htm")):         # pages saved by this app say where they came from
            with open(path, "rb") as f:
                m = re.search(rb"<!-- source: (\S+?) -->", f.read(2000))
            if m:
                entry["source"] = html.unescape(m.group(1).decode("utf-8", "replace"))
        def section(s_):                                     # "Title › Part" -> "Part": the title is shown anyway
            return "" if s_ == title else s_[len(title) + 3:] if s_.startswith(title + " › ") else s_
        passages = [{"doc": name, "title": title[:200], "section": section(s_)[:200], "text": t} for s_, t in parts]
    except KnowledgeError as e:
        entry["error"] = str(e)
    except Exception as e:                                    # one bad file must not stop the rest
        entry["error"] = f"Couldn't read it ({type(e).__name__})."
    return entry, passages


def refresh(wait=True):
    """Bring the keyword index up to date with the folder (only changed files are read), then
    start adding meaning-search vectors in the background if an embedding model is set.
    Reading hundreds of PDFs takes minutes, so searches and the library screen don't wait for
    it (wait=False): they use what is already indexed and show the progress.
    -> True if anything changed (False also when another refresh is already running)."""
    if not _refresh_lock.acquire(blocking=wait):
        return False
    try:
        return _refresh()
    finally:
        _refresh_lock.release()


QUICK = 3                              # changed files read on the spot; more go to the background


def _pending():
    with _lock:
        idx, files = _load(), _files()
        return [n for n, p in files.items() if idx["files"].get(n, {}).get("sig") != _sig(p)] +                [n for n in idx["files"] if n not in files]


def refresh_quick():
    """For searches and the library screen: a few changed files are read right away; a big batch
    (an import, a folder of PDFs dropped in) is read in the background, and until it finishes the
    library answers from what is already indexed."""
    if not _refresh_lock.acquire(blocking=False):
        return False                                     # someone is already reading
    try:
        if len(_pending()) <= QUICK:
            return _refresh()
    finally:
        _refresh_lock.release()
    threading.Thread(target=refresh, name="knowledge-refresh", daemon=True).start()
    return False


def _refresh():
    changed = False
    with _lock:
        idx = _load()
        files = _files()
        for name in list(idx["files"]):
            if name not in files:
                idx["chunks"] = [c for c in idx["chunks"] if c["doc"] != name]
                del idx["files"][name]
                changed = True
        todo = []
        for name, path in files.items():
            sig = _sig(path)
            if idx["files"].get(name, {}).get("sig") != sig:
                todo.append((name, path, sig))
        model = embed_model() or None
        if idx.get("embed_model") != model:                  # a different model's vectors don't mix
            for c in idx["chunks"]:
                c.pop("vec", None)
            idx["embed_model"] = model
            changed = True
        if changed:
            _save(idx)
            _cache["bm25"] = None
    _reading.update(running=bool(todo), done=0, total=len(todo))
    try:
        for n, (name, path, sig) in enumerate(todo):
            entry, passages = _read_one(name, path, sig)
            with _lock:
                idx = _load()
                idx["chunks"] = [c for c in idx["chunks"] if c["doc"] != name] + passages
                idx["files"][name] = entry
                _cache["bm25"] = None
                if n % 25 == 24:                              # progress survives a restart
                    _save(idx)
            _reading["done"] = n + 1
            changed = True
    finally:
        _reading["running"] = False
    with _lock:
        idx = _load()
        if todo:
            _save(idx)
        need_vectors = bool(idx.get("embed_model")) and any("vec" not in c for c in idx["chunks"])
    if need_vectors:
        start_embedding()
    return changed


def has_docs():
    try:
        return bool(_files())
    except OSError:
        return False


# ── meaning search (embeddings from the Local AI server) ──────────────────────
def _prefix(model, kind):
    # nomic-embed-text was trained with these task prefixes; other models ignore them harmlessly.
    return ("search_document: " if kind == "doc" else "search_query: ") if "nomic" in (model or "") else ""


def embed(texts, kind="doc", timeout=60):
    """-> list of unit vectors from the Local AI server's /v1/embeddings (OpenAI-compatible)."""
    model = embed_model()
    url, key = _embed_endpoint()
    if not model or not url:
        raise KnowledgeError("Meaning search needs a Local AI address and an embedding model.")
    body = json.dumps({"model": model, "input": [_prefix(model, kind) + t[:6000] for t in texts]}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url + "/embeddings", data=body, headers=headers, method="POST")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            data = json.loads(r.read(200_000_000))
    except urllib.error.HTTPError as e:
        raise KnowledgeError(f"The Local AI server refused the embedding request ({e.code}). Is '{model}' an embedding model on it?")
    except Exception as e:
        raise KnowledgeError(f"Can't reach the Local AI server for meaning search ({type(e).__name__}).")
    vecs = [d["embedding"] for d in sorted(data.get("data", []), key=lambda d: d.get("index", 0))]
    if len(vecs) != len(texts):
        raise KnowledgeError("The embedding answer didn't match the request.")
    out = []
    for v in vecs:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([round(x / n, 5) for x in v])
    return out


def start_embedding():
    with _lock:
        if _job["running"]:
            return False
        _job.update(running=True, phase="meaning search", done=0, total=0, error=None)
    threading.Thread(target=_embed_all, name="knowledge-embed", daemon=True).start()
    return True


def _chunk_key(c):
    """What a vector belongs to: the passage itself, not its position. The list can be
    reordered or rebuilt (an import, an edited or deleted document) while a batch is out."""
    return c.get("doc"), c.get("title"), c.get("section"), c.get("text")


def _embed_all():
    try:
        with _lock:
            idx = _load()
            todo = [{"key": _chunk_key(c), "text": f"{c['title']}. {c['section']}\n{c['text']}"}
                    for c in idx["chunks"] if "vec" not in c]
            _job["total"] = len(todo)
            model = idx.get("embed_model")
        for start in range(0, len(todo), EMBED_BATCH):
            batch = todo[start:start + EMBED_BATCH]
            vecs = embed([b["text"] for b in batch], "doc")
            with _lock:
                idx = _load()
                if idx.get("embed_model") != model:          # settings changed under us: start again later
                    break
                waiting = {}
                for c in idx["chunks"]:
                    if "vec" not in c:
                        waiting.setdefault(_chunk_key(c), []).append(c)
                for b, v in zip(batch, vecs):
                    for c in waiting.get(b["key"], ()):       # gone or changed meanwhile: nothing to attach
                        c["vec"] = v
                _job["done"] = min(len(todo), start + len(batch))
                if (start // EMBED_BATCH) % 10 == 9:
                    _save(idx)
        with _lock:
            _save(_load())
    except KnowledgeError as e:
        _job["error"] = str(e)
    except Exception as e:
        _job["error"] = f"{type(e).__name__}: {e}"
    finally:
        _job.update(running=False, finished_at=time.time())


# ── search ────────────────────────────────────────────────────────────────────
def search(query, k=5):
    """-> {"results": [{doc, title, section, text, score}], "mode": "keyword" | "keyword + meaning", "note"}"""
    query = str(query or "").strip()
    if not query:
        raise KnowledgeError("Empty search.")
    refresh_quick()
    with _lock:
        idx = _load()
        chunks = idx["chunks"]
        if not chunks:
            return {"results": [], "mode": "keyword", "note": "The knowledge library is empty."}
        if _cache["bm25"] is None:
            _cache["bm25"] = _BM25(chunks)
        bm = _cache["bm25"].scores(query)
        have_vecs = [i for i, c in enumerate(chunks) if "vec" in c]
    ranked_kw = [i for i in sorted(range(len(bm)), key=lambda i: -bm[i]) if bm[i] > 0][:50]
    mode, note, vec_rel = "keyword", None, {}
    if have_vecs and embed_model():
        try:
            qv = embed([query], "query", timeout=15)[0]
            sims = sorted(((i, sum(a * b for a, b in zip(qv, chunks[i]["vec"]))) for i in have_vecs), key=lambda x: -x[1])[:50]
            hi, lo = sims[0][1], sims[-1][1]
            vec_rel = {i: (v - lo) / ((hi - lo) or 1) for i, v in sims}   # best match 1, 50th 0
            mode = "keyword + meaning"
        except KnowledgeError as e:
            note = f"Meaning search is unavailable right now ({e}); keyword results only."
    # Relevance 0..1 from both searches, then a moderate boost for more authoritative sources:
    # Ubiquiti x1.0, standards x0.97, NIST/CISA x0.94, other vendors x0.89, expert x0.85, community x0.82.
    top_bm = bm[ranked_kw[0]] if ranked_kw else 1
    fused = Counter()
    for i in set(ranked_kw) | set(vec_rel):
        kw = bm[i] / top_bm if top_bm else 0
        fused[i] = (0.5 * kw + 0.5 * vec_rel.get(i, 0)) if vec_rel else kw
    manifest, tiers = sources(), {}
    for i in list(fused):
        doc = chunks[i]["doc"]
        if doc not in tiers:
            tiers[doc] = authority_of(_info(doc, idx["files"].get(doc), manifest))
        fused[i] *= 0.6 + 0.4 * AUTHORITY[tiers[doc]][1]
        if fused[i] <= 0:
            del fused[i]
    best, per_doc = [], Counter()
    for i, _ in fused.most_common():                   # at most two passages from one document
        if per_doc[chunks[i]["doc"]] < 2:
            best.append(i)
            per_doc[chunks[i]["doc"]] += 1
        if len(best) >= max(1, min(int(k or 5), 10)):
            break
    results = [{"doc": chunks[i]["doc"], "title": chunks[i]["title"], "section": chunks[i]["section"],
                "authority": AUTHORITY[tiers[chunks[i]["doc"]]][0], "authority_rank": AUTHORITY[tiers[chunks[i]["doc"]]][2],
                "source": _info(chunks[i]["doc"], idx["files"].get(chunks[i]["doc"]), manifest).get("source"),
                "text": chunks[i]["text"][:PASSAGE_SHOWN], "score": round(fused[i] * 1000, 2)} for i in best]
    out = {"results": results, "mode": mode}
    if note:
        out["note"] = note
    if not results:
        out["note"] = "Nothing in the knowledge library matches that. Try other words."
    return out


# ── managing documents ────────────────────────────────────────────────────────
def _safe_name(name, ext=None):
    base = os.path.basename(str(name or "").replace("\\", "/")).strip() or "document"
    stem, e = os.path.splitext(base)
    e = (ext or e).lower()
    if e not in TYPES:
        raise KnowledgeError(f"Only {', '.join(sorted({v for v in TYPES.values()}))} files can be added.")
    stem = re.sub(r"[^\w\-. ()]+", "_", stem).strip(" .")[:100] or "document"
    return stem + e


def _unique(name):
    d, (stem, ext) = folder(), os.path.splitext(name)
    out, n = name, 2
    while os.path.exists(os.path.join(d, out)):
        out, n = f"{stem} ({n}){ext}", n + 1
    return out


def add_file(name, data):
    """Save a document into the library, or open a shared library (.zip). -> its file name / a summary."""
    if str(name).lower().endswith(".zip"):
        if len(data) > 60_000_000:
            raise KnowledgeError("That shared library is over 60 MB.")
        return import_package(data)
    if len(data) > MAX_FILE:
        raise KnowledgeError(f"Too big: documents can be {MAX_FILE // 1_000_000} MB at most.")
    with _lock:
        os.makedirs(folder(), exist_ok=True)
        name = _unique(_safe_name(name))
        with open(os.path.join(folder(), name), "wb") as f:
            f.write(data)
    refresh_quick()
    return name


def add_url(url):
    """Download a public web page (or PDF) into the library. -> its file name."""
    url = str(url or "").strip()
    if not re.match(r"^https?://[^\s/]+", url, re.I):
        raise KnowledgeError("That isn't a web address (it should start with http:// or https://).")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (unifi-flinchbot knowledge library)"})
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=cfg.ssl_context()))
        with opener.open(req, timeout=30) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            data = r.read(MAX_FETCH + 1)
            final = r.geturl()
    except urllib.error.HTTPError as e:
        raise KnowledgeError(f"The site answered with error {e.code}.")
    except Exception as e:
        raise KnowledgeError(f"Couldn't download that page ({type(e).__name__}). Is this computer online?")
    if len(data) > MAX_FETCH:
        raise KnowledgeError(f"That page is over {MAX_FETCH // 1_000_000} MB.")
    path = urllib.parse.urlparse(final).path
    if "pdf" in ctype or path.lower().endswith(".pdf"):
        ext, title = ".pdf", os.path.basename(path) or "document"
    elif "html" in ctype or "xml" in ctype:
        ext = ".html"
        title, _ = _html_to_text(data.decode("utf-8", "replace"))
        title = title or os.path.basename(path.rstrip("/")) or urllib.parse.urlparse(final).netloc
        # remember where it came from, for the AI and for you
        data = f"<!-- source: {html.escape(final)} -->\n".encode() + data
    elif ctype.startswith("text/"):
        ext, title = ".txt", os.path.basename(path.rstrip("/")) or "page"
    else:
        raise KnowledgeError(f"That address isn't a web page, text or PDF ({ctype or 'unknown type'}).")
    name = add_file(_safe_name(re.sub(r"\s*[|·–-]\s*[^|·–-]*$", "", title) or title, ext), data)
    record_source(name, source=final, **{"from": "page"})
    return name


def delete(name):
    with _lock:
        name = os.path.basename(str(name or ""))
        path = os.path.join(folder(), name)
        if not name or not os.path.isfile(path):
            raise KnowledgeError("That document isn't in the library.")
        os.remove(path)
        m = sources()
        if m.pop(name, None) is not None:
            save_sources(m)
    refresh_quick()


def status():
    refresh_quick()
    with _lock:
        idx = _load()
        per_doc = Counter(c["doc"] for c in idx["chunks"])
        vec_doc = Counter(c["doc"] for c in idx["chunks"] if "vec" in c)
        manifest, docs = sources(), []
        for name, path in _files().items():
            e = idx["files"].get(name, {})
            st = os.stat(path)
            info = _info(name, e, manifest)
            tier = authority_of(info)
            lic, why = licence_of(info, tier)
            docs.append({"name": name, "title": e.get("title") or name, "type": e.get("type"), "size": st.st_size,
                         "added": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                         "passages": per_doc.get(name, 0), "with_meaning": vec_doc.get(name, 0), "error": e.get("error"),
                         "authority": AUTHORITY[tier][0], "tier": tier, "licence": lic, "licence_why": why,
                         "source": info.get("source")})
        total = len(idx["chunks"])
        vecs = sum(1 for c in idx["chunks"] if "vec" in c)
    return {"folder": folder(), "documents": docs, "passages": total, "with_meaning": vecs,
            "embed_model": embed_model() or None, "local_ai": bool(cfg.get_setting("LOCAL_AI_URL")),
            "job": dict(_job), "reading": dict(_reading)}


# ── sharing ───────────────────────────────────────────────────────────────────
PACKAGE_FORMAT = "flinchbot-knowledge"
MAX_PACKAGE = 400_000_000          # uncompressed bytes accepted from a shared library


def export_package(include_own=False):
    """A ZIP of the documents that may legally be shared, with a NOTICE crediting each and saying
    how many were left out and why. The NOTICE never names a left-out document: a title can say
    a lot (a customer, a project), and leaving the document out must leave its name out too.
    Never the index: it holds text from every document. -> (zip bytes, summary)"""
    st = status()
    take = [d for d in st["documents"] if not d["error"] and
            (d["licence"] == "shareable" or (include_own and d["licence"] == "yours"))]
    left = [d for d in st["documents"] if d not in take]
    stamp = time.strftime("%Y-%m-%d")
    notice = [f"Knowledge library exported by UniFi x flinchbot on {stamp}.", "",
              "Only documents that may be shared are included. Each keeps its own terms:", ""]
    for d in take:
        notice += [f"* {d['title']}", f"  File: documents/{d['name']}",
                   f"  Source: {d.get('source') or 'added by the person who exported this library'}",
                   f"  Terms: {d['licence_why']}", ""]
    if left:
        kinds = {"yours": "the exporter's own document(s), not chosen for sharing",
                 "personal": "document(s) whose publisher doesn't allow redistribution"}
        counts = {}
        for d in left:
            k = d["licence"] if d["licence"] in kinds else ("unreadable" if d["error"] else "personal")
            counts[k] = counts.get(k, 0) + 1
        kinds["unreadable"] = "document(s) that couldn't be read"
        notice += [f"{len(left)} other document(s) in the exporter's library were left out:", ""] +                   [f"* {n} {kinds[k]}" for k, n in sorted(counts.items())] + [""]
    m = sources()
    meta = {"format": PACKAGE_FORMAT, "version": 1, "exported": stamp,
            "documents": [{"file": d["name"], "title": d["title"], "licence": d["licence"], "terms": d["licence_why"],
                           "source": d.get("source"), "from": (m.get(d["name"]) or {}).get("from")} for d in take]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("NOTICE.txt", "\n".join(notice))
        z.writestr("library.json", json.dumps(meta, indent=1))
        for d in take:
            z.write(os.path.join(folder(), d["name"]), "documents/" + d["name"])
    return buf.getvalue(), {"included": len(take), "left_out": len(left),
                            "left_out_examples": [d["title"] for d in left[:5]]}


def import_package(data):
    """Open a library someone exported. Only document types the library reads are taken, under
    safe names, within size limits; their sources and terms come along. -> summary text."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise KnowledgeError("That ZIP file is damaged.")
    with z:
        meta = {}
        if "library.json" in z.namelist():
            try:
                meta = json.loads(z.read("library.json"))
            except ValueError:
                meta = {}
        if meta.get("format") != PACKAGE_FORMAT:
            raise KnowledgeError("That ZIP isn't a knowledge library exported from this app.")
        info = {d.get("file"): d for d in meta.get("documents", []) if isinstance(d, dict)}
        members = [i for i in z.infolist() if i.filename.startswith("documents/") and not i.is_dir()]
        if sum(i.file_size for i in members) > MAX_PACKAGE:
            raise KnowledgeError("That library is too big to open.")
        added = 0
        for i in members:
            base = os.path.basename(i.filename)
            if os.path.splitext(base)[1].lower() not in TYPES or i.file_size > MAX_FILE:
                continue
            with _lock:
                os.makedirs(folder(), exist_ok=True)
                name = _unique(_safe_name(base))
                with open(os.path.join(folder(), name), "wb") as f:
                    f.write(z.read(i))
            d = info.get(base) or {}
            record_source(name, source=d.get("source"), shared=True,
                          **{"from": d.get("from") if d.get("from") in ("rfc", "security") else "shared"})
            added += 1
    refresh_quick()
    return f"{added} documents from a shared library"


def open_folder():
    """Show the library folder in Explorer / Finder so many files can be dropped in at once."""
    import subprocess
    import sys
    os.makedirs(folder(), exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(folder())                               # noqa: S606 - our own data folder
    elif sys.platform == "darwin":
        subprocess.Popen(["open", folder()])
    else:
        subprocess.Popen(["xdg-open", folder()])
