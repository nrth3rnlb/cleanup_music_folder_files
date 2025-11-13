# -*- coding: utf-8 -*-
"""
cleanup_music_folder_files.py
Clean and rename music folders (audio, sidecars, PDFs, CUE/LOG). Recursive option.

Configuration:
 - TOML config is loaded from (in order):
     1) CLI --config / -c
     2) ./config.toml
     3) ~/.config/rename_music/rename_music.toml

Usage:
  python cleanup_music_folder_files.py --path /path/to/music -c ./config.toml -v -r
Dry-run is default; use --force / -f to apply changes.
"""
from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import sys
from collections import defaultdict
from importlib.metadata import version, PackageNotFoundError
from typing import List, Dict, Set, Optional, Tuple, Union

try:
    __version__ = version("cleanup_music_folder_files")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

# TOML loader: prefer stdlib tomllib (3.11+), fallback to tomli if available
try:
    import tomllib  # Python 3.11+

    def load_toml_file(path: str) -> dict:
        with open(path, 'rb') as fh:
            return tomllib.load(fh)
except Exception:
    try:
        import tomli

        def load_toml_file(path: str) -> dict:
            with open(path, 'rb') as fh:
                return tomli.load(fh)
    except Exception:
        load_toml_file = None  # no TOML support available

# Optional libs
try:
    import magic  # python-magic
    HAVE_MAGIC = True
except Exception:
    HAVE_MAGIC = False

try:
    from PyPDF2 import PdfReader
    HAVE_PYPDF2 = True
except Exception:
    HAVE_PYPDF2 = False

ACTION_PREFIX_WIDTH = 22

# ---------- Defaults (overridable via config.toml) ----------
PDF_EXTS: Set[str] = {'.pdf'}
AUX_EXTS: Set[str] = {'.cue'}

SIDECAR_EXTS: Set[str] = {
    '.lrc',
    '.txt'}
SIDECAR_PREFIXES: List[str] = [
    'cover',
    'albumart',
    'folder',
    'front',
    'back',
    'poster']

IGNORE_NO_SPACE = True
DRY_RUN_DEFAULT = True
RECURSIVE_DEFAULT = True

TRACK_RE = re.compile(
    r'^\s*(\d{1,2})\s*(?:[-\u2010\u2013\u2014\u2212]\s*|\s+)?(.+)$',
    flags=re.UNICODE)
ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00]')

# ---------- Runtime state ----------
action_counters = defaultdict(int)
action_log: List[str] = []  # collects concrete actions (for the summary)
ROOT_BASE: Optional[str] = None  # set in main() for relative path display

# ---------- Output helpers ----------


def _print(msg: str = ""):
    print(msg, flush=True)


def vinfo(msg: str, verbose_level: int, indent: int = 0):
    if verbose_level >= 2:
        _print(f"{' ' * indent}[INFO] {msg}")


def vwarn(msg: str, verbose_level: int, indent: int = 0):
    if verbose_level >= 1:
        _print(f"{' ' * indent}[WARN] {msg}")


def _relpath_display(path: str) -> str:
    if not path:
        return path
    if ROOT_BASE:
        try:
            rel = os.path.relpath(path, ROOT_BASE)
        except Exception:
            rel = path
    else:
        rel = path
    return rel.replace(os.sep, '/')


def _format_filepath(filepath: Union[str, Tuple[str, str]]) -> str:
    if isinstance(filepath, (tuple, list)):
        src, dst = filepath
        return f"Reformat filename: [{_relpath_display(src)}] -> [{_relpath_display(dst)}]"
    return _relpath_display(filepath)


def vaction(msg: str, verbose_level: int, dry_run: bool,
            indent: int = 0, filepath: Union[None, str, Tuple[str, str]] = None):
    prefix = "[DRY]" if dry_run else "[DO ]"
    if filepath:
        fp = _format_filepath(filepath)
        entry = f"{prefix} {msg} :: {fp}"
    else:
        entry = f"{prefix} {msg}"
    action_log.append(entry)
    if verbose_level >= 1:
        _print(f"{' ' * indent}{entry}")


