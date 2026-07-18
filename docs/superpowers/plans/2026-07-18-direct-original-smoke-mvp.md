# Android Direct Original Smoke MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework the existing `tools/export_originals.py` prototype into a minimal CLI that discovers the running MuMu Xin Shiguangji process, finds three app-emitted original-image URLs in memory, dry-runs safely, and downloads exactly three JPEG samples when explicitly executed.

**Architecture:** Keep one Python standard-library CLI rather than introducing a package hierarchy. Separate behavior through focused functions inside the script: MuMu/ADB discovery, log parsing, URL validation, no-proxy/no-redirect downloading, minimal JPEG validation, and CLI orchestration. Unit tests use injected subprocess results and fake openers; the final checkpoint runs against the already configured local MuMu instance.

**Tech Stack:** Python 3 standard library (`argparse`, `hashlib`, `json`, `pathlib`, `subprocess`, `urllib`), MuMu bundled `mumutool`/`adb`, `unittest`.

---

## File structure

- Modify and track: `tools/export_originals.py` — single Smoke CLI; reuse the prototype’s useful extraction and `.part` download structure, remove raw-log/full-batch behavior.
- Create: `tests/test_export_originals.py` — all offline parser, dry-run, device/log and downloader tests using synthetic data.
- Modify: `.gitignore` — ignore Python bytecode in addition to existing photo/log artifacts.
- Modify: `README.md` — document the two Smoke commands and sensitive sample cleanup.

Do not create `feed.log`, `urls.txt`, `fails.txt`, a report file, or a module hierarchy for this MVP.

### Task 1: Adopt the prototype and lock URL selection behavior

**Files:**
- Modify: `tools/export_originals.py:1-217`
- Create: `tests/test_export_originals.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add Python generated-file ignores**

Append:

```gitignore
__pycache__/
*.py[cod]
```

Do not delete or stage the existing generated `tools/__pycache__` directory; it becomes ignored.

- [ ] **Step 2: Write failing parser tests**

Create `tests/test_export_originals.py` with `unittest` cases proving:

```python
def test_extracts_only_original_https_cdn_jpegs_in_first_seen_order(self):
    host = export_originals.CDN_HOST
    text = "\n".join([
        f"压缩地址::https://{host}/small.jpeg",
        f"原图地址::https://{host}/a.jpeg",
        f"原图地址::https://{host}/a.jpeg",
        f"原图地址::https://{host}/folder/a%20b.jpeg",
        "原图地址::https://evil.example/a.jpeg",
        f"原图地址::http://{host}/plain.jpeg",
        f"原图地址::https://{host}/not-a-photo.png",
    ])
    self.assertEqual(
        export_originals.extract_urls(text),
        [
            f"https://{host}/a.jpeg",
            f"https://{host}/folder/a%20b.jpeg",
        ],
    )
```

Also test that a nonstandard port, lookalike hostname, query that changes the `.jpeg` path match, and a malformed URL are rejected without printing their values.

- [ ] **Step 3: Run the parser tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_export_originals -v
```

Expected: FAIL because the prototype uses regex-only URL acceptance and exposes batch-only functions.

- [ ] **Step 4: Replace regex-only acceptance with a small standard URL validator**

Retain a marker regex only to isolate the URL token, then validate it with `urllib.parse.urlsplit`:

```python
def validate_original_url(raw: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    if parsed.hostname != CDN_HOST or port not in (None, 443):
        return None
    if parsed.username or parsed.password or parsed.fragment:
        return None
    if not parsed.path.endswith(".jpeg"):
        return None
    return raw
```

`extract_urls` must preserve the raw, normally encoded path and first-seen ordering. Add `select_samples(urls, count=3)` that returns the first three unique candidates and raises a redacted `SmokeError("not-enough-candidates")` when fewer than three exist.

Delete the prototype’s `capture`, `download-from-feed.log`, URL-hash filename, and `fails.txt` behavior. Keep only reusable helpers and the new Smoke orchestration entrypoint.

- [ ] **Step 5: Run parser tests**

Run:

```bash
python3 -m unittest tests.test_export_originals -v
```

Expected: parser and three-candidate selection tests PASS.

- [ ] **Step 6: Commit the parser boundary**

