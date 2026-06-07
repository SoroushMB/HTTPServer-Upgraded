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

### 7. SSH + SFTP Server (`--sftp` flag)

**Files:** `sftp_server.py` (new), `server.py` — CLI + import

New optional flags:

```bash
python3 -m http.server 8000 --sftp
python3 -m http.server 8000 --sftp --sftp-port 2222
python3 -m http.server 8000 --sftp --sftp-username foo --sftp-password bar
```

When `--sftp` is passed:
- A paramiko-based **SSH server** starts in a background daemon thread
- Serves the same `--directory` root as the HTTP server
- Supports **both interactive shell and SFTP** on the same port
- Shell: `ssh http@localhost -p 2222` — full bash login shell with PTY
- Exec: `ssh http@localhost -p 2222 command` — single command execution
- SFTP: `sftp -P 2222 http@localhost` — file transfer
- SSH RSA host key auto-generated at `~/.ssh/http_server_rsa_key`
- Default port: **2222** (configurable with `--sftp-port`)
- Default username: `http` (configurable with `--sftp-username`)
- Auto-generated random password unless `--sftp-password` is provided
- Authentication: password only (no public key)
- Jailed to the served directory (no escape outside `--directory`)

---

### 8. Web Terminal (`/terminal` endpoint)

**File:** `server.py` — `SimpleHTTPRequestHandler`

Added a browser-based terminal at **`http://localhost:8000/terminal`**:

- Uses **xterm.js** from CDN for a full terminal emulator in the browser
- Type any shell command and press Enter — output streams back
- Supports `cd` with persistent **cwd tracking** across commands
- Commands execute via `POST /terminal/exec` with JSON `{"command": "...", "cwd": "..."}`
- Perfect for quick shell access without leaving the browser
- Timeout: 30 seconds per command

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

# With SFTP file access alongside HTTP
python3 -m http.server 8000 --sftp

# SFTP with custom credentials
python3 -m http.server 8000 --sftp --sftp-username admin --sftp-password mypass

# SSH into the server (interactive shell)
# ssh http@localhost -p 2222

# Run a single command via SSH
# ssh http@localhost -p 2222 ls -la

# SFTP file transfer
# sftp -P 2222 http@localhost

# Web terminal at http://localhost:8000/terminal
# (always available, no extra flag needed)
```

All visual and backend upgrades are **active by default** — no extra flags
needed except `--cors` for cross-origin access and `--sftp` for the SFTP server.
The web terminal at `/terminal` is always available.

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
| | `do_GET()` — `/terminal` route |
| | `do_POST()` — `/terminal/exec` route |
| | CLI parser — `--cors`, `--sftp`, `--sftp-port`, `--sftp-username`, `--sftp-password` flags |
| | `import gzip`, `import secrets`, `import json`, `import shlex`, `import subprocess` — added |
| | `from http import HTTPStatus, sftp_server` — added |
| `/usr/lib/python3.14/http/sftp_server.py` | **New file** — paramiko-based SSH+SFTP server with PTY shell, exec, chroot jailing |

## Backup

Original unmodified stdlib files are backed up at:
`~/http-server/backup/http-module-original/`

---

## Phase 3 — PUT / DELETE (stdlib file management)

**File:** `server.py` — `SimpleHTTPRequestHandler`

### HTTP PUT (`do_PUT`)
- Upload files via `PUT <path>` with raw body (no multipart)
- Requires `Content-Length` header (rejects chunked encoding → **411**)
- Max upload size: 500 MB (configurable via `self.max_upload_size`, **413** when exceeded)
- Filename sanitization: rejects control chars (`ord < 32` or `== 127`) and `/` → **400**
- Symlink escape guard via `os.path.realpath()` → **403**
- PUT to a directory → **405 Method Not Allowed**
- Empty files (Content-Length: 0) create empty files
- Returns **201 Created** on success

### HTTP DELETE (`do_DELETE`)
- Delete files via `DELETE <path>`
- Directories removed recursively with `shutil.rmtree()` (symlinks in tree are deleted, not followed)
- Non-existent paths → **404 Not Found**
- DELETE on root → **403 Forbidden**
- Symlink escape guard via `os.path.realpath()` → **403**
- Returns **204 No Content** on success

### Upload Button (browser UI)
- "Upload File" button in directory listing (hidden via CSS until JS enables it)
- File picker → PUT fetch per file → success/error feedback via alert
- Page auto-reloads on successful upload

### Terminal Security Warning
- Warning banner added between nav bar and terminal: ⚠ "All commands run on the server with your user privileges"

---

## Phase 4 — Code Cleanup & Paramiko Removal

- Removed paramiko dependency entirely (`import paramiko`, paramiko-based SSH/SFTP server)
- Deleted `sftp_server.py` (entire file)
- Removed `--sftp`, `--sftp-port`, `--sftp-username`, `--sftp-password` CLI flags
- Removed `import secrets` (no longer needed)
- Removed ~130 lines of docstrings, unused imports (`shlex`, `signal`), dead code
- Terminal font changed to **JetBrains Mono** (via Google Fonts)
- Breadcrumb path fixed: uses real URL segments from `self.path` instead of hardcoded `~`
- YAML icon fixed: `♙` → `⚙`
- All imports verified stdlib-only

---

## Files Modified (Complete History)

| File | Phase | Changes |
|------|-------|---------|
| `/usr/lib/python3.14/http/server.py` | 1 | `list_directory()` — full HTML template replacement |
| | 1 | `icon_map` — emoji-based file type icons |
| | 2 | `send_head()` — ETag, Range, symlink guard |
| | 2 | `list_directory()` — gzip compression |
| | 2 | `end_headers()` / `do_OPTIONS()` — CORS |
| | 2 | `do_GET()` / `do_POST()` — terminal routing |
| | 2 | CLI parser — `--cors`, `--sftp`, `--sftp-port`, `--sftp-username`, `--sftp-password` |
| | 2 | `import gzip`, `import secrets`, `import json`, `import subprocess` |
| | 3 | `do_PUT()` — file upload with size limit, filename sanitization |
| | 3 | `do_DELETE()` — file/directory removal, root guard |
| | 3 | Upload button in directory listing UI |
| | 3 | Terminal warning banner |
| | 4 | Removed `--sftp` flags, paramiko references, unused imports |
| | 4 | JetBrains Mono, breadcrumb fix, yaml icon fix |
| | 4 | All imports deduplicated to stdlib-only |
| `/usr/lib/python3.14/http/sftp_server.py` | 2 | **Created** — paramiko-based SSH+SFTP server |
| | 4 | **Deleted** — paramiko removed, functionality replaced by PUT/DELETE + terminal |

## Backup

Original unmodified stdlib files are backed up at:
`~/http-server/backup/http-module-original/`