def brief(msg: str):
    _print(msg)

# ---------- Filesystem / metadata helpers ----------


def get_mime_type(path: str) -> Optional[str]:
    if HAVE_MAGIC:
        try:
            return magic.from_file(path, mime=True)
        except Exception:
            pass
    t, _ = mimetypes.guess_type(path)
    return t


def is_audio(path: str) -> bool:
    t = get_mime_type(path)
    return bool(t and t.startswith('audio'))


def file_mtime(path: str) -> float:
    return os.path.getmtime(path)


def file_size(path: str) -> int:
    return os.path.getsize(path)


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def sanitize_filename_component(s: str) -> str:
    s2 = ILLEGAL_CHARS_RE.sub(' - ', s)
    s2 = re.sub(r'\s+', ' ', s2).strip()
    return s2


def extract_track_info(filename: str) -> Optional[tuple[int, str]]:
    base = os.path.splitext(filename)[0]
    if IGNORE_NO_SPACE and ' ' not in filename:
        return None
    m = TRACK_RE.match(base)
    if not m:
        return None
    try:
        track = int(m.group(1))
    except ValueError:
        return None
    rest = m.group(2).strip()
    return track, rest


def read_pdf_metadata(path: str) -> dict:
    meta = {}
    if not HAVE_PYPDF2:
        return meta
    try:
        reader = PdfReader(path)
        info = reader.metadata
        if not info:
            return meta
        title = info.title if hasattr(
            info, 'title') else info.get('/Title') or info.get('Title')
        author = info.author if hasattr(
            info, 'author') else info.get('/Author') or info.get('Author')
        if title:
            meta['Title'] = str(title).strip()
        if author:
            meta['Author'] = str(author).strip()
    except Exception:
        pass
    return meta


def unique_target_path(base_dir: str, base_name: str, ext: str) -> str:
    """
    Generate a unique target path for base_name + ext in base_dir.
    Preserve base_name exactly (including trailing dots). If the plain candidate
    exists, add " - N" suffixed names until a non-existing path is found.
    """
    candidate = f"{base_name}{ext}"
    full = os.path.join(base_dir, candidate)
    if not os.path.exists(full):
        return full
    suffix = 1
    while True:
        candidate = f"{base_name} - {suffix}{ext}"
        full = os.path.join(base_dir, candidate)
        if not os.path.exists(full):
            return full
        suffix += 1


def choose_preferred_by_mtime(paths: List[str]) -> str:
    """Choose preferred file from paths by modification time (newest)."""
    mtimes = [file_mtime(p) for p in paths]
    try:
        max_mtime = max(mtimes)
    except ValueError:
        raise ValueError("Cannot choose preferred file from empty path list.")

    candidates = [p for p, m in zip(paths, mtimes) if m == max_mtime]
    if len(candidates) == 1:
        return candidates[0]

    return sorted(candidates)[0]

# ---------- Sidecar prefix detection ----------

def is_sidecar_by_prefix(filename: str) -> bool:
    base = os.path.splitext(filename)[0].lower()
    for prefix in SIDECAR_PREFIXES:
        p = prefix.lower()
        # match exact or startswith + separator (e.g. 'cover', 'cover.jpg',
        # 'cover-1.jpg')
        if base == p:
            return True
        if base.startswith(p + ' ') or base.startswith(p +
                                                       '-') or base.startswith(p + '_') or base.startswith(p + '.'):
            return True
        if base.startswith(p):
            # allow cover1, cover2? be conservative: require next char to be
            # non-alpha if longer
            if len(base) > len(p) and not base[len(p)].isalpha():
                return True
    return False

# ---------- Core operations ----------


