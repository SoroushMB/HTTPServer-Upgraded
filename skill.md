# HTTP Mastery

Upgrade Python's built-in `http.server` to a modern file server — browser file management, web terminal, media streaming, and image/video previews — using **zero external dependencies**.

## Core Principle

Everything must work with `python3 -m http.server 8000` — no `pip install`, no config files, no extra flags for core features.

## Architecture

```
ThreadingHTTPServer (built-in, Python 3.14+)
  └─ SimpleHTTPRequestHandler (subclass)
       ├── send_head()        → ETag, Range, symlink guard
       ├── list_directory()   → Full HTML UI, gzip, breadcrumbs
       ├── do_GET() / do_HEAD → Terminal routing, file serving
       ├── do_PUT()           → File upload with validation
       ├── do_DELETE()        → File/directory removal
       ├── do_MOVE()          → File/directory rename
       ├── do_MKCOL()         → Directory creation
       ├── do_POST()          → Terminal command execution
       └── do_OPTIONS()       → CORS preflight
```

## Implementation Phases

### Phase 1 — Visual Overhaul
- Replace `<ul>` directory listing with styled HTML (Google Sans, GSAP animations, emoji icons)
- Breadcrumb path: dynamic from `self.path`
- FOUC prevention: `.ani{opacity:0}` + JS fallback (GSAP or 2.5s timeout)

### Phase 2 — Backend Hardening
| Feature | Implementation | Why |
|---------|---------------|-----|
| **Symlink guard** | `os.path.realpath()` on both sides → check `startswith` | Prevent symlink escape outside served directory |
| **Windows compat** | `os.path.normcase()` on both sides | `startswith` is case-sensitive, Windows paths aren't |
| **ETag** | `W/"inode-mtime-size"` | Conditional requests → 304 Not Modified |
| **Range** | `Range: bytes=start-end` parsing | Video scrubbing, partial content → 206 |
| **gzip** | `gzip.compress()` listing HTML | ~76% size reduction |
| **CORS** | `Access-Control-Allow-Origin: *` | Cross-origin browser access (opt-in `--cors` flag) |

### Phase 3 — PUT / DELETE (File Management)
- `do_PUT()`: read loop in 64 KB chunks, Content-Length required (411), max 500 MB (413), control-char rejection (400), symlink guard (403)
- `do_DELETE()`: `shutil.rmtree()` for dirs, `os.remove()` for files, root guard (403), non-existent (404)
- Upload button in UI: hidden `<input type=file>` → `fetch(method:PUT)` → reload

### Phase 4 — Terminal
- `GET /terminal` → xterm.js page (JetBrains Mono, CDN-loaded)
- `POST /terminal/exec` → `subprocess.run(shell=True, timeout=30)` → JSON response
- cwd tracking across commands via `cd` parsing
- Warning banner: commands run with user privileges

### Phase 5 — Streaming Resilience
- `copyfile()`: wrap `shutil.copyfileobj()` in `try/except OSError` — client disconnect mid-stream no longer produces tracebacks
- `do_PUT()`: chunked read (64 KB), cleanup partial file on disconnect

### Phase 6 — Algorithmic Optimizations
| Optimization | Before | After |
|-------------|--------|-------|
| `icon_map` dict | Rebuilt every listing → 31 allocations | Class constant, built once |
| `entry_html()` calls | 3× per entry (1 unused) | 2× per entry |
| Sort key | `lambda a: a.lower()` → new function | `str.lower` → builtin |
| `import time` inside `date_str()` | Local import per call | Module-level import |
| Terminal JSON body | No limit | 65 KB cap |

### Phase 7 — Directory Ops, Search, Sort, Preview
- **MKCOL**: `do_MKCOL()` → `os.mkdir()`, escape guard (403), already-exists (405)
- **MOVE**: `do_MOVE()` → `os.rename()` via `Destination` header, escape guard, non-existent (404)
- **Search**: `<input id="filter">` → `filterList()` hides/shows `.entry-wrap` by `data-name`
- **Sort**: 3 buttons (name/size/date), toggle asc/desc, JS DOM reorder
- **Rename UI**: `&#9998;` button on hover → dialog → `fetch(MOVE)` → reload
- **Image preview**: click `.png/.jpg/.gif/.svg` → lightbox overlay
- **Video preview**: click `.mp4/.webm` → lightbox `<video>` player
- **Video thumbnails**: `<video preload=metadata>` seeking to 40% duration → canvas capture → static `<img>`

## Performance Rules

1. **No inner functions in request handlers** — Use `@staticmethod` for `size_str()`, `date_str()`, `entry_html()` instead of defining them inside `list_directory()`
2. **Class constants for static data** — `icon_map` (31 entries) belongs on the class, not rebuilt per request
3. **Avoid duplicate work** — Don't build data structures (like an `entries_html` list) that you never consume
4. **Use builtin functions** — `str.lower` instead of `lambda a: a.lower()`
5. **Module-level imports** — Don't `import time as _time` inside a per-request function
6. **Cap request bodies** — Terminal JSON: 65 KB limit; file upload: 500 MB limit
7. **socket errors are expected** — Client disconnects during streaming are normal; catch `OSError` in `copyfile()` without logging

## Path Sanitization (Security)

The `translate_path()` method filters out `..` and `.` components. Symlinks are handled by a second layer: resolve both the translated path and the root directory via `os.path.realpath()`, then verify the path starts with the root. This is defense-in-depth.

**Windows**: Always wrap with `os.path.normcase()` — `startswith` is case-sensitive but Windows paths are not.

## Protocol Handling

- HTTP/1.0 with keep-alive via `Connection: keep-alive`
- HTTP/2: not worth it for file serving — protocol overhead outweighs benefits for large transfers
- Range requests: single-range `bytes=start-end` format only (most clients)
- Chunked transfer encoding: rejected for PUT (requires Content-Length)

## Key Lessons

1. **Python stdlib is enough** for a fully-featured file server — no framework needed
2. **`os.path.normcase()` is essential for cross-platform** path comparison (Linux no-op, Windows lowercases)
3. **Client disconnect is normal** during media streaming — catch and ignore socket errors
4. **Browsers can generate video thumbnails** via `<video preload=metadata>` + canvas — no ffmpeg needed
5. **Inline functions in request handlers** are wasteful — Python creates a new function object per request
6. **`translate_path()` already filters path traversal** — the realpath guard is defense-in-depth, not a replacement
