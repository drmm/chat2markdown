#!/usr/bin/env python3
"""
Export chats from VSCode/Cursor/Claude Code/Codex to markdown files.
Standalone Python version of the VSCode extension.

Quick single-file test:
  python exportChats.py --input-file "%USER%/.codex/sessions/example.jsonl"

Batch export:
python exportChats.py --claude          # incremental export (new sessions only)
python exportChats.py --all --claude    # everything
  python exportChats.py --codex
  python exportChats.py --vscode
  python exportChats.py --cursor
  python exportChats.py --all
  python exportChats.py --rename-uuids
  python exportChats.py --input-file "%USER%/path/to/file.jsonl"  (one file, CLI style)
"""

import os
import re
import json
import csv
import shutil
import hashlib
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from chatmain_config import load_config
from title_chat import configure_titles, get_title_for_chat

# Regex to detect UUID-like filenames
UUID_PATTERN = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', re.IGNORECASE)
CODEX_SESSION_ID_PATTERN = re.compile(
    r'^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<id>[0-9a-f-]{36})\.jsonl$',
    re.IGNORECASE,
)

RUN_INDIVIDUAL_FILE = False
RUN_INDIVIDUAL_PATH = ""

# Configuration paths
VSCODE_CHAT_PATH = "%APPDATA%/Code/User/workspaceStorage"
CURSOR_CHAT_PATH = "%APPDATA%/Cursor/User/workspaceStorage"
CURSOR_DB_PATH = "%APPDATA%/Cursor/User/globalStorage/state.vscdb"
DEFAULT_EXPORT_VSCODE = "./vscodechat"
DEFAULT_EXPORT_CURSOR = "./cursorcodechat"

# Claude Code configuration
CLAUDE_PROJECTS_PATH = "%USER%/.claude/projects"
DEFAULT_EXPORT_CLAUDE = "./claudecodechat"

# Codex configuration
CODEX_SESSIONS_PATH = "%USER%/.codex/sessions"
DEFAULT_EXPORT_CODEX = "./codexchat"

# Unified archive configuration
DEFAULT_ARCHIVE_ROOT = "./archive"
DEFAULT_ARCHIVE_VSCODE = "./archive/vscode/chats"
DEFAULT_ARCHIVE_CURSOR = "./archive/cursor/chats"
DEFAULT_ARCHIVE_CLAUDE_CHATS = "./archive/claude/chats"
DEFAULT_ARCHIVE_CLAUDE_PLANS = "./archive/claude/plans"
DEFAULT_ARCHIVE_CODEX_CHATS = "./archive/codex/chats"
DEFAULT_ARCHIVE_CODEX_PLANS = "./archive/codex/plans"
DEFAULT_ARCHIVE_COPILOT_CLI_CHATS = "./archive/copilot-cli/chats"
DEFAULT_ARCHIVE_COPILOT_CLI_PLANS = "./archive/copilot-cli/plans"
DEFAULT_ARCHIVE_NIA_PLANS = "./archive/nia/plans"
DEFAULT_INDEX_PATH = "./archive/index.csv"
MIN_ARCHIVE_ARTIFACT_BYTES = 1024
LOCAL_TIME_ZONE = ZoneInfo("America/New_York")
LOCAL_TIME_ZONE_LABEL = "America/New_York"

CLAUDE_PLANS_PATH = "%USER%/.claude/plans"
CLAUDE_PLAN_MIRROR_PATH = ""
COPILOT_CLI_STATE_PATH = "%USER%/.copilot/session-state"
NIA_PLAN_PATHS = [
    "../../github/nia/WireFrameGoogleNIAMerged/docs",
    "../../github/nia/WireFrameGoogleNIAMerged/chat",
    "../../github/nia/docs",
]

_display_path = str


def display_path(value):
    return _display_path(value)


def parse_datetime_setting(value, timezone_name=LOCAL_TIME_ZONE_LABEL, date_only_end=False):
    """Parse a config/CLI date value into a timezone-aware datetime."""
    text = str(value or "").strip().strip('"\'')
    if not text:
        return None
    zone = ZoneInfo(timezone_name)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        clock = (23, 59, 59) if date_only_end else (0, 0, 0)
        base = datetime.strptime(text, "%Y-%m-%d")
        return base.replace(hour=clock[0], minute=clock[1], second=clock[2], tzinfo=zone)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def parse_optional_int(value):
    text = str(value or "").strip()
    return int(text) if text else None


def build_modified_window(cfg, section="export"):
    """Return the source-file modified date window configured for exports."""
    timezone_name = cfg.get("export", "default_timezone", LOCAL_TIME_ZONE_LABEL)
    modified_after = parse_datetime_setting(cfg.get(section, "modified_after", ""), timezone_name)
    modified_before = parse_datetime_setting(cfg.get(section, "modified_before", ""), timezone_name, date_only_end=True)
    lookback_days = parse_optional_int(cfg.get(section, "lookback_days", ""))
    if modified_after is None and lookback_days is not None:
        modified_after = datetime.now(ZoneInfo(timezone_name)) - timedelta(days=lookback_days)
    return modified_after, modified_before


def path_modified_in_window(path, modified_after=None, modified_before=None):
    if not modified_after and not modified_before:
        return True
    try:
        modified = datetime.fromtimestamp(Path(path).stat().st_mtime, ZoneInfo(LOCAL_TIME_ZONE_LABEL))
    except Exception:
        return True
    if modified_after and modified < modified_after:
        return False
    if modified_before and modified > modified_before:
        return False
    return True


def timestamp_in_window(timestamp, modified_after=None, modified_before=None):
    if not modified_after and not modified_before:
        return True
    if not timestamp:
        return True
    try:
        if isinstance(timestamp, (int, float)):
            raw_ts = timestamp / 1000 if timestamp > 1e12 else timestamp
            dt = datetime.fromtimestamp(raw_ts, timezone.utc).astimezone(ZoneInfo(LOCAL_TIME_ZONE_LABEL))
        else:
            dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo(LOCAL_TIME_ZONE_LABEL))
            else:
                dt = dt.astimezone(ZoneInfo(LOCAL_TIME_ZONE_LABEL))
    except Exception:
        return True
    if modified_after and dt < modified_after:
        return False
    if modified_before and dt > modified_before:
        return False
    return True


def _ensure_container_slot(container, key, next_key=None):
    """Ensure a nested dict/list slot exists for JSONL patch application."""
    if isinstance(key, int):
        if not isinstance(container, list):
            raise TypeError(f"Expected list for key {key}, got {type(container).__name__}")
        while len(container) <= key:
            container.append(None)
        if container[key] is None:
            container[key] = [] if isinstance(next_key, int) else {}
        return container[key]

    if not isinstance(container, dict):
        raise TypeError(f"Expected dict for key {key}, got {type(container).__name__}")
    if key not in container or container[key] is None:
        container[key] = [] if isinstance(next_key, int) else {}
    return container[key]


def _set_nested_value(container, path, value):
    """Set a nested value on a dict/list path."""
    if not path:
        raise ValueError("Path cannot be empty")

    current = container
    for index, key in enumerate(path[:-1]):
        current = _ensure_container_slot(current, key, path[index + 1])

    last_key = path[-1]
    if isinstance(last_key, int):
        if not isinstance(current, list):
            raise TypeError(f"Expected list for key {last_key}, got {type(current).__name__}")
        while len(current) <= last_key:
            current.append(None)
        current[last_key] = value
    else:
        if not isinstance(current, dict):
            raise TypeError(f"Expected dict for key {last_key}, got {type(current).__name__}")
        current[last_key] = value


def _get_or_create_list(container, path):
    """Return the list at the given path, creating it if needed."""
    if not path:
        if not isinstance(container, list):
            raise TypeError(f"Expected list root, got {type(container).__name__}")
        return container

    current = container
    for index, key in enumerate(path):
        next_key = path[index + 1] if index + 1 < len(path) else None
        if index == len(path) - 1:
            if isinstance(key, int):
                if not isinstance(current, list):
                    raise TypeError(f"Expected list for key {key}, got {type(current).__name__}")
                while len(current) <= key:
                    current.append(None)
                if current[key] is None:
                    current[key] = []
                elif not isinstance(current[key], list):
                    raise TypeError(f"Expected list at key {key}, got {type(current[key]).__name__}")
                return current[key]

            if not isinstance(current, dict):
                raise TypeError(f"Expected dict for key {key}, got {type(current).__name__}")
            if key not in current or current[key] is None:
                current[key] = []
            elif not isinstance(current[key], list):
                raise TypeError(f"Expected list at key {key}, got {type(current[key]).__name__}")
            return current[key]

        current = _ensure_container_slot(current, key, next_key)

    raise ValueError("Unable to resolve list path")


def apply_jsonl_patch(state, entry):
    """Apply one VS Code chat session JSONL patch line to the session state."""
    if not isinstance(entry, dict):
        return state

    kind = entry.get('kind')
    if kind == 0:
        base_value = entry.get('v')
        return base_value if isinstance(base_value, dict) else state

    if kind == 1:
        path = entry.get('k') or []
        _set_nested_value(state, path, entry.get('v'))
        return state

    if kind == 2:
        path = entry.get('k') or []
        insert_at = entry.get('i')
        values = entry.get('v') or []
        target_list = _get_or_create_list(state, path)
        if insert_at is None:
            insert_at = len(target_list)
        target_list[insert_at:insert_at] = values
        return state

    return state


