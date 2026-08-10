#!/usr/bin/env python3
"""Exercise the Indicator class with stubbed GTK/AppIndicator modules.

Offline by default: the usage endpoint is replaced by a fixture recorded from a
real response, so the suite never burns rate-limit budget. Pass --live to hit
the real endpoint once (needs a valid Claude Code login).

Run: python3 tests/test_stubbed_indicator.py [--live]
"""
import sys, types, json, pathlib, tempfile, time

TMP = pathlib.Path(tempfile.mkdtemp(prefix="claude-usage-test-"))
LIVE = "--live" in sys.argv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import claude_usage_monitor as m

m.CACHE_PATH = TMP / "state.json"

# Recorded from GET /api/oauth/usage, trimmed to the fields the app reads.
FIXTURE = {
    "five_hour": {"utilization": 3.0, "resets_at": "2026-08-10T18:19:59.794686+00:00"},
    "seven_day": {"utilization": 37.0, "resets_at": "2026-08-13T15:59:59.794711+00:00"},
    "seven_day_opus": None,
    "extra_usage": {"is_enabled": False},
    "limits": [
        {"kind": "session", "group": "session", "percent": 3, "severity": "normal",
         "resets_at": "2026-08-10T18:19:59.794686+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 37, "severity": "normal",
         "resets_at": "2026-08-13T15:59:59.794711+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 37, "severity": "normal",
         "resets_at": "2026-08-13T15:59:59.794982+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": False},
    ],
}
FIXTURE_CREDENTIALS = {
    "token": "test-token",
    "expires_at": None,
    "subscription": "team",
    "tier": "default_claude_max_5x",
}

REAL_FETCH, REAL_LOAD = m.fetch_usage, m.load_credentials
if not LIVE:
    m.fetch_usage = lambda token, timeout: json.loads(json.dumps(FIXTURE))
    m.load_credentials = lambda: dict(FIXTURE_CREDENTIALS)

calls = {"icons": [], "labels": [], "notifications": [], "timers": []}


class Item:
    def __init__(self, label=""): self.label, self.handler, self.sensitive = label, None, False
    def set_sensitive(self, v): self.sensitive = v
    def set_use_underline(self, v): self.underline = v
    def connect(self, sig, h): self.handler = h
    def show(self): pass


class Menu:
    def __init__(self): self.children = []
    def get_children(self): return list(self.children)
    def remove(self, c): self.children.remove(c)
    def append(self, c): self.children.append(c)


class Ind:
    def set_status(self, *a): pass
    def set_title(self, *a): pass
    def set_label(self, l, g): calls["labels"].append(l)
    def set_menu(self, menu): self.menu = menu
    def set_icon_full(self, icon, desc): calls["icons"].append(icon)


Gtk = types.SimpleNamespace(
    Menu=Menu,
    MenuItem=types.SimpleNamespace(new_with_label=Item),
    SeparatorMenuItem=lambda: Item("---"),
    main_quit=lambda: calls.setdefault("quit", True),
)
GLib = types.SimpleNamespace(
    idle_add=lambda fn, *a: fn(*a),
    timeout_add_seconds=lambda sec, fn, *a: calls["timers"].append(sec) or len(calls["timers"]),
    source_remove=lambda i: None,
    PRIORITY_DEFAULT=0,
    unix_signal_add=lambda *a: None,
)
Gio = types.SimpleNamespace(
    File=types.SimpleNamespace(new_for_path=lambda p: types.SimpleNamespace(
        monitor_file=lambda *a: types.SimpleNamespace(connect=lambda *b: None))),
    FileMonitorFlags=types.SimpleNamespace(NONE=0),
    AppInfo=types.SimpleNamespace(launch_default_for_uri=lambda *a: calls.setdefault("uri", a[0])),
)


class Note:
    def __init__(self, title, body, icon): calls["notifications"].append((title, body))
    def set_urgency(self, u): pass
    def show(self): pass


