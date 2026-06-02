# import section
import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chatmain_config import ChatMainConfig, load_config

# config section
exclude_all_messages_from = [] #added
Apply_filter_to_messages_from = ["Assistant", "User", "Claude"]
date_time_after = "2026-05-25"
dates_time_before = ""
folder_path = "./filtered"
export_excluded_to_path = ""
import_file = "./archive/claude/chats/example.md"
characters_min = 150
message_lines_min = 2
default_timezone = "America/New_York"
deduplicate_messages = True
exclude_tool_messages = True
output_mode = "filtered_only" # "all" writes filter + excluded + summary + split files
write_split_files = False
overwrite_existing = False

# code below here
HEADER_RE = re.compile(r"^##\s+Message\s+(\d+)\s*--\s*(.*?)\s*$")
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TOOL_RE = re.compile(r"^\s*\[Tool:\s*", re.IGNORECASE)
BASE_DIR = Path(__file__).resolve().parent

@dataclass
class ChatMessage:
    message_id: int
    role: str
    timestamp_raw: str
    timestamp: datetime | None
    body: str
    start_line: int
    end_line: int
    char_count: int
    nonempty_line_count: int
    is_tool: bool


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, list):
                return [str(item).strip() for item in loaded if str(item).strip()]
        except json.JSONDecodeError:
            pass
    parts = re.split(r"[,\n]+", text) if "," in text or "\n" in text else text.split()
    return [part.strip().strip('"\'') for part in parts if part.strip()]


def parse_datetime_value(value: object, timezone_name: str, date_only_end: bool = False, blank_is_now: bool = False) -> datetime | None:
    text = str(value).strip().strip('"\'') if value is not None else ""
    zone = ZoneInfo(timezone_name)
    if not text:
        return datetime.now(zone) if blank_is_now else None
    if DATE_ONLY_RE.match(text):
        clock = (23, 59, 59) if date_only_end else (0, 0, 1)
        base = datetime.strptime(text, "%Y-%m-%d")
        return base.replace(hour=clock[0], minute=clock[1], second=clock[2], tzinfo=zone)
    parts = text.rsplit(" ", 1)
    if len(parts) == 2 and "/" in parts[1]:
        zone = ZoneInfo(parts[1])
        return datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed


def default_settings() -> dict[str, object]:
    return {
        "exclude_all_messages_from": exclude_all_messages_from,
        "Apply_filter_to_messages_from": Apply_filter_to_messages_from,
        "date_time_after": date_time_after,
        "dates_time_before": dates_time_before,
        "folder_path": folder_path,
        "export_excluded_to_path": export_excluded_to_path,
        "import_file": import_file,
        "characters_min": characters_min,
        "message_lines_min": message_lines_min,
        "default_timezone": default_timezone,
        "deduplicate_messages": deduplicate_messages,
        "exclude_tool_messages": exclude_tool_messages,
        "output_mode": output_mode,
        "write_split_files": write_split_files,
        "overwrite_existing": overwrite_existing,
    }


def coerce_setting(key: str, value: object, current: object) -> object:
    if isinstance(current, bool):
        return parse_bool(value)
    if isinstance(current, int):
        return int(value)
    if isinstance(current, list):
        return parse_list(value)
    return str(value).strip().strip('"\'')


def load_ini(settings: dict[str, object], ini_path: str | None = None) -> tuple[dict[str, object], ChatMainConfig]:
    cfg = load_config(ini_path)
    section = cfg.parser["filter"] if cfg.parser.has_section("filter") else cfg.parser["DEFAULT"]
    aliases = {
        "date_time_before": "dates_time_before",
        "roles": "Apply_filter_to_messages_from",
        "from_roles": "Apply_filter_to_messages_from",
        "filter_roles": "Apply_filter_to_messages_from",
        "exclude_roles": "exclude_all_messages_from",
    }
    for key in list(settings):
        if key in section:
            settings[key] = coerce_setting(key, section[key], settings[key])
    for source, target in aliases.items():
        if source in section:
            settings[target] = coerce_setting(target, section[source], settings[target])
    if cfg.parser.has_option("paths", "filter_output_dir"):
        settings["folder_path"] = cfg.parser.get("paths", "filter_output_dir")
    return settings, cfg