def load_jsonl_session(chat_file_path):
    """Load the newer VS Code/Copilot chat session JSONL format into one session dict."""
    state = {}
    with open(chat_file_path, 'r', encoding='utf-8') as file_handle:
        for line_num, raw_line in enumerate(file_handle, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                print(f"  Warning: invalid JSON at line {line_num} in {Path(chat_file_path).name}")
                continue
            state = apply_jsonl_patch(state, entry)
    return state


def load_chat_session(chat_file_path):
    """Load either legacy JSON chat files or newer JSONL chat session files."""
    chat_file_path = Path(chat_file_path)
    if chat_file_path.suffix.lower() == '.jsonl':
        return load_jsonl_session(chat_file_path)

    with open(chat_file_path, 'r', encoding='utf-8') as file_handle:
        return json.load(file_handle)


# ── Claude Code session helpers ──────────────────────────────────────


def stream_claude_jsonl(filepath):
    """Yield parsed JSON records from a Claude Code JSONL file line-by-line."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
        for line_num, raw_line in enumerate(fh, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f"  Warning: invalid JSON at line {line_num} in {Path(filepath).name}")


def extract_claude_session_metadata(filepath):
    """Single streaming pass to extract session metadata without storing messages."""
    filepath = Path(filepath)
    meta = {
        'ai_title': None, 'session_id': filepath.stem,
        'project_slug': filepath.parent.name,
        'first_timestamp': None, 'last_timestamp': None,
        'model': None, 'git_branch': None, 'cwd': None,
        'version': None, 'entrypoint': None, 'slug': None,
        'first_user_text': None,
        'user_msg_count': 0, 'assistant_msg_count': 0,
        'has_real_content': False,
    }

    for record in stream_claude_jsonl(filepath):
        rtype = record.get('type')

        if rtype == 'ai-title':
            meta['ai_title'] = record.get('aiTitle')
            continue

        ts = record.get('timestamp')
        if ts:
            if meta['first_timestamp'] is None:
                meta['first_timestamp'] = ts
            meta['last_timestamp'] = ts

        if rtype == 'user':
            content = record.get('message', {}).get('content', [])
            has_text = any(
                isinstance(b, dict) and b.get('type') == 'text' and b.get('text', '').strip()
                for b in content
            )
            if has_text:
                meta['user_msg_count'] += 1
                meta['has_real_content'] = True
                if meta['first_user_text'] is None:
                    raw_texts = [b.get('text', '') for b in content
                                 if isinstance(b, dict) and b.get('type') == 'text']
                    joined = ' '.join(raw_texts)
                    joined = re.sub(r'<system-reminder>.*?</system-reminder>', '', joined, flags=re.DOTALL)
                    joined = re.sub(r'<ide_opened_file>.*?</ide_opened_file>', '', joined, flags=re.DOTALL)
                    joined = re.sub(r'<[^>]+>', '', joined)  # strip any remaining XML/HTML tags
                    joined = re.sub(r'\s+', ' ', joined)  # collapse whitespace/newlines
                    cleaned = joined.strip()[:80].strip()
                    # Trim to last word boundary if possible
                    if len(joined.strip()) > 80 and ' ' in cleaned:
                        cleaned = cleaned[:cleaned.rfind(' ')]
                    if cleaned:
                        meta['first_user_text'] = cleaned
            if meta['git_branch'] is None:
                meta['git_branch'] = record.get('gitBranch')
                meta['cwd'] = record.get('cwd')
                meta['version'] = record.get('version')
                meta['entrypoint'] = record.get('entrypoint')
                meta['slug'] = record.get('slug')

        elif rtype == 'assistant':
            content = record.get('message', {}).get('content', [])
            has_text = any(
                isinstance(b, dict) and b.get('type') == 'text' and b.get('text', '').strip()
                for b in content
            )
            if has_text:
                meta['assistant_msg_count'] += 1
                meta['has_real_content'] = True
            if meta['model'] is None:
                meta['model'] = record.get('message', {}).get('model')

    return meta


def format_claude_tool_summary(block):
    """Create a one-line summary of a tool_use content block."""
    if not isinstance(block, dict) or block.get('type') != 'tool_use':
        return ""

    name = block.get('name', 'Unknown')
    inp = block.get('input', {})

    detail = ""
    if name == 'Read':
        fp = inp.get('file_path', '')
        detail = Path(fp).name if fp else ''
    elif name == 'Bash':
        cmd = inp.get('command', '')
        detail = cmd[:60] + ('...' if len(cmd) > 60 else '')
    elif name in ('Grep', 'GrepTool'):
        detail = f'pattern "{inp.get("pattern", "")[:40]}"'
    elif name in ('Glob', 'GlobTool'):
        detail = inp.get('pattern', '')[:40]
    elif name == 'Edit':
        fp = inp.get('file_path', '')
        detail = Path(fp).name if fp else ''
    elif name == 'Write':
        fp = inp.get('file_path', '')
        detail = Path(fp).name if fp else ''
    elif name == 'WebFetch':
        detail = inp.get('url', '')[:60]
    else:
        for v in inp.values():
            if isinstance(v, str) and v.strip():
                detail = v[:50]
                break

    if detail:
        return f"[Tool: {name} -- {detail}]"
    return f"[Tool: {name}]"


def extract_claude_text_content(content_blocks, include_tool_summaries=True):
    """Extract readable text from a Claude Code message content array."""
    parts = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue

        btype = block.get('type')

        if btype == 'text':
            text = block.get('text', '').strip()
            if text:
                parts.append(text)

        elif btype == 'tool_use' and include_tool_summaries:
            summary = format_claude_tool_summary(block)
            if summary:
                parts.append(summary)

        # Skip: tool_result, thinking, and anything else

    return '\n\n'.join(parts)


def process_claude_session(session_path, output_dir, project_slug, overwrite=False,
                           include_tool_summaries=True):
    """Process one Claude Code JSONL session file into a markdown file."""
    session_path = Path(session_path)
    output_dir = Path(output_dir)

    # Phase 1: metadata scan
    meta = extract_claude_session_metadata(session_path)

    if not meta['has_real_content']:
        return False, f"SKIP (empty): {meta['session_id']}"

    # Title priority: ai-title > first user message > slug > session_id
    title = meta['ai_title'] or meta['first_user_text'] or meta['slug'] or meta['session_id']
    safe_title = sanitize_filename(title)

    output_file = output_dir / f"{safe_title}.md"
    if output_file.exists() and not overwrite:
        if is_small_artifact_path(output_file):
            return False, f"SKIP (exists-small): {safe_title}"
        else:
            existing_header = read_markdown_header(output_file)
            if existing_header.get('Session ID') == meta['session_id']:
                if archive_file_is_current_for_source(output_file, session_path):
                    return False, f"SKIP (current): {safe_title}"
            else:
                # Collision: another session has the same title — append counter
                counter = 2
                while output_file.exists():
                    output_file = output_dir / f"{safe_title}_{counter}.md"
                    if is_small_artifact_path(output_file):
                        return False, f"SKIP (exists-small): {output_file.stem}"
                    existing_header = read_markdown_header(output_file)
                    if existing_header.get('Session ID') == meta['session_id']:
                        if archive_file_is_current_for_source(output_file, session_path):
                            return False, f"SKIP (current): {output_file.stem}"
                        break
                    counter += 1
                safe_title = output_file.stem

    # Phase 2: content extraction (second streaming pass)
    md_lines = []

    md_lines.append(f"# {title}\n")
    md_lines.append(f"- **Source:** Claude Code")
    md_lines.append(f"- **Project:** {project_slug}")
    if meta['model']:
        md_lines.append(f"- **Model:** {meta['model']}")
    if meta['git_branch']:
        md_lines.append(f"- **Branch:** {meta['git_branch']}")
    if meta['entrypoint']:
        md_lines.append(f"- **Entrypoint:** {meta['entrypoint']}")
    if meta['version']:
        md_lines.append(f"- **Claude Version:** {meta['version']}")
    md_lines.append(f"- **Messages:** {meta['user_msg_count']} user, {meta['assistant_msg_count']} assistant")

    first_ts = format_timestamp(meta['first_timestamp'])
    last_ts = format_timestamp(meta['last_timestamp'])
    if first_ts:
        md_lines.append(f"- **Started:** {first_ts}")
    if last_ts:
        md_lines.append(f"- **Last activity:** {last_ts}")
    if meta['cwd']:
        md_lines.append(f"- **Working dir:** {display_path(meta['cwd'])}")
    md_lines.append(f"- **Session ID:** {meta['session_id']}")
    md_lines.append(f"- **Source file:** {display_path(session_path)}")
    md_lines.append("")

    msg_num = 0
    for record in stream_claude_jsonl(session_path):
        rtype = record.get('type')
        if rtype not in ('user', 'assistant'):
            continue

        if record.get('isSidechain'):
            continue

        content_blocks = record.get('message', {}).get('content', [])

        if rtype == 'user':
            has_user_text = any(
                isinstance(b, dict) and b.get('type') == 'text' and b.get('text', '').strip()
                for b in content_blocks
            )
            if not has_user_text:
                continue

        text = extract_claude_text_content(content_blocks, include_tool_summaries)
        if not text:
            continue

        msg_num += 1
        role = "User" if rtype == 'user' else "Claude"
        ts = format_timestamp(record.get('timestamp'))

        md_lines.append(f"## Message {msg_num} -- {role}")
        if ts:
            md_lines.append(f"*{ts}*\n")
        else:
            md_lines.append("")

        md_lines.append(text)
        md_lines.append("")
        md_lines.append("---\n")

    if msg_num == 0:
        return False, f"SKIP (no messages): {meta['session_id']}"

    markdown_text = '\n'.join(md_lines)
    if markdown_text_size_bytes(markdown_text) < MIN_ARCHIVE_ARTIFACT_BYTES:
        return False, f"SKIP (<1KB): {meta['session_id']}"

    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(markdown_text)

    preserve_modification_time(session_path, output_file)

    return True, safe_title


def export_claude_sessions(output_path, projects_path=None, sample_per_project=0,
                           sample_total=0, include_tool_summaries=True,
                           overwrite=False, modified_after=None, modified_before=None):
    """Export all Claude Code sessions from all project directories."""
    print("Starting Claude Code session export...")

    projects_root = Path(projects_path or CLAUDE_PROJECTS_PATH)
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    if not projects_root.exists():
        print(f"Claude Code projects directory not found: {projects_root}")
        return False

    exported_count = 0
    skipped_count = 0
    error_count = 0
    empty_count = 0

    try:
        project_dirs = sorted(
            [d for d in projects_root.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime, reverse=True
        )
    except Exception:
        project_dirs = [d for d in projects_root.iterdir() if d.is_dir()]

    print(f"Found {len(project_dirs)} project directories")
    print(f"Output: {display_path(output_path)}")

    for project_dir in project_dirs:
        project_slug = project_dir.name

        # Skip memory/ and other non-session subdirs
        try:
            jsonl_files = sorted(
                [p for p in project_dir.glob("*.jsonl") if path_modified_in_window(p, modified_after, modified_before)],
                key=lambda f: f.stat().st_mtime, reverse=True
            )
        except Exception:
            jsonl_files = [p for p in project_dir.glob("*.jsonl") if path_modified_in_window(p, modified_after, modified_before)]

        if not jsonl_files:
            continue

        print(f"\nProject: {project_slug} ({len(jsonl_files)} sessions)")
        per_project_count = 0

        for jsonl_file in jsonl_files:
            try:
                success, result = process_claude_session(
                    jsonl_file, output_path, project_slug,
                    overwrite=overwrite,
                    include_tool_summaries=include_tool_summaries
                )

                if success:
                    print(f"  Exported: {result}")
                    exported_count += 1
                    per_project_count += 1
                elif result.startswith("SKIP (empty)") or result.startswith("SKIP (no messages)") or result.startswith("SKIP (<1KB)"):
                    empty_count += 1
                elif result.startswith("SKIP"):
                    skipped_count += 1
                else:
                    print(f"  Error: {result}")
                    error_count += 1

            except Exception as e:
                print(f"  Error processing {jsonl_file.name}: {str(e)[:100]}")
                error_count += 1

            if sample_total and exported_count >= sample_total:
                print(f"  Reached global sample limit ({sample_total})")
                break
            if sample_per_project and per_project_count >= sample_per_project:
                print(f"  Reached project sample limit ({sample_per_project}) for {project_slug}")
                break

        if sample_total and exported_count >= sample_total:
            break

    print(f"\nClaude Code export complete!")
    print(f"  Exported: {exported_count}")
    print(f"  Skipped (existing): {skipped_count}")
    print(f"  Skipped (empty): {empty_count}")
    if error_count:
        print(f"  Errors: {error_count}")

    return exported_count > 0


# ── Copilot/Cursor request helpers ───────────────────────────────────


# -- Codex session helpers -------------------------------------------------


def stream_codex_jsonl(filepath):
    """Yield parsed JSON records from a Codex session JSONL file."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
        for line_num, raw_line in enumerate(fh, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"  Warning: invalid JSON at line {line_num} in {Path(filepath).name}: {exc}")


def codex_session_id_from_path(filepath):
    """Extract the Codex session id from a rollout filename when possible."""
    match = CODEX_SESSION_ID_PATTERN.match(Path(filepath).name)
    return match.group('id') if match else Path(filepath).stem


def find_codex_session_index(sessions_path):
    """Find session_index.jsonl near a Codex sessions folder."""
    base_path = Path(os.path.expandvars(str(sessions_path))).expanduser()
    search_roots = []

    if base_path.exists():
        search_roots.append(base_path if base_path.is_dir() else base_path.parent)
        search_roots.extend(base_path.parents)
    else:
        search_roots.append(base_path.parent)

    search_roots.append(Path.home() / ".codex")

    seen = set()
    for root in search_roots:
        if root in seen:
            continue
        seen.add(root)
        candidate = root / "session_index.jsonl"
        if candidate.exists() and candidate.is_file():
            return candidate

    return None


def load_codex_session_index(sessions_path):
    """Load Codex thread names keyed by session id."""
    index_path = find_codex_session_index(sessions_path)
    title_by_id = {}
    if not index_path:
        return title_by_id

    for record in stream_codex_jsonl(index_path):
        session_id = str(record.get('id') or '').strip()
        thread_name = str(record.get('thread_name') or '').strip()
        if session_id and thread_name:
            title_by_id[session_id] = thread_name

    return title_by_id


def extract_codex_content_text(content):
    """Extract readable text from Codex response_item content."""
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, dict):
        text = content.get('text')
        return text.strip() if isinstance(text, str) else ''

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                raw_text = item.get('text')
                text = raw_text.strip() if isinstance(raw_text, str) else ''
            else:
                text = ''
            if text:
                parts.append(text)
        return '\n\n'.join(parts)

    return ''