def process_aux_files(dirpath: str, files: List[str], exts: Set[str],
                      use_pdf_meta: bool, dry_run: bool, verbose_level: int) -> None:
    selected = [f for f in files if os.path.splitext(f)[1].lower() in exts]
    if not selected:
        return

    vinfo(f"Found files ({', '.join(sorted(exts))}): {selected}", verbose_level, indent=2)

    checksum_map: Dict[str, List[str]] = {}
    for fn in selected:
        path = os.path.join(dirpath, fn)
        try:
            h = sha256_of_file(path)
        except Exception as e:
            vwarn(f"Warning: could not compute SHA256 for {fn}: {e}", verbose_level, indent=4)
            continue
        checksum_map.setdefault(h, []).append(fn)

    survivors: List[str] = []
    for h, group in checksum_map.items():
        if len(group) == 1:
            survivors.append(group[0])
            continue
        group_paths = [os.path.join(dirpath, g) for g in group]
        preferred = choose_preferred_by_mtime(group_paths)
        newest_name = os.path.basename(preferred)
        survivors.append(newest_name)
        for g in group:
            if g == newest_name:
                continue
            path = os.path.join(dirpath, g)
            action_counters['removed_duplicates'] += 1
            vaction(
                f"Remove duplicate (checksum) {g}",
                verbose_level,
                dry_run,
                indent=4)
            if not dry_run:
                try:
                    os.remove(path)
                except Exception as e:
                    vwarn(
                        f"Error removing {path}: {e}",
                        verbose_level,
                        indent=6)

    survivors_paths = [os.path.join(dirpath, s) for s in survivors]
    info_list = []
    for p in survivors_paths:
        md = read_pdf_metadata(p) if use_pdf_meta else {}
        mtime = file_mtime(p)
        info_list.append({'path': p, 'meta': md, 'mtime': mtime})

    folder_name = os.path.basename(os.path.abspath(dirpath))
    meta_named = []
    standard_named = []

    for info in info_list:
        meta = info['meta']
        if use_pdf_meta and (meta.get('Title') or meta.get('Author')):
            parts = [folder_name]
            if meta.get('Author'):
                parts.append(sanitize_filename_component(meta['Author']))
            if meta.get('Title'):
                parts.append(sanitize_filename_component(meta['Title']))
            base_name = ' - '.join(parts)
            meta_named.append((info, base_name))
        else:
            standard_named.append(info)

    # metadata-based
    for info, base_name in meta_named:
        ext = os.path.splitext(info['path'])[1].lower()
        src = info['path']
        candidate_no_suffix = os.path.join(dirpath, base_name + ext)
        if os.path.abspath(src) == os.path.abspath(candidate_no_suffix):
            vinfo(f"{os.path.basename(src)} remains as {os.path.basename(src)}", verbose_level, indent=4)
            continue
        rename(base_name, dirpath, dry_run, ext, src, verbose_level)

    # standard names
    standard_named_sorted = sorted(standard_named, key=lambda i: i['mtime'])
    idx = 1
    for info in standard_named_sorted:
        ext = os.path.splitext(info['path'])[1].lower()
        src = info['path']
        base_name = folder_name
        candidate_no_suffix = os.path.join(dirpath, base_name + ext)
        if os.path.abspath(src) == os.path.abspath(candidate_no_suffix):
            vinfo(f"{os.path.basename(src)} remains as {os.path.basename(src)}", verbose_level, indent=4)
            idx += 1
            continue
        rename(base_name, dirpath, dry_run, ext, src, verbose_level)
        idx += 1

def rename(base_name: str, dirpath: str, dry_run: bool, ext: str, src: str, verbose_level: int):
    target_full = unique_target_path(dirpath, base_name, ext)
    action_counters['renames'] += 1
    if ext in AUX_EXTS or ext in PDF_EXTS:
        prefix = "{:{w}}".format("Rename Album Related:", w=ACTION_PREFIX_WIDTH)
    else:
        prefix = "{:{w}}".format("Rename Audio:", w=ACTION_PREFIX_WIDTH)
    vaction(f"{prefix}[{os.path.basename(src)}] -> [{os.path.basename(target_full)}]",
            verbose_level, dry_run, indent=4)
    if not dry_run:
        try:
            os.replace(src, target_full)
        except Exception as e:
            vwarn(
                f"Error renaming [{src}] -> [{target_full}]: {e}",
                verbose_level,
                indent=6)