Notify = types.SimpleNamespace(
    init=lambda name: None,
    Notification=types.SimpleNamespace(new=Note),
    Urgency=types.SimpleNamespace(CRITICAL=2, NORMAL=1),
)
AppIndicator = types.SimpleNamespace(
    Indicator=types.SimpleNamespace(new=lambda *a: Ind()),
    IndicatorCategory=types.SimpleNamespace(SYSTEM_SERVICES=0),
    IndicatorStatus=types.SimpleNamespace(ACTIVE=1),
)
MODULES = {"Gtk": Gtk, "GLib": GLib, "Gio": Gio, "Notify": Notify, "AppIndicator": AppIndicator}

failures = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


config = m.load_config()


def settle(ind, timeout=5.0):
    """The constructor kicks off a fetch in a thread; wait for it to land."""
    end = time.monotonic() + timeout
    while ind.fetching and time.monotonic() < end:
        time.sleep(0.01)
    time.sleep(0.05)
    return ind


def fresh_indicator():
    ind = settle(m.Indicator(config, MODULES))
    ind.last_fetch = 0.0  # the constructor's own fetch would otherwise throttle us
    return ind


print(f"[1] fetch through the indicator ({'live' if LIVE else 'fixture'})")
ind = fresh_indicator()
ind._fetch_worker()
check("state populated", ind.state is not None)
check("no error", ind.error is None, str(ind.error))
if ind.state:
    check("session percent present", ind.state["session_percent"] is not None)
    check("weekly percent present", ind.state["weekly_percent"] is not None)
    check("normal icon used", calls["icons"] and calls["icons"][-1] == m.ICONS["normal"], str(calls["icons"]))
    check("label has two percents", calls["labels"][-1].count("%") == 2, calls["labels"][-1])
    labels = [c.label for c in ind.indicator.menu.children]
    check("menu has refresh/open/quit",
          all(any(x in l for l in labels) for x in ("Rafraîchir", "Ouvrir", "Quitter")), str(labels))
    check("menu lists limits", any("Hebdo" in l for l in labels), str(labels))
    check("timer rescheduled at interval", calls["timers"][-1] == config["interval_seconds"], str(calls["timers"]))
    check("cache written", m.CACHE_PATH.exists())

print("[2] threshold notifications (synthetic 96%)")
calls["notifications"].clear()
ind.notified = {}
hot = json.loads(json.dumps(ind.state))
hot["limits"][0]["percent"] = 96.0
ind.check_thresholds(hot)
check("one notification for crossing 80+95", len(calls["notifications"]) == 1, str(calls["notifications"]))
check("notification names 95", "95" in calls["notifications"][0][0], str(calls["notifications"]))
calls["notifications"].clear()
ind.check_thresholds(hot)
check("no duplicate on second pass", len(calls["notifications"]) == 0, str(calls["notifications"]))
hot["limits"][0]["resets_at"] = "2099-01-01T00:00:00+00:00"
ind.check_thresholds(hot)
check("re-arms after window reset", len(calls["notifications"]) == 1, str(calls["notifications"]))

print("[2b] 429 throttling")
ind.failures = 0
ind.throttled_until = 0.0
ind.on_result(None, m.UsageError("HTTP 429", "rate-limit", retry_after=300))
check("throttled_until armed", ind.throttled_until > time.monotonic() + 200, str(ind.throttled_until))
check("retry-after beats backoff", calls["timers"][-1] >= 300, str(calls["timers"][-1]))
before = len(calls["timers"])
ind.last_fetch = 0.0
ind.start_fetch()
settle(ind)
check("fetch blocked while throttled", not ind.fetching and len(calls["timers"]) == before + 1)
check("throttle persisted to cache",
      json.loads(m.CACHE_PATH.read_text()).get("throttle_until_wall", 0) > time.time() + 200,
      str(json.loads(m.CACHE_PATH.read_text()).get("throttle_until_wall")))
resumed = m.Indicator(config, MODULES)
check("throttle survives restart", resumed.throttled_until > time.monotonic() + 200,
      str(resumed.throttled_until - time.monotonic()))
