"""Configuration loading, path normalization, and account resolution.

Paths are normalized to forward slashes everywhere in tokenDiary. Backslash UNC
paths to WSL read unreliably from Python on this machine, and mixing styles makes 
scan_state keys inconsistent between runs.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import timedelta, timezone

DEFAULT_CONFIG_NAME = "config.toml"
ENV_FILE_NAME = ".env"

# TOML has no native variable interpolation, so we do it ourselves: any string
# value in config.toml may contain ${VAR}, resolved against .env and the real
# environment. This keeps machine-specific and identifying values (home paths,
# account emails) out of the committed config.
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Fallbacks used when a key is absent from config.toml. Defined once here and
# referenced everywhere else -- a tuning value must never be spelled twice in
# code, or the copies drift and the config silently stops being the source of
# truth. config.toml states them explicitly for discoverability; these apply
# only when the file omits them.
DEFAULT_TZ_OFFSET_HOURS = 8       # PLAN.md D8
DEFAULT_HOT_WINDOW_HOURS = 48     # PLAN.md D5
DEFAULT_WATERMARK_SLACK_SECONDS = 300   # PLAN.md §6.1


class ConfigError(Exception):
    """Configuration is missing, malformed, or internally inconsistent."""


def norm_path(p: str) -> str:
    """Normalize to forward slashes, no trailing slash, preserving UNC prefix."""
    p = str(p).replace("\\", "/")
    while "//" in p[2:]:
        p = p[:2] + p[2:].replace("//", "/")
    return p[:-1] if p.endswith("/") and len(p) > 1 else p


@dataclass(slots=True)
class Account:
    uuid: str | None = None
    email: str | None = None
    org_name: str | None = None
    source_file: str | None = None
    error: str | None = None

    @property
    def resolved(self) -> bool:
        return self.uuid is not None


@dataclass(slots=True)
class Source:
    id: str
    label: str
    path: str
    account_hint: str | None = None

    def rel(self, abs_path: str) -> str:
        """Path relative to this source's root, forward-slashed."""
        abs_path = norm_path(abs_path)
        root = self.path + "/"
        return abs_path[len(root):] if abs_path.startswith(root) else abs_path


@dataclass(slots=True)
class Config:
    sources: list[Source]
    timezone_offset_hours: int = DEFAULT_TZ_OFFSET_HOURS
    week_start: str = "monday"
    ingest_from: str | None = None
    hot_window_hours: int = DEFAULT_HOT_WINDOW_HOURS
    watermark_slack_seconds: int = DEFAULT_WATERMARK_SLACK_SECONDS
    dashboard_default_from: str | None = None
    root_dir: str = "."
    unused_source_slots: int = 0

    @property
    def tz(self) -> timezone:
        """Local timezone used for local_date / local_hour (PLAN.md D8)."""
        return timezone(timedelta(hours=self.timezone_offset_hours))

    @property
    def db_path(self) -> str:
        return f"{self.root_dir}/data/tokendiary.db"

    def source(self, source_id: str) -> Source:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise ConfigError(f"unknown source id: {source_id!r}")


def find_config(start: str | None = None) -> str:
    """Locate config.toml: explicit path, $TOKENDIARY_CONFIG, or package parent."""
    if start:
        if not os.path.exists(start):
            raise ConfigError(f"config not found: {start}")
        return norm_path(start)

    env = os.environ.get("TOKENDIARY_CONFIG")
    if env:
        if not os.path.exists(env):
            raise ConfigError(f"TOKENDIARY_CONFIG points at a missing file: {env}")
        return norm_path(env)

    here = norm_path(os.path.dirname(os.path.abspath(__file__)))
    candidate = f"{os.path.dirname(here)}/{DEFAULT_CONFIG_NAME}"
    if os.path.exists(candidate):
        return norm_path(candidate)
    raise ConfigError(f"no {DEFAULT_CONFIG_NAME} found (looked at {candidate})")