def dedupe_non_pdf_by_checksum(
        dirpath: str, files: List[str], dry_run: bool, verbose_level: int) -> List[str]:
    excluded_exts = PDF_EXTS.union(AUX_EXTS).union(SIDECAR_EXTS)
    candidates = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in excluded_exts:
            continue
        if is_sidecar_by_prefix(f):
            continue
        candidates.append(f)

    checksum_map: Dict[str, List[str]] = {}
    for fn in candidates:
        path = os.path.join(dirpath, fn)
        if not os.path.isfile(path):
            continue
        try:
            h = sha256_of_file(path)
        except Exception as e:
            vwarn(f"Warning: could not compute SHA256 for {fn}: {e}", verbose_level, indent=2)
            continue
        checksum_map.setdefault(h, []).append(fn)

    for h, group in checksum_map.items():
        if len(group) <= 1:
            continue
        group_paths = [os.path.join(dirpath, g) for g in group]
        # use generic preference selection using mtime
        preferred = choose_preferred_by_mtime(group_paths)
        newest_name = os.path.basename(preferred)
        action_counters['removed_duplicates'] += len(group) - 1
        vinfo(
            f"Checksum duplicate group: [{group}] -> keep: [{newest_name}]",
            verbose_level,
            indent=2)
        for g in group:
            if g == newest_name:
                continue
            path = os.path.join(dirpath, g)
            vaction(
                f"Remove duplicate (checksum) [{g}]",
                verbose_level,
                dry_run,
                indent=4)
            if not dry_run:
                try:
                    os.remove(path)
                except Exception as e:
                    vwarn(
                        f"Error removing [{path}]: {e}",
                        verbose_level,
                        indent=6)

    all_entries = sorted(os.listdir(dirpath))
    return [f for f in all_entries if os.path.isfile(os.path.join(dirpath, f))]


