# cleanup_music_folder_files.py

A utility to clean up and rename files in music folders according to a consistent set of rules.

Overview
--------
This script processes a single directory (or recursively with `--recursive`) and:
- Detects audio files by MIME type (prefers python-magic if installed).
- Deduplicates files by SHA256 checksum and keeps the preferred copy.
- Handles PDFs, CUE and LOG files:
  - CUE and LOG are treated like PDFs but without reading PDF metadata.
  - PDFs may be renamed using PDF metadata (Title/Author) when PyPDF2 is installed.
  - If only one PDF is present it is renamed to `FolderName.pdf`. If multiple PDFs are present they are numbered `FolderName - 1.pdf`, `FolderName - 2.pdf`, etc.
- Groups audio files by leading track number (e.g. `01 - Title.flac` or `01 Title.flac`). The canonical audio filename for the track is chosen according to:
  1. modification time (mtime) — newest preferred
  2. audio bitrate (if mutagen is installed and can read the file)
  3. file size (larger preferred)
  4. a configured extension preference for lossy formats
  5. deterministic fallback
- Quality rules:
  - Uncompressed / "raw" formats (e.g. `.wav`, `.aiff`, and retro tracker formats like `.sid`, `.mod`) are preserved — if present, they are not removed in favour of other formats.
  - If both lossless (e.g. `.flac`) and lossy (e.g. `.mp3`) files exist for the same track, lossless is preferred.
  - If only lossy formats exist, the selection follows the criteria above.
- Sidecar files (e.g. `.lrc`, `.txt`) with the same leading track number are renamed to match the canonical audio base name. If a target exists, modification times decide whether to replace or delete the source.
- Files without whitespace in the filename are ignored for track-pattern processing (but still considered during checksum deduplication).
- dot-directories (starting with `.`) and symlinked directories are excluded when running recursively.


Safety
----------------
Use with caution: run with `-v` or `-vv` first to verify the proposed changes before using `--force`. It is recommended to test on a copy of your data first.


Default behaviour
-----------------
- Dry-run by default: proposed actions are only displayed.
- Use `--force` (or `-f`) to apply the changes.

Dependencies (optional but recommended)
--------------------------------------
- mutagen (for bitrate and length extraction; improves selection among audio files)
  - pip install mutagen
- PyPDF2 (for PDF metadata extraction)
  - pip install PyPDF2
- python-magic (for more reliable MIME detection)
  - pip install python-magic
  - On Windows use `python-magic-bin` if appropriate.

Usage
-----
Basic dry-run (shows what would be done):
```
python cleanup_music_folder_files.py --path '/path/to/album' -v
```

Show detailed internal INFO messages:
```
python cleanup_music_folder_files.py --path '/path/to/album' -vv
```

Apply changes:
```
python cleanup_music_folder_files.py --path '/path/to/album' --force -v
```

Recurse into subdirectories:
```
python cleanup_music_folder_files.py --path '/path/to/music' --recursive -v
```


Behaviour notes
--------------
- The script treats Unicode characters literally (no normalization). Characters that are invalid in filenames on the platform are sanitised when building new names from metadata.
- The script records a list of concrete proposed actions during a dry-run. When run with `-v` this list is printed; with `--force` the same list is shown as executed actions.
- By default the script skips files that do not contain a space in their filename (except PDFs/CUE/LOG), because such files are commonly system files or art files. You can change this behaviour in the script by editing the `IGNORE_NO_SPACE` flag.


Configuration
-------------
This tool supports a TOML configuration file to make local/global adjustments without editing the script.

Default config filename (checked in order):
1. `--config / -c <path/to/config.toml>`
2. `./config.toml`
3. `~/.config/rename_music/config.toml`

Behavior
--------
- The script merges values from the config into safe defaults.
- Validation checks types and basic shapes (lists/booleans).
- CLI flags still override runtime behaviour (e.g. `--force`, `--recursive`).

Usage examples
--------------
- Dry-run (default), using config in `.`:
  `python cleanup_music_folder_files.py --path '/path/to/album' -v`

- Specify config explicitly:
  `python cleanup_music_folder_files.py -p '/path/to/music' -c ~/.config/rename_music/config.toml -v -r`

- Apply changes to an entire music collection (recursively), showing a summary:
  `python cleanup_music_folder_files.py -p '/path/to/music' --force -v -r`