def clean_codex_title_text(text, max_chars=90):
    """Create a compact title fallback from a Codex user message."""
    text = text or ''
    if "## My request for Codex:" in text:
        text = text.split("## My request for Codex:", 1)[1]

    text = re.sub(r'<(system-reminder|ide_opened_file)>.*?</\1>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return ''

    cleaned = text[:max_chars].strip()
    if len(text) > max_chars and ' ' in cleaned:
        cleaned = cleaned[:cleaned.rfind(' ')].strip()
    return cleaned


def is_codex_context_seed(text):
    """True for injected context records that should not become chat turns."""
    normalized = (text or '').lstrip()
    return (
        normalized.startswith("# AGENTS.md instructions")
        or normalized.startswith("<permissions instructions>")
        or (
            normalized.startswith("# Context from my IDE setup:")
            and "## My request for Codex:" not in normalized
        )
    )


def format_codex_tool_summary(payload):
    """Create a one-line summary for a Codex tool call response item."""
    name = payload.get('name') or payload.get('type') or 'tool'
    raw_args = payload.get('arguments')
    parsed_args = None

    if isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed_args = raw_args
    elif isinstance(raw_args, dict):
        parsed_args = raw_args

    detail = ''
    if isinstance(parsed_args, dict):
        if name == 'shell_command':
            detail = parsed_args.get('command', '')
        elif name in ('view_image', 'read_mcp_resource'):
            detail = parsed_args.get('path') or parsed_args.get('uri') or ''
        elif name in ('tool_search_tool', 'find'):
            detail = parsed_args.get('query') or parsed_args.get('pattern') or ''
        elif name in ('open', 'click'):
            detail = parsed_args.get('ref_id') or ''
        else:
            for key in ('command', 'path', 'query', 'pattern', 'ref_id', 'name'):
                value = parsed_args.get(key)
                if isinstance(value, str) and value.strip():
                    detail = value
                    break
    elif isinstance(parsed_args, str):
        detail = parsed_args

    detail = re.sub(r'\s+', ' ', detail).strip()
    if len(detail) > 120:
        detail = detail[:117].rstrip() + '...'

    return f"[Tool: {name} -- {detail}]" if detail else f"[Tool: {name}]"


def extract_codex_session_metadata(session_path, title_by_id):
    """Single pass over a Codex JSONL session to collect export metadata."""
    session_path = Path(session_path)
    session_id = codex_session_id_from_path(session_path)
    meta = {
        'session_id': session_id,
        'thread_name': title_by_id.get(session_id),
        'first_timestamp': None,
        'last_timestamp': None,
        'cwd': None,
        'originator': None,
        'cli_version': None,
        'source': None,
        'model_provider': None,
        'model': None,
        'first_user_text': None,
        'user_msg_count': 0,
        'assistant_msg_count': 0,
        'tool_call_count': 0,
        'has_real_content': False,
    }

    for record in stream_codex_jsonl(session_path):
        ts = record.get('timestamp')
        if ts:
            if meta['first_timestamp'] is None:
                meta['first_timestamp'] = ts
            meta['last_timestamp'] = ts

        payload = record.get('payload') or {}
        if not isinstance(payload, dict):
            continue

        if record.get('type') == 'session_meta':
            meta['session_id'] = str(payload.get('id') or meta['session_id'])
            meta['cwd'] = payload.get('cwd') or meta['cwd']
            meta['originator'] = payload.get('originator') or meta['originator']
            meta['cli_version'] = payload.get('cli_version') or meta['cli_version']
            meta['source'] = payload.get('source') or meta['source']
            meta['model_provider'] = payload.get('model_provider') or meta['model_provider']
            continue

        if record.get('type') == 'turn_context':
            meta['model'] = payload.get('model') or meta['model']
            meta['cwd'] = payload.get('cwd') or meta['cwd']
            continue

        if record.get('type') == 'event_msg' and payload.get('type') == 'thread_name_updated':
            thread_name = str(payload.get('thread_name') or '').strip()
            if thread_name:
                meta['thread_name'] = thread_name
            continue

        if record.get('type') == 'event_msg' and payload.get('type') == 'user_message':
            message = str(payload.get('message') or '').strip()
            if message:
                meta['user_msg_count'] += 1
                meta['has_real_content'] = True
                if meta['first_user_text'] is None:
                    meta['first_user_text'] = clean_codex_title_text(message)
            continue

        if record.get('type') == 'event_msg' and payload.get('type') == 'agent_message':
            message = str(payload.get('message') or '').strip()
            if message:
                meta['assistant_msg_count'] += 1
                meta['has_real_content'] = True
            continue

        if record.get('type') == 'response_item' and payload.get('type') == 'function_call':
            meta['tool_call_count'] += 1

    return meta


def codex_existing_file_matches_session(output_file, session_id):
    """Check whether an existing export belongs to this Codex session."""
    if is_small_artifact_path(output_file):
        return False
    try:
        head = output_file.read_text(encoding='utf-8', errors='ignore')[:2000]
    except Exception:
        return False
    return f"- **Session ID:** {session_id}" in head


def resolve_codex_output_file(output_dir, safe_title, session_id, source_path=None, overwrite=False):
    """Return the target output file, or None when the session is already exported."""
    output_file = output_dir / f"{safe_title}.md"
    if overwrite or not output_file.exists():
        return output_file

    if codex_existing_file_matches_session(output_file, session_id):
        if archive_file_is_current_for_source(output_file, source_path):
            return None
        return output_file

    counter = 2
    while output_file.exists():
        output_file = output_dir / f"{safe_title}_{counter}.md"
        if codex_existing_file_matches_session(output_file, session_id):
            if archive_file_is_current_for_source(output_file, source_path):
                return None
            return output_file
        counter += 1

    return output_file


def append_codex_markdown_message(md_lines, message_number, role, timestamp, text):
    """Append one Codex chat turn using the existing exporter message style."""
    md_lines.append(f"## Message {message_number} -- {role}")
    formatted_ts = format_timestamp(timestamp)
    if formatted_ts:
        md_lines.append(f"*{formatted_ts}*\n")
    else:
        md_lines.append("")
    md_lines.append(text)
    md_lines.append("")
    md_lines.append("---\n")


def process_codex_session(session_path, output_dir, title_by_id=None, overwrite=False,
                          include_tool_summaries=True):
    """Process one Codex JSONL session file into a markdown transcript."""
    session_path = Path(session_path)
    output_dir = Path(output_dir)
    title_by_id = title_by_id or {}

    if session_path.name.lower() == "session_index.jsonl":
        return False, "SKIP (index file)"

    meta = extract_codex_session_metadata(session_path, title_by_id)
    if not meta['has_real_content']:
        return False, f"SKIP (empty): {meta['session_id']}"

    title = meta['thread_name'] or meta['first_user_text'] or meta['session_id']
    safe_title = sanitize_filename(title)[:120].strip(" .")
    if not safe_title:
        safe_title = meta['session_id']

    output_file = resolve_codex_output_file(output_dir, safe_title, meta['session_id'], source_path=session_path, overwrite=overwrite)
    if output_file is None:
        return False, f"SKIP (exists): {safe_title}"

    md_lines = []
    md_lines.append(f"# {title}\n")
    md_lines.append("- **Source:** Codex")
    md_lines.append(f"- **Session ID:** {meta['session_id']}")
    md_lines.append(f"- **Messages:** {meta['user_msg_count']} user, {meta['assistant_msg_count']} assistant")
    if include_tool_summaries:
        md_lines.append(f"- **Tool calls:** {meta['tool_call_count']}")
    if meta['model']:
        md_lines.append(f"- **Model:** {meta['model']}")
    if meta['model_provider']:
        md_lines.append(f"- **Model provider:** {meta['model_provider']}")
    if meta['originator']:
        md_lines.append(f"- **Originator:** {meta['originator']}")
    if meta['source']:
        md_lines.append(f"- **Client source:** {meta['source']}")
    if meta['cli_version']:
        md_lines.append(f"- **Codex version:** {meta['cli_version']}")

    first_ts = format_timestamp(meta['first_timestamp'])
    last_ts = format_timestamp(meta['last_timestamp'])
    if first_ts:
        md_lines.append(f"- **Started:** {first_ts}")
    if last_ts:
        md_lines.append(f"- **Last activity:** {last_ts}")
    if meta['cwd']:
        md_lines.append(f"- **Working dir:** {display_path(meta['cwd'])}")
    md_lines.append(f"- **Source file:** {display_path(session_path)}")
    md_lines.append("")

    header_len = len(md_lines)
    msg_num = 0
    saw_event_messages = False

    for record in stream_codex_jsonl(session_path):
        payload = record.get('payload') or {}
        if not isinstance(payload, dict):
            continue

        if record.get('type') == 'event_msg' and payload.get('type') == 'user_message':
            text = str(payload.get('message') or '').strip()
            if not text:
                continue
            saw_event_messages = True
            msg_num += 1
            append_codex_markdown_message(md_lines, msg_num, "User", record.get('timestamp'), text)
            continue

        if record.get('type') == 'event_msg' and payload.get('type') == 'agent_message':
            text = str(payload.get('message') or '').strip()
            if not text:
                continue
            saw_event_messages = True
            msg_num += 1
            append_codex_markdown_message(md_lines, msg_num, "Assistant", record.get('timestamp'), text)
            continue

        if include_tool_summaries and record.get('type') == 'response_item' and payload.get('type') == 'function_call':
            summary = format_codex_tool_summary(payload)
            if summary:
                msg_num += 1
                append_codex_markdown_message(md_lines, msg_num, "Assistant", record.get('timestamp'), summary)

    if not saw_event_messages:
        msg_num = 0
        md_lines = md_lines[:header_len]
        for record in stream_codex_jsonl(session_path):
            payload = record.get('payload') or {}
            if not isinstance(payload, dict):
                continue
            if record.get('type') != 'response_item' or payload.get('type') != 'message':
                continue

            role = payload.get('role')
            if role not in ('user', 'assistant'):
                continue

            text = extract_codex_content_text(payload.get('content'))
            if not text or is_codex_context_seed(text):
                continue

            msg_num += 1
            md_role = "User" if role == 'user' else "Assistant"
            append_codex_markdown_message(md_lines, msg_num, md_role, record.get('timestamp'), text)

    if msg_num == 0:
        return False, f"SKIP (no messages): {meta['session_id']}"

    markdown_text = '\n'.join(md_lines)
    if markdown_text_size_bytes(markdown_text) < MIN_ARCHIVE_ARTIFACT_BYTES:
        return False, f"SKIP (<1KB): {meta['session_id']}"

    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(markdown_text)

    preserve_modification_time(session_path, output_file)
    return True, output_file.stem


def export_codex_sessions(output_path, sessions_path=None, sample_total=0,
                          include_tool_summaries=True, overwrite=False,
                          modified_after=None, modified_before=None):
    """Export Codex session JSONL transcripts to markdown files."""
    print("Starting Codex session export...")

    sessions_root = Path(os.path.expandvars(str(sessions_path or CODEX_SESSIONS_PATH))).expanduser()
    output_path = Path(os.path.expandvars(str(output_path))).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)

    if not sessions_root.exists():
        print(f"Codex sessions directory not found: {sessions_root}")
        return False

    title_by_id = load_codex_session_index(sessions_root)

    if sessions_root.is_file():
        session_files = [sessions_root] if path_modified_in_window(sessions_root, modified_after, modified_before) else []
    else:
        session_files = [
            p for p in sessions_root.rglob("*.jsonl")
            if p.name.lower() != "session_index.jsonl"
            and path_modified_in_window(p, modified_after, modified_before)
        ]

    try:
        session_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    except Exception:
        session_files = list(session_files)

    print(f"Found {len(session_files)} Codex session file(s)")
    print(f"Output: {display_path(output_path)}")

    exported_count = 0
    skipped_count = 0
    empty_count = 0
    error_count = 0

    for session_file in session_files:
        try:
            success, result = process_codex_session(
                session_file,
                output_path,
                title_by_id=title_by_id,
                overwrite=overwrite,
                include_tool_summaries=include_tool_summaries,
            )

            if success:
                print(f"  Exported: {result}")
                exported_count += 1
            elif result.startswith("SKIP (empty)") or result.startswith("SKIP (no messages)") or result.startswith("SKIP (<1KB)"):
                empty_count += 1
            elif result.startswith("SKIP"):
                skipped_count += 1
            else:
                print(f"  Error: {result}")
                error_count += 1
        except Exception as exc:
            print(f"  Error processing {session_file.name}: {str(exc)[:120]}")
            error_count += 1

        if sample_total and exported_count >= sample_total:
            print(f"  Reached sample limit ({sample_total}), stopping export...")
            break

    print("\nCodex export complete!")
    print(f"  Exported: {exported_count}")
    print(f"  Skipped (existing): {skipped_count}")
    print(f"  Skipped (empty): {empty_count}")
    if error_count:
        print(f"  Errors: {error_count}")

    return exported_count > 0


def extract_request_text(request):
    """Extract user text from either legacy or newer request shapes."""
    if not isinstance(request, dict):
        return ""

    message = request.get('message')
    if isinstance(message, dict) and message.get('text'):
        return message['text']

    for key in ('prompt', 'inputText', 'text'):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value

    input_state = request.get('inputState')
    if isinstance(input_state, dict):
        value = input_state.get('inputText')
        if isinstance(value, str) and value.strip():
            return value

    return ""


def flatten_response_chunk(chunk):
    """Extract readable text from mixed response chunk formats."""
    if isinstance(chunk, str):
        return chunk.strip()

    if isinstance(chunk, list):
        parts = [flatten_response_chunk(item) for item in chunk]
        return '\n\n'.join(part for part in parts if part)

    if not isinstance(chunk, dict):
        return ""

    if chunk.get('value'):
        return str(chunk['value']).strip()

    for key in ('text', 'markdown', 'content', 'body'):
        value = chunk.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    kind = chunk.get('kind')
    if kind == 'thinking' and isinstance(chunk.get('value'), str):
        return f"[thinking]\n{chunk['value'].strip()}"

    if kind == 'reference':
        reference = chunk.get('reference')
        if isinstance(reference, dict):
            return reference.get('external') or reference.get('path') or ""

    collected = []
    for key in ('items', 'children', 'value', 'v'):
        value = chunk.get(key)
        if isinstance(value, (dict, list)):
            text = flatten_response_chunk(value)
            if text:
                collected.append(text)

    return '\n\n'.join(collected)


def extract_response_text(request):
    """Extract assistant text from either legacy response chunks or newer response event lists."""
    if not isinstance(request, dict):
        return ""

    response = request.get('response', [])
    if not isinstance(response, list):
        response = [response]

    parts = []
    for chunk in response:
        text = flatten_response_chunk(chunk)
        if text:
            parts.append(text)

        if isinstance(chunk, dict):
            inline_reference = chunk.get('inlineReference', {})
            if isinstance(inline_reference, dict) and inline_reference.get('external'):
                parts.append(f"[link]({inline_reference['external']})")

    return '\n\n'.join(parts).strip()


