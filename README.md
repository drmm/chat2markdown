# Portable ChatMain Refactor

This folder is a copy-based refactor of the ChatMain utilities. The original files one directory up are intentionally untouched.

## What It Does

- `export_chat_archive.py` exports VS Code, Cursor, Claude Code, Codex, and Copilot CLI chat/session data into Markdown under the configured archive folder.
- `filter_chat.py` filters exported Markdown conversations by role, date, message length, duplicate content, and tool-message status.
- `searchMD.py` searches configured folders for terms and writes a CSV of matching files.
- `title_chat.py` creates readable Markdown filenames/titles using local Ollama first, then optional cloud providers from `.env`.
- `exportChats.py` remains a compatibility wrapper for the exporter.

## Configuration

Copy `config.example.ini` to `config.ini` for local use. Keep `config.example.ini` as the shareable template. Do not commit `config.ini`, `.env`, `archive/`, or `filtered/`; those can contain local paths, generated chat transcripts, and private conversation content.

Path tokens:

- `%USER%` means the current user's home folder.
- `%APPDATA%` and `%LOCALAPPDATA%` use the machine environment variables.
- `%SYSTEMROOT%` and `%WINDIR%` cover Windows system paths when source metadata points there.
- `%DRIVE_C%` style tokens are used as a last-resort display form for absolute drive paths.
- `${CONFIG_DIR}` means this folder.
- Relative paths like `./archive` resolve from the folder containing `config.ini`.

The Python files in this folder should not need machine-specific drive paths. Change `archive_root` in `config.ini` to move or rename the whole archive folder. Leave the per-source output paths blank unless you want one source to write somewhere different.

Cleanup is disabled by default. Leave `cleanup_small_artifacts`, `cleanup_legacy_plan_artifacts`, and `cleanup_duplicate_artifacts` set to `false` when testing against an existing archive you do not want pruned.

Useful safety settings:

- `dry_run = true` previews an export without writing, renaming, deleting, or rebuilding the index.
- `test_mode = true` limits selected sources to one item per source/type where supported.
- `overwrite_existing = false` skips or creates numbered outputs instead of replacing existing files.
- `lookback_days`, `modified_after`, and `modified_before` limit exports/searches by source file modification date.

## Config Reference

| Setting | Controls | Blank Means |
|---|---|---|
| `paths.archive_root` | Main archive folder. Rename or move the archive by changing this. | Use `./archive` from the template. |
| `paths.index_path` | Where `index.csv` is written. | Write `index.csv` inside `archive_root`. |
| `paths.filter_output_dir` | Where filtered markdown files are written. | Use `./filtered`. |
| `paths.search_output` | Default CSV path for `searchMD.py`. | Write under `archive_root/search/search.csv`. |
| `paths.vscode_output`, `cursor_output`, `claude_output`, `codex_output` | Per-source archive folders. | Inherit from `archive_root`. |
| `sources.vscode_workspace_storage` | VS Code chat source folder. | No VS Code source. |
| `sources.cursor_workspace_storage` | Cursor workspace source folder. | No Cursor workspace source. |
| `sources.cursor_db` | Cursor SQLite chat database. | No Cursor DB source. |
| `sources.claude_projects` | Claude Code session source folder. | No Claude chat source. |
| `sources.claude_plans` | Claude plan markdown source folder. | No Claude plan source. |
| `sources.claude_plan_mirror` | Optional mirrored Claude plan folder. | Off. |
| `sources.codex_sessions` | Codex session source folder. | No Codex source. |
| `sources.copilot_cli_state` | Copilot CLI session-state source folder. | No Copilot CLI source. |
| `sources.extra_plan_paths` | Extra markdown plan folders copied when running `--plans` or `--all`. Separate multiple folders with semicolons. | Off. |
| `export.dry_run` | Preview export plan without writing, renaming, deleting, or indexing. | `false` in the example. |
| `export.test_mode` | Export one item per selected source/type where supported. | Off. |
| `export.sample_limit` | Limit exported files per workspace/source. | `0` means no limit. |
| `export.lookback_days` | Only export source files modified within the last N days. | No lookback limit. |
| `export.modified_after`, `modified_before` | Exact export date window. | No date limit. |
| `export.cleanup_*` | Optional archive pruning actions. | Off. |
| `filter.import_file` | Markdown chat file to filter. | No input file. |
| `filter.roles` | Roles to keep after filtering. | Keep none unless CLI overrides. |
| `filter.exclude_roles` | Roles to remove entirely. | Remove none. |
| `filter.date_time_after`, `date_time_before` | Message timestamp window for filtering. | No message date limit. |
| `filter.output_mode` | `filtered_only` or `all` outputs. | Uses script default. |
| `filter.overwrite_existing` | Whether filter outputs can replace existing files. | `false`; create numbered outputs. |
| `search.folders` | Folders `searchMD.py` scans. | No search folder. |
| `search.filetypes` | File extensions to scan, such as `.md, .py`. | No filetypes. |
| `search.match_any`, `match_all` | Search terms. | Use CLI terms or no terms. |
| `search.lookback_days`, `modified_after`, `modified_before` | Search file modified-date window. | No date limit unless configured. |
| `titles.*` | Title-generation settings and optional model providers. | Local/default title behavior. |

## Optional Roadmap

- Add per-source date windows, such as separate Codex or Claude lookback settings.
- Add a config validation command that prints every resolved path and whether it exists.
- Add a temporary proof export command that writes one chat and one plan per selected source into a temp folder.

## Example Commands

```bash
python export_chat_archive.py --config-ini config.ini --codex --test
python export_chat_archive.py --config-ini config.ini --codex --test --run-live
python filter_chat.py --config-ini config.ini
python searchMD.py --config-ini config.ini --any optimizer --output "%TEMP%/chat2markdown-search.csv"
```