def process_audio_and_sidecars(
        dirpath: str, files: List[str], dry_run: bool, verbose_level: int) -> None:
    files_after_dedupe = dedupe_non_pdf_by_checksum(
        dirpath, files, dry_run, verbose_level)

    audio_files: List[str] = []
    other_files: List[str] = []

    for fn in files_after_dedupe:
        full = os.path.join(dirpath, fn)
        if not os.path.isfile(full):
            continue
        if os.path.splitext(fn)[1].lower() in PDF_EXTS.union(AUX_EXTS):
            continue
        if os.path.splitext(fn)[1].lower(
        ) in SIDECAR_EXTS or is_sidecar_by_prefix(fn):
            other_files.append(fn)
            continue
        if IGNORE_NO_SPACE and ' ' not in fn:
            vinfo(f"Ignoring (no space in filename): {fn}", verbose_level, indent=4)
            continue
        if is_audio(full):
            audio_files.append(fn)
        else:
            other_files.append(fn)

    vinfo(f"Audio files: {audio_files}", verbose_level, indent=2)
    vinfo(f"Other sidecar files to consider: {other_files}", verbose_level, indent=2)

    track_map: Dict[int, List[str]] = defaultdict(list)
    for a in audio_files:
        info = extract_track_info(a)
        if not info:
            vinfo(
                f"Ignoring audio without track pattern: {a}",
                verbose_level,
                indent=4)
            continue
        track, rest = info
        track_map[track].append(a)

    canonical_by_track: Dict[int, str] = {}

    for track, group in track_map.items():
        ext_map: Dict[str, List[str]] = defaultdict(list)
        for g in group:
            ext = os.path.splitext(g)[1].lower()
            ext_map[ext].append(g)

        vinfo(f"Track {track:02d} - Group: {group}", verbose_level, indent=2)

        to_keep = set()

        # choose a single preferred file per track using mtime
        full_paths = [os.path.join(dirpath, g) for g in group]
        preferred = choose_preferred_by_mtime(full_paths)
        to_keep.add(os.path.basename(preferred))
        vinfo(
            f"Track {track:02d}: keep preferred: {os.path.basename(preferred)}",
            verbose_level,
            indent=4)

        remaining = [os.path.join(dirpath, k) for k in to_keep]
        if not remaining:
            chosen = choose_preferred_by_mtime([os.path.join(dirpath, g) for g in group])
            canonical_by_track[track] = os.path.basename(chosen)
        else:
            chosen = choose_preferred_by_mtime(remaining)
            canonical_by_track[track] = os.path.basename(chosen)
        vinfo(
            f"Track [{track:02d}]: canonical file for naming: [{canonical_by_track[track]}]",
            verbose_level,
            indent=4)

    # Sidecar handling
    for f in other_files:
        info = extract_track_info(f)
        if not info:
            vinfo(
                f"Ignoring file without track pattern: [{f}]",
                verbose_level,
                indent=4)
            continue
        track, rest = info
        if track not in canonical_by_track:
            vinfo(
                f"No canonical audio file for track [{track:02d}], ignoring: [{f}]",
                verbose_level,
                indent=4)
            continue
        canonical_audio = canonical_by_track[track]
        target_base = os.path.splitext(canonical_audio)[0]
        ext = os.path.splitext(f)[1]
        target_name = f"{target_base}{ext}"
        src = os.path.join(dirpath, f)
        target = os.path.join(dirpath, target_name)

        if os.path.abspath(src) == os.path.abspath(target):
            vinfo(
                f"Sidecar [{f}] already has the target name.",
                verbose_level,
                indent=6)
            continue

        if os.path.exists(target):
            target_mtime = file_mtime(target)
            src_mtime = file_mtime(src)
            if target_mtime < src_mtime:
                action_counters['replacements'] += 1
                vaction(
                    f"Replace older target [{os.path.basename(target)}] with [{f}]",
                    verbose_level,
                    dry_run,
                    indent=6)
                if not dry_run:
                    try:
                        os.replace(src, target)
                    except Exception as e:
                        vwarn(
                            f"Error replacing [{src}] -> [{target}]: {e}",
                            verbose_level,
                            indent=8)
            else:
                action_counters['removed_sidecars'] += 1

                prefix = "{:{w}}".format("Delete Sidecar:", w=ACTION_PREFIX_WIDTH)
                vaction(
                    f"{prefix}[{f}], newer target [{os.path.basename(target)}]",
                    verbose_level,
                    dry_run,
                    indent=6)
                if not dry_run:
                    try:
                        os.remove(src)
                    except Exception as e:
                        vwarn(
                            f"Error deleting [{src}]: {e}",
                            verbose_level,
                            indent=8)
        else:
            action_counters['renamed_sidecars'] += 1
            prefix = "{:{w}}".format("Rename Sidecar:", w=ACTION_PREFIX_WIDTH)
            vaction(
                f"{prefix}[{f}] -> [{target_name}]",
                verbose_level,
                dry_run,
                indent=6)
            if not dry_run:
                try:
                    os.replace(src, target)
                except Exception as e:
                    vwarn(
                        f"Error renaming [{src}] -> [{target}]: {e}",
                        verbose_level,
                        indent=8)

# ---------- Directory processing ----------


def process_directory(dirpath: str, dry_run: bool, verbose_level: int) -> None:
    brief(f"\n=== Processing directory: {dirpath}")
    all_entries = sorted(os.listdir(dirpath))
    files = [
        f for f in all_entries if os.path.isfile(
            os.path.join(
                dirpath,
                f))]
    if not files:
        vinfo("No files in this directory.", verbose_level, indent=2)
        return
    # PDFs first
    process_aux_files(
        dirpath,
        files,
        PDF_EXTS,
        use_pdf_meta=True,
        dry_run=dry_run,
        verbose_level=verbose_level)
    # refresh and handle CUE/LOG (AUX)
    all_entries = sorted(os.listdir(dirpath))
    files = [
        f for f in all_entries if os.path.isfile(
            os.path.join(
                dirpath,
                f))]
    process_aux_files(
        dirpath,
        files,
        AUX_EXTS,
        use_pdf_meta=False,
        dry_run=dry_run,
        verbose_level=verbose_level)
    # refresh and handle audio+sidecars
    all_entries = sorted(os.listdir(dirpath))
    files = [
        f for f in all_entries if os.path.isfile(
            os.path.join(
                dirpath,
                f))]
    process_audio_and_sidecars(
        dirpath,
        files,
        dry_run=dry_run,
        verbose_level=verbose_level)