settle(resumed)
check("rate-limit icon is stale", calls["icons"][-1] == m.ICONS["stale"], str(calls["icons"][-1]))
ind.throttled_until = 0.0
ind.error = None

print("[2c] min interval between fetches")
ind.last_fetch = time.monotonic()
before = len(calls["timers"])
ind.start_fetch()
settle(ind)
check("burst click deferred", not ind.fetching and len(calls["timers"]) == before + 1)

print("[3] severity -> icon")
calls["icons"].clear()
ind.state["max_severity"] = "critical"
ind.refresh_ui()
check("critical icon", calls["icons"][-1] == m.ICONS["critical"], str(calls["icons"]))

print("[4] missing credentials -> degraded, no crash")
m.load_credentials = REAL_LOAD
saved = m.CREDENTIALS_PATH
m.CREDENTIALS_PATH = pathlib.Path("/nonexistent/.credentials.json")
calls["icons"].clear()
ind2 = fresh_indicator()
ind2.state = None
ind2._fetch_worker()
check("error captured", ind2.error is not None and ind2.error.kind == "no-credentials", str(ind2.error))
check("stale icon", calls["icons"][-1] == m.ICONS["stale"], str(calls["icons"]))
check("error shown in menu", any("⚠" in c.label for c in ind2.indicator.menu.children))
check("backoff applied", calls["timers"][-1] > config["interval_seconds"], str(calls["timers"][-3:]))

print("[5] expired token")
creds = json.loads(saved.read_text())
creds["claudeAiOauth"]["expiresAt"] = 1000
p = TMP / "expired.json"
p.write_text(json.dumps(creds))
m.CREDENTIALS_PATH = p
try:
    m.load_credentials()
    check("raises on expired", False)
except m.UsageError as e:
    check("raises on expired", e.kind == "expired", e.kind)
m.CREDENTIALS_PATH = saved

print("[6] raw-field fallback (no limits[] key)")
raw = {"five_hour": {"utilization": 12, "resets_at": "2026-08-10T18:20:00+00:00"},
       "seven_day": {"utilization": 44, "resets_at": "2026-08-13T16:00:00+00:00"},
       "seven_day_opus": {"utilization": 3, "resets_at": None}}
st = m.normalize(raw, {"subscription": "team", "tier": "x"})
check("3 limits rebuilt", len(st["limits"]) == 3, str(len(st["limits"])))
check("session=12", st["session_percent"] == 12)
check("weekly=44", st["weekly_percent"] == 44)
check("renders without reset ts", "pas de reset" in m.render_text(st, config))

print("[7] parsing and formatting")
check("retry-after seconds", m.parse_retry_after("120") == 120.0)
check("retry-after garbage -> None", m.parse_retry_after("soon") is None)
check("retry-after empty -> None", m.parse_retry_after(None) is None)
check("retry-after http-date", (m.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") or 0) > 0)
check("interval floored", config["interval_seconds"] >= m.MIN_INTERVAL_SECONDS)
check("percent rounding", m.format_percent(3.0) == "3" and m.format_percent(3.25) == "3.2", m.format_percent(3.25))
check("none percent", m.format_percent(None) == "?")
check("bar width", len(m.bar(50)) == 20 and m.bar(50).count("█") == 10)
check("empty payload survives", m.normalize({}, {})["session_percent"] is None)
check("tier humanized", m.humanize_tier("default_claude_max_5x") == "Max 5x", str(m.humanize_tier("default_claude_max_5x")))
check("tier none-safe", m.humanize_tier(None) is None)
check("header has no raw underscore",
      "_" not in m.header_text({"subscription": "team", "tier": "default_claude_max_5x"}))
check("underline disabled on items",
      all(getattr(c, "underline", None) is False for c in ind.indicator.menu.children if c.label != "---"))
check("quit saves cache", (ind.on_quit_clicked() is None) and calls.get("quit") is True)
check("open uri", (ind.on_open_clicked() is None) and calls.get("uri") == m.USAGE_PAGE)

print()
print(f"FAILURES: {failures}" if failures else "ALL PASS")
sys.exit(1 if failures else 0)
