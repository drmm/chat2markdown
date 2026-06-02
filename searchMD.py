from __future__ import annotations

import csv
import re
import argparse
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Iterable

from chatmain_config import load_config

# Folder names to skip while recursively walking. Keep "archive" searchable by
# default because old scripts often live there.
EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
    "node_modules",
    "site-packages",
    "dist",
    "build",
    ".next",
    ".vite",
    "coverage",
    "htmlcov",
    "playwright-report",
    "test-results",
    "screenshots",
    "backups",
    "backup",
}

MIN_SIZE_BYTES = 2 * 1024

# sort options:
# "count_match_all", "count_match_any", "modified_recent", "modified_oldest",
# "inTitle", "count_high", "count_low"
SORT_BY = "count_high"

# require at least one hit from configured match_any or match_all terms
REQUIRE_SOME_MATCH = True

# recurse into subfolders
RECURSIVE = True
# ----------------------------


def compile_pattern(term: str) -> re.Pattern:
    escaped = re.escape(term.strip())
    # whole-phrase, case-insensitive
    return re.compile(rf"(?i)\b{escaped}\b")


def count_term_occurrences(text: str, terms: list[str]) -> int:
    total = 0
    for term in terms:
        if term.strip():
            total += len(compile_pattern(term).findall(text))
    return total


def all_terms_present(text: str, terms: list[str]) -> bool:
    active = [t.strip() for t in terms if t.strip()]
    if not active:
        return False
    return all(compile_pattern(term).search(text) for term in active)


def any_term_present(text: str, terms: list[str]) -> bool:
    active = [t.strip() for t in terms if t.strip()]
    if not active:
        return False
    return any(compile_pattern(term).search(text) for term in active)


def iter_files(
    folders: Iterable[str],
    exclude_dirs: Iterable[str] | None = None,
    filetypes: Iterable[str] | None = None,
    recursive: bool | None = None,
) -> Iterable[Path]:
    excluded = {name.lower() for name in (exclude_dirs or EXCLUDE_DIRS)}
    allowed = {item if item.startswith(".") else f".{item}" for item in (filetypes or [])}
    do_recursive = RECURSIVE if recursive is None else recursive
    for folder in folders:
        base = Path(folder)
        if not base.exists():
            continue
        if do_recursive:
            for root, dirs, files in os.walk(base):
                dirs[:] = [name for name in dirs if name.lower() not in excluded]
                for filename in files:
                    path = Path(root) / filename
                    if path.suffix.lower() in allowed:
                        yield path
        else:
            for path in base.glob("*"):
                if path.is_file() and path.suffix.lower() in allowed:
                    yield path


def parse_datetime_setting(value: object, date_only_end: bool = False) -> datetime | None:
    text = str(value or "").strip().strip('"\'')
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        clock = (23, 59, 59) if date_only_end else (0, 0, 0)
        base = datetime.strptime(text, "%Y-%m-%d")
        return base.replace(hour=clock[0], minute=clock[1], second=clock[2])
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


def parse_optional_int(value: object) -> int | None:
    text = str(value or "").strip()
    return int(text) if text else None


def build_modified_window(cfg) -> tuple[datetime | None, datetime | None]:
    modified_after = parse_datetime_setting(cfg.get("search", "modified_after", ""))
    modified_before = parse_datetime_setting(cfg.get("search", "modified_before", ""), date_only_end=True)
    lookback_days = parse_optional_int(cfg.get("search", "lookback_days", ""))
    if modified_after is None and lookback_days is not None:
        modified_after = datetime.now() - timedelta(days=lookback_days)
    return modified_after, modified_before


def file_in_date_range(path: Path, modified_after: datetime | None = None, modified_before: datetime | None = None) -> bool:
    modified = datetime.fromtimestamp(path.stat().st_mtime)
    if modified_after and modified < modified_after:
        return False
    if modified_before and modified > modified_before:
        return False
    return True


def sort_rows(rows: list[dict], sort_by: str | None = None) -> list[dict]:
    sort_by = sort_by or SORT_BY
    if sort_by == "count_match_all":
        return sorted(rows, key=lambda r: r["count_all"], reverse=True)
    if sort_by == "count_match_any":
        return sorted(rows, key=lambda r: r["count_any"], reverse=True)
    if sort_by == "modified_recent":
        return sorted(rows, key=lambda r: r["_modified_dt"], reverse=True)
    if sort_by == "modified_oldest":
        return sorted(rows, key=lambda r: r["_modified_dt"])
    if sort_by == "inTitle":
        return sorted(rows, key=lambda r: (r["in_title"], r["count_total"]), reverse=True)
    if sort_by == "count_low":
        return sorted(rows, key=lambda r: r["count_total"])
    # default: "count_high"
    return sorted(rows, key=lambda r: r["count_total"], reverse=True)


def print_table(rows: list[dict], limit: int | None = None) -> None:
    display = rows if limit is None else rows[:limit]

    headers = ["title", "count_total", "count_all", "count_any", "in_title", "name_match", "modified", "path"]
    widths = {h: len(h) for h in headers}

    for row in display:
        for h in headers:
            widths[h] = min(max(widths[h], len(str(row[h]))), 80)

    def fmt(val: str, width: int) -> str:
        s = str(val)
        return s if len(s) <= width else s[: width - 3] + "..."

    line = " | ".join(h.ljust(widths[h]) for h in headers)
    sep = "-+-".join("-" * widths[h] for h in headers)
    print(line)
    print(sep)

    for row in display:
        print(" | ".join(fmt(row[h], widths[h]).ljust(widths[h]) for h in headers))