# ---------- Config helpers ----------


def find_config_paths(given_path: Optional[str] = None) -> List[str]:
    candidates = []
    if given_path:
        candidates.append(os.path.abspath(given_path))
    candidates.append(os.path.abspath('./config.toml'))  # repo-local config
    home = os.path.expanduser('~')
    candidates.append(
        os.path.join(
            home,
            '.config',
            'rename_music',
            'config.toml'))
    return candidates


def load_and_apply_config(config_path: Optional[str], verbose_level: int):
    if not load_toml_file:
        if config_path:
            print(
                "No TOML support (tomllib/tomli). Ignoring config.",
                file=sys.stderr)
        return None
    cfg_candidates = find_config_paths(config_path)
    used = None
    for p in cfg_candidates:
        if os.path.isfile(p):
            try:
                cfg = load_toml_file(p)
            except Exception as e:
                print(
                    f"Warning: failed to load TOML config {p}: {e}",
                    file=sys.stderr)
                continue
            errs = validate_config(cfg)
            if errs:
                print(f"Config validation errors in {p}:", file=sys.stderr)
                for e in errs:
                    print(f" - {e}", file=sys.stderr)
                # still merge what we can
            merge_config_into_globals(cfg)
            used = p
            break
    if used and verbose_level >= 1:
        print(f"[CONFIG] Loaded configuration from: {used}")
    return used


def validate_config(cfg: dict) -> List[str]:
    errs = []
    bool_keys = ['ignore_no_space', 'dry_run_default', 'recursive_default']
    list_keys = ['pdf_exts', 'aux_exts', 'sidecar_exts', 'sidecar_prefixes', 'exclude_dirs']
    for k in bool_keys:
        if k in cfg and not isinstance(cfg[k], bool):
            errs.append(f"'{k}' must be a boolean")
    for k in list_keys:
        if k in cfg and not isinstance(cfg[k], list):
            errs.append(f"'{k}' must be a list")
    return errs


def merge_config_into_globals(cfg: dict) -> None:
    global PDF_EXTS, AUX_EXTS, SIDECAR_EXTS, SIDECAR_PREFIXES
    global IGNORE_NO_SPACE, DRY_RUN_DEFAULT, RECURSIVE_DEFAULT

    def _as_list(v):
        return v if isinstance(v, list) else None

    if 'ignore_no_space' in cfg and isinstance(cfg['ignore_no_space'], bool):
        IGNORE_NO_SPACE = cfg['ignore_no_space']
    if 'dry_run_default' in cfg and isinstance(cfg['dry_run_default'], bool):
        DRY_RUN_DEFAULT = cfg['dry_run_default']
    if 'recursive_default' in cfg and isinstance(
            cfg['recursive_default'], bool):
        RECURSIVE_DEFAULT = cfg['recursive_default']

    if 'pdf_exts' in cfg and _as_list(cfg['pdf_exts']) is not None:
        PDF_EXTS = set(_.lower() for _ in cfg['pdf_exts'])
    if 'aux_exts' in cfg and _as_list(cfg['aux_exts']) is not None:
        AUX_EXTS = set(_.lower() for _ in cfg['aux_exts'])

    if 'sidecar_exts' in cfg and _as_list(cfg['sidecar_exts']) is not None:
        SIDECAR_EXTS = set(_.lower() for _ in cfg['sidecar_exts'])
    if 'sidecar_prefixes' in cfg and _as_list(
            cfg['sidecar_prefixes']) is not None:
        SIDECAR_PREFIXES = [str(x).lower() for x in cfg['sidecar_prefixes']]

# ---------- CLI / main ----------