def export_single_chat_file(chat_file_path, output_dir, workspace_id=None, overwrite=False):
    """Process one explicit chat file and return the created markdown path."""
    chat_file_path = Path(os.path.expandvars(chat_file_path))
    output_dir = Path(os.path.expandvars(output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)

    workspace_name = workspace_id or chat_file_path.parent.parent.name
    success, result = process_chat_file(chat_file_path, output_dir, workspace_name, overwrite=overwrite)
    if not success:
        raise RuntimeError(result)

    output_path = output_dir / f"{result}.md"
    return output_path

def sanitize_filename(title):
    """Sanitize title for use as filename."""
    return "".join(c if c.isalnum() or c in " ._-()" else "_" for c in title)

def is_uuid_filename(filename):
    """Check if a filename (without extension) looks like a UUID."""
    name = Path(filename).stem
    return bool(UUID_PATTERN.match(name))

def preserve_modification_time(src_path, dest_path):
    """Copy modification time from source to destination file."""
    try:
        stat_info = os.stat(src_path)
        os.utime(dest_path, (stat_info.st_atime, stat_info.st_mtime))
        return True
    except Exception as e:
        print(f"  Warning: Could not preserve modification time: {e}")
        return False


def markdown_text_size_bytes(text):
    """Return UTF-8 encoded size for a markdown string."""
    return len(str(text).encode('utf-8'))


def sha256_file(path):
    """Return SHA256 hex digest for one file path."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def is_small_artifact_path(path, minimum_bytes=None):
    """Return True when a file exists and is smaller than the archive threshold."""
    if minimum_bytes is None:
        minimum_bytes = MIN_ARCHIVE_ARTIFACT_BYTES
    try:
        return Path(path).is_file() and Path(path).stat().st_size < minimum_bytes
    except Exception:
        return False


def archive_file_is_current_for_source(archive_path, source_path, tolerance_seconds=1.0):
    """Return True when an existing archive file is at least as new as its source."""
    if not source_path:
        return True
    try:
        archive_mtime = Path(archive_path).stat().st_mtime
        source_mtime = Path(source_path).stat().st_mtime
        return archive_mtime + tolerance_seconds >= source_mtime
    except Exception:
        return False


def remove_file_if_exists(path):
    """Delete one file if present."""
    try:
        file_path = Path(path)
        if file_path.exists():
            file_path.unlink()
            return True
    except Exception:
        return False
    return False


def cleanup_small_archive_artifacts(archive_root, minimum_bytes=None):
    """Delete archived markdown files smaller than the configured usefulness threshold."""
    if minimum_bytes is None:
        minimum_bytes = MIN_ARCHIVE_ARTIFACT_BYTES
    archive_root = Path(archive_root)
    if not archive_root.exists():
        return 0

    removed = 0
    for md_file in archive_root.rglob("*.md"):
        if is_small_artifact_path(md_file, minimum_bytes=minimum_bytes):
            if remove_file_if_exists(md_file):
                removed += 1

    if removed:
        print(f"Removed {removed} archived markdown file(s) smaller than {minimum_bytes} bytes")
    else:
        print(f"No archived markdown files smaller than {minimum_bytes} bytes found")
    return removed


def cleanup_duplicate_archive_artifacts(archive_root):
    """Delete duplicate markdown files with identical content inside the same archive folder."""
    archive_root = Path(archive_root)
    if not archive_root.exists():
        return 0

    removed = 0
    seen = {}
    files = sorted(archive_root.rglob("*.md"), key=lambda p: (str(p.parent).lower(), str(p).lower()))
    for md_file in files:
        try:
            digest = sha256_file(md_file)
        except Exception:
            continue
        key = (str(md_file.parent).lower(), digest)
        if key in seen:
            if remove_file_if_exists(md_file):
                removed += 1
        else:
            seen[key] = md_file

    if removed:
        print(f"Removed {removed} duplicate archived markdown file(s) with identical content")
    else:
        print("No duplicate archived markdown files found")
    return removed


def cleanup_legacy_plan_artifacts(archive_root):
    """Delete legacy archive plan files that do not carry source metadata."""
    archive_root = Path(archive_root)
    if not archive_root.exists():
        return 0

    removed = 0
    for md_file in archive_root.rglob("*.md"):
        try:
            rel_parts = {p.lower() for p in md_file.relative_to(archive_root).parts}
        except Exception:
            continue
        if 'plans' not in rel_parts:
            continue
        header = read_markdown_header(md_file)
        if header.get('Source file'):
            continue
        if remove_file_if_exists(md_file):
            removed += 1

    if removed:
        print(f"Removed {removed} legacy plan file(s) without source metadata")
    else:
        print("No legacy plan files without source metadata found")
    return removed


def build_copied_plan_markdown(src, title, app_label, session_id=None):
    """Wrap a copied plan source with archive metadata so it can be re-indexed later."""
    src = Path(src)
    session_id = session_id or src.stem
    title = title or src.stem
    try:
        original_text = src.read_text(encoding='utf-8', errors='ignore').strip()
    except Exception:
        original_text = ""

    lines = [
        f"# {title}",
        "",
        f"- **Source:** {app_label}",
        f"- **Session ID:** {session_id}",
        f"- **Source file:** {display_path(src)}",
    ]
    created = path_time_utc(src, 'created')
    modified = path_time_utc(src, 'modified')
    if created:
        lines.append(f"- **Created:** {created}")
    if modified:
        lines.append(f"- **Last modified:** {modified}")
    lines.append("")
    lines.append("## Original Content")
    lines.append("")
    if original_text:
        lines.append(original_text)
    else:
        lines.append("_Original file was empty or unreadable._")
    lines.append("")
    return '\n'.join(lines)


def read_markdown_title(path):
    """Return the first markdown H1 title, falling back to the file stem."""
    path = Path(path)
    try:
        with path.open('r', encoding='utf-8', errors='ignore') as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = re.sub(r'\s+', ' ', stripped[2:]).strip()
                    return title or path.stem
    except Exception:
        pass
    return path.stem


def read_markdown_header(path):
    """Read simple '- **Key:** value' metadata from the top of an exported markdown file."""
    header = {}
    try:
        with Path(path).open('r', encoding='utf-8', errors='ignore') as fh:
            for i, line in enumerate(fh):
                if i > 80:
                    break
                match = re.match(r"^- \*\*(.+?):\*\* (.*)$", line.strip())
                if match:
                    header[match.group(1).strip()] = match.group(2).strip()
    except Exception:
        pass
    return header


def unique_markdown_path(output_dir, title):
    """Resolve a non-conflicting markdown path inside output_dir."""
    output_dir = Path(output_dir)
    safe_title = sanitize_filename(title).strip(" .")[:120] or "untitled"
    candidate = output_dir / f"{safe_title}.md"
    counter = 2
    while candidate.exists():
        candidate = output_dir / f"{safe_title}_{counter}.md"
        counter += 1
    return candidate


def stable_source_markdown_path(output_dir, source_path, title=None):
    """Resolve a stable markdown path for a copied/extracted source artifact."""
    output_dir = Path(output_dir)
    source_path = Path(source_path)
    label = sanitize_filename(title or source_path.stem).strip(" .")[:90] or source_path.stem
    digest = hashlib.sha1(str(source_path).lower().encode('utf-8')).hexdigest()[:8]
    return output_dir / f"{label}-{digest}.md"


def markdown_file_matches_session(path, session_id):
    """Check whether a markdown export belongs to the given session id."""
    path = Path(path)
    if is_small_artifact_path(path):
        return False
    header = read_markdown_header(path)
    return header.get('Session ID') == session_id


def find_markdown_for_session(output_dir, session_id):
    """Return an existing markdown file for one session id, if present."""
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return None
    for md_file in sorted(output_dir.glob("*.md")):
        if markdown_file_matches_session(md_file, session_id):
            return md_file
    return None


def build_markdown_session_lookup(output_dir):
    """Build a Session ID -> markdown path lookup for fast incremental skips."""
    output_dir = Path(output_dir)
    lookup = {}
    if not output_dir.exists():
        return lookup

    for md_file in sorted(output_dir.glob("*.md")):
        if is_small_artifact_path(md_file):
            continue
        session_id = read_markdown_header(md_file).get('Session ID')
        if session_id and session_id not in lookup:
            lookup[session_id] = md_file
    return lookup


def stable_session_markdown_path(output_dir, title, session_id, mutate_existing=False):
    """Resolve a readable, stable markdown path keyed by session id."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_title = sanitize_filename(title).strip(" .")[:80] or sanitize_filename(session_id)
    safe_session = sanitize_filename(session_id)
    candidate = output_dir / f"{safe_title}-{safe_session}.md"

    existing = find_markdown_for_session(output_dir, session_id)
    if existing is None:
        if candidate.exists() and not mutate_existing:
            return unique_markdown_path(output_dir, f"{safe_title}-{safe_session}")
        return candidate

    if existing == candidate:
        return candidate

    if not mutate_existing:
        return existing

    if not candidate.exists():
        existing.rename(candidate)
        return candidate

    if markdown_file_matches_session(candidate, session_id):
        return candidate

    counter = 2
    while True:
        alt = output_dir / f"{safe_title}-{safe_session}_{counter}.md"
        if not alt.exists():
            existing.rename(alt)
            return alt
        if markdown_file_matches_session(alt, session_id):
            return alt
        counter += 1


def clean_index_value(value):
    """Normalize one CSV field without changing the meaning."""
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def path_time_utc(path, attr):
    """Return a filesystem timestamp as local Eastern time text."""
    try:
        stat = Path(path).stat()
        ts = stat.st_ctime if attr == 'created' else stat.st_mtime
        return datetime.fromtimestamp(ts, timezone.utc).astimezone(LOCAL_TIME_ZONE).strftime(
            f"%Y-%m-%d %H:%M:%S {LOCAL_TIME_ZONE_LABEL}"
        )
    except Exception:
        return ""


def file_size_kb(path):
    """Return a file size in KB rounded to one decimal place."""
    try:
        return f"{Path(path).stat().st_size / 1024:.1f}"
    except Exception:
        return ""


def enrich_index_row(row):
    """Add useful searchable/cross-reference fields to a base index row."""
    row = dict(row)
    source_path = clean_index_value(row.get('source_path'))
    artifact_path = clean_index_value(row.get('artifact_path'))
    size_path = artifact_path or source_path

    created = (
        row.get('date_created')
        or row.get('Started')
        or row.get('Created')
        or path_time_utc(source_path, 'created')
    )
    modified = (
        row.get('date_modified')
        or row.get('Last activity')
        or row.get('Last message')
        or path_time_utc(source_path, 'modified')
    )

    app = row.get('app') or row.get('Source') or ""
    model = row.get('model') or row.get('Model') or ""
    project = row.get('project') or row.get('Project') or ""
    working_dir = row.get('working_dir') or row.get('Working dir') or ""
    messages = row.get('messages') or row.get('Messages') or ""
    related_key = row.get('related_key') or row.get('session_id') or ""

    return {
        'session_id': clean_index_value(row.get('session_id')),
        'source_path': source_path,
        'title': clean_index_value(row.get('title')),
        'type': clean_index_value(row.get('type')),
        'artifact_path': artifact_path,
        'app': clean_index_value(app),
        'date_created': clean_index_value(created),
        'date_modified': clean_index_value(modified),
        'model': clean_index_value(model),
        'project': clean_index_value(project),
        'working_dir': clean_index_value(working_dir),
        'messages': clean_index_value(messages),
        'related_key': clean_index_value(related_key),
        'file_size_kb': clean_index_value(row.get('file_size_kb') or file_size_kb(size_path)),
    }


def write_index(index_path, rows, display_path=None):
    """Rewrite the canonical archive index."""
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        'session_id', 'source_path', 'title', 'type',
        'artifact_path', 'app', 'date_created', 'date_modified',
        'model', 'project', 'working_dir', 'messages', 'related_key',
        'file_size_kb',
    ]

    unique = {}
    for row in rows:
        row = enrich_index_row(row)
        artifact_path = row.get('artifact_path')
        if artifact_path and not Path(artifact_path).exists():
            continue
        if artifact_path and is_small_artifact_path(artifact_path):
            continue
        session_id = row['session_id']
        source_path = row['source_path']
        title = row['title']
        rtype = row['type']
        if not (session_id and source_path and title and rtype):
            continue
        unique[(session_id, source_path, title, rtype)] = row

    ordered = sorted(unique.values(), key=lambda r: (r['type'], r['title'].lower(), r['session_id']))
    output_rows = []
    for row in ordered:
        output_row = dict(row)
        if display_path:
            for key in ('source_path', 'artifact_path', 'working_dir'):
                if output_row.get(key):
                    output_row[key] = display_path(output_row[key])
        output_rows.append(output_row)
    try:
        with index_path.open('w', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)
    except PermissionError:
        fallback_path = index_path.with_name(f"{index_path.stem}_enriched{index_path.suffix}")
        print(f"  Warning: {index_path} is locked; writing {fallback_path} instead")
        with fallback_path.open('w', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)

    return len(ordered)