def main() -> None:
    parser = argparse.ArgumentParser(description="Search markdown folders and write a CSV of matches.")
    parser.add_argument("--config-ini", "--ini", dest="config_ini", default=None,
                        help="Config file to read before applying CLI overrides.")
    parser.add_argument("--any", action="append", dest="match_any",
                        help="Term that may appear. Repeat for multiple terms.")
    parser.add_argument("--all", action="append", dest="match_all",
                        help="Term that must appear. Repeat for multiple terms.")
    parser.add_argument("--folder", action="append", dest="folders",
                        help="Folder to search. Repeat for multiple folders.")
    parser.add_argument("--filetype", action="append", dest="filetypes",
                        help="File extension to search, such as .md or .py. Repeat for multiple types.")
    parser.add_argument("--output", default=None,
                        help="CSV output path.")
    parser.add_argument("--min-size", type=int, default=None,
                        help="Minimum markdown file size in bytes.")
    parser.add_argument("--no-min-size", action="store_true",
                        help="Search markdown files regardless of size.")
    parser.add_argument("--exclude-dir", action="append", dest="exclude_dirs", default=[],
                        help="Directory name to skip while recursing. Repeat for multiple names.")
    args = parser.parse_args()

    cfg = load_config(args.config_ini)
    folders = [str(cfg.resolve_path(folder)) for folder in (args.folders or cfg.get_list("search", "folders"))]
    match_any = args.match_any if args.match_any is not None else cfg.get_list("search", "match_any")
    match_all = args.match_all if args.match_all is not None else cfg.get_list("search", "match_all")
    configured_min_size = cfg.get_int("search", "min_size_bytes", MIN_SIZE_BYTES)
    min_size = 0 if args.no_min_size else (args.min_size if args.min_size is not None else configured_min_size)
    exclude_dirs = set(cfg.get_list("search", "exclude_dirs", EXCLUDE_DIRS)) | set(args.exclude_dirs or [])
    filetypes = args.filetypes if args.filetypes is not None else cfg.get_list("search", "filetypes")
    recursive = cfg.get_bool("search", "recursive", RECURSIVE)
    sort_by = cfg.get("search", "sort_by", SORT_BY)
    require_some_match = cfg.get_bool("search", "require_some_match", REQUIRE_SOME_MATCH)
    modified_after, modified_before = build_modified_window(cfg)
    archive_root = cfg.resolve_path(cfg.get("paths", "archive_root", "./archive"))
    configured_output = args.output or cfg.get("paths", "search_output", "")
    output_path = cfg.resolve_path(configured_output) if str(configured_output).strip() else archive_root / "search" / "search.csv"

    if not folders:
        raise SystemExit("No search folders configured. Set [search] folders in config.ini or pass --folder.")
    if not filetypes:
        raise SystemExit("No filetypes configured. Set [search] filetypes in config.ini or pass --filetype.")

    rows: list[dict] = []
    scanned = 0

    for path in iter_files(folders, exclude_dirs, filetypes=filetypes, recursive=recursive):
        try:
            stat = path.stat()
            if stat.st_size <= min_size:
                continue
            if not file_in_date_range(path, modified_after, modified_before):
                continue

            scanned += 1

            text = path.read_text(encoding="utf-8", errors="ignore")
            title = path.stem
            combined = f"{title}\n{text}"

            count_any = count_term_occurrences(combined, match_any)
            all_present = all_terms_present(combined, match_all)
            count_all = count_term_occurrences(combined, match_all) if all_present else 0
            count_total = count_any + count_all

            in_title = count_term_occurrences(title, match_any + match_all)
            name_match = "yes" if in_title > 0 else "no"

            active_all = [t.strip() for t in match_all if t.strip()]
            if active_all and not all_present:
                continue

            if require_some_match and count_total == 0:
                continue

            modified_dt = datetime.fromtimestamp(stat.st_mtime)

            rows.append(
                {
                    "title": title,
                    "count_total": count_total,
                    "count_all": count_all,
                    "count_any": count_any,
                    "in_title": in_title,
                    "name_match": name_match,
                    "modified": modified_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "path": cfg.display_path(path),
                    "_modified_dt": modified_dt,
                }
            )
        except Exception as e:
            print(f"Skipping {path}: {e}")

    rows = sort_rows(rows, sort_by)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["title", "count_total", "count_all", "count_any", "in_title", "name_match", "modified", "path"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: v for k, v in row.items() if not k.startswith("_")})

    print("\n=== Search Complete ===")
    print(f"Scanned files: {scanned}")
    print(f"Matches found: {len(rows)}")
    print(f"CSV saved to: {cfg.display_path(output_path)}")
    if modified_after or modified_before:
        print(f"Modified after: {modified_after or 'none'}")
        print(f"Modified before: {modified_before or 'none'}")

    if rows:
        print("\nTop results:\n")
        print_table(rows[:10])
    else:
        print("\nNo matches found.")

if __name__ == "__main__":
    main()