```bash
git add .gitignore tools/export_originals.py tests/test_export_originals.py
git commit -m "feat: select three original image candidates"
```

### Task 2: Discover MuMu and implement a zero-download dry-run

**Files:**
- Modify: `tools/export_originals.py`
- Modify: `tests/test_export_originals.py`

- [ ] **Step 1: Write failing MuMu/ADB discovery tests**

Use injected `run_command(argv)` results to cover:

- MuMu `info all` with exactly one running instance returns its ADB port;
- zero or multiple running instances produce redacted errors;
- the script uses MuMu’s bundled ADB path;
- `adb connect 127.0.0.1:<port>` succeeds;
- `adb -s 127.0.0.1:<port> shell pidof com.childfolio.family` returns one PID;
- `adb ... logcat -d --pid=<pid> -v brief` is used;
- no command argument ever contains a photo URL.

- [ ] **Step 2: Run discovery tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_export_originals.MuMuDiscoveryTests -v
```

Expected: FAIL because discovery functions do not exist.

- [ ] **Step 3: Implement minimal discovery**

Use the known application locations:

```python
MUMUTOOL = Path("/Applications/MuMuPlayer.app/Contents/MacOS/mumutool")
ADB = Path(
    "/Applications/MuMuPlayer.app/Contents/MacOS/"
    "MuMuEmulator.app/Contents/MacOS/tools/adb"
)
PACKAGE = "com.childfolio.family"
```

Implement:

```python
def discover_running_device(run_command=run_command) -> Device: ...
def discover_app_pid(device: Device, run_command=run_command) -> int: ...
def read_current_logcat(device: Device, pid: int, run_command=run_command) -> str: ...
```

Errors expose only codes such as `mumu-not-running`, `ambiguous-device`, `app-not-running`, or `logcat-failed`.

- [ ] **Step 4: Write and implement dry-run orchestration**

`run_smoke(execute=False, ...)` must:

1. discover device and PID;
2. read current PID logcat into memory;
3. extract and select three URLs;
4. print only device presence, PID presence, unique candidate count, planned sample count, and repository-root output parent;
5. never construct or call the downloader and never create JPEG/output directories.

Add a fake downloader that raises if touched, then assert dry-run leaves it untouched.

Add CLI wiring tests around `main(argv)` proving:

- `main([])` calls orchestration with `execute=False`;
- `main(["--execute"])` calls orchestration with `execute=True`;
- unknown/invalid arguments never invoke orchestration or the downloader;
- the no-argument CLI path leaves the fake downloader untouched and creates no output directory.

These tests protect the real checkpoint command `python3 tools/export_originals.py` from accidentally becoming a download command through an argparse default mistake.

- [ ] **Step 5: Run all offline tests**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all parser, discovery, logcat, and dry-run tests PASS.

- [ ] **Step 6: Commit dry-run**

```bash
git add tools/export_originals.py tests/test_export_originals.py
git commit -m "feat: add MuMu original URL dry run"
```

### Task 3: Download and validate three samples in process

**Files:**
- Modify: `tools/export_originals.py`
- Modify: `tests/test_export_originals.py`

- [ ] **Step 1: Write failing opener and downloader tests**

Use fake responses/openers; do not access the real CDN. Cover:

- `build_opener()` includes `ProxyHandler({})` and a redirect handler that rejects every redirect;
- exactly the three selected URLs are passed to the in-process opener;
- output is always under `<repository-root>/build/direct-original-smoke/<timestamp>/` even after changing the caller CWD;
- if `<timestamp>` exists, a new run uses `<timestamp>-1`, then `<timestamp>-2`, without modifying any existing file;
- missing `.gitignore` coverage for `build/` stops before downloading;
- HTTP non-200, redirect, network exception, wrong Content-Type, file smaller than 1 KiB, over 50 MiB, or missing `FF D8` leaves neither a completed JPEG nor `.part`;
- a valid synthetic JPEG becomes `sample-01.jpeg` through `.part` then `os.replace`;
- URL substrings do not appear in stdout, stderr, filenames, or returned error text;
- SHA-256 duplicates are reported without choosing a fourth URL.

- [ ] **Step 2: Run downloader tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_export_originals.DownloadTests -v
```

