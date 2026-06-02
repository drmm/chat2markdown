from __future__ import annotations

import configparser
import os
import re
from pathlib import Path
from typing import Iterable


# Change this one line if no-argument runs should use a different config file.
default_config_file = "config.ini"

# Fallback used only when default_config_file is missing.
fallback_config_file = "config.example.ini"

CONFIG_FILENAMES = (default_config_file, fallback_config_file)


def find_config_path(config_ini: str | None = None) -> Path:
    if config_ini:
        path = Path(config_ini)
        return path if path.is_absolute() else (Path.cwd() / path).resolve()

    here = Path(__file__).resolve().parent
    for filename in CONFIG_FILENAMES:
        path = here / filename
        if path.exists():
            return path
    return here / "config.example.ini"


class ChatMainConfig:
    def __init__(self, path: Path, parser: configparser.ConfigParser):
        self.path = path
        self.base_dir = path.parent
        self.parser = parser

    @property
    def tokens(self) -> dict[str, str]:
        home = Path.home()
        tokens = {
            "USER": str(home),
            "HOME": str(home),
            "APPDATA": os.environ.get("APPDATA", ""),
            "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
            "CONFIG_DIR": str(self.base_dir),
            "PROJECT_ROOT": str(self.base_dir),
        }
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if system_root:
            tokens["SYSTEMROOT"] = system_root
            tokens["WINDIR"] = system_root
        system_drive = os.environ.get("SystemDrive")
        if system_drive:
            drive_name = system_drive.rstrip(":\\/").upper()
            tokens[f"DRIVE_{drive_name}"] = system_drive.rstrip("\\/") + "\\"
        return tokens

    def get(self, section: str, key: str, fallback: object = "") -> str:
        if self.parser.has_option(section, key):
            return self.parser.get(section, key)
        if self.parser.has_option("DEFAULT", key):
            return self.parser.get("DEFAULT", key)
        return str(fallback)

    def get_bool(self, section: str, key: str, fallback: bool = False) -> bool:
        value = self.get(section, key, fallback)
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    def get_int(self, section: str, key: str, fallback: int = 0) -> int:
        value = self.get(section, key, fallback)
        return int(str(value).strip())

    def get_float(self, section: str, key: str, fallback: float = 0.0) -> float:
        value = self.get(section, key, fallback)
        return float(str(value).strip())

    def get_list(self, section: str, key: str, fallback: Iterable[str] | None = None) -> list[str]:
        configured = self.parser.has_option(section, key) or self.parser.has_option("DEFAULT", key)
        raw = self.get(section, key, "")
        if not str(raw).strip():
            return [] if configured else list(fallback or [])
        pieces = re.split(r"[;\n,]+", str(raw))
        return [piece.strip().strip('"\'') for piece in pieces if piece.strip()]

    def expand_tokens(self, value: object) -> str:
        text = str(value or "").strip().strip('"\'')
        if not text:
            return ""

        for name, replacement in self.tokens.items():
            text = text.replace(f"%{name}%", replacement)
            text = text.replace(f"${{{name}}}", replacement)

        def replace_env(match: re.Match[str]) -> str:
            name = match.group(1)
            return os.environ.get(name, match.group(0))

        text = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", replace_env, text)
        return os.path.expandvars(text)

    def resolve_path(self, value: object, fallback: object = "") -> Path:
        raw = str(value or "").strip() or str(fallback or "").strip()
        expanded = self.expand_tokens(raw)
        if not expanded:
            return self.base_dir
        path = Path(expanded).expanduser()
        if path.is_absolute():
            return path
        return (self.base_dir / path).resolve()

    def resolve_path_list(self, section: str, key: str, fallback: Iterable[str] | None = None) -> list[Path]:
        return [self.resolve_path(item) for item in self.get_list(section, key, fallback)]

    def display_path(self, value: object) -> str:
        path = Path(value)
        try:
            return path.resolve().relative_to(self.base_dir.resolve()).as_posix()
        except ValueError:
            pass

        resolved = str(path.resolve())
        for token, replacement in self.tokens.items():
            if not replacement:
                continue
            root = str(Path(replacement).resolve())
            if resolved.lower().startswith(root.lower()):
                suffix = resolved[len(root) :].lstrip("\\/")
                return f"%{token}%/{suffix.replace(os.sep, '/')}" if suffix else f"%{token}%"
        if path.is_absolute() and path.drive:
            drive_name = path.drive.rstrip(":").upper()
            suffix = str(path.resolve())[len(path.drive) :].lstrip("\\/")
            return f"%DRIVE_{drive_name}%/{suffix.replace(os.sep, '/')}" if suffix else f"%DRIVE_{drive_name}%"
        return str(path)


def load_config(config_ini: str | None = None) -> ChatMainConfig:
    path = find_config_path(config_ini)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return ChatMainConfig(path, parser)


def copy_section(settings: dict[str, object], cfg: ChatMainConfig, section: str, aliases: dict[str, str] | None = None) -> dict[str, object]:
    if not cfg.parser.has_section(section):
        return settings
    aliases = aliases or {}
    source = cfg.parser[section]
    for key in list(settings):
        ini_key = key
        if ini_key in source:
            settings[key] = source[ini_key]
    for ini_key, target_key in aliases.items():
        if ini_key in source and target_key in settings:
            settings[target_key] = source[ini_key]
    return settings