def index_exported_markdown_tree(root_dir):
    """Index exported markdown files by reading their H1 and top-of-file metadata."""
    rows = []
    root_dir = Path(root_dir)
    if not root_dir.exists():
        return rows

    for md_file in root_dir.rglob("*.md"):
        if is_small_artifact_path(md_file):
            continue
        rel_parts = {p.lower() for p in md_file.relative_to(root_dir).parts}
        rtype = 'plan' if 'plans' in rel_parts else 'chat'
        header = read_markdown_header(md_file)
        if rtype == 'plan' and not header.get('Source file'):
            continue
        rows.append({
            'session_id': header.get('Session ID') or md_file.stem,
            'source_path': header.get('Source file') or str(md_file),
            'title': read_markdown_title(md_file),
            'type': rtype,
            'artifact_path': str(md_file),
            **header,
        })
    return rows


def copy_plan_markdowns(source_dir, output_dir, copied_sources=None, copied_hashes=None,
                        max_items=0, modified_after=None, modified_before=None,
                        overwrite=False):
    """Copy markdown plan files and return index rows pointing at the original sources."""
    rows = []
    source_dir = Path(os.path.expandvars(str(source_dir))).expanduser()
    output_dir = Path(output_dir)
    copied_sources = copied_sources if copied_sources is not None else set()
    copied_hashes = copied_hashes if copied_hashes is not None else set()

    if not source_dir.exists():
        return rows

    output_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(source_dir.rglob("*.md")):
        if not path_modified_in_window(src, modified_after, modified_before):
            continue
        if is_small_artifact_path(src):
            continue
        source_key = str(src.resolve()).lower()
        if source_key in copied_sources:
            continue
        copied_sources.add(source_key)

        try:
            content_hash = sha256_file(src)
        except Exception as exc:
            print(f"  Warning: could not hash plan {src}: {exc}")
            continue
        if content_hash in copied_hashes:
            continue
        copied_hashes.add(content_hash)

        title = read_markdown_title(src)
        dest = stable_source_markdown_path(output_dir, src, title or src.stem)
        if dest.exists() and not overwrite:
            continue
        markdown_text = build_copied_plan_markdown(src, title, "Markdown plan", session_id=src.stem)
        if markdown_text_size_bytes(markdown_text) < MIN_ARCHIVE_ARTIFACT_BYTES:
            continue
        try:
            dest.write_text(markdown_text + '\n', encoding='utf-8')
            preserve_modification_time(src, dest)
        except Exception as exc:
            print(f"  Warning: could not copy plan {src}: {exc}")
            continue
        if is_small_artifact_path(dest):
            remove_file_if_exists(dest)
            continue

        rows.append({
            'session_id': src.stem,
            'source_path': str(src),
            'title': title,
            'type': 'plan',
            'artifact_path': str(dest),
            'app': 'Markdown plan',
            'date_created': path_time_utc(src, 'created'),
            'date_modified': path_time_utc(src, 'modified'),
            'related_key': src.stem,
        })
        if max_items and len(rows) >= max_items:
            break
    return rows


def extract_codex_update_plan_payload(payload):
    """Return parsed update_plan arguments from a Codex function call payload."""
    if payload.get('name') != 'update_plan':
        return None

    raw_args = payload.get('arguments')
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            return None

    if not isinstance(raw_args, dict):
        return None

    return raw_args


def extract_codex_plan_events(session_file):
    """Collect assistant/agent context and update_plan calls from one Codex session."""
    events = []
    for record in stream_codex_jsonl(session_file):
        payload = record.get('payload') or {}
        if not isinstance(payload, dict):
            continue

        timestamp = record.get('timestamp')
        rtype = record.get('type')

        if rtype == 'event_msg' and payload.get('type') == 'agent_message':
            text = str(payload.get('message') or '').strip()
            if text:
                events.append({
                    'kind': 'assistant_context',
                    'timestamp': timestamp,
                    'text': text,
                })
            continue

        if rtype == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'assistant':
            text = extract_codex_content_text(payload.get('content'))
            if text:
                events.append({
                    'kind': 'assistant_context',
                    'timestamp': timestamp,
                    'text': text,
                })
            continue

        if rtype == 'response_item' and payload.get('type') == 'function_call':
            args = extract_codex_update_plan_payload(payload)
            if args is not None:
                events.append({
                    'kind': 'update_plan',
                    'timestamp': timestamp,
                    'payload': payload,
                    'arguments': args,
                })

    return events


def collect_nearby_codex_plan_context(events, index, max_messages=4):
    """Return nearby assistant context around one update_plan event."""
    snippets = []
    seen = set()

    cursor = index - 1
    while cursor >= 0 and len(snippets) < 2:
        event = events[cursor]
        if event.get('kind') == 'update_plan':
            break
        if event.get('kind') == 'assistant_context':
            text = clean_index_value(event.get('text'))
            if text and text not in seen:
                snippets.insert(0, (event.get('timestamp'), text))
                seen.add(text)
        cursor -= 1

    cursor = index + 1
    while cursor < len(events) and len(snippets) < max_messages:
        event = events[cursor]
        if event.get('kind') == 'update_plan':
            break
        if event.get('kind') == 'assistant_context':
            text = clean_index_value(event.get('text'))
            if text and text not in seen:
                snippets.append((event.get('timestamp'), text))
                seen.add(text)
        cursor += 1

    return snippets


def extract_codex_plans(output_dir, sessions_path=None, max_items=0,
                        modified_after=None, modified_before=None,
                        overwrite=False):
    """Extract embedded Codex update_plan calls into markdown plan files."""
    print("Extracting Codex embedded plans...")
    rows = []
    sessions_root = Path(os.path.expandvars(str(sessions_path or CODEX_SESSIONS_PATH))).expanduser()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not sessions_root.exists():
        print(f"Codex sessions directory not found: {sessions_root}")
        return rows

    title_by_id = load_codex_session_index(sessions_root)
    session_files = [sessions_root] if sessions_root.is_file() else [
        p for p in sessions_root.rglob("*.jsonl") if p.name.lower() != "session_index.jsonl"
    ]
    session_files = [p for p in session_files if path_modified_in_window(p, modified_after, modified_before)]

    for session_file in session_files:
        session_id = codex_session_id_from_path(session_file)
        title = title_by_id.get(session_id) or session_id
        events = extract_codex_plan_events(session_file)
        plan_num = 0

        for idx, event in enumerate(events):
            if event.get('kind') != 'update_plan':
                continue

            args = event.get('arguments') or {}
            plan_items = args.get('plan') or []
            plan_num += 1
            plan_title = f"{title} -- plan {plan_num}"
            dest = output_dir / f"{sanitize_filename(session_id)}-plan-{plan_num}.md"
            if dest.exists() and not overwrite:
                continue
            lines = [
                f"# {plan_title}",
                "",
                "- **Source:** Codex",
                f"- **Session ID:** {session_id}",
                f"- **Source file:** {display_path(session_file)}",
            ]
            ts = format_timestamp(event.get('timestamp'))
            if ts:
                lines.append(f"- **Captured:** {ts}")
            if args.get('explanation'):
                lines.append(f"- **Explanation:** {clean_index_value(args.get('explanation'))}")
            lines.append("")

            if plan_items:
                lines.append("## Plan Checklist")
                lines.append("")
                for item in plan_items:
                    if isinstance(item, dict):
                        status = str(item.get('status') or '').strip()
                        step = clean_index_value(item.get('step'))
                        if step:
                            lines.append(f"- [{status}] {step}" if status else f"- {step}")
                    elif str(item).strip():
                        lines.append(f"- {clean_index_value(item)}")
                lines.append("")

            context_snippets = collect_nearby_codex_plan_context(events, idx)
            if context_snippets:
                lines.append("## Surrounding Assistant Context")
                lines.append("")
                for snippet_ts, snippet_text in context_snippets:
                    pretty_ts = format_timestamp(snippet_ts)
                    if pretty_ts:
                        lines.append(f"### {pretty_ts}")
                        lines.append("")
                    lines.append(snippet_text)
                    lines.append("")

            lines.append("## Raw update_plan Arguments")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(args, indent=2, ensure_ascii=False))
            lines.append("```")

            markdown_text = '\n'.join(lines) + '\n'
            if markdown_text_size_bytes(markdown_text) < MIN_ARCHIVE_ARTIFACT_BYTES:
                continue

            try:
                dest.write_text(markdown_text, encoding='utf-8')
                preserve_modification_time(session_file, dest)
            except Exception as exc:
                print(f"  Warning: could not write Codex plan {dest}: {exc}")
                continue

            rows.append({
                'session_id': session_id,
                'source_path': str(session_file),
                'title': plan_title,
                'type': 'plan',
                'artifact_path': str(dest),
                'app': 'Codex',
                'date_created': ts,
                'date_modified': ts,
                'related_key': session_id,
            })
            if max_items and len(rows) >= max_items:
                print(f"Reached Codex plan sample limit ({max_items})")
                print(f"Extracted {len(rows)} Codex plan snapshots")
                return rows

    print(f"Extracted {len(rows)} Codex plan snapshots")
    return rows


