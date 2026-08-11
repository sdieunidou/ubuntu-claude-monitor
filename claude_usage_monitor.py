#!/usr/bin/env python3
"""Tray indicator showing Claude usage limits on Ubuntu.

Reads the Claude Code OAuth access token from ~/.claude/.credentials.json and
polls https://api.anthropic.com/api/oauth/usage -- the same endpoint the
`/usage` slash command renders.

This program never refreshes the token and never writes the credentials file.
Refresh tokens rotate server-side, so consuming one here would invalidate the
copy Claude Code holds and log it out. When the access token expires we simply
degrade to a stale state and wait for Claude Code to refresh it (the file is
watched, so a refresh is picked up within a second).

Standalone: stdlib only, plus PyGObject for the indicator (CLI modes need no
GUI libraries at all).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_ID = "ubuntu-claude-monitor"
APP_NAME = "Claude Usage"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_PAGE = "https://claude.ai/settings/usage"
CLAUDE_CLI_VERSION = "2.1.226"

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
SETTINGS_PATH = Path.home() / ".claude.json"
CONFIG_PATH = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / APP_ID / "config.toml"
CACHE_PATH = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
) / APP_ID / "state.json"

DEFAULTS = {
    "interval_seconds": 120,
    "thresholds": [80, 95],
    "label_format": "{session}% · {weekly}%",
    "show_scoped": True,
    "show_inactive": True,
    "notifications": True,
    "http_timeout": 15,
    "max_backoff_seconds": 900,
    "min_fetch_interval": 20,
}

# The endpoint rate-limits well before this, and the numbers move slowly.
MIN_INTERVAL_SECONDS = 60

# Widest label we ever draw; the panel reserves this much so digits stop jumping.
LABEL_GUIDE = "100% · 100%"
SNI_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
# Milliseconds the blanked label must stay visible for the panel to notice it.
LABEL_REPUSH_GAP_MS = 400

ICONS = {
    "normal": "utilities-system-monitor-symbolic",
    "warning": "dialog-warning-symbolic",
    "critical": "dialog-error-symbolic",
    "stale": "network-offline-symbolic",
}

SEVERITY_RANK = {"normal": 0, "warning": 1, "critical": 2, "exceeded": 3}

KIND_LABELS = {
    "session": "Session",
    "weekly_all": "Hebdo · tous modèles",
    "weekly_scoped": "Hebdo",
    "weekly_cowork": "Hebdo · Cowork",
    "weekly_oauth_apps": "Hebdo · apps OAuth",
}

# Raw-field fallback, used only if the API stops returning `limits`.
FALLBACK_FIELDS = [
    ("five_hour", "session", "session", None),
    ("seven_day", "weekly_all", "weekly", None),
    ("seven_day_opus", "weekly_scoped", "weekly", "Opus"),
    ("seven_day_sonnet", "weekly_scoped", "weekly", "Sonnet"),
    ("seven_day_cowork", "weekly_cowork", "weekly", "Cowork"),
    ("seven_day_oauth_apps", "weekly_oauth_apps", "weekly", "apps OAuth"),
]


class UsageError(Exception):
    """Fetch failed. `kind` is one of: no-credentials, expired, auth, http, network, rate-limit."""

    def __init__(self, message: str, kind: str = "http", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


# --------------------------------------------------------------------------- config


def load_config() -> dict:
    config = dict(DEFAULTS)
    if not CONFIG_PATH.exists():
        return config
    try:
        import tomllib

        with CONFIG_PATH.open("rb") as handle:
            user = tomllib.load(handle)
    except Exception as exc:  # noqa: BLE001 - a bad config must not kill the daemon
        print(f"config illisible ({exc}), valeurs par défaut", file=sys.stderr)
        return config
    for key, value in user.items():
        if key in config:
            config[key] = value
        else:
            print(f"clé de config inconnue ignorée: {key}", file=sys.stderr)
    try:
        interval = int(config["interval_seconds"])
    except (TypeError, ValueError):
        interval = DEFAULTS["interval_seconds"]
    if interval < MIN_INTERVAL_SECONDS:
        print(
            f"interval_seconds={interval} trop court, plancher à {MIN_INTERVAL_SECONDS} s",
            file=sys.stderr,
        )
        interval = MIN_INTERVAL_SECONDS
    config["interval_seconds"] = interval
    return config


# ---------------------------------------------------------------------- credentials


def load_credentials() -> dict:
    """Return {token, expires_at, subscription, tier}. Raises UsageError."""
    try:
        raw = json.loads(CREDENTIALS_PATH.read_text())
    except FileNotFoundError:
        raise UsageError(
            f"{CREDENTIALS_PATH} absent — connecte-toi avec `claude`", "no-credentials"
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"credentials illisibles: {exc}", "no-credentials") from None

    oauth = raw.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise UsageError("pas de accessToken dans les credentials", "no-credentials")

    expires_at = oauth.get("expiresAt")
    expires_dt = None
    if isinstance(expires_at, (int, float)):
        expires_dt = datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)
        if expires_dt <= datetime.now(timezone.utc):
            raise UsageError(
                "access token expiré — lance `claude` pour le rafraîchir", "expired"
            )

    return {
        "token": token,
        "expires_at": expires_dt,
        "subscription": oauth.get("subscriptionType"),
        "tier": oauth.get("rateLimitTier"),
    }


def account_fingerprint() -> str | None:
    """Stable per-account id, so a cache from another login is never reused.

    Tokens rotate on every refresh, so they cannot identify an account; the UUIDs
    Claude Code keeps in ~/.claude.json can. Hashed to avoid copying identity data
    into a second file. Returns None when the account cannot be determined.
    """
    try:
        account = json.loads(SETTINGS_PATH.read_text()).get("oauthAccount") or {}
    except (OSError, json.JSONDecodeError):
        return None
    parts = [account.get("accountUuid"), account.get("organizationUuid")]
    if not any(parts):
        return None
    import hashlib

    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- http


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date (RFC 9110)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


def fetch_usage(token: str, timeout: int) -> dict:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-cli/{CLAUDE_CLI_VERSION} (external, cli)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        retry_after = None
        if exc.code == 429:
            kind = "rate-limit"
            retry_after = parse_retry_after(
                exc.headers.get("Retry-After") if exc.headers else None
            )
            message = "HTTP 429 (trop de requêtes)"
            if retry_after:
                message += f", réessai dans {int(retry_after)} s"
        elif exc.code in (401, 403):
            kind = "auth"
            message = f"HTTP {exc.code} — token refusé, lance `claude`"
        else:
            kind = "http"
            message = f"HTTP {exc.code}"
        raise UsageError(message, kind, retry_after) from None
    except urllib.error.URLError as exc:
        raise UsageError(f"réseau: {exc.reason}", "network") from None
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise UsageError(f"réponse invalide: {exc}", "network") from None


# ------------------------------------------------------------------ normalisation


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _severity(raw, percent) -> str:
    if isinstance(raw, str) and raw:
        return raw
    if percent is None:
        return "normal"
    if percent >= 95:
        return "critical"
    if percent >= 80:
        return "warning"
    return "normal"


def _limit_label(kind: str, scope_model: str | None) -> str:
    base = KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())
    if scope_model and scope_model not in base:
        return f"{base} · {scope_model}"
    return base


def _limit_key(entry: dict) -> str:
    return f"{entry['kind']}:{entry['scope_model'] or '-'}:{entry['resets_at'] or '-'}"


def normalize(payload: dict, credentials: dict) -> dict:
    limits: list[dict] = []

    for item in payload.get("limits") or []:
        if not isinstance(item, dict):
            continue
        percent = item.get("percent")
        scope = item.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name") if scope else None
        kind = str(item.get("kind") or "inconnu")
        limits.append(
            {
                "kind": kind,
                "group": item.get("group") or kind,
                "percent": float(percent) if isinstance(percent, (int, float)) else None,
                "severity": _severity(item.get("severity"), percent),
                "resets_at": item.get("resets_at"),
                "scope_model": model,
                "is_active": bool(item.get("is_active")),
                "label": _limit_label(kind, model),
            }
        )

    if not limits:  # API shape changed: rebuild from the raw buckets.
        for field, kind, group, model in FALLBACK_FIELDS:
            bucket = payload.get(field)
            if not isinstance(bucket, dict):
                continue
            percent = bucket.get("utilization")
            if not isinstance(percent, (int, float)):
                continue
            limits.append(
                {
                    "kind": kind,
                    "group": group,
                    "percent": float(percent),
                    "severity": _severity(None, percent),
                    "resets_at": bucket.get("resets_at"),
                    "scope_model": model,
                    "is_active": False,
                    "label": _limit_label(kind, model),
                }
            )

    def percent_of(predicate) -> float | None:
        values = [
            limit["percent"]
            for limit in limits
            if limit["percent"] is not None and predicate(limit)
        ]
        return max(values) if values else None

    session = percent_of(lambda item: item["group"] == "session")
    weekly = percent_of(lambda item: item["kind"] == "weekly_all")
    if weekly is None:
        weekly = percent_of(lambda item: item["group"] == "weekly")

    max_severity = "normal"
    for limit in limits:
        if SEVERITY_RANK.get(limit["severity"], 1) > SEVERITY_RANK.get(max_severity, 0):
            max_severity = limit["severity"]

    extra = payload.get("extra_usage")
    extra_usage = None
    if isinstance(extra, dict) and extra.get("is_enabled"):
        extra_usage = {
            "utilization": extra.get("utilization"),
            "used_credits": extra.get("used_credits"),
            "monthly_limit": extra.get("monthly_limit"),
            "currency": extra.get("currency"),
        }

    return {
        "limits": limits,
        "session_percent": session,
        "weekly_percent": weekly,
        "max_severity": max_severity,
        "extra_usage": extra_usage,
        "subscription": credentials.get("subscription"),
        "tier": credentials.get("tier"),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------------ rendering


def human_delta(target: datetime | None) -> str:
    if target is None:
        return ""
    seconds = int((target - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "maintenant"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days} j {hours} h"
    if hours:
        return f"{hours} h {minutes:02d}"
    return f"{minutes} min"


def local_reset(target: datetime | None) -> str:
    if target is None:
        return ""
    local = target.astimezone()
    if local.date() == datetime.now().date():
        return local.strftime("%H:%M")
    return local.strftime("%d/%m %H:%M")


def reset_text(iso: str | None) -> str:
    target = _parse_ts(iso)
    if target is None:
        return "pas de reset"
    delta = human_delta(target)
    return f"reset {local_reset(target)} (dans {delta})" if delta else f"reset {local_reset(target)}"


def bar(percent: float | None, width: int = 20) -> str:
    if percent is None:
        return "?" * width
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def format_percent(percent: float | None) -> str:
    if percent is None:
        return "?"
    return f"{percent:.0f}" if abs(percent - round(percent)) < 0.05 else f"{percent:.1f}"


def humanize_tier(tier: str | None) -> str | None:
    """`default_claude_max_5x` -> `Max 5x`."""
    if not isinstance(tier, str) or not tier:
        return None
    cleaned = tier.removeprefix("default_").removeprefix("claude_").replace("_", " ").strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else None


def header_text(state: dict) -> str:
    header = "Claude"
    if state.get("subscription"):
        header += f" · {state['subscription']}"
    tier = humanize_tier(state.get("tier"))
    if tier:
        header += f" ({tier})"
    return header


def render_text(state: dict, config: dict) -> str:
    lines = [header_text(state), ""]

    width = max(
        (len(limit["label"]) for limit in state["limits"] if _visible(limit, config)),
        default=0,
    )
    for limit in state["limits"]:
        if not _visible(limit, config):
            continue
        marker = "▸" if limit["is_active"] else " "
        lines.append(
            f"{marker} {limit['label']:<{width}}  {bar(limit['percent'])} "
            f"{format_percent(limit['percent']):>4}%  {reset_text(limit['resets_at'])}"
        )

    extra = state.get("extra_usage")
    if extra:
        used = extra.get("used_credits")
        limit_value = extra.get("monthly_limit")
        currency = extra.get("currency") or ""
        detail = f"{used}/{limit_value} {currency}".strip() if used is not None else ""
        lines.append(f"  Crédits extra    {format_percent(extra.get('utilization'))}%  {detail}")

    lines.append("")
    lines.append(f"maj {datetime.now().strftime('%H:%M:%S')}")
    return "\n".join(lines)


def _visible(limit: dict, config: dict) -> bool:
    if limit["kind"] == "weekly_scoped" and not config["show_scoped"]:
        return False
    if not limit["is_active"] and not config["show_inactive"]:
        return limit["kind"] in ("session", "weekly_all")
    return True


# ---------------------------------------------------------------------- cache/state


def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(data: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name: two writers must not race on the same rename source.
        tmp = CACHE_PATH.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data))
        tmp.replace(CACHE_PATH)
    except OSError as exc:
        print(f"cache non écrit: {exc}", file=sys.stderr)


# ------------------------------------------------------------------------ CLI modes


def run_once(config: dict, as_json: bool) -> int:
    try:
        credentials = load_credentials()
        state = normalize(fetch_usage(credentials["token"], config["http_timeout"]), credentials)
    except UsageError as exc:
        if as_json:
            print(json.dumps({"error": str(exc), "kind": exc.kind}))
        else:
            print(f"erreur ({exc.kind}): {exc}", file=sys.stderr)
        return 1
    print(json.dumps(state, indent=2, ensure_ascii=False) if as_json else render_text(state, config))
    return 0


def run_raw(config: dict) -> int:
    try:
        credentials = load_credentials()
        payload = fetch_usage(credentials["token"], config["http_timeout"])
    except UsageError as exc:
        print(f"erreur ({exc.kind}): {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


# ------------------------------------------------------------------------ indicator


class Indicator:
    """AppIndicator front end. Fetches off the main loop, updates via idle_add."""

    def __init__(self, config: dict, gi_modules: dict) -> None:
        self.config = config
        self.Gtk = gi_modules["Gtk"]
        self.GLib = gi_modules["GLib"]
        self.Gio = gi_modules["Gio"]
        self.Notify = gi_modules["Notify"]
        self.AppIndicator = gi_modules["AppIndicator"]

        self.state: dict | None = None
        self.error: UsageError | None = None
        self.failures = 0
        self.timer_id: int | None = None
        self.fetching = False
        self.credentials_monitor = None
        self.debounce_id: int | None = None
        self.last_fetch = 0.0
        self.throttled_until = 0.0
        self.watcher_id: int | None = None
        self.repush_id: int | None = None

        cache = load_cache()
        self.account = account_fingerprint()
        self.notified: dict[str, list[int]] = {}
        # Percentages and rate-limit pauses are per account: a cache written under a
        # different login must not be shown, or we would display someone else's usage.
        cached_account = cache.get("account")
        if self.account is not None and cached_account is not None and cached_account != self.account:
            print("cache d'un autre compte ignoré", file=sys.stderr)
        else:
            self.notified = cache.get("notified", {})
            if isinstance(cache.get("state"), dict):
                self.state = cache["state"]
            # A restart must not clear an active 429 pause, so it is stored as wall clock.
            pending = cache.get("throttle_until_wall")
            if isinstance(pending, (int, float)):
                remaining = pending - time.time()
                if 0 < remaining <= self.config["max_backoff_seconds"]:
                    self.throttled_until = time.monotonic() + remaining
                    print(f"429 encore actif, pause de {int(remaining)} s", file=sys.stderr)

        self.indicator = self.AppIndicator.Indicator.new(
            APP_ID, ICONS["normal"], self.AppIndicator.IndicatorCategory.SYSTEM_SERVICES
        )
        self.indicator.set_status(self.AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title(APP_NAME)
        self.indicator.set_label("…", LABEL_GUIDE)
        self.menu = self.Gtk.Menu()
        self.indicator.set_menu(self.menu)

        if self.Notify is not None and config["notifications"]:
            self.Notify.init(APP_NAME)

        self.rebuild_menu()
        self.watch_credentials()
        self.watch_panel()
        self.GLib.idle_add(self.start_fetch)
        self.schedule(self.config["interval_seconds"])

    # -- persistence

    def persist(self) -> None:
        remaining = self.throttled_until - time.monotonic()
        save_cache(
            {
                "account": self.account,
                "state": self.state,
                "notified": self.notified,
                "throttle_until_wall": time.time() + remaining if remaining > 0 else None,
            }
        )

    def check_account_switch(self) -> None:
        """Drop everything account-scoped when the user logs in as someone else."""
        fingerprint = account_fingerprint()
        if fingerprint is None or fingerprint == self.account:
            return
        print("changement de compte détecté, état local vidé", file=sys.stderr)
        self.account = fingerprint
        self.state = None
        self.notified = {}
        self.throttled_until = 0.0
        self.failures = 0
        self.error = None
        self.refresh_ui()

    # -- scheduling

    def schedule(self, delay: int) -> None:
        if self.timer_id is not None:
            self.GLib.source_remove(self.timer_id)
        self.timer_id = self.GLib.timeout_add_seconds(max(5, int(delay)), self.on_timer)

    def on_timer(self) -> bool:
        self.timer_id = None
        self.start_fetch()
        return False  # rescheduled by on_result, with backoff if needed

    def watch_credentials(self) -> None:
        try:
            gfile = self.Gio.File.new_for_path(str(CREDENTIALS_PATH))
            self.credentials_monitor = gfile.monitor_file(self.Gio.FileMonitorFlags.NONE, None)
            self.credentials_monitor.connect("changed", self.on_credentials_changed)
        except Exception as exc:  # noqa: BLE001 - watching is a bonus, not required
            print(f"surveillance credentials indisponible: {exc}", file=sys.stderr)

    def watch_panel(self) -> None:
        """Re-push the label whenever the panel's tray host comes back.

        GNOME's appindicator extension only redraws the label when it receives
        XAyatanaNewLabel, and libayatana only emits that when the text changes.
        So when the extension (or the whole shell) restarts, libayatana quietly
        re-registers the icon but never resends the label, and the panel shows a
        bare icon until our percentages happen to move.
        """
        try:
            self.watcher_id = self.Gio.bus_watch_name(
                self.Gio.BusType.SESSION,
                SNI_WATCHER_NAME,
                self.Gio.BusNameWatcherFlags.NONE,
                self.on_panel_appeared,
                None,
            )
        except Exception as exc:  # noqa: BLE001 - the label just stays stale, keep polling
            print(f"surveillance du panneau indisponible: {exc}", file=sys.stderr)

    def on_panel_appeared(self, _connection, _name, _owner) -> None:
        # libayatana re-registers the item on its own; give it a moment first.
        if self.repush_id is not None:
            self.GLib.source_remove(self.repush_id)
        self.repush_id = self.GLib.timeout_add_seconds(2, self.blank_label)

    def blank_label(self) -> bool:
        self.repush_id = None
        # The extension drops a property refresh whose value equals its cache, so
        # the real text only lands if an empty label goes out first.
        self.indicator.set_label("", LABEL_GUIDE)
        self.repush_id = self.GLib.timeout_add(LABEL_REPUSH_GAP_MS, self.restore_label)
        return False

    def restore_label(self) -> bool:
        self.repush_id = None
        self.indicator.set_label(self.label_text(), LABEL_GUIDE)
        return False

    def on_credentials_changed(self, *_args) -> None:
        # Claude Code rewrites the file on refresh; debounce the write burst.
        if self.debounce_id is not None:
            self.GLib.source_remove(self.debounce_id)
        self.debounce_id = self.GLib.timeout_add_seconds(2, self.on_debounced_change)

    def on_debounced_change(self) -> bool:
        self.debounce_id = None
        self.start_fetch()
        return False

    # -- fetching

    def start_fetch(self, *_args) -> bool:
        if self.fetching:
            return False
        self.check_account_switch()
        now = time.monotonic()
        # Floor between any two requests, so a burst of menu clicks or a flurry of
        # credential-file writes cannot walk us into a 429.
        wait = max(
            self.config["min_fetch_interval"] - (now - self.last_fetch),
            self.throttled_until - now,
        )
        if wait > 0:
            print(f"fetch différé de {wait:.0f} s (anti-429)", file=sys.stderr)
            self.schedule(max(1, int(wait) + 1))
            return False
        self.last_fetch = now
        self.fetching = True
        threading.Thread(target=self._fetch_worker, daemon=True).start()
        return False

    def _fetch_worker(self) -> None:
        try:
            credentials = load_credentials()
            payload = fetch_usage(credentials["token"], self.config["http_timeout"])
            state = normalize(payload, credentials)
        except UsageError as exc:
            self.GLib.idle_add(self.on_result, None, exc)
            return
        except Exception as exc:  # noqa: BLE001 - never let the thread kill the daemon
            self.GLib.idle_add(self.on_result, None, UsageError(str(exc), "http"))
            return
        self.GLib.idle_add(self.on_result, state, None)

    def on_result(self, state: dict | None, error: UsageError | None) -> bool:
        self.fetching = False
        if state is not None:
            self.state = state
            self.error = None
            self.failures = 0
            self.check_thresholds(state)
            self.persist()
            print(
                f"usage: session {format_percent(state['session_percent'])}%, "
                f"hebdo {format_percent(state['weekly_percent'])}%",
                file=sys.stderr,
            )
            self.schedule(self.config["interval_seconds"])
        else:
            self.error = error
            self.failures += 1
            backoff = min(
                self.config["interval_seconds"] * (2 ** min(self.failures, 6)),
                self.config["max_backoff_seconds"],
            )
            # The server's Retry-After wins when it asks for a longer pause.
            retry_after = getattr(error, "retry_after", None) or 0
            if retry_after:
                backoff = max(backoff, retry_after)
            if error is not None and error.kind == "rate-limit":
                self.throttled_until = time.monotonic() + backoff
                self.persist()
            print(
                f"fetch échoué ({error.kind}): {error} — nouvel essai dans {int(backoff)} s",
                file=sys.stderr,
            )
            self.schedule(int(backoff))
        self.refresh_ui()
        return False

    # -- notifications

    def check_thresholds(self, state: dict) -> None:
        if not self.config["notifications"] or self.Notify is None:
            return
        thresholds = sorted(int(value) for value in self.config["thresholds"])
        live_keys = set()
        for limit in state["limits"]:
            if limit["percent"] is None:
                continue
            key = _limit_key(limit)
            live_keys.add(key)
            already = set(self.notified.get(key, []))
            crossed = [value for value in thresholds if limit["percent"] >= value]
            new = [value for value in crossed if value not in already]
            if new:
                self.notify(limit, max(new))
                self.notified[key] = sorted(already | set(new))
        # Drop keys for windows that already reset.
        self.notified = {key: value for key, value in self.notified.items() if key in live_keys}

    def notify(self, limit: dict, threshold: int) -> None:
        urgency = "critical" if threshold >= 95 else "normal"
        body = f"{format_percent(limit['percent'])}% utilisés — {reset_text(limit['resets_at'])}"
        try:
            note = self.Notify.Notification.new(
                f"Claude — {limit['label']} ≥ {threshold}%", body, ICONS["warning"]
            )
            note.set_urgency(
                self.Notify.Urgency.CRITICAL if urgency == "critical" else self.Notify.Urgency.NORMAL
            )
            note.show()
        except Exception as exc:  # noqa: BLE001 - a failed toast must not break polling
            print(f"notification échouée: {exc}", file=sys.stderr)

    # -- UI

    def label_text(self) -> str:
        state = self.state
        if state is None:
            return "Claude ?"
        return self.config["label_format"].format(
            session=format_percent(state["session_percent"]),
            weekly=format_percent(state["weekly_percent"]),
        )

    def refresh_ui(self) -> None:
        state = self.state
        if state is None:
            self.indicator.set_icon_full(ICONS["stale"], APP_NAME)
        else:
            icon = ICONS["stale"] if self.error else ICONS.get(
                state["max_severity"], ICONS["warning"]
            )
            self.indicator.set_icon_full(icon, APP_NAME)
        self.indicator.set_label(self.label_text(), LABEL_GUIDE)
        self.rebuild_menu()

    def add_item(self, text: str, *, enabled: bool = False, handler=None) -> None:
        item = self.Gtk.MenuItem.new_with_label(text)
        # dbusmenu escapes "_" as a mnemonic marker, which GNOME renders as "__".
        item.set_use_underline(False)
        item.set_sensitive(enabled)
        if handler is not None:
            item.connect("activate", handler)
        item.show()
        self.menu.append(item)

    def add_separator(self) -> None:
        separator = self.Gtk.SeparatorMenuItem()
        separator.show()
        self.menu.append(separator)

    def rebuild_menu(self) -> None:
        for child in self.menu.get_children():
            self.menu.remove(child)

        state = self.state
        if state is not None:
            self.add_item(header_text(state))
            self.add_separator()
            for limit in state["limits"]:
                if not _visible(limit, self.config):
                    continue
                marker = "▸ " if limit["is_active"] else "   "
                self.add_item(
                    f"{marker}{limit['label']} — {format_percent(limit['percent'])}%"
                    f"  ·  {reset_text(limit['resets_at'])}"
                )
            extra = state.get("extra_usage")
            if extra:
                self.add_item(
                    f"   Crédits extra — {format_percent(extra.get('utilization'))}%"
                )
            fetched = _parse_ts(state.get("fetched_at"))
            if fetched:
                self.add_item(f"   maj {local_reset(fetched)}")

        if self.error is not None:
            self.add_separator()
            self.add_item(f"⚠ {self.error}")

        self.add_separator()
        self.add_item("Rafraîchir maintenant", enabled=True, handler=self.on_refresh_clicked)
        self.add_item("Ouvrir la page d'usage", enabled=True, handler=self.on_open_clicked)
        self.add_item("Quitter", enabled=True, handler=self.on_quit_clicked)

    def on_refresh_clicked(self, *_args) -> None:
        self.failures = 0
        self.start_fetch()

    def on_open_clicked(self, *_args) -> None:
        try:
            self.Gio.AppInfo.launch_default_for_uri(USAGE_PAGE, None)
        except Exception as exc:  # noqa: BLE001
            print(f"ouverture navigateur échouée: {exc}", file=sys.stderr)

    def on_quit_clicked(self, *_args) -> None:
        self.persist()
        if self.repush_id is not None:
            self.GLib.source_remove(self.repush_id)
            self.repush_id = None
        if self.watcher_id is not None:
            self.Gio.bus_unwatch_name(self.watcher_id)
            self.watcher_id = None
        self.Gtk.main_quit()


def import_gi() -> dict:
    import gi

    gi.require_version("Gtk", "3.0")
    indicator_module = None
    for name, version in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
        try:
            gi.require_version(name, version)
            indicator_module = __import__("gi.repository", fromlist=[name])
            indicator_module = getattr(indicator_module, name)
            break
        except (ValueError, ImportError):
            continue
    if indicator_module is None:
        raise SystemExit(
            "AppIndicator introuvable. Installe:\n"
            "  sudo apt install gir1.2-ayatanaappindicator3-0.1"
        )

    from gi.repository import Gio, GLib, Gtk

    notify = None
    try:
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify

        notify = Notify
    except (ValueError, ImportError):
        print("libnotify absent, notifications désactivées", file=sys.stderr)

    return {
        "Gtk": Gtk,
        "GLib": GLib,
        "Gio": Gio,
        "Notify": notify,
        "AppIndicator": indicator_module,
    }


def run_indicator(config: dict) -> int:
    modules = import_gi()
    Gtk, GLib = modules["Gtk"], modules["GLib"]
    indicator = Indicator(config, modules)

    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: indicator.on_quit_clicked() or True)

    Gtk.main()
    return 0


# ----------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=APP_ID,
        description="Indicateur de limites d'usage Claude pour Ubuntu.",
    )
    parser.add_argument("--once", action="store_true", help="afficher l'usage et quitter")
    parser.add_argument("--json", action="store_true", help="sortie JSON normalisée (avec --once)")
    parser.add_argument("--raw", action="store_true", help="afficher la réponse brute de l'API")
    parser.add_argument("--watch", type=int, metavar="SEC", help="boucle terminal toutes les SEC")
    args = parser.parse_args(argv)

    config = load_config()

    if args.raw:
        return run_raw(config)
    if args.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")
                run_once(config, as_json=False)
                time.sleep(max(5, args.watch))
        except KeyboardInterrupt:
            return 0
    if args.once or args.json:
        return run_once(config, as_json=args.json)
    return run_indicator(config)


if __name__ == "__main__":
    sys.exit(main())