def main():
    global ROOT_BASE
    parser = argparse.ArgumentParser(description="Clean and rename music folders (Audio, Sidecars, PDFs, CUE/LOG).",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {__version__}')
    parser.add_argument(
        '--path',
        '-p',
        default='.',
        help='Target directory (default: current folder)')
    parser.add_argument(
        '--recursive',
        '-r',
        action='store_true',
        help='Recurse into subdirectories.')
    parser.add_argument(
        '--force',
        '-f',
        action='store_true',
        help='Apply changes (default: dry-run).')
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help='Increase verbosity: -v = dry-run output & summary, -vv = include INFO details.')
    parser.add_argument(
        '--config',
        '-c',
        default=None,
        help='Path to TOML config file (overrides defaults).')
    args = parser.parse_args()

    dirpath = os.path.abspath(args.path)
    if not os.path.isdir(dirpath):
        print(f"Error: {dirpath} is not a directory.", file=sys.stderr)
        sys.exit(1)

    # set base for relative path display
    ROOT_BASE = dirpath

    verbose_level = args.verbose
    dry_run = not args.force
    recursive = args.recursive

    # load config if available
    if args.config or os.path.isfile(os.path.abspath('./config.toml')) or os.path.isfile(
            os.path.join(os.path.expanduser('~'), '.config', 'rename_music', 'config.toml')):
        try:
            load_and_apply_config(args.config, verbose_level)
        except Exception as e:
            print(f"Warning: failed to load config: {e}", file=sys.stderr)

    # show optional-deps hints (always)
    missing = []
    if not HAVE_PYPDF2:
        missing.append(("PyPDF2", "pip install PyPDF2"))
    if not HAVE_MAGIC:
        missing.append(
            ("python-magic", "pip install python-magic (or python-magic-bin on Windows)"))

    if missing:
        brief("\nIMPORTANT: Optional dependencies are missing (recommended):")
        for name, cmd in missing:
            brief(f" - {name}: {cmd}")
        brief("The script works without these packages but selection/detection will be limited.\n")

    vinfo(f"Starting processing: {dirpath} (recursive={recursive}) (dry-run={dry_run})", verbose_level, indent=0)
    if HAVE_MAGIC:
        vinfo(
            "libmagic available: MIME detection via python-magic",
            verbose_level,
            indent=2)
    else:
        vinfo(
            "libmagic not available: MIME detection via mimetypes.guess_type (extension-based)",
            verbose_level,
            indent=2)
    if HAVE_PYPDF2:
        vinfo(
            "PyPDF2 available: PDF metadata will be read",
            verbose_level,
            indent=2)
    else:
        vinfo(
            "PyPDF2 not available: PDF metadata will not be read. PDFs will be enumerated.",
            verbose_level,
            indent=2)

    if recursive:
        for root, dirs, _ in os.walk(dirpath, topdown=True):
            dirs[:] = [d for d in dirs if not d.startswith(
                '.') and not os.path.islink(os.path.join(root, d))]
            process_directory(
                root,
                dry_run=dry_run,
                verbose_level=verbose_level)
    else:
        process_directory(
            dirpath,
            dry_run=dry_run,
            verbose_level=verbose_level)

    total_actions = sum(action_counters.values())

    # short hint always shown, but only show the --force suggestion when we
    # genuinely ran in dry-run
    if total_actions > 0 and dry_run:
        brief("\nRun with --force (or -f) to perform the above actions.")
    elif total_actions == 0:
        brief("\nNo changes necessary.")

    # dry-run summary & concrete action list at -v or -vv
    if dry_run and verbose_level >= 1:
        brief("\n--- Dry-run summary ---")

        if action_log:
            brief("\nConcrete actions (preview):")
            for i, entry in enumerate(action_log, start=1):
                brief(f" {i:03d}. {entry}")
        brief("\nRun with --force (or -f) to perform the above actions.")
    elif not dry_run:
        brief("\n--- Execution summary ---")
        if action_log:
            brief("\nConcrete actions (performed):")
            for i, entry in enumerate(action_log, start=1):
                brief(f" {i:03d}. {entry}")
        brief("\nChanges have been applied.")


if __name__ == '__main__':
    main()