Expected: FAIL because the safe in-process downloader is not implemented.

- [ ] **Step 3: Implement the no-proxy/no-redirect opener**

Use:

```python
class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def build_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        RejectRedirects(),
    )
```

Do not use `urllib.request.urlopen`, `curl`, `requests`, environment proxies, or external download subprocesses.

- [ ] **Step 4: Implement bounded JPEG streaming**

`download_sample(opener, url, destination, timeout=120)` must:

- request in process;
- require status 200 and `Content-Type` main type `image/jpeg`;
- stream 64 KiB chunks to `destination + ".part"`;
- abort and delete `.part` when bytes exceed 50 MiB;
- require more than 1 KiB and first bytes `FF D8`;
- compute SHA-256 during streaming;
- use one cleanup boundary that deletes `.part` on every normal failure, including network exceptions, HTTP/type failures, short content, over-limit content, invalid SOI, and validation exceptions;
- rename to the completed destination only after validation;
- return a small result containing ordinal, byte count, SHA-256, and redacted status—never the URL.

- [ ] **Step 5: Implement execute orchestration**

Before three GETs:

- print the account-ownership and sensitive EXIF warning;
- derive the repository root from `Path(__file__).resolve()`;
- verify the repository `.gitignore` contains a rule covering `build/`;
- set `umask 077` and create one timestamped output directory;
- create the run directory with `mkdir(exist_ok=False)`; if the base timestamp exists, try `-1`, `-2`, and so on until a new directory is created;
- create sample files only inside that newly created directory and never replace files from an existing run;
- assign `sample-01.jpeg` through `sample-03.jpeg`.

Download sequentially. Preserve successful samples when another sample fails. Print only success/failure counts, byte counts, hashes, and the output directory.

- [ ] **Step 6: Run all tests**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests PASS and no network is used.

- [ ] **Step 7: Commit the downloader**

```bash
git add tools/export_originals.py tests/test_export_originals.py
git commit -m "feat: download three original JPEG samples"
```

### Task 4: Document and run the real three-sample checkpoint

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document exact commands**

Add:

```bash
python3 tools/export_originals.py
python3 tools/export_originals.py --execute
```

Document that the first command is dry-run, the second downloads exactly three samples, and successful samples should be manually deleted after inspection because pixels and EXIF may be sensitive.

- [ ] **Step 2: Run syntax, unit, and repository checks**

Run:

```bash
python3 -B -m py_compile tools/export_originals.py
python3 -m unittest discover -s tests -v
git check-ignore build/direct-original-smoke/example/sample-01.jpeg
git status --short
```

Expected:

- compile succeeds;
- all tests PASS;
- `git check-ignore` prints the sample path;
- only intentional source/doc changes are present; generated bytecode and build artifacts are ignored.

- [ ] **Step 3: Run live dry-run**

Run:

```bash
python3 tools/export_originals.py
```

Expected: identifies the running MuMu/App, reports at least three unique original candidates, plans three samples, creates no JPEG, and prints no URL.

- [ ] **Step 4: Obtain explicit live execution approval at the checkpoint**

Show the dry-run result to the user. Do not infer permission from the dry-run. Confirm that the user wants the three complete GETs now.

- [ ] **Step 5: Run the live three-sample download**

After approval:

```bash
python3 tools/export_originals.py --execute
```

Expected: at most three downloads, with up to three valid JPEGs under the timestamped ignored output directory; no URL is printed or stored by the tool.

- [ ] **Step 6: User validates the samples**

Ask the user to open the three files in macOS and report:

- whether they belong to the current account;
- whether they open normally;
- their displayed dimensions;
- whether quality matches the expected original.

- [ ] **Step 7: End at the manual validation checkpoint**

Report only the redacted success/failure counts and the local sample directory. Do not modify README, create a result commit, or push anything as part of the Smoke validation. Publishing a validation result is a separately requested follow-up.

## Explicitly deferred

- Streaming logcat while the user manually scrolls;
- persistent resume state or full-batch retry;
- UI Automator scrolling;
- Photos import, video handling, ordering, and person detection.

The next plan begins only after the user confirms the three downloaded samples are acceptable.