def load_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE .env file. Missing file is not an error.

    Deliberately minimal: no export prefixes, no multi-line values, no
    interpolation inside the .env itself. Enough for machine-specific paths and
    identifiers, and small enough to have no failure modes worth debugging.
    """
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key.strip()] = value
    return out


def expand_vars(value, env: dict[str, str], missing: set[tuple[str, str]], where: str = ""):
    """Recursively expand ${VAR} in every string of a parsed TOML structure.

    An unresolved name expands to the empty string and is recorded in `missing`
    with the top-level section it appeared under. The caller decides what that
    means: outside [[sources]] it is a hard error, while an entirely unset
    source slot is simply an unused slot (see load_config).
    """
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in env:
                missing.add((name, where))
                return ""
            return env[name]
        return _VAR_PATTERN.sub(sub, value)
    if isinstance(value, dict):
        return {k: expand_vars(v, env, missing, where or k) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_vars(v, env, missing, where) for v in value]
    return value


def load_config(path: str | None = None) -> Config:
    cfg_path = find_config(path)
    with open(cfg_path, "rb") as fh:
        raw = tomllib.load(fh)

    # Real environment wins over .env, so a shell override or CI secret can
    # replace a checked-out default without editing files.
    root = norm_path(os.path.dirname(cfg_path)) or "."
    env_file = f"{root}/{ENV_FILE_NAME}"
    env = {**load_env_file(env_file), **os.environ}
    missing: set[tuple[str, str]] = set()
    raw = expand_vars(raw, env, missing)

    # Outside [[sources]], an unresolved variable is always a mistake.
    stray = sorted(name for name, where in missing if where != "sources")
    if stray:
        raise ConfigError(
            f"{cfg_path}: unresolved variable(s): {', '.join(stray)}\n"
            f"  define them in {env_file} (see {ENV_FILE_NAME}.example) "
            f"or export them in the environment"
        )

    # config.toml ships a fixed number of anonymous source slots so that neither
    # the identity nor the count of configured accounts appears in a tracked
    # file. A slot with nothing filled in is unused; a half-filled slot is a
    # mistake and is rejected, because silently dropping a source would render
    # as "you did no work" rather than as an error (PLAN.md §6.6).
    sources: list[Source] = []
    seen: set[str] = set()
    unused = 0
    for i, s in enumerate(raw.get("sources") or []):
        sid = str(s.get("id") or "").strip()
        spath = str(s.get("path") or "").strip()
        if not sid and not spath:
            unused += 1
            continue
        if not sid or not spath:
            filled, empty = ("id", "path") if sid else ("path", "id")
            raise ConfigError(
                f"{cfg_path}: source slot {i + 1} has {filled!r} but no {empty!r}.\n"
                f"  Fill in both, or leave the whole slot's variables unset to skip it.\n"
                f"  Check {env_file} for the slot's TD_S{i + 1}_* entries."
            )
        if sid in seen:
            raise ConfigError(f"{cfg_path}: duplicate source id {sid!r}")
        seen.add(sid)
        sources.append(
            Source(
                id=sid,
                label=str(s.get("label") or sid),
                path=norm_path(spath),
                account_hint=(s.get("account_hint") or "").strip() or None,
            )
        )

    if not sources:
        raise ConfigError(
            f"{cfg_path}: no sources configured.\n"
            f"  Fill in at least TD_S1_ID and TD_S1_PATH in {env_file} "
            f"(see {ENV_FILE_NAME}.example)."
        )

    ingest = raw.get("ingest") or {}
    dash = raw.get("dashboard") or {}
    tz_off = int(raw.get("timezone_offset_hours", DEFAULT_TZ_OFFSET_HOURS))
    if not -12 <= tz_off <= 14:
        raise ConfigError(f"{cfg_path}: timezone_offset_hours out of range: {tz_off}")

    return Config(
        sources=sources,
        timezone_offset_hours=tz_off,
        week_start=str(raw.get("week_start", "monday")).lower(),
        ingest_from=(ingest.get("from") or None),
        hot_window_hours=int(ingest.get("hot_window_hours", DEFAULT_HOT_WINDOW_HOURS)),
        watermark_slack_seconds=int(
            ingest.get("watermark_slack_seconds", DEFAULT_WATERMARK_SLACK_SECONDS)
        ),
        dashboard_default_from=(dash.get("default_from") or None),
        root_dir=root,
        unused_source_slots=unused,
    )


def resolve_account(source: Source) -> Account:
    """Read account identity from the install's .claude.json.

    Source roots look like `<home>/.claude/projects`, and the account file sits
    at `<home>/.claude.json` -- two levels up, not inside the .claude directory.
    Identity is read rather than trusted from config so that re-logging into a
    different account on a machine is detected instead of silently mislabeled.
    """
    parent = os.path.dirname(source.path)           # <home>/.claude
    home = os.path.dirname(parent)                  # <home>
    candidates = [f"{home}/.claude.json", f"{parent}.json"]

    for cand in candidates:
        cand = norm_path(cand)
        if not os.path.exists(cand):
            continue
        try:
            with open(cand, "rb") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            return Account(error=f"{cand}: unreadable ({exc.__class__.__name__})")
        oauth = data.get("oauthAccount") or {}
        if not oauth.get("accountUuid"):
            return Account(source_file=cand, error=f"{cand}: no oauthAccount.accountUuid")
        return Account(
            uuid=oauth.get("accountUuid"),
            email=oauth.get("emailAddress"),
            org_name=oauth.get("organizationName"),
            source_file=cand,
        )

    return Account(error=f"no .claude.json found near {source.path}")
