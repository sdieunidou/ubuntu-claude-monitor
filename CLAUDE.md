# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

GNOME/Ubuntu tray indicator showing Claude usage-limit percentages, polled from
`GET https://api.anthropic.com/api/oauth/usage` (the undocumented endpoint behind
`claude /usage`). Single Python file, stdlib only (`urllib`, no `requests`), plus
PyGObject for the tray. No dependency on `ccusage`/`claude-monitor` and no log parsing.

## Commands

```bash
python3 tests/test_stubbed_indicator.py            # full suite (offline, uses a recorded fixture)
python3 tests/test_stubbed_indicator.py --live     # same, but hits the real endpoint once

./claude_usage_monitor.py --once                   # text render, then exit
./claude_usage_monitor.py --json                   # normalized state as JSON
./claude_usage_monitor.py --raw                    # untouched API response
./claude_usage_monitor.py --watch 60               # terminal loop
./claude_usage_monitor.py                          # tray indicator (default mode)

./install.sh                                       # apt deps + smoke test + systemd user unit
systemctl --user restart claude-usage-monitor      # the unit runs this repo's file in place,
                                                   # so a restart picks up your edits
journalctl --user -u claude-usage-monitor -f       # one line per poll cycle
```

No linter, formatter, or build step is configured. The test suite is a flat script,
not pytest: there is no way to run a single case, but each block prints a `[n]` header
so failures are easy to locate. `--once` is the quickest end-to-end check of a change to
fetching or normalisation.

## Architecture

`claude_usage_monitor.py` is the whole program, split by `# ---- section` banners:
config → credentials → http → normalisation → rendering → cache → CLI modes → indicator → main.

**The `state` dict is the contract.** `normalize()` turns the API payload into one dict
(`session_percent`, `weekly_percent`, `limits[]`, `max_severity`, `subscription`, `tier`,
`fetched_at`). Everything downstream consumes only that: `render_text()` for the CLI modes,
`Indicator.refresh_ui()`/`rebuild_menu()` for the tray, and `save_cache()` for persistence.
Adding a field means touching `normalize()` plus whichever consumers should show it — never
reach back into the raw payload from a consumer.

`normalize()` reads `payload["limits"]` when present and otherwise rebuilds equivalent
entries from the flat legacy keys in `FALLBACK_FIELDS` (`five_hour`, `seven_day`, …), so the
app survives the endpoint dropping `limits[]`. Keep both paths working.

**Threading.** `start_fetch()` spawns a daemon thread running `_fetch_worker()`, which hands
results back with `GLib.idle_add(self.on_result, ...)`. Nothing GTK/AppIndicator may be
touched off the main loop.

**Errors** all surface as `UsageError(message, kind, retry_after)`; `kind` drives the icon,
the menu warning line, and the backoff. Failures never blank the display — the last good
state stays visible with a warning line under it.

## Invariants that are easy to break

**Never write `~/.claude/.credentials.json`, and never use the refresh token.** Refresh
tokens rotate server-side; consuming one here invalidates Claude Code's own copy and logs the
user out of Claude Code. On expiry the indicator degrades (offline icon + "lance `claude`")
and waits for Claude Code to refresh. The file is read on every poll and watched via
`Gio.File.monitor_file`, with a 2 s debounce because Claude Code rewrites it in bursts.

**Anti-429 has three layers**, all needed: a 60 s floor on `interval_seconds`
(`MIN_INTERVAL_SECONDS`), `min_fetch_interval` between any two requests regardless of origin
(menu clicks, credential-file churn), and honouring `Retry-After` when it exceeds the computed
backoff. An active pause is persisted as wall clock (`throttle_until_wall`) so a restart does
not reset it to zero.

**Cache and notification state are per account.** `account_fingerprint()` hashes UUIDs from
`~/.claude.json` — tokens rotate, so they cannot serve as identity. A cache written under a
different login is ignored, and `check_account_switch()` clears state, sent notifications, and
the 429 pause when the fingerprint changes mid-run.

**Notification dedup is keyed on `kind:scope:resets_at`**, so a window reset re-arms the
threshold instead of staying silent forever.

**The panel label needs a blank before a re-push.** GNOME's appindicator extension only
redraws the label on `XAyatanaNewLabel`, libayatana only emits that when the text *changes*,
and the extension drops a property refresh whose value equals its cache. So when the extension
or the shell restarts, the icon re-registers with no label. `watch_panel()` watches
`org.kde.StatusNotifierWatcher`; on reappearance `blank_label()` sends `""` and, one timeout
later, `restore_label()` sends the real text. Removing either step silently reintroduces the
bare-icon bug.

## Testing conventions

`Indicator.__init__` takes a `gi_modules` dict (`Gtk`, `GLib`, `Gio`, `Notify`,
`AppIndicator`) built by `import_gi()` in production and by hand-rolled
`types.SimpleNamespace` stubs in `tests/test_stubbed_indicator.py`. **Any new gi API the
indicator calls must be added to those stubs**, or the suite fails at import time. The stubs
record into a `calls` dict (`icons`, `labels`, `guides`, `timers`, `watched`, …) that the
assertions read. `GLib.timeout_add`/`timeout_add_seconds` are stubbed to record and *not*
fire, so multi-step timer sequences are driven by calling the callbacks directly.

The suite runs headless but reads the real `~/.claude` for the degraded-path and
expired-token cases, so it needs a Claude Code login on the machine.

## Language

UI strings, log lines, README, and `install.sh` output are French. Code comments, commit
messages, and identifiers are English.
