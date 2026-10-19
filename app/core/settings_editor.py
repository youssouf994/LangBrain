"""
Settings editor behind the /config page.

The files stay the main way to configure LangBrain: `.env` (secrets and environment) and `configurazione.toml` /
`config.toml`; an extension (app/core/estensioni.py) can add the TOML file of its node. This module is the fallback for
people who prefer a form: it edits the SAME files in place, line by line, so comments, order and names written by hand are kept.
Before every write the previous version is copied to `<file>.bak`, and `restore_backups` brings it back.

Nothing is written unless the whole change set is valid: values are checked by kind, and the new TOML text is run
through the real loaders (`costruisci_configurazione`, the loader of the node) before it replaces the file.
Secrets are write-only: `describe` never returns them, only whether they are set.
"""

import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, find_dotenv

from app.core.configurazione import (
    ErroreConfigurazione, Configurazione, _VALORI_EN, costruisci_configurazione, normalizza_nomi, percorso_configurazione,
)
from app.core.estensioni import SorgenteImpostazioni, estensioni
from app.core.lingua import scegli

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = ("google_studio", "openrouter", "mistral", "anthropic", "local", "auto")


class SettingsError(ValueError):
    """Some values are not valid; `errors` maps field id -> message (field "" = the whole change set)."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k or 'file'}: {v}" for k, v in errors.items()))
        self.errors = errors


# --------------------------------------------------------------------------------------------------- field registry

@dataclass(frozen=True)
class Field:
    id: str                      # "env:DEFAULT_PROVIDER", "config:hitl.level", "<source of a node>:<key>"
    source: str                  # env | config | the id of a source added by an extension
    group: str                   # section of the page inside the tab
    kind: str                    # text | secret | int | float | bool | choice | list | multiline
    key: str                     # env name, or the English "section.key" / the setting name of a node
    default: Any = None
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    items: str = "str"           # list items: str | float
    restart: bool = False        # the running process only reads it at start
    optional: bool = False       # an empty value removes the line (the code default applies again)
    flag_as_int: bool = False    # TOML flags written as 0/1 (hitl, demo) instead of true/false
    names: dict[str, tuple[str, str]] = field(default_factory=dict)  # config: {"it": (section, key), "en": (section, key)}

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.id.lower()).strip("_")


def env_field(key, group, kind="text", default=None, **kw) -> Field:
    return Field(f"env:{key}", "env", group, kind, key, default, **kw)


def cfg_field(section_en, section_it, key_en, key_it, group, kind, default, **kw) -> Field:
    return Field(f"config:{section_en}.{key_en}", "config", group, kind, f"{section_en}.{key_en}", default,
                 names={"en": (section_en, key_en), "it": (section_it, key_it)}, **kw)


_DEFAULTS = Configurazione()
_HITL_ON_TIMEOUT = ("human", "system", "reject")
_HITL_LEVEL = ("nodes", "brain", "both")
_POLICY = ("reject", "allow")
_PROMPT_NAMES = (("brain_escalation", "brain_escalation"), ("brain_escalation_user", "brain_escalation_utente"),
                 ("troubleshooting", "troubleshooting"), ("override", "override"), ("routine_check", "controllo_macro"))

CORE_FIELDS: tuple[Field, ...] = (
    # ---- .env
    env_field("DEFAULT_PROVIDER", "llm", "choice", "google_studio", choices=PROVIDERS),
    env_field("MAO_FALLBACK", "llm", "choice", "", choices=("", "0", "1")),
    env_field("MAO_TIMEOUT_SECONDS", "llm", "float", "40", minimum=1),
    env_field("MAO_MAX_TOKENS_MINIMO", "llm", "int", "8192", minimum=256),
    env_field("MAO_MAX_TOKENS_LIMITE", "llm", "int", "32768", minimum=256),
    env_field("GEMINI_API_KEY", "google", "secret"),
    env_field("GEMINI_MODEL", "google", default="gemini-3.5-flash"),
    env_field("GOOGLE_BASE_URL", "google", default="https://generativelanguage.googleapis.com/v1beta/openai/"),
    env_field("OPENROUTER_API_KEY", "openrouter", "secret"),
    env_field("OPENROUTER_MODEL", "openrouter", default="openrouter/auto"),
    env_field("OPENROUTER_BASE_URL", "openrouter", default="https://openrouter.ai/api/v1"),
    env_field("OPENROUTER_REFERER", "openrouter", default="https://github.com/youssouf994/LangBrain"),
    env_field("OPENROUTER_APP_TITLE", "openrouter", default="LangBrain"),
    env_field("MISTRAL_API_KEY", "mistral", "secret"),
    env_field("MISTRAL_MODEL", "mistral", default="ministral-8b-latest"),
    env_field("MISTRAL_BASE_URL", "mistral", default="https://api.mistral.ai/v1"),
    env_field("ANTHROPIC_API_KEY", "anthropic", "secret"),
    env_field("ANTHROPIC_MODEL", "anthropic", default="claude-haiku-5-5"),
    env_field("ANTHROPIC_BASE_URL", "anthropic", default="https://api.anthropic.com"),
    env_field("LOCAL_MODEL", "local", default="local-model"),
    env_field("LOCAL_MODEL_BASE_URL", "local", default="http://127.0.0.1:8080/v1"),
    env_field("LOCAL_MODEL_DOCKER_BASE_URL", "local", default="http://host.docker.internal:8080/v1"),
    env_field("LOCAL_API_KEY", "local", "secret"),
    env_field("API_KEY_TIROCINANTE", "access", "secret"),
    env_field("API_KEY_MEDICO_DI_GUARDIA", "access", "secret"),
    env_field("API_KEY_PRIMARIO", "access", "secret"),
    env_field("API_KEY", "access", "secret"),
    env_field("DB_PATH", "runtime", default="langbrain.db", restart=True),
    env_field("FLAG_TTL_MINUTES", "runtime", "int", "60", minimum=1, restart=True),
    # ---- configurazione.toml
    cfg_field("general", "generale", "language", "lingua", "general", "choice", _DEFAULTS.lingua, choices=("it", "en")),
    cfg_field("llm", "llm", "fallback", "fallback", "llm", "bool", _DEFAULTS.llm_fallback),
    cfg_field("llm", "llm", "provider_order", "ordine_provider", "llm", "list", _DEFAULTS.ordine_provider),
    cfg_field("hitl", "hitl", "timer_enabled", "timer_attivo", "hitl", "bool", _DEFAULTS.hitl_timer_attivo, flag_as_int=True),
    cfg_field("hitl", "hitl", "default_timer_seconds", "timer_predefinito_secondi", "hitl", "int", _DEFAULTS.hitl_timer_secondi, minimum=1),
    cfg_field("hitl", "hitl", "on_timeout", "azione_alla_scadenza", "hitl", "choice", "human", choices=_HITL_ON_TIMEOUT),
    cfg_field("hitl", "hitl", "level", "livello", "hitl", "choice", "both", choices=_HITL_LEVEL),
    cfg_field("hitl", "hitl", "brain_critical_targets", "target_critici_brain", "hitl", "list", _DEFAULTS.hitl_target_critici_brain),
    cfg_field("policy", "politica", "unlisted_devices", "dispositivi_non_elencati", "devices", "choice", "reject", choices=_POLICY),
    cfg_field("signals", "segnali", "window_hours", "finestra_ore", "signals", "float", _DEFAULTS.finestra_segnali_ore, minimum=0.01, maximum=2160),
    cfg_field("demo", "demo", "seed_conflict_at_start", "conflitto_all_avvio", "demo", "bool", False, flag_as_int=True),
    cfg_field("demo", "demo", "web_page", "pagina_web", "demo", "bool", True, flag_as_int=True),
    *(cfg_field("prompts", "prompt", en, it, "prompts", "multiline", "", optional=True) for en, it in _PROMPT_NAMES),
)


def fields() -> tuple[Field, ...]:
    """The fields of LangBrain and those added by the extensions."""
    return CORE_FIELDS + tuple(f for e in estensioni() for f in e.campi)


def by_id() -> dict[str, Field]:
    return {f.id: f for f in fields()}


def sources() -> dict[str, SorgenteImpostazioni]:
    """The TOML files of the nodes (env and config are built in)."""
    return {s.id: s for e in estensioni() for s in e.sorgenti}



# --------------------------------------------------------------------------------------------------- file locations

def env_path() -> Path:
    return Path(os.getenv("LANGBRAIN_ENV_FILE") or find_dotenv() or ROOT / ".env")


def config_path() -> Path:
    return percorso_configurazione()


def _path_of(source: str) -> Path:
    if source == "env":
        return env_path()
    if source == "config":
        return config_path()
    return sources()[source].percorso()


def source_ids() -> list[str]:
    return ["env", "config", *sources()]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# --------------------------------------------------------------------------------------------------- TOML line editor

def _scan_value(lines: list[str], i: int, col: int) -> tuple[int, int | None]:
    """
    Walks the value that starts at lines[i][col:]: returns (index of its last line, column of a trailing `#` comment
    on that line or None). Understands strings (also triple-quoted), brackets over several lines and comments.
    """
    depth = 0
    triple: str | None = None
    quote: str | None = None
    while i < len(lines):
        line = lines[i]
        j = col
        while j < len(line):
            c = line[j]
            if triple:
                if line.startswith(triple, j) and not (triple == '"""' and line[j - 1] == "\\"):
                    triple, j = None, j + 2
            elif quote:
                if c == "\\" and quote == '"':
                    j += 1
                elif c == quote:
                    quote = None
            elif line.startswith('"""', j) or line.startswith("'''", j):
                triple, j = line[j:j + 3], j + 2
            elif c in "\"'":
                quote = c
            elif c == "#":
                return i, j if depth == 0 else None
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            j += 1
        if depth <= 0 and not triple:
            return i, None
        i, col = i + 1, 0
    return len(lines) - 1, None