def export_copilot_cli_sessions(output_dir, state_path=None, max_items=0,
                                modified_after=None, modified_before=None,
                                overwrite=False):
    """Export Copilot CLI events.jsonl sessions to markdown transcripts."""
    print("Starting Copilot CLI session export...")
    rows = []
    state_root = Path(os.path.expandvars(str(state_path or COPILOT_CLI_STATE_PATH))).expanduser()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not state_root.exists():
        print(f"Copilot CLI session-state directory not found: {state_root}")
        return rows

    event_files = sorted(
        [p for p in state_root.glob("*/events.jsonl") if path_modified_in_window(p, modified_after, modified_before)],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    print(f"Found {len(event_files)} Copilot CLI event file(s)")

    for event_file in event_files:
        session_id = event_file.parent.name
        messages = []
        first_user = None
        model = None
        started = None
        last_ts = None
        cwd = None

        for record in stream_codex_jsonl(event_file):
            rtype = record.get('type')
            data = record.get('data') or {}
            ts = record.get('timestamp')
            if ts:
                started = started or ts
                last_ts = ts

            if rtype == 'session.start':
                session_data = data or {}
                model = model or session_data.get('model')
                context = session_data.get('context') or {}
                cwd = context.get('cwd') or cwd
                continue

            if rtype == 'session.model_change':
                model = data.get('newModel') or model
                continue

            if rtype == 'user.message':
                text = str(data.get('content') or '').strip()
                if text:
                    first_user = first_user or clean_codex_title_text(text)
                    messages.append(('User', ts, text))
                continue

            if rtype == 'assistant.message':
                text = str(data.get('content') or '').strip()
                if text:
                    messages.append(('Assistant', ts, text))

        if not messages:
            continue

        title = first_user or session_id
        dest = stable_session_markdown_path(output_dir, title, session_id, mutate_existing=overwrite)
        if dest.exists() and not overwrite:
            continue
        lines = [
            f"# {title}",
            "",
            "- **Source:** Copilot CLI",
            f"- **Session ID:** {session_id}",
        ]
        if model:
            lines.append(f"- **Model:** {model}")
        if started:
            lines.append(f"- **Started:** {format_timestamp(started)}")
        if last_ts:
            lines.append(f"- **Last activity:** {format_timestamp(last_ts)}")
        if cwd:
            lines.append(f"- **Working dir:** {display_path(cwd)}")
        lines.append(f"- **Source file:** {display_path(event_file)}")
        lines.append("")

        for i, (role, ts, text) in enumerate(messages, 1):
            lines.append(f"## Message {i} -- {role}")
            if ts:
                lines.append(f"*{format_timestamp(ts)}*")
                lines.append("")
            lines.append(text)
            lines.append("")
            lines.append("---")
            lines.append("")

        markdown_text = '\n'.join(lines)
        if markdown_text_size_bytes(markdown_text) < MIN_ARCHIVE_ARTIFACT_BYTES:
            continue

        try:
            dest.write_text(markdown_text, encoding='utf-8')
            preserve_modification_time(event_file, dest)
        except Exception as exc:
            print(f"  Warning: could not export Copilot CLI session {event_file}: {exc}")
            continue

        rows.append({
            'session_id': session_id,
            'source_path': str(event_file),
            'title': title,
            'type': 'chat',
            'artifact_path': str(dest),
            'app': 'Copilot CLI',
            'date_created': format_timestamp(started),
            'date_modified': format_timestamp(last_ts),
            'model': model or '',
            'working_dir': cwd or '',
            'related_key': session_id,
        })
        if max_items and len(rows) >= max_items:
            break

    print(f"Exported {len(rows)} Copilot CLI chats to {display_path(output_dir)}")
    return rows


def copy_copilot_cli_plans(output_dir, state_path=None, max_items=0,
                           modified_after=None, modified_before=None,
                           overwrite=False):
    """Copy Copilot CLI plan.md files into the archive and index them."""
    rows = []
    state_root = Path(os.path.expandvars(str(state_path or COPILOT_CLI_STATE_PATH))).expanduser()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not state_root.exists():
        return rows

    for plan_file in sorted(state_root.glob("*/plan.md")):
        if not path_modified_in_window(plan_file, modified_after, modified_before):
            continue
        if is_small_artifact_path(plan_file):
            continue
        session_id = plan_file.parent.name
        title = read_markdown_title(plan_file)
        dest = stable_session_markdown_path(output_dir, title, session_id, mutate_existing=overwrite)
        if dest.exists() and not overwrite:
            continue
        markdown_text = build_copied_plan_markdown(plan_file, title, "Copilot CLI", session_id=session_id)
        if markdown_text_size_bytes(markdown_text) < MIN_ARCHIVE_ARTIFACT_BYTES:
            continue
        try:
            dest.write_text(markdown_text + '\n', encoding='utf-8')
            preserve_modification_time(plan_file, dest)
        except Exception as exc:
            print(f"  Warning: could not copy Copilot CLI plan {plan_file}: {exc}")
            continue
        if is_small_artifact_path(dest):
            remove_file_if_exists(dest)
            continue
        rows.append({
            'session_id': session_id,
            'source_path': str(plan_file),
            'title': title,
            'type': 'plan',
            'artifact_path': str(dest),
            'app': 'Copilot CLI',
            'date_created': path_time_utc(plan_file, 'created'),
            'date_modified': path_time_utc(plan_file, 'modified'),
            'related_key': session_id,
        })
        if max_items and len(rows) >= max_items:
            break
    return rows


def build_archive_index(archive_root, index_path, extra_rows=None, display_path=None):
    """Build index.csv from archived markdown plus explicit source-aware plan rows."""
    rows = index_exported_markdown_tree(archive_root)
    rows.extend(extra_rows or [])
    count = write_index(index_path, rows, display_path=display_path)
    shown_index_path = display_path(index_path) if display_path else index_path
    print(f"Wrote archive index: {shown_index_path} ({count} rows)")
    return count

def rename_uuid_files(directory, max_chars=10000):
    """Scan directory for UUID-named .md files and rename them using LLM-generated titles."""
    directory = Path(directory)
    if not directory.exists():
        print(f"Directory does not exist: {directory}")
        return 0
    
    renamed_count = 0
    
    for md_file in directory.glob("*.md"):
        if not is_uuid_filename(md_file.name):
            continue
        
        print(f"Found UUID file: {md_file.name}")
        
        try:
            # Generate title using LLM
            title = get_title_for_chat(str(md_file))
            title = sanitize_filename(title)[:100]  # Cap at 100 chars
            
            if not title or title == "Could not generate title":
                print(f"  Skipping - could not generate title")
                continue
            
            # Create new filename
            new_name = f"{title}.md"
            new_path = md_file.parent / new_name
            
            # Avoid overwriting existing files
            if new_path.exists():
                counter = 1
                while new_path.exists():
                    new_name = f"{title}_{counter}.md"
                    new_path = md_file.parent / new_name
                    counter += 1
            
            # Get original modification time before rename
            orig_mtime = os.path.getmtime(md_file)
            orig_atime = os.path.getatime(md_file)
            
            # Rename the file
            md_file.rename(new_path)
            
            # Restore original modification time
            os.utime(new_path, (orig_atime, orig_mtime))
            
            print(f"  Renamed to: {new_name}")
            renamed_count += 1
            
        except Exception as e:
            # Handle unicode errors in error messages
            try:
                print(f"  Error renaming {md_file.name}: {str(e)[:100]}")
            except:
                print(f"  Error renaming {md_file.name}: (unicode error in message)")
    
    return renamed_count

def extract_text_from_richtext(richtext):
    """Extract plain text from Cursor's richText JSON format."""
    if not richtext:
        return ""
    if isinstance(richtext, str):
        try:
            richtext = json.loads(richtext)
        except:
            return richtext
    
    def extract_text(node):
        if isinstance(node, dict):
            if node.get('type') == 'text':
                return node.get('text', '')
            children = node.get('children', [])
            return ' '.join(extract_text(c) for c in children if c)
        elif isinstance(node, list):
            return ' '.join(extract_text(c) for c in node if c)
        return ''
    
    return extract_text(richtext.get('root', {})).strip()

def export_cursor_from_db(output_path, max_items=0, skip_repair=False, overwrite=False,
                          modified_after=None, modified_before=None):
    """Export Cursor chats from SQLite database (new format)."""
    print("Exporting Cursor chats from SQLite database...")
    
    db_path = os.path.expandvars(CURSOR_DB_PATH)
    output_path = Path(os.path.expandvars(output_path))
    output_path.mkdir(parents=True, exist_ok=True)
    
    if not os.path.exists(db_path):
        print(f"Cursor database not found: {db_path}")
        return 0
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    exported_count = 0
    
    try:
        # Get all composerData entries
        cursor.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData%'")
        composer_rows = cursor.fetchall()

        # Parse createdAt from each composerData value (if present) and sort by it descending
        parsed = []
        for comp_key, comp_value in composer_rows:
            try:
                comp_data = json.loads(comp_value)
                created = None
                # try to find createdAt from composer header or bubbles
                headers = comp_data.get('fullConversationHeadersOnly') or []
                if headers and isinstance(headers, list):
                    # some entries may include timestamps in headers
                    created = None
                # Fallback: check top-level createdAt
                if not created:
                    created = comp_data.get('createdAt')
                parsed.append((comp_key, comp_value, created or 0))
            except Exception:
                parsed.append((comp_key, comp_value, 0))

        # Sort by created timestamp descending so newest conversations processed first
        parsed.sort(key=lambda t: t[2], reverse=True)

        for comp_key, comp_value, _created in parsed:
            if not timestamp_in_window(_created, modified_after, modified_before):
                continue
            try:
                comp_data = json.loads(comp_value)
                composer_id = comp_data.get('composerId', comp_key.split(':')[-1])
                status = comp_data.get('status', 'unknown')
                
                # Skip empty/incomplete chats
                headers = comp_data.get('fullConversationHeadersOnly', [])
                if len(headers) < 2:  # Need at least user + assistant message
                    continue
                
                # Build conversation from bubbles
                md_content = []
                md_content.append(f"# {composer_id}\n")
                md_content.append("- **Source:** Cursor")
                md_content.append(f"- **Session ID:** {composer_id}")
                md_content.append(f"- **Status:** {status}")
                md_content.append(f"- **Messages:** {len(headers)}")
                md_content.append(f"- **Source file:** {display_path(db_path)}")
                md_content.append("")
                
                # Get each bubble's content
                # Try to get timestamp from composer level first - prefer lastUpdatedAt, fallback to createdAt
                created_at = comp_data.get('lastUpdatedAt') or comp_data.get('createdAt')
                for i, header in enumerate(headers):
                    bubble_id = header.get('bubbleId')
                    bubble_type = header.get('type', 0)  # 1 = user, 2 = assistant
                    
                    bubble_key = f"bubbleId:{composer_id}:{bubble_id}"
                    cursor.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (bubble_key,))
                    bubble_row = cursor.fetchone()
                    
                    if bubble_row:
                        bubble_data = json.loads(bubble_row[0])
                        
                        # Get timestamp from first bubble if not already set
                        if not created_at and bubble_data.get('createdAt'):
                            created_at = bubble_data.get('createdAt')
                        
                        # Get message text
                        text = bubble_data.get('text', '')
                        if not text:
                            text = extract_text_from_richtext(bubble_data.get('richText'))
                        
                        if text:
                            role = "User" if bubble_type == 1 else "Assistant"
                            md_content.append(f"## {role}")
                            md_content.append(text)
                            md_content.append("")
                            md_content.append("---\n")
                
                # Only save if we have content
                if len(md_content) > 4:
                    # Use composer_id as filename (will be renamed later by LLM)
                    output_file = output_path / f"{composer_id}.md"
                    
                    # Skip if file already exists (incremental export)
                    if output_file.exists() and not overwrite:
                        if is_small_artifact_path(output_file):
                            continue
                        elif archive_file_is_current_for_source(output_file, db_path):
                            continue

                    markdown_text = '\n'.join(md_content)
                    if markdown_text_size_bytes(markdown_text) < MIN_ARCHIVE_ARTIFACT_BYTES:
                        continue
                    
                    with open(output_file, 'w', encoding='utf-8') as f:
                        f.write(markdown_text)
                    
                    # Set modification time if we have createdAt
                    if created_at:
                        try:
                            ts = created_at / 1000 if created_at > 1e12 else created_at
                            os.utime(output_file, (ts, ts))
                        except:
                            pass
                    
                    exported_count += 1
                    if exported_count % 50 == 0:
                        print(f"  Exported {exported_count} chats...")

                    # If max_items set, stop after reaching it
                    if max_items and exported_count >= max_items:
                        print(f"  Reached sample limit of {max_items}, stopping export...")
                        break
                        
            except Exception as e:
                continue  # Skip problematic entries
        
        print(f"Exported {exported_count} Cursor chats to {display_path(output_path)}")
        
    finally:
        conn.close()
    
    # After export optionally run a repair pass to fill any placeholder files
    if not skip_repair:
        try:
            repair_cursor_placeholders(output_path, db_path)
        except Exception as e:
            print(f"  Warning: repair pass failed: {e}")

    return exported_count


def repair_cursor_placeholders(output_path, db_path):
    """Scan exported markdown files for placeholder lines and try to reconstruct full conversation from DB."""
    PH = "(Conversation content not fully reconstructed in this pass — run full export for details.)"
    output_path = Path(output_path)
    if not output_path.exists():
        return 0

    repaired = 0
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        for md_file in output_path.glob("*.md"):
            try:
                text = md_file.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                continue

            if PH not in text:
                continue

            # Try to find composer key inside the file (line like: - **ComposerKey:** composerData:ID)
            composer_id = None
            for line in text.splitlines():
                if 'ComposerKey' in line and 'composerData:' in line:
                    parts = line.split('composerData:')
                    if len(parts) > 1:
                        composer_id = parts[1].strip()
                        break

            if not composer_id:
                # Could not find composer id; skip
                continue

            # Try to fetch composerData from DB
            key = f"composerData:{composer_id}"
            cur.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row:
                continue

            try:
                comp_data = json.loads(row[0])
            except Exception:
                continue

            # Try various locations for full messages
            messages = []
            # 1) Inline bubbles
            if isinstance(comp_data.get('bubbles'), list) and comp_data.get('bubbles'):
                for b in comp_data.get('bubbles'):
                    text_piece = b.get('text') or ''
                    if not text_piece:
                        text_piece = extract_text_from_richtext(b.get('richText'))
                    role = 'User' if b.get('type') == 1 else 'Assistant'
                    if text_piece:
                        messages.append((role, text_piece, b.get('createdAt')))

            # 2) Some composerData stores 'fullConversationHeadersOnly' and bubbles stored separately
            if not messages and comp_data.get('fullConversationHeadersOnly'):
                headers = comp_data.get('fullConversationHeadersOnly') or []
                for header in headers:
                    bubble_id = header.get('bubbleId')
                    if not bubble_id:
                        continue
                    # Try multiple possible key formats to locate bubble data
                    tried = False
                    for pattern in (f"bubbleId:{composer_id}:{bubble_id}", f"bubble:{composer_id}:{bubble_id}", f"bubbleData:{composer_id}:{bubble_id}"):
                        cur.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (pattern,))
                        brow = cur.fetchone()
                        if brow:
                            try:
                                bdata = json.loads(brow[0])
                                text_piece = bdata.get('text') or extract_text_from_richtext(bdata.get('richText'))
                                role = 'User' if header.get('type') == 1 else 'Assistant'
                                if text_piece:
                                    messages.append((role, text_piece, bdata.get('createdAt')))
                                    tried = True
                                    break
                            except Exception:
                                continue
                    if not tried:
                        # Couldn't find bubble by key; skip
                        continue

            # 3) Last resort: look for any keys containing the composer_id and 'bubble' and load them
            if not messages:
                cur.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE ?", (f"%{composer_id}%bubble%",))
                for k, v in cur.fetchall():
                    try:
                        bdata = json.loads(v)
                        text_piece = bdata.get('text') or extract_text_from_richtext(bdata.get('richText'))
                        role = 'User' if bdata.get('type') == 1 else 'Assistant'
                        if text_piece:
                            messages.append((role, text_piece, bdata.get('createdAt')))
                    except Exception:
                        continue

            # If we found messages, rewrite file with reconstructed content
            if messages:
                md_lines = []
                md_lines.append(f"# {composer_id}\n")
                md_lines.append(f"- **ComposerKey:** composerData:{composer_id}")
                if comp_data.get('createdAt'):
                    md_lines.append(f"- **Created:** {comp_data.get('createdAt')}")
                md_lines.append(f"- **SizeKB:** {round(len(row[0]) / 1024, 1)}")
                md_lines.append("")

                # Append ordered messages
                for role, txt, created in messages:
                    md_lines.append(f"## {role}")
                    if created:
                        md_lines.append(str(created))
                    md_lines.append(txt)
                    md_lines.append("")
                    md_lines.append("---\n")

                markdown_text = '\n'.join(md_lines)
                if markdown_text_size_bytes(markdown_text) < MIN_ARCHIVE_ARTIFACT_BYTES:
                    continue

                try:
                    md_file.write_text(markdown_text, encoding='utf-8')
                    repaired += 1
                except Exception:
                    continue

    finally:
        if conn:
            conn.close()

    if repaired:
        print(f"Repaired {repaired} placeholder files in {output_path}")
    return repaired

