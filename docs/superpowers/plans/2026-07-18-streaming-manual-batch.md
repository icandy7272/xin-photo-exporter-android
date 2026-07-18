# Streaming Manual Batch Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a practical `batch` command that collects current and streaming original-photo URLs while the user manually scrolls MuMu, then downloads every discovered candidate after an explicit confirmation with stable date-aware filenames and two in-memory retry rounds.

**Architecture:** Keep the existing single standard-library CLI and its tested network boundary. Add small pure helpers for record dates and stable paths, one injected `Popen`-style streaming collector, and one sequential batch orchestrator that reuses `download_sample`; preserve the existing no-argument Smoke behavior. URLs remain process-memory-only, while stable URL hashes let later re-collection skip valid completed files.

**Tech Stack:** Python 3 standard library (`argparse`, `datetime`, `hashlib`, `os`, `pathlib`, `subprocess`, `time`, `urllib`), MuMu bundled ADB, macOS `/usr/bin/SetFile`, `unittest`.

---

## File structure

- Modify: `tools/export_originals.py` — keep the single CLI; add date/path helpers, streaming logcat lifecycle, batch download/retry state, and `batch` dispatch.
- Modify: `tests/test_export_originals.py` — add focused offline tests with fake processes, fake openers, injected input/sleep/date setters, and temporary directories.
- Modify: `README.md` — replace Smoke-only status with both Smoke and manual batch usage, limitations, cancellation, output, and date behavior.

Do not add URL manifests, log files, databases, external Python dependencies, automatic scrolling, concurrency, content-duplicate scanning, `.invalid-*`/`.stale-*` backups, or filesystem ownership/symlink hardening.

### Task 1: Stable date-aware destinations and file dates

**Files:**
- Modify: `tools/export_originals.py`
- Modify: `tests/test_export_originals.py`

- [ ] **Step 1: Write failing date and stable-path tests**

Add `BatchPathTests` using synthetic allowed-host URLs. Cover:

```python
def test_extracts_record_date_and_builds_stable_destination(self):
    url = (
        f"https://{export_originals.CDN_HOST}/provider/1/moments/images/"
        "2026-06-04/opaque.jpeg"
    )
    self.assertEqual(export_originals.extract_record_date(url), date(2026, 6, 4))
    first = export_originals.batch_destination(url, Path("/tmp/out"))
    second = export_originals.batch_destination(url, Path("/tmp/out"))
    self.assertEqual(first, second)
    self.assertRegex(first.name, r"^2026-06-04_[0-9a-f]{64}\.jpeg$")

def test_missing_or_invalid_date_uses_unknown_date_without_rejecting_url(self):
    missing = f"https://{export_originals.CDN_HOST}/provider/1/no-date/a.jpeg"
    invalid = (
        f"https://{export_originals.CDN_HOST}/provider/1/moments/images/"
        "2026-02-30/b.jpeg"
    )
    self.assertIsNone(export_originals.extract_record_date(missing))
    self.assertIsNone(export_originals.extract_record_date(invalid))
    self.assertTrue(export_originals.batch_destination(missing, Path("/tmp")).name.startswith("unknown-date_"))
    self.assertTrue(export_originals.batch_destination(invalid, Path("/tmp")).name.startswith("unknown-date_"))
```

Also prove two different URLs produce different paths and the returned name does not contain the remote opaque name.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_export_originals.BatchPathTests -v
```

Expected: FAIL because `extract_record_date` and `batch_destination` do not exist.

- [ ] **Step 3: Implement the minimal pure helpers**

Add:

```python
from datetime import date, datetime, time as datetime_time

RECORD_DATE_RE = re.compile(r"(?:^|/)moments/images/(\d{4}-\d{2}-\d{2})(?:/|$)")
BATCH_OUTPUT = REPOSITORY_ROOT / "build" / "originals"

def extract_record_date(url: str) -> date | None:
    path = urllib.parse.urlsplit(url).path
    match = RECORD_DATE_RE.search(path)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None

def batch_destination(url: str, output_dir: Path) -> Path:
    record_date = extract_record_date(url)
    prefix = record_date.isoformat() if record_date else "unknown-date"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return output_dir / f"{prefix}_{digest}.jpeg"
```

Date parsing never changes `validate_original_url`: a valid allowed-host JPEG without a usable date must still download.

- [ ] **Step 4: Add failing file-date tests**

Use a temporary JPEG and injected command runner. Prove:

- local noon is used for `os.utime`;
- `/usr/bin/SetFile` receives only `-d`, `-m`, the formatted local date, and the local destination path;
- no URL is present in any external command argument;
- missing date is a no-op success;
- `SetFile` missing/nonzero or `os.utime` failure returns `False` without deleting or modifying the JPEG contents.

Expose a testable boundary:

```python
def apply_record_date(
    destination: Path,
    record_date: date | None,
    run_command=run_command,
) -> bool: ...
```

- [ ] **Step 5: Run the date-setter tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_export_originals.BatchDateTests -v
```