_HEADER = re.compile(r"^\s*(\[\[?)\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _locate(lines: list[str], sections: tuple[str, ...], keys: tuple[str, ...]):
    """
    -> (key span (first, last, indent, key, comment) | None, section header index | None, index of the last line of the
    section's last assignment | None). `sections` may contain "" for the top level of the file.
    """
    current = ""
    header = None if "" not in sections else -1
    last_assignment = None
    found = None
    i = 0
    while i < len(lines):
        line = lines[i]
        h = _HEADER.match(line)
        if h and not line.lstrip().startswith("#"):
            current = h.group(2).strip() if h.group(1) == "[" else f"[[{h.group(2).strip()}"
            if current in sections:
                header, last_assignment = i, None
            i += 1
            continue
        m = re.match(r"^(\s*)([A-Za-z0-9_.\"'-]+)\s*=\s*", line)
        if m and not line.lstrip().startswith("#"):
            last, comment_col = _scan_value(lines, i, m.end())
            if current in sections:
                last_assignment = last
                if m.group(2).strip("\"'") in keys and found is None:
                    comment = lines[last][comment_col:].rstrip("\r\n") if comment_col is not None else None
                    found = (i, last, m.group(1), m.group(2), comment)
            i = last + 1
            continue
        i += 1
    return found, header, last_assignment


def toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text if any(c in text for c in ".en") else text + ".0"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_literal(v) for v in value) + "]"
    text = str(value)
    if "\n" in text:
        body = text.replace("\\", "\\\\").replace('"""', '""\\"').replace("\r", "")
        return f'"""\n{body}"""'
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t") + '"'