def format_timestamp(timestamp):
    """Format timestamp in local Eastern time."""
    if timestamp:
        if isinstance(timestamp, (int, float)):
            try:
                ts = timestamp / 1000 if timestamp > 1e12 else timestamp
                dt = datetime.fromtimestamp(ts, timezone.utc).astimezone(LOCAL_TIME_ZONE)
                return dt.strftime(f"%Y-%m-%d %H:%M:%S {LOCAL_TIME_ZONE_LABEL}")
            except:
                return str(timestamp)
        try:
            dt = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TIME_ZONE)
            else:
                dt = dt.astimezone(LOCAL_TIME_ZONE)
            return dt.strftime(f"%Y-%m-%d %H:%M:%S {LOCAL_TIME_ZONE_LABEL}")
        except:
            return timestamp
    return ""

def process_chat_file(chat_file_path, output_dir, workspace_id, overwrite=False, app_name="VSCode",
                      existing_by_session=None):
    """Process a single chat JSON file and convert to markdown."""
    try:
        chat_file_path = Path(chat_file_path)
        output_dir = Path(output_dir)
        session_id = chat_file_path.stem

        existing_output = None
        if existing_by_session is not None:
            existing_output = existing_by_session.get(session_id)
        elif not overwrite:
            existing_output = find_markdown_for_session(output_dir, session_id)

        if existing_output and not overwrite:
            if is_small_artifact_path(existing_output):
                return False, f"SKIP (exists-small): {existing_output.stem}"
            elif archive_file_is_current_for_source(existing_output, chat_file_path):
                return False, f"SKIP (exists): {existing_output.stem}"

        data = load_chat_session(chat_file_path)
        if not isinstance(data, dict):
            return False, f"SKIP (invalid session shape): {chat_file_path.stem}"

        raw_requests = data.get('requests', [])
        if not isinstance(raw_requests, list):
            raw_requests = []
        requests = [request for request in raw_requests if isinstance(request, dict)]

        # Extract metadata
        title = data.get('customTitle') or data.get('sessionId') or chat_file_path.stem
        title = sanitize_filename(title)

        # Skip if file already exists (incremental export)
        output_file = output_dir / f"{title}.md"
        if output_file.exists() and not overwrite:
            if is_small_artifact_path(output_file):
                return False, f"SKIP (exists-small): {title}"
            elif archive_file_is_current_for_source(output_file, chat_file_path):
                return False, f"SKIP (exists): {title}"

        message_count = len(requests)
        created_date = format_timestamp(data.get('creationDate'))
        last_message_date = format_timestamp(data.get('lastMessageDate'))

        # Build markdown content
        md_content = []
        md_content.append(f"# {title}\n")
        md_content.append(f"- **Source:** {app_name}")
        md_content.append(f"- **Session ID:** {chat_file_path.stem}")
        md_content.append(f"- **Workspace:** {workspace_id}")
        md_content.append(f"- **Messages:** {message_count}")
        if created_date:
            md_content.append(f"- **Created:** {created_date}")
        if last_message_date:
            md_content.append(f"- **Last message:** {last_message_date}")
        md_content.append(f"- **Source file:** {display_path(chat_file_path)}")
        md_content.append("")

        # Process each request/message
        for i, request in enumerate(requests, 1):
            timestamp = format_timestamp(request.get('messageTimestamp'))

            md_content.append(f"## Message {i} — {data.get('requesterUsername', 'User')}")
            if timestamp:
                md_content.append(f"*{timestamp}*\n")
            else:
                md_content.append("")

            # User message
            user_text = extract_request_text(request)
            if user_text:
                md_content.append(user_text)
                md_content.append("")

            # Assistant response
            md_content.append(f"{data.get('responderUsername', 'Assistant')}:\n")
            assistant_text = extract_response_text(request)
            if assistant_text:
                md_content.append(assistant_text)
            md_content.append("")
            md_content.append("---\n")

        if message_count == 0:
            return False, f"SKIP (no messages): {chat_file_path.stem}"

        markdown_text = '\n'.join(md_content)
        if markdown_text_size_bytes(markdown_text) < MIN_ARCHIVE_ARTIFACT_BYTES:
            return False, f"SKIP (<1KB): {chat_file_path.stem}"

        # Write output file (output_file already set above)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(markdown_text)

        # Preserve the original modification time from source JSON
        preserve_modification_time(chat_file_path, output_file)
        if existing_by_session is not None:
            existing_by_session[session_id] = output_file

        return True, title

    except Exception as e:
        return False, f"Error processing {chat_file_path}: {str(e)}"

def export_chats(source_path, output_path, app_name="VSCode", sample_per_workspace=0, sample_total=0,
                 overwrite=False, modified_after=None, modified_before=None):
    """Export all chats from the specified source path."""
    print(f"Starting {app_name} chat export...")
    print(f"Source: {display_path(source_path)}")
    print(f"Output: {display_path(output_path)}")

    # Expand environment variables
    source_path = os.path.expandvars(source_path)
    output_path = Path(os.path.expandvars(output_path))

    # Create output directory
    output_path.mkdir(parents=True, exist_ok=True)

    # Check if source exists
    source_dir = Path(source_path)
    if not source_dir.exists():
        print(f"Error: Source directory does not exist: {source_dir}")
        return False

    exported_count = 0
    skipped_count = 0
    error_count = 0
    existing_by_session = build_markdown_session_lookup(output_path)

    # Scan workspace directories
    # Process workspaces sorted by modification time (most recent first)
    workspace_dirs = [p for p in source_dir.iterdir() if p.is_dir()]
    try:
        workspace_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        # Fall back to unsorted iteration if stat fails for any reason
        workspace_dirs = [p for p in source_dir.iterdir() if p.is_dir()]

    for workspace_dir in workspace_dirs:

        chat_sessions_dir = workspace_dir / "chatSessions"
        if not chat_sessions_dir.exists():
            continue

        print(f"Processing workspace: {workspace_dir.name}")

        # Process each JSON file in chatSessions (optionally sample N per workspace, or sample_total overall)
        per_ws_count = 0

        # Collect and sort chat JSON/JSONL files by modification time (most recent first)
        json_files = [p for p in chat_sessions_dir.iterdir()
                      if p.is_file()
                      and p.suffix.lower() in ('.json', '.jsonl')
                      and path_modified_in_window(p, modified_after, modified_before)]
        try:
            json_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            json_files = [p for p in chat_sessions_dir.iterdir()
                          if p.is_file()
                          and p.suffix.lower() in ('.json', '.jsonl')
                          and path_modified_in_window(p, modified_after, modified_before)]

        for json_file in json_files:
            success, result = process_chat_file(
                json_file, output_path, workspace_dir.name, overwrite=overwrite, app_name=app_name,
                existing_by_session=existing_by_session,
            )
            if success:
                print(f"  Exported: {result}")
                exported_count += 1
                per_ws_count += 1
            elif result.startswith("SKIP"):
                skipped_count += 1
                # Don't print skips to reduce noise, but count them
            else:
                print(f"  Error: {result}")
                error_count += 1

            # If a global sample_total is set, stop entirely once reached
            if sample_total and exported_count >= sample_total:
                print(f"  Reached global sample limit ({sample_total}), stopping export...")
                return True

            if sample_per_workspace and per_ws_count >= sample_per_workspace:
                print(f"  Reached sample limit ({sample_per_workspace}) for workspace {workspace_dir.name}")
                break

    print(f"\nExport complete!")
    print(f"Successfully exported: {exported_count} chats")
    print(f"Skipped (already exist): {skipped_count} chats")
    if error_count > 0:
        print(f"Errors encountered: {error_count}")

    return exported_count > 0