Expected: FAIL because `apply_record_date` does not exist.

- [ ] **Step 6: Implement best-effort creation and modification dates**

For a real date:

1. build a timezone-aware local `datetime` at `12:00:00` and call `os.utime` with its timestamp;
2. run `/usr/bin/SetFile -d "MM/DD/YYYY 12:00:00" -m "MM/DD/YYYY 12:00:00" <destination>`;
3. return `True` only when both operations succeed;
4. catch expected `OSError`/command failures and return `False` without printing the local filename or any URL.

For `None`, return `True` without a subprocess.

- [ ] **Step 7: Run all tests and commit**

Run:

```bash
python3 -m unittest discover -s tests -v
git diff --check
```

Expected: all existing 18 tests plus new path/date tests PASS.

Commit:

```bash
git add tools/export_originals.py tests/test_export_originals.py
git commit -m "feat: add stable dated batch destinations"
```

### Task 2: Stream current and new PID logcat with clean cancellation

**Files:**
- Modify: `tools/export_originals.py`
- Modify: `tests/test_export_originals.py`

- [ ] **Step 1: Write failing streaming collector tests**

Create a `FakeLogcatProcess` with iterable stdout, `poll`, `terminate`, `wait`, and `kill`. Add `StreamingCollectorTests` proving:

- the process argv is exactly bundled ADB + serial + `logcat --pid=<pid> -v brief` and contains no photo URL;
- lines already yielded when the process starts and later yielded lines are both parsed;
- duplicate originals do not increase the count; compressed markers and unsafe URLs are ignored;
- progress output contains only counts, never URL/path/hash;
- a simulated `KeyboardInterrupt` terminates and waits for the child, returning first-seen unique candidates;
- a wait timeout causes `kill` followed by a final wait;
- stdout absence, spawn failure, or unexpected process exit raises a redacted `SmokeError("logcat-stream-failed")`.

Use injected boundaries:

```python
def start_logcat_stream(device: Device, pid: int, popen=subprocess.Popen): ...

def collect_streaming_urls(
    device: Device,
    pid: int,
    *,
    popen=subprocess.Popen,
    progress=print,
) -> list[str]: ...
```

- [ ] **Step 2: Run the collector tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_export_originals.StreamingCollectorTests -v
```

Expected: FAIL because the streaming functions do not exist.

- [ ] **Step 3: Implement streaming collection and lifecycle cleanup**

Start text-mode Popen with stdout pipe and stderr discarded/captured without printing. Iterate stdout, call `extract_urls(line)`, and append unseen URLs to an ordered list/set. Print only `已发现唯一原图：<count>` when the count grows.

Treat `KeyboardInterrupt` as the user’s normal end-of-collection signal. In `finally`, terminate a still-running child, `wait(timeout=2)`, then kill and wait if timeout occurs. If iteration ends without a user interrupt, inspect return code and raise the redacted stream failure instead of entering download.

- [ ] **Step 4: Write failing confirmation and batch CLI dispatch tests**

Prove:

- `main(["batch"])` calls `run_batch` and does not call `run_smoke`;
- existing `main([])` and `main(["--execute"])` behavior remains unchanged;
- exact `DOWNLOAD` returns approval; blank, EOF, lowercase, surrounding alternatives, or any other input cancels;
- zero candidates never asks for confirmation and never creates `build/originals`;
- cancellation never calls a downloader or creates the output directory.

- [ ] **Step 5: Run the CLI tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_export_originals.BatchCliTests -v
```

Expected: FAIL because batch dispatch/orchestration does not exist.

- [ ] **Step 6: Implement the collection and confirmation half of `run_batch`**

Extend argparse with an optional `batch` subcommand while retaining the top-level `--execute` Smoke flag. Implement:

```python
def confirm_download(candidate_count: int, input_fn=input) -> bool: ...

def run_batch(
    *,
    run_command=run_command,
    popen=subprocess.Popen,
    input_fn=input,
    downloader=None,
    output_dir=BATCH_OUTPUT,
) -> int: ...
```

`run_batch` discovers device/PID, collects, prints the final count, returns success-with-cancel message for zero/cancel, and calls the injected downloader only after exact approval. Do not create the output directory before approval.

- [ ] **Step 7: Run all tests and commit**