def apply_cli(settings: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    for key, value in vars(args).items():
        if key == "config_ini" or value is None:
            continue
        if key in settings:
            settings[key] = value
    return settings


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Filter exported Markdown chat transcripts into review-ready files.")
    parser.add_argument("--config-ini", "--ini", dest="config_ini", default=None)
    parser.add_argument("--import-file", dest="import_file", default=None)
    parser.add_argument("--folder-path", dest="folder_path", default=None)
    parser.add_argument("--export-excluded-to-path", dest="export_excluded_to_path", default=None)
    parser.add_argument("--from-roles", "--roles", nargs="+", dest="Apply_filter_to_messages_from", default=None)
    parser.add_argument("--exclude-all-messages-from", "--exclude-roles", nargs="+", dest="exclude_all_messages_from", default=None)
    parser.add_argument("--date-time-after", dest="date_time_after", default=None)
    parser.add_argument("--date-time-before", dest="dates_time_before", default=None)
    parser.add_argument("--characters-min", type=int, dest="characters_min", default=None)
    parser.add_argument("--message-lines-min", type=int, dest="message_lines_min", default=None)
    parser.add_argument("--default-timezone", dest="default_timezone", default=None)
    parser.add_argument("--deduplicate", dest="deduplicate_messages", action="store_true", default=None)
    parser.add_argument("--no-deduplicate", dest="deduplicate_messages", action="store_false")
    parser.add_argument("--exclude-tool-messages", dest="exclude_tool_messages", action="store_true", default=None)
    parser.add_argument("--include-tool-messages", dest="exclude_tool_messages", action="store_false")
    parser.add_argument("--output-mode", choices=["all", "filtered_only"], dest="output_mode", default=None)
    parser.add_argument("--write-split-files", dest="write_split_files", action="store_true", default=None)
    parser.add_argument("--no-split-files", dest="write_split_files", action="store_false")
    parser.add_argument("--overwrite-existing", dest="overwrite_existing", action="store_true", default=None)
    parser.add_argument("--no-overwrite-existing", dest="overwrite_existing", action="store_false", default=None)
    return parser


def relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def sanitize_filename(text: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._+-]+", "_", text).strip("._")
    return safe or "chat"


def unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def trim_body_lines(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "---":
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def parse_messages(markdown: str, timezone_name: str) -> list[ChatMessage]:
    lines = markdown.splitlines()
    messages: list[ChatMessage] = []
    index = 0
    while index < len(lines):
        match = HEADER_RE.match(lines[index])
        if not match:
            index += 1
            continue
        start_line = index + 1
        message_id = int(match.group(1))
        role = match.group(2).strip()
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        timestamp_raw = ""
        if index < len(lines) and lines[index].strip().startswith("*") and lines[index].strip().endswith("*"):
            timestamp_raw = lines[index].strip().strip("*").strip()
            index += 1
        body_lines: list[str] = []
        while index < len(lines) and not HEADER_RE.match(lines[index]):
            body_lines.append(lines[index])
            index += 1
        end_line = index if index else len(lines)
        body_lines = trim_body_lines(body_lines)
        body = "\n".join(body_lines).strip("\n")
        timestamp = parse_datetime_value(timestamp_raw, timezone_name) if timestamp_raw else None
        char_count = len(body.strip())
        nonempty_line_count = sum(1 for line in body.splitlines() if line.strip())
        is_tool = bool(TOOL_RE.match(body))
        messages.append(ChatMessage(message_id, role, timestamp_raw, timestamp, body, start_line, end_line, char_count, nonempty_line_count, is_tool))
    return messages


def normalized_body_hash(message: ChatMessage) -> str:
    normalized_body = re.sub(r"\s+", " ", message.body.strip()).lower()
    return hashlib.sha256(f"{message.role.lower()}::{normalized_body}".encode("utf-8")).hexdigest()


def filter_messages(messages: list[ChatMessage], settings: dict[str, object]) -> tuple[list[ChatMessage], list[tuple[ChatMessage, list[str]]], Counter]:
    filter_roles = {role.lower() for role in parse_list(settings["Apply_filter_to_messages_from"])}
    exclude_roles = {role.lower() for role in parse_list(settings["exclude_all_messages_from"])}
    after = parse_datetime_value(settings["date_time_after"], str(settings["default_timezone"]), date_only_end=False, blank_is_now=False)
    before = parse_datetime_value(settings["dates_time_before"], str(settings["default_timezone"]), date_only_end=True, blank_is_now=True)
    seen: set[str] = set()
    included: list[ChatMessage] = []
    excluded: list[tuple[ChatMessage, list[str]]] = []
    reason_counts: Counter = Counter()
    for message in messages:
        reasons: list[str] = []
        role = message.role.lower()
        if role in exclude_roles:
            reasons.append("role_excluded")
        elif role in filter_roles:
            digest = normalized_body_hash(message)
            if after and message.timestamp and message.timestamp < after:
                reasons.append("before_date_time_after")
            if before and message.timestamp and message.timestamp > before:
                reasons.append("after_date_time_before")
            if int(settings["characters_min"]) > 0 and message.char_count < int(settings["characters_min"]):
                reasons.append("below_characters_min")
            if int(settings["message_lines_min"]) > 0 and message.nonempty_line_count < int(settings["message_lines_min"]):
                reasons.append("below_message_lines_min")
            if parse_bool(settings["exclude_tool_messages"]) and message.is_tool:
                reasons.append("tool_message")
            if parse_bool(settings["deduplicate_messages"]) and digest in seen:
                reasons.append("duplicate_message")
            seen.add(digest)
        if reasons:
            excluded.append((message, reasons))
            reason_counts.update(reasons)
        else:
            included.append(message)
    return included, excluded, reason_counts


def format_message(message: ChatMessage, reasons: list[str] | None = None) -> str:
    lines = [f"## Message {message.message_id} -- {message.role}", f"*{message.timestamp_raw}*", ""]
    if reasons:
        lines.extend([f"Excluded because: {', '.join(reasons)}", ""])
    lines.extend([message.body.strip(), "", "---"])
    return "\n".join(lines).rstrip() + "\n"


def write_markdown(path: Path, title: str, source_path: Path, messages: list[ChatMessage], settings: dict[str, object], excluded: list[tuple[ChatMessage, list[str]]] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", "", f"Source: `{relpath(source_path)}`", f"Generated: `{datetime.now(ZoneInfo(str(settings['default_timezone']))).isoformat(timespec='seconds')}`", f"Apply filters to roles: `{', '.join(parse_list(settings['Apply_filter_to_messages_from'])) or 'none'}`", f"Exclude all messages from roles: `{', '.join(parse_list(settings['exclude_all_messages_from'])) or 'none'}`", f"Date/time after: `{settings['date_time_after']}`", f"Date/time before: `{settings['dates_time_before'] or 'now'}`", f"characters_min: `{settings['characters_min']}`", f"message_lines_min: `{settings['message_lines_min']}`", f"deduplicate_messages: `{settings['deduplicate_messages']}`", f"exclude_tool_messages: `{settings['exclude_tool_messages']}`", ""]
    if excluded is not None:
        for message, reasons in excluded:
            lines.append(format_message(message, reasons).rstrip())
            lines.append("")
    else:
        for message in messages:
            lines.append(format_message(message).rstrip())
            lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_outputs(source_path: Path, messages: list[ChatMessage], included: list[ChatMessage], excluded: list[tuple[ChatMessage, list[str]]], reason_counts: Counter, settings: dict[str, object]) -> dict[str, object]:
    output_dir = Path(str(settings["folder_path"]))
    excluded_dir = Path(str(settings["export_excluded_to_path"])) if str(settings["export_excluded_to_path"]).strip() else output_dir / "excluded"
    split_dir = output_dir / "split"
    base = sanitize_filename(source_path.stem)
    selected_path = output_dir / f"{base}+filter.md"
    excluded_path = excluded_dir / f"{base}+excluded.md"
    summary_path = output_dir / f"{base}+filter_summary.json"
    output_mode = str(settings.get("output_mode", "all")).strip().lower()
    if output_mode not in {"all", "filtered_only"}:
        raise ValueError(f"Unknown output_mode: {output_mode}")
    excluded_file: str | None = None
    summary_file: str | None = None
    split_files: dict[str, str] = {}
    avoid_overwrite = not parse_bool(settings.get("overwrite_existing", False))
    if avoid_overwrite:
        selected_path = unique_output_path(selected_path)
        excluded_path = unique_output_path(excluded_path)
        summary_path = unique_output_path(summary_path)
    write_markdown(selected_path, f"{base}+filter", source_path, included, settings)
    if output_mode == "all":
        write_markdown(excluded_path, f"{base}+excluded", source_path, [], settings, excluded=excluded)
        excluded_file = relpath(excluded_path)
    if output_mode == "all" and parse_bool(settings["write_split_files"]):
        grouped: dict[str, list[ChatMessage]] = defaultdict(list)
        for message in included:
            grouped[message.role].append(message)
        for role, role_messages in grouped.items():
            split_path = split_dir / f"{base}+filter_{sanitize_filename(role)}.md"
            if avoid_overwrite:
                split_path = unique_output_path(split_path)
            write_markdown(split_path, f"{base}+filter_{role}", source_path, role_messages, settings)
            split_files[role] = relpath(split_path)
    summary_settings = dict(settings)
    for path_key in ("folder_path", "export_excluded_to_path", "import_file"):
        if str(summary_settings.get(path_key, "")).strip():
            summary_settings[path_key] = relpath(Path(str(summary_settings[path_key])))
    summary = {
        "source_file": relpath(source_path),
        "selected_file": relpath(selected_path),
        "excluded_file": excluded_file,
        "summary_file": summary_file,
        "split_files": split_files,
        "total_messages": len(messages),
        "included_messages": len(included),
        "excluded_messages": len(excluded),
        "included_by_role": Counter(message.role for message in included),
        "excluded_reason_counts": reason_counts,
        "settings": summary_settings,
    }
    if output_mode == "all":
        summary_file = relpath(summary_path)
        summary["summary_file"] = summary_file
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def print_summary(summary: dict[str, object]) -> None:
    print(json.dumps(summary, indent=2, default=str))


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    settings = default_settings()
    settings, cfg = load_ini(settings, args.config_ini)
    settings = apply_cli(settings, args)
    settings["folder_path"] = str(cfg.resolve_path(settings["folder_path"]))
    if str(settings["export_excluded_to_path"]).strip():
        settings["export_excluded_to_path"] = str(cfg.resolve_path(settings["export_excluded_to_path"]))
    source_path = cfg.resolve_path(settings["import_file"])
    if not source_path.exists():
        print(f"Input file not found: {source_path}", file=sys.stderr)
        return 2
    markdown = source_path.read_text(encoding="utf-8")
    messages = parse_messages(markdown, str(settings["default_timezone"]))
    included, excluded, reason_counts = filter_messages(messages, settings)
    summary = write_outputs(source_path, messages, included, excluded, reason_counts, settings)
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