def main():
    global CURSOR_DB_PATH, MIN_ARCHIVE_ARTIFACT_BYTES, _display_path

    # ── Quick single-file shortcut ──────────────────────────────────────
    if RUN_INDIVIDUAL_FILE:
        p = Path(RUN_INDIVIDUAL_PATH)
        if not p.exists():
            print(f"File not found: {p}")
            return 1
        # Auto-extract workspace id: path is …/<workspace_id>/chatSessions/<file>
        workspace_id = p.parent.parent.name if p.parent.name == "chatSessions" else "unknown"
        try:
            out = export_single_chat_file(p, DEFAULT_EXPORT_VSCODE, workspace_id=workspace_id, overwrite=True)
        except Exception as e:
            print(f"Single-file export failed: {e}")
            return 1
        print(f"Exported → {out}")
        return 0
    # ──────────────────────────────────────────────────────────────────────

    parser = argparse.ArgumentParser(description="Export chat transcripts and plan artifacts to a searchable archive")
    parser.add_argument('--config-ini', '--ini', dest='config_ini', default=None,
                       help='Config file to read before applying CLI overrides')
    parser.add_argument('--vscode', action='store_true', help='Export VSCode chats')
    parser.add_argument('--cursor', action='store_true', help='Export Cursor chats')
    parser.add_argument('--codex', action='store_true', help='Export Codex chats')
    parser.add_argument('--copilot-cli', action='store_true', help='Export Copilot CLI events.jsonl chats')
    parser.add_argument('--copilot-cli-only', action='store_true',
                       help='Export only Copilot CLI chats and plans')
    parser.add_argument('--plans', action='store_true', help='Copy/extract plan artifacts into the archive')
    parser.add_argument('--all', action='store_true', help='Export all known chat sources and plan artifacts')
    parser.add_argument('--clean-small', action='store_true',
                       help='Delete archived markdown files smaller than 1 KB before rebuilding the index')
    parser.add_argument('--clean-legacy-plans', action='store_true',
                       help='Delete archived plan markdown files missing source metadata')
    parser.add_argument('--clean-duplicates', action='store_true',
                       help='Delete duplicate archived markdown files with identical content')
    parser.add_argument('--input-file',
                       help='Process one explicit chat file (.json or .jsonl)')
    parser.add_argument('--output-dir',
                       help='Output directory to use with --input-file')
    parser.add_argument('--workspace-id',
                       help='Workspace ID label to embed with --input-file')
    parser.add_argument('--force', action='store_true',
                       help='Overwrite existing markdown exports instead of skipping current files')
    parser.add_argument('--dry-run', dest='dry_run', action='store_true', default=None,
                       help='Print the configured export plan without writing files')
    parser.add_argument('--run-live', dest='dry_run', action='store_false', default=None,
                       help='Override config dry_run=true and allow writes')
    parser.add_argument('--rename-uuids', action='store_true', 
                       help='Rename UUID-named files using LLM-generated titles')
    parser.add_argument('--vscode-source', default=None,
                       help='VSCode workspace storage path')
    parser.add_argument('--cursor-source', default=None,
                       help='Cursor workspace storage path')
    parser.add_argument('--cursor-db', default=None,
                       help='Cursor SQLite state database path')
    parser.add_argument('--archive-root', default=None,
                       help='Unified archive root directory')
    parser.add_argument('--index-path', default=None,
                       help='Archive index CSV path')
    parser.add_argument('--vscode-output', default=None,
                       help='VSCode export directory')
    parser.add_argument('--cursor-output', default=None,
                       help='Cursor export directory')
    parser.add_argument('--codex-source', default=None,
                       help='Codex sessions directory')
    parser.add_argument('--codex-output', default=None,
                       help='Codex export directory')
    parser.add_argument('--claude', action='store_true',
                       help='Export Claude Code sessions from the configured Claude projects directory')
    parser.add_argument('--claude-source', default=None,
                       help='Claude Code projects directory')
    parser.add_argument('--claude-output', default=None,
                       help='Claude Code export directory')
    parser.add_argument('--copilot-cli-source', default=None,
                       help='Copilot CLI session-state directory')
    parser.add_argument('--no-tool-summaries', action='store_true',
                       help='Omit [Tool: ...] summary lines from Claude/Codex exports')
    parser.add_argument('--sample', type=int, default=0,
                       help='Limit number of files exported per workspace (0 = all)')
    parser.add_argument('--test', action='store_true',
                       help='Export one item per selected source/type')

    args = parser.parse_args()

    cfg = load_config(args.config_ini)
    configure_titles(args.config_ini)
    _display_path = cfg.display_path
    archive_root_default = cfg.get("paths", "archive_root", "./archive").strip() or "./archive"
    archive_root = cfg.resolve_path(args.archive_root or archive_root_default)

    args.vscode_source = str(cfg.resolve_path(args.vscode_source or cfg.get("sources", "vscode_workspace_storage", VSCODE_CHAT_PATH)))
    args.cursor_source = str(cfg.resolve_path(args.cursor_source or cfg.get("sources", "cursor_workspace_storage", CURSOR_CHAT_PATH)))
    CURSOR_DB_PATH = str(cfg.resolve_path(args.cursor_db or cfg.get("sources", "cursor_db", CURSOR_DB_PATH)))
    args.codex_source = str(cfg.resolve_path(args.codex_source or cfg.get("sources", "codex_sessions", CODEX_SESSIONS_PATH)))
    args.claude_source = str(cfg.resolve_path(args.claude_source or cfg.get("sources", "claude_projects", CLAUDE_PROJECTS_PATH)))
    args.copilot_cli_source = str(cfg.resolve_path(args.copilot_cli_source or cfg.get("sources", "copilot_cli_state", COPILOT_CLI_STATE_PATH)))

    def output_path(cli_value, config_key, default_path):
        configured = cli_value or cfg.get("paths", config_key, "")
        return cfg.resolve_path(configured) if str(configured).strip() else Path(default_path)

    args.vscode_output = str(output_path(args.vscode_output, "vscode_output", archive_root / "vscode" / "chats"))
    args.cursor_output = str(output_path(args.cursor_output, "cursor_output", archive_root / "cursor" / "chats"))
    args.codex_output = str(output_path(args.codex_output, "codex_output", archive_root / "codex" / "chats"))
    args.claude_output = str(output_path(args.claude_output, "claude_output", archive_root / "claude" / "chats"))
    index_path = output_path(args.index_path, "index_path", archive_root / "index.csv")
    include_tool_summaries = cfg.get_bool("export", "include_tool_summaries", True) and not args.no_tool_summaries
    if cfg.get_bool("export", "overwrite_existing", False):
        args.force = True
    dry_run_enabled = cfg.get_bool("export", "dry_run", False) if args.dry_run is None else bool(args.dry_run)
    MIN_ARCHIVE_ARTIFACT_BYTES = cfg.get_int("export", "min_archive_artifact_bytes", MIN_ARCHIVE_ARTIFACT_BYTES)
    clean_small_enabled = args.clean_small or cfg.get_bool("export", "cleanup_small_artifacts", False)
    clean_legacy_enabled = args.clean_legacy_plans or cfg.get_bool("export", "cleanup_legacy_plan_artifacts", False)
    clean_duplicates_enabled = args.clean_duplicates or cfg.get_bool("export", "cleanup_duplicate_artifacts", False)
    repair_cursor_placeholders = cfg.get_bool("export", "repair_cursor_placeholders", True)
    sample_plan_artifacts = cfg.get_bool("export", "sample_plan_artifacts", True)
    overwrite_plan_artifacts = args.force or cfg.get_bool("export", "overwrite_plan_artifacts", False)
    overwrite_copilot_cli_artifacts = args.force or cfg.get_bool("export", "overwrite_copilot_cli_artifacts", False)
    modified_after, modified_before = build_modified_window(cfg, "export")

    claude_plan_sources = []
    for value in [cfg.get("sources", "claude_plans", CLAUDE_PLANS_PATH), cfg.get("sources", "claude_plan_mirror", CLAUDE_PLAN_MIRROR_PATH)]:
        if str(value).strip():
            claude_plan_sources.append(cfg.resolve_path(value))
    nia_plan_sources = cfg.resolve_path_list("sources", "extra_plan_paths", NIA_PLAN_PATHS)

    # Default to exporting VSCode+Cursor+Codex if no specific option given
    if not (
        args.vscode
        or args.cursor
        or args.codex
        or args.copilot_cli
        or args.copilot_cli_only
        or args.plans
        or args.all
        or args.claude
        or args.clean_small
        or args.clean_legacy_plans
        or args.clean_duplicates
    ):
        args.all = True

    success = False

    run_vscode = args.all or args.vscode
    run_cursor = args.all or args.cursor
    run_codex = args.all or args.codex
    run_claude = args.all or args.claude
    run_copilot_cli = args.all or args.copilot_cli or args.copilot_cli_only
    run_plan_phase = (
        args.all
        or args.plans
        or args.claude
        or args.copilot_cli
        or args.copilot_cli_only
    )

    # Determine sample behavior: --test requests a global sample of 1 per source
    if cfg.get_bool("export", "test_mode", False):
        args.test = True
    configured_sample_limit = cfg.get_int("export", "sample_limit", 0)
    sample_limit = args.sample if args.sample else configured_sample_limit
    sample_total = 1 if args.test else 0
    plan_sample_total = (sample_total if sample_total else sample_limit) if sample_plan_artifacts else 0

    if dry_run_enabled:
        print("DRY RUN - no files will be written, renamed, deleted, or indexed.")
        print(f"archive_root: {cfg.display_path(archive_root)}")
        print(f"index_path: {cfg.display_path(index_path)}")
        print(f"vscode_source: {cfg.display_path(args.vscode_source)}")
        print(f"cursor_source: {cfg.display_path(args.cursor_source)}")
        print(f"cursor_db: {cfg.display_path(CURSOR_DB_PATH)}")
        print(f"claude_source: {cfg.display_path(args.claude_source)}")
        print(f"codex_source: {cfg.display_path(args.codex_source)}")
        print(f"copilot_cli_source: {cfg.display_path(args.copilot_cli_source)}")
        print(f"vscode_output: {cfg.display_path(args.vscode_output)}")
        print(f"cursor_output: {cfg.display_path(args.cursor_output)}")
        print(f"claude_output: {cfg.display_path(args.claude_output)}")
        print(f"codex_output: {cfg.display_path(args.codex_output)}")
        print(f"run_vscode={run_vscode} run_cursor={run_cursor} run_claude={run_claude} run_codex={run_codex} run_copilot_cli={run_copilot_cli} run_plans={run_plan_phase}")
        print(f"test_mode={args.test} sample_limit={sample_limit} sample_total={sample_total}")
        print(f"modified_after={modified_after or 'none'}")
        print(f"modified_before={modified_before or 'none'}")
        print(f"overwrite_existing={args.force}")
        print(f"repair_cursor_placeholders={repair_cursor_placeholders}")
        print(f"sample_plan_artifacts={sample_plan_artifacts}")
        print(f"overwrite_plan_artifacts={overwrite_plan_artifacts}")
        print(f"overwrite_copilot_cli_artifacts={overwrite_copilot_cli_artifacts}")
        print(f"cleanup_small_artifacts={clean_small_enabled}")
        print(f"cleanup_legacy_plan_artifacts={clean_legacy_enabled}")
        print(f"cleanup_duplicate_artifacts={clean_duplicates_enabled}")
        if args.rename_uuids:
            print("mode: rename UUID files")
        if args.input_file:
            print(f"input_file: {cfg.display_path(cfg.resolve_path(args.input_file))}")
            output_dir = cfg.resolve_path(args.output_dir) if args.output_dir else Path(args.vscode_output or cfg.resolve_path(DEFAULT_EXPORT_VSCODE))
            print(f"input_output_dir: {cfg.display_path(output_dir)}")
        return 0

    # Handle rename-uuids mode
    if args.rename_uuids:
        print("Scanning for UUID-named files to rename...")
        vscode_count = rename_uuid_files(args.vscode_output)
        cursor_count = rename_uuid_files(args.cursor_output)
        codex_count = rename_uuid_files(args.codex_output)
        claude_count = rename_uuid_files(args.claude_output)
        print(f"\nRenamed {vscode_count} VSCode, {cursor_count} Cursor, {codex_count} Codex, {claude_count} Claude Code files")
        return 0

    if args.input_file:
        input_file = cfg.resolve_path(args.input_file)
        output_dir = cfg.resolve_path(args.output_dir) if args.output_dir else Path(args.vscode_output or cfg.resolve_path(DEFAULT_EXPORT_VSCODE))
        try:
            output_path = export_single_chat_file(
                input_file,
                output_dir,
                workspace_id=args.workspace_id,
                overwrite=args.force,
            )
        except Exception as e:
            print(f"Single-file export failed: {e}")
            return 1

        print(f"Exported single chat to {display_path(output_path)}")
        return 0

    if run_vscode:
        if export_chats(
            args.vscode_source, args.vscode_output, "VSCode",
            sample_per_workspace=sample_limit, sample_total=sample_total,
            overwrite=args.force,
            modified_after=modified_after, modified_before=modified_before,
        ):
            success = True

    if run_cursor:
        # If cursor-source is a directory, export like VSCode; else use DB
        cursor_source_path = os.path.expandvars(args.cursor_source)
        if os.path.isdir(cursor_source_path):
            if export_chats(
                args.cursor_source, args.cursor_output, "Cursor",
                sample_per_workspace=sample_limit, sample_total=sample_total,
                overwrite=args.force,
                modified_after=modified_after, modified_before=modified_before,
            ):
                success = True
        else:
            # Use new SQLite-based export for Cursor (DB export uses sample_total if set, otherwise sample_limit)
            db_max = sample_total if sample_total else sample_limit
            if export_cursor_from_db(
                args.cursor_output,
                max_items=db_max,
                skip_repair=not repair_cursor_placeholders,
                overwrite=args.force,
                modified_after=modified_after,
                modified_before=modified_before,
            ) > 0:
                success = True

    if run_codex:
        codex_sample_total = sample_total if sample_total else sample_limit
        if export_codex_sessions(
            args.codex_output,
            sessions_path=args.codex_source,
            sample_total=codex_sample_total,
            include_tool_summaries=include_tool_summaries,
            overwrite=args.force,
            modified_after=modified_after, modified_before=modified_before,
        ):
            success = True

    if run_claude:
        if export_claude_sessions(
            args.claude_output,
            projects_path=args.claude_source,
            sample_per_project=sample_limit,
            sample_total=sample_total,
            include_tool_summaries=include_tool_summaries,
            overwrite=args.force,
            modified_after=modified_after, modified_before=modified_before,
        ):
            success = True

    extra_index_rows = []

    if run_copilot_cli:
        copilot_rows = export_copilot_cli_sessions(
            archive_root / "copilot-cli" / "chats",
            state_path=args.copilot_cli_source,
            max_items=sample_total if sample_total else sample_limit,
            modified_after=modified_after,
            modified_before=modified_before,
            overwrite=overwrite_copilot_cli_artifacts,
        )
        if copilot_rows:
            success = True

    if run_plan_phase:
        if not args.copilot_cli_only:
            copied_sources = set()
            claude_plan_hashes = set()
            for claude_plan_path in claude_plan_sources:
                extra_index_rows.extend(copy_plan_markdowns(
                    claude_plan_path,
                    archive_root / "claude" / "plans",
                    copied_sources=copied_sources,
                    copied_hashes=claude_plan_hashes,
                    max_items=plan_sample_total,
                    modified_after=modified_after,
                    modified_before=modified_before,
                    overwrite=overwrite_plan_artifacts,
                ))
            nia_plan_hashes = set()
            for nia_plan_path in nia_plan_sources:
                extra_index_rows.extend(copy_plan_markdowns(
                    nia_plan_path,
                    archive_root / "nia" / "plans",
                    copied_hashes=nia_plan_hashes,
                    max_items=plan_sample_total,
                    modified_after=modified_after,
                    modified_before=modified_before,
                    overwrite=overwrite_plan_artifacts,
                ))
            extra_index_rows.extend(extract_codex_plans(
                archive_root / "codex" / "plans",
                sessions_path=args.codex_source,
                max_items=plan_sample_total,
                modified_after=modified_after,
                modified_before=modified_before,
                overwrite=overwrite_plan_artifacts,
            ))

        if args.all or args.plans or args.copilot_cli or args.copilot_cli_only:
            extra_index_rows.extend(copy_copilot_cli_plans(
                archive_root / "copilot-cli" / "plans",
                state_path=args.copilot_cli_source,
                max_items=plan_sample_total,
                modified_after=modified_after,
                modified_before=modified_before,
                overwrite=overwrite_plan_artifacts,
            ))

        if extra_index_rows:
            success = True

    codex_only = args.codex and not (
        args.all
        or args.vscode
        or args.cursor
        or args.claude
        or args.copilot_cli
        or args.copilot_cli_only
        or args.plans
    )
    if codex_only:
        cleanup_root = archive_root / "codex"
    elif args.copilot_cli_only:
        cleanup_root = archive_root / "copilot-cli"
    else:
        cleanup_root = archive_root

    if clean_small_enabled:
        cleanup_removed = cleanup_small_archive_artifacts(cleanup_root)
        if cleanup_removed:
            success = True

    if clean_legacy_enabled:
        legacy_removed = cleanup_legacy_plan_artifacts(cleanup_root)
        if legacy_removed:
            success = True

    if clean_duplicates_enabled:
        duplicate_removed = cleanup_duplicate_archive_artifacts(cleanup_root)
        if duplicate_removed:
            success = True

    index_count = build_archive_index(archive_root, index_path, extra_rows=extra_index_rows, display_path=cfg.display_path)
    if index_count:
        success = True

    if not success:
        print("No chats were exported. Check your paths and ensure you have chat data.")
        return 1

    return 0

if __name__ == "__main__":
    exit(main())