Run:

```bash
python3 -m unittest discover -s tests -v
git diff --check
```

Expected: all tests PASS; Smoke CLI tests prove backward compatibility.

Commit:

```bash
git add tools/export_originals.py tests/test_export_originals.py
git commit -m "feat: collect streaming original candidates"
```

### Task 3: Download every candidate with skip, retry, and interruption

**Files:**
- Modify: `tools/export_originals.py`
- Modify: `tests/test_export_originals.py`

- [ ] **Step 1: Write failing existing-file and stale-part tests**

Add `BatchDownloadTests` proving:

- an existing file larger than 1 KiB with JPEG SOI is counted `existing`, does not call the opener, and still calls the date setter;
- a too-small or non-SOI final file is not skipped;
- a stale deterministic `.part` is deleted before a new attempt;
- when replacement download fails, the previous invalid final file remains byte-for-byte unchanged;
- when replacement succeeds, validated `.part` atomically replaces the invalid final;
- filenames/output never appear in normal progress output, only the fixed output directory may be printed at the end.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_export_originals.BatchDownloadTests -v
```

Expected: FAIL because batch download behavior is missing and stale `.part` blocks `xb`.

- [ ] **Step 3: Implement existing-file and stale-part behavior**

Only after observing RED, modify `download_sample` so it removes its deterministic `.part` before opening it with `xb`. Keep its current cleanup on normal failure. Add a cheap local helper:

```python
def looks_like_existing_jpeg(path: Path) -> bool:
    try:
        return path.stat().st_size > MIN_BYTES and path.open("rb").read(2) == b"\xff\xd8"
    except OSError:
        return False
```

Do not delete or truncate an invalid final destination before the new `.part` has passed every download/JPEG check; the existing `os.replace(part, destination)` remains the only final replacement point.

- [ ] **Step 4: Implement one-candidate outcome and summary types**

Use small redacted data classes/enums that never carry a URL in printable form:

```python
@dataclass(frozen=True)
class CandidateOutcome:
    status: str  # "downloaded", "existing", or "failed"
    date_failed: bool = False

@dataclass(frozen=True)
class BatchSummary:
    total: int
    downloaded: int
    existing: int
    failed: int
    date_failed: int
    unprocessed: int
```

Define a production candidate boundary:

```python
def download_batch_candidate(
    url: str,
    output_dir: Path,
    *,
    opener,
    date_setter=apply_record_date,
) -> CandidateOutcome: ...
```

It may receive the URL internally, but `CandidateOutcome`, exceptions, and terminal strings may not carry or print it. `failed` means this attempt failed and the orchestrator decides whether another retry round remains.

- [ ] **Step 5: Write failing retry-round tests**

Using an injected `candidate_downloader` fake with the same callable contract as `download_batch_candidate`, plus `sleep_fn`, prove:

- all candidates receive a first attempt in first-seen order;
- a first candidate failure does not block later candidates;
- only failed candidates enter retry round 1;
- only still-failed candidates enter retry round 2;
- a success is never retried again;
- there are at most three total attempts per candidate;
- sleep is called immediately before each nonempty retry pass;
- final summary distinguishes downloaded, existing, failed, date_failed, and unprocessed;
- URLs never appear in stdout/stderr or returned error strings.

- [ ] **Step 6: Run retry tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_export_originals.BatchRetryTests -v
```

Expected: FAIL because `download_batch` does not exist.

- [ ] **Step 7: Implement sequential batch download and two retry rounds**

Implement:

```python
def download_batch(
    urls: list[str],
    output_dir: Path,
    *,
    opener=None,
    sleep_fn=time.sleep,
    date_setter=apply_record_date,
    candidate_downloader=None,
) -> BatchSummary: ...
```

When `candidate_downloader is None`, assign `download_batch_candidate` inside the function so tests can inject a fake without Python default-binding surprises. The injected callable is invoked as:

```python
candidate_downloader(
    url,
    output_dir,
    opener=opener,
    date_setter=date_setter,
)
```

Before network access, verify `.gitignore` covers `build/`, set `umask 077`, and create `output_dir`. For each URL:

1. derive stable destination and record date;
2. if valid existing JPEG, count existing and best-effort apply date;
3. otherwise call the existing safe downloader;
4. after successful download, best-effort apply date;
5. on redacted `SmokeError`, retain the URL only in the next in-memory retry queue.

Run at most three passes total. Print ordinal/total and redacted outcome only; do not print destination names. Restore umask in `finally`.

Use a fixed two-second wait immediately before each nonempty retry pass: once before retry pass 1 if the first pass had failures, and once before retry pass 2 if retry pass 1 still had failures. Never sleep after the final pass.

