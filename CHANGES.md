# HTTPServer (Upgraded) — All Changes

This document chronicles every modification made to Python 3.14's built-in
`http.server` module — from the initial visual overhaul to the backend
hardening — all resulting in a single command that still works everywhere:

```bash
python3 -m http.server 8000
```

---

## Phase 1 — Visual Overhaul

### Directory Listing UI
**File:** `server.py` — `SimpleHTTPRequestHandler.list_directory()`

Replaced the stock `<ul>` directory listing with a full-page styled HTML
template featuring:

- **Google Sans** via Google Fonts (weights 300–900)
- **Black (`#000`) background, white (`#fff`) text** — no grey anywhere
- **GSAP animations** (fade + slide stagger on entries) loaded from CDN
- **Fallback** if GSAP fails: a `setTimeout` and `DOMContentLoaded` check
  make all elements visible within 2.5 seconds (no FOUC)
- **Decorative elements**: two subtle gradient lines, floating dots, grain
  texture overlay, radial gradient vignettes
- **Breadcrumb path** at the top with clickable segments
- **Folder / file section labels** separating the two groups
- **File size** (human-readable: B/KB/MB/GB) and **modification date**
  shown on hover
- **Folder and file counts** displayed in the stats bar
- **Back-link** to parent directory when not at root
- **Responsive** layout for mobile via `@media(max-width:768px)`

### Emoji File-Type Icons
Replaced circled-letter icons (ⓘ, Ⓝ, Ⓢ, …) with recognizable emoji:

| Extension | Emoji | Meaning |
|-----------|-------|---------|
| `.py` | 🐍 | Python |
| `.js` | ⚡ | JavaScript |
| `.html` / `.htm` | 🌐 | Web page |
| `.css` | 🎨 | Stylesheet |
| `.json` | 📋 | Data |
| `.txt` | 📝 | Text |
| `.md` / `.rst` | 📖 | Documentation |
| `.xml` | 📰 | Markup |
| `.png` / `.jpg` / `.jpeg` | 🖼 | Image |
| `.gif` | 📹 | Animation |
| `.svg` | 📐 | Vector |
| `.ico` | 🪟 | Icon |
| `.zip` / `.tar` / `.gz` / `.deb` / `.rpm` | 📦 | Archive |
| `.mp4` / `.mkv` / `.avi` / `.mov` | 🎬 | Video |
| `.mp3` / `.wav` / `.flac` / `.ogg` | 🎵 | Audio |
| `.pdf` | 📑 | Document |
| `.sh` | 💻 | Script |
| `.yml` / `.yaml` / `.toml` / `.conf` / `.cfg` | ⚙ | Config |
| `.exe` / `.msi` | 💠 | Executable |
| `.ttf` / `.otf` / `.woff` / `.woff2` | 🖨 | Font |
| `.db` / `.sqlite` / `.sqlite3` | 💾 | Database |
| `.iso` / `.img` | 💿 | Disk image |
| `.log` | 📜 | Log |
| `.key` / `.pem` / `.crt` | 🔑 | Certificate |
| `.lock` | 🔒 | Lock file |
| `.dockerfile` | 🐳 | Docker |
| *(directory)* | 📁 | Folder |
| *(default file)* | 📄 | Generic file |

---

## Phase 2 — Backend Upgrades

### 1. Threading (built-in)
Python 3.14 already uses `ThreadingHTTPServer` by default — concurrent
browser connections no longer block each other.  No code change needed,
but confirmed active.

### 2. Symlink Escape Guard
**File:** `server.py` — `SimpleHTTPRequestHandler.send_head()`

Added a check that resolves both the served path and the root directory
with `os.path.realpath()` and verifies the former starts with the latter.
Symlinks pointing outside `--directory` now return **403 Forbidden**.

```
# Before:  ln -s /etc/passwd leak.txt → curl serves /etc/passwd
# After:   curl → 403 "Path outside served directory"
```

### 3. ETag & Conditional Requests
**File:** `server.py` — `SimpleHTTPRequestHandler.send_head()`

- **ETag** generated as `W/"inode-hex(timestamp)-hex(size)"` (weak
  validator)
- **`If-None-Match`** → **304 Not Modified** when ETag matches
- Preserves existing `If-Modified-Since` → 304 as fallback

### 4. HTTP Range Requests (Partial Content)
**File:** `server.py` — `SimpleHTTPRequestHandler.send_head()`

- Parses `Range: bytes=<start>-<end>` (single range)
- Supports:
  - `bytes=0-499` — absolute start/end
  - `bytes=500-` — from offset to end
  - `bytes=-500` — last N bytes (suffix range)
- Returns **206 Partial Content** with `Content-Range` header
- Invalid ranges return **416 Range Not Satisfiable**
- `Accept-Ranges: bytes` advertises capability
- `If-Range` support — only process Range if entity unchanged
- Video scrubbing and media playback now work in browsers

### 5. gzip Compression (Directory Listing)
**File:** `server.py` — `SimpleHTTPRequestHandler.list_directory()`

When the client sends `Accept-Encoding: gzip`, the generated HTML is
compressed with `gzip.compress()` before sending.  Only applied when
the compressed payload is actually smaller (avoids wasting CPU on tiny
listings).  Typical reduction: **13 KB → 3 KB (~76%)**.

### 6. CORS Support (`--cors` flag)
**File:** `server.py` — `SimpleHTTPRequestHandler` + CLI

New optional flag:

```bash
python3 -m http.server 8000 --cors
```

When enabled:
- `Access-Control-Allow-Origin: *` on every response
- `Access-Control-Allow-Methods: GET, HEAD, OPTIONS`
- `Access-Control-Allow-Headers: *`
- `OPTIONS` preflight requests return **204 No Content** with CORS headers

---

## How to Use

```bash
# Basic — serve current directory (with all upgrades active by default)
python3 -m http.server 8000

# With cross-origin support
python3 -m http.server 8000 --cors

# Serve a specific directory
python3 -m http.server 8000 --directory /path/to/serve

# With HTTPS (self-signed cert)
python3 -m http.server 8000 --tls-cert cert.pem --tls-key key.pem
```

All visual and backend upgrades are **active by default** — no extra flags
needed except `--cors` for cross-origin access.

---

## Files Modified

| File | Changes |
|------|---------|
| `/usr/lib/python3.14/http/server.py` | `list_directory()` — full HTML template replacement |
| | `icon_map` — emoji-based file type icons |
| | `send_head()` — ETag, Range, symlink guard |
| | `list_directory()` — gzip compression |
| | `end_headers()` — CORS headers |
| | `do_OPTIONS()` — CORS preflight handler |
| | CLI parser — `--cors` flag |
| | `import gzip` — added |

## Backup

Original unmodified stdlib files are backed up at:
`~/http-server/backup/http-module-original/`