def set_toml_value(text: str, sections: tuple[str, ...], keys: tuple[str, ...], value: Any, *, new_section: str, new_key: str) -> str:
    """
    Sets `key = value` in the first of `sections` that exists, keeping everything else as written. `value` None removes
    the line. If the key is missing it is added after the last assignment of its section; if the section is missing it
    is appended at the end of the file. `keys` are all the names (Italian, English) the key may have in the file.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    found, header, last_assignment = _locate(lines, sections, keys)
    if found:
        first, last, indent, name, comment = found
        if value is None:
            del lines[first:last + 1]
        else:
            tail = f"  {comment}" if comment else ""
            lines[first:last + 1] = [f"{indent}{name} = {toml_literal(value)}{tail}\n"]
    elif value is not None:
        new = f"{new_key} = {toml_literal(value)}\n"
        if header is not None:
            at = (last_assignment if last_assignment is not None else header) + 1
            lines.insert(max(at, 0), new)
        else:
            if lines and lines[-1].strip():
                lines.append("\n")
            lines += [f"[{new_section}]\n", new]
    return "".join(lines)


# --------------------------------------------------------------------------------------------------- .env line editor

_ENV_SAFE = re.compile(r"^[A-Za-z0-9_./:@%+=,~-]*$")


def env_literal(value: str) -> str:
    if _ENV_SAFE.match(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def set_env_value(text: str, key: str, value: str) -> str:
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    new = f"{key}={env_literal(value)}\n"
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new
            return "".join(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return "".join(lines) + new


# --------------------------------------------------------------------------------------------------- reading

def _style(text: str) -> str:
    """'it' when the file uses the Italian section names (configurazione.toml), otherwise 'en'."""
    return "it" if re.search(r"^\s*\[(generale|politica|dispositivi|prompt|organismo)\b", text, re.M) else "en"


def _to_en(field_: Field, value: Any) -> Any:
    section_it, key_it = field_.names["it"]
    return {v: k for k, v in _VALORI_EN.get((section_it, key_it), {}).items() if k != "refuse"}.get(value, "human" if value == "nessuna" else value)


def _current(field_: Field, env: dict, config: dict, tables: dict[str, dict]) -> tuple[Any, bool]:
    """-> (value as the user would write it, whether the file sets it)."""
    if field_.source == "env":
        if field_.key in env and env[field_.key] is not None:
            return env[field_.key], True
        return os.environ.get(field_.key, ""), False
    if field_.source == "config":
        section_it, key_it = field_.names["it"]
        present = key_it in (config.get(section_it) or {})
        if not present:
            return field_.default, False
        value = config[section_it][key_it]
        if field_.kind == "choice":
            value = _to_en(field_, value)
        if field_.kind == "bool":
            value = bool(value)
        return value, True
    table = tables.get(field_.source, {})
    present = field_.key in table
    value = table.get(field_.key, field_.default)
    if value is None:
        value = ""
    return (list(value) if isinstance(value, tuple) else value), present


def describe() -> dict[str, Any]:
    """Every field with its current value (secrets only as set / not set) and the files involved."""
    env = dotenv_values(env_path()) if env_path().is_file() else {}
    config_text = _read(config_path())
    try:
        config = normalizza_nomi(tomllib.loads(config_text)) if config_text else {}
    except (tomllib.TOMLDecodeError, ErroreConfigurazione):
        config = {}
    tables: dict[str, dict] = {}
    for sid, source in sources().items():
        text = _read(source.percorso())
        try:
            data = tomllib.loads(text) if text else {}
            table = data.get(source.sezione, data)
        except tomllib.TOMLDecodeError:
            table = {}
        tables[sid] = table if isinstance(table, dict) else {}
    result = []
    for f in fields():
        value, in_file = _current(f, env, config, tables)
        item = {"id": f.id, "source": f.source, "group": f.group, "kind": f.kind, "key": f.key, "slug": f.slug,
                "choices": list(f.choices), "min": f.minimum, "max": f.maximum, "items": f.items, "restart": f.restart, "optional": f.optional,
                "in_file": in_file, "default": f.default if f.kind != "secret" else None}
        if f.kind == "secret":
            item["is_set"] = bool(value)
            item["value"] = None
        else:
            item["value"] = value
        result.append(item)
    views = {"env": "config", "config": "config", **{sid: source.vista for sid, source in sources().items()}}
    paths = {sid: _path_of(sid) for sid in source_ids()}
    return {
        "fields": result,
        "files": {sid: {"name": p.name, "exists": p.is_file(), "backup": p.with_name(p.name + ".bak").is_file(), "view": views[sid]}
                  for sid, p in paths.items()},
    }


# --------------------------------------------------------------------------------------------------- validation

def _fail(msg_it: str, msg_en: str):
    raise ValueError(scegli(msg_it, msg_en))


def coerce(f: Field, raw: Any) -> Any:
    """The value checked and converted to its final type; ValueError (in the active language) if it is not valid."""
    if f.kind in ("text", "secret", "multiline"):
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            _fail("Serve un testo.", "A text is required.")
        if f.kind != "multiline":
            if "\n" in raw or "\r" in raw:
                _fail("Il valore deve stare su una riga.", "The value must be on one line.")
            raw = raw.strip() if f.kind == "text" else raw
        return raw
    if f.kind == "choice":
        if raw not in f.choices:
            _fail(f"Valore non ammesso: scegliere tra {', '.join(c or '(vuoto)' for c in f.choices)}.",
                  f"Value not allowed: choose one of {', '.join(c or '(empty)' for c in f.choices)}.")
        return raw
    if f.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if raw in (0, 1, "0", "1", "true", "false"):
            return raw in (1, "1", "true")
        _fail("Serve vero o falso.", "True or false is required.")
    if f.kind in ("int", "float"):
        if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
            _fail("Serve un numero.", "A number is required.")
        try:
            number = int(str(raw).strip()) if f.kind == "int" else float(str(raw).strip())
        except ValueError:
            _fail("Serve un numero valido.", "A valid number is required.")
        if number != number or number in (float("inf"), float("-inf")):
            _fail("Serve un numero finito.", "A finite number is required.")
        if f.minimum is not None and number < f.minimum:
            _fail(f"Il valore minimo è {f.minimum:g}.", f"The minimum is {f.minimum:g}.")
        if f.maximum is not None and number > f.maximum:
            _fail(f"Il valore massimo è {f.maximum:g}.", f"The maximum is {f.maximum:g}.")
        return number
    if f.kind == "list":
        if isinstance(raw, str):
            raw = [p for p in (s.strip() for s in raw.split(",")) if p]
        if not isinstance(raw, list):
            _fail("Serve un elenco.", "A list is required.")
        try:
            return [float(v) for v in raw] if f.items == "float" else [str(v).strip() for v in raw if str(v).strip()]
        except ValueError:
            _fail("L'elenco deve contenere solo numeri.", "The list must contain only numbers.")
    raise AssertionError(f.kind)


def _env_text(f: Field, value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _check_toml(source: str, path: Path, text: str) -> None:
    try:
        if source == "config":
            costruisci_configurazione(tomllib.loads(text))
        else:
            # The loaders read from a path: validate a copy with the real file's name, never the real one.
            with tempfile.TemporaryDirectory() as tmp:
                probe = Path(tmp) / path.name
                probe.write_text(text, encoding="utf-8")
                sources()[source].valida(probe)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(scegli(f"TOML non valido: {e}", f"Invalid TOML: {e}")) from e
    except (ErroreConfigurazione, ValueError, TypeError) as e:
        raise ValueError(str(e)) from e


def _apply(f: Field, text: str, value: Any) -> str:
    if f.source == "env":
        return set_env_value(text, f.key, _env_text(f, value))
    if f.source == "config":
        style = _style(text)
        sections = tuple({f.names["it"][0], f.names["en"][0]})
        keys = (f.names["it"][1], f.names["en"][1])
        section_new, key_new = f.names[style]
        if f.kind == "choice" and style == "it":
            value = _VALORI_EN.get(f.names["it"], {}).get(value, value)  # the page speaks English, the file Italian
        if f.kind == "bool" and f.flag_as_int:
            value = int(value)
        if f.optional and value in ("", None):
            value = None
        return set_toml_value(text, sections, keys, value, new_section=section_new, new_key=key_new)
    # a node's file: top level only for a flat file (no header of its section) that already has assignments
    section = sources()[f.source].sezione
    has_header = re.search(rf"^\s*\[{section}\]", text, re.M) is not None
    flat = not has_header and re.search(r"^[A-Za-z0-9_]+\s*=", text, re.M) is not None
    if f.optional and value in ("", None):
        value = None
    return set_toml_value(text, ("" if flat else section,), (f.key,), value, new_section=section, new_key=f.key)


# --------------------------------------------------------------------------------------------------- writing

def _write(path: Path, text: str) -> None:
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save(changes: dict[str, Any]) -> dict[str, Any]:
    """
    Applies {field id: value}. All or nothing: any invalid value or any file that the real loader would reject raises
    SettingsError and no file is touched. Returns the ids saved and those that need a restart to take effect.
    """
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}
    known = by_id()
    for fid, raw in changes.items():
        f = known.get(fid)
        if f is None:
            errors[fid] = scegli("Impostazione sconosciuta.", "Unknown setting.")
            continue
        try:
            values[fid] = coerce(f, raw)
        except ValueError as e:
            errors[fid] = str(e)
    if errors:
        raise SettingsError(errors)
    new_texts: dict[str, str] = {}
    for fid, value in values.items():
        f = known[fid]
        path = _path_of(f.source)
        new_texts[f.source] = _apply(f, new_texts.get(f.source, _read(path)), value)
    for source, text in new_texts.items():
        if source != "env":
            try:
                _check_toml(source, _path_of(source), text)
            except ValueError as e:
                culprit = {fid: str(e) for fid in values if known[fid].source == source}
                raise SettingsError(culprit) from e
    for source, text in new_texts.items():
        try:
            _write(_path_of(source), text)
        except OSError as e:
            raise SettingsError({"": scegli(f"Impossibile scrivere {_path_of(source).name}: {e}", f"Cannot write {_path_of(source).name}: {e}")}) from e
    for fid, value in values.items():
        f = known[fid]
        if f.source == "env":
            os.environ[f.key] = _env_text(f, value)  # effective now; the .env reload of the MAO sees the same value
    if any(known[fid].source == "env" for fid in values):
        _notify_model_access()
    return {"saved": sorted(values), "restart": sorted(fid for fid in values if known[fid].restart)}


def _notify_model_access() -> None:
    """
    The model access object builds its provider clients (keys included) when it reads the environment: tell it that the
    .env changed, so that a key typed in the settings page is used at the next call, without a restart.
    """
    try:
        from app.MAO.model_access_object import ricarica_env_se_modificato
        ricarica_env_se_modificato()
    except Exception:  # never fail a save because of the notification
        pass


def restore_backups() -> list[str]:
    """Puts back the version before the last save of each file that has a `.bak`; the current one becomes the `.bak`."""
    restored = []
    for source in source_ids():
        path = _path_of(source)
        backup = path.with_name(path.name + ".bak")
        if backup.is_file():
            current = _read(path)
            shutil.copy2(backup, path)
            backup.write_text(current, encoding="utf-8")
            restored.append(path.name)
    if restored:
        for key, value in (dotenv_values(env_path()) if env_path().is_file() else {}).items():
            if value is not None and key in {f.key for f in fields() if f.source == "env"}:
                os.environ[key] = value
        _notify_model_access()
    return restored