- [ ] **Step 8: Add failing interruption cleanup test**

Make a fake opener/response raise `KeyboardInterrupt` during a chunk read. Prove the current `.part` is removed, completed earlier JPEGs remain, no later candidate is attempted, and the summary/exit path reports the correct unprocessed count without swallowing the interrupt into `download-failed`.

Lock the accounting rule:

- `failed` contains only candidates that reached a terminal exhausted state after all three allowed attempts;
- when interruption stops the batch, `unprocessed` contains every candidate without a terminal downloaded/existing/exhausted result: the interrupted current candidate, not-yet-attempted candidates, and candidates waiting in an earlier failure queue;
- every return path satisfies `total == downloaded + existing + failed + unprocessed`.

- [ ] **Step 9: Implement interruption handling at the cleanup boundary**

Adjust `download_sample` so `.part` cleanup also occurs for `KeyboardInterrupt`, then re-raises it. Catch the interrupt only at `download_batch`/`run_batch`, stop issuing requests, and print the partial redacted summary. Do not retry after user cancellation.

- [ ] **Step 10: Connect `run_batch` to the real downloader and run all tests**

Set the default downloader after function definitions without binding problems in tests. Ensure success exit code requires `failed == 0` and `unprocessed == 0`; otherwise return nonzero. The terminal phrase is “本轮候选下载完成”, never “账号全量完成”.

Run:

```bash
python3 -B -m py_compile tools/export_originals.py
python3 -m unittest discover -s tests -v
git diff --check
```

Expected: all tests PASS, no network used, existing Smoke behavior unchanged.

- [ ] **Step 11: Commit the batch downloader**

```bash
git add tools/export_originals.py tests/test_export_originals.py
git commit -m "feat: download collected originals with retries"
```

### Task 4: Document and run the manual batch checkpoint

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README with exact batch workflow**

Document:

```bash
python3 tools/export_originals.py batch
```

Explain: start MuMu/App first; tool consumes current buffer and streams; manually scroll; `Ctrl-C`; exact `DOWNLOAD`; output under ignored `build/originals/`; date prefix plus creation/mtime; up to two retry rounds; existing valid files skip; URL list is not persisted, so interrupted collection requires re-scrolling; this completes only discovered candidates, not proven account coverage.

- [ ] **Step 2: Run the full offline release checks**

Run:

```bash
python3 -B -m py_compile tools/export_originals.py
python3 -m unittest discover -s tests -v
git check-ignore build/originals/2026-06-04_example.jpeg
git diff --check
git status --short
```

Expected: compile succeeds; all tests pass; check-ignore prints the sample path; only intentional README/source/test/plan changes are present; no image/log/URL artifact is tracked.

- [ ] **Step 3: Commit documentation**

```bash
git add README.md
git commit -m "docs: add manual batch export workflow"
```

- [ ] **Step 4: Run a live collection-only cancellation checkpoint**

With MuMu and the logged-in App open, run interactively:

```bash
python3 tools/export_originals.py batch
```

Confirm current-buffer candidates appear, manually scroll a small amount, press `Ctrl-C`, then press Enter rather than `DOWNLOAD`. Verify no new `build/originals/*.jpeg` was created and no URL was printed.

- [ ] **Step 5: Obtain explicit live batch approval**

Show the user the collection count and cancellation result. Ask whether to rerun and enter `DOWNLOAD`. Do not infer permission from the earlier Smoke download or design approval; this run can issue many GETs.

- [ ] **Step 6: Run the approved small real batch**

After approval, rerun `batch`, scroll only enough to collect a deliberately small user-agreed set, stop, and enter `DOWNLOAD`. Do not attempt a full-account scroll in the first live run.

- [ ] **Step 7: Verify local results without exposing image contents**

Check:

- JPEG count and absence of `.part`;
- `file`/`sips` decode and dimensions;
- filename date prefixes;
- Finder creation and modification dates via `mdls`/`stat`;
- sample files are ignored by Git;
- rerunning collection for the same small set reports existing files and performs no repeated GETs.

- [ ] **Step 8: End at the manual-use checkpoint**

Report redacted discovered/downloaded/existing/failed/date-failed/unprocessed counts and the local output directory. Do not claim account coverage. Do not add UI Automator, URL persistence, videos, person detection, Photos import, or concurrency in this plan.

## Explicitly deferred

- UI Automator scrolling and end-of-list detection;
- proof that every account photo was discovered;
- persistent URL/resume manifests;
- exact post time within a day;
- video export;
- person detection;
- Mac Photos import;
- concurrent downloads and content-duplicate analysis.
