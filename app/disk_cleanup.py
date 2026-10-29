"""
disk_cleanup — on-demand (never automatic, no background loop) scan for
mechanically-verifiable, safe-to-delete cruft: broken symlinks, regenerable
build/cache artifacts, stale rotated logs, emptyable Trash, and old
installers sitting in Downloads. Deliberately NOT "the model's opinion this
seems unused" — every candidate here is true by construction (a broken
symlink points nowhere, __pycache__/.pyc/.DS_Store are always regenerable,
a rotated .log.N is always superseded by its base file) or by an explicit,
disclosed age threshold on installer files only.

Two-step by design: scan() is read-only and just reports. delete() only
ever removes paths that were IDs in the most recent scan() result — a
hallucinated or typo'd path can't do anything, it just won't match any
known ID. There is no path-based delete API on purpose.
"""

import json
import os
import time
from pathlib import Path

HOME = Path.home()

# Deliberately NOT the whole home directory. A first version that walked
# HOME directly found 6541 "broken symlinks" — 6280 of them inside
# ~/Library/Containers, which is macOS's per-app sandbox area: things like
# Chrome/Docker/Codex's SingletonLock/SingletonSocket files, which are only
# "live" while that app is actually running. Technically a dangling symlink
# at scan time, but not cruft — an app-managed sandbox isn't a safe place
# for an outside process to be judging what's stale. Scoped down to
# directories that are unambiguously "my files": Desktop, Documents,
# Downloads, plus whatever extra roots the user lists in their own
# config/projects.json (gitignored — empty by default on a fresh clone).
# Anything under ~/Library, ~/.docker, ~/.codex, or similar tool/OS-internal
# state is out of scope.
_PROJECTS_CONFIG_FILE = Path(__file__).parent.parent / "config" / "projects.json"


def _extra_scan_roots():
    try:
        cfg = json.loads(_PROJECTS_CONFIG_FILE.read_text())
        return [Path(p) for p in cfg.get("extra_cleanup_scan_roots", [])]
    except Exception:
        return []


_SCAN_ROOTS = [
    HOME / "Desktop",
    HOME / "Documents",
    HOME / "Downloads",
    *_extra_scan_roots(),
]

# .N / .old rotated-log patterns — the base file (without the suffix) is
# always the live one; a numbered/old sibling is by definition superseded.
_STALE_LOG_SUFFIXES = (".1", ".2", ".3", ".4", ".5", ".old", ".bak")

_OLD_INSTALLER_EXTS = (".dmg", ".pkg")
_OLD_INSTALLER_AGE_DAYS = 180  # disclosed threshold, not a guess

_SKIP_DIR_NAMES = {".git", "node_modules", ".Trash"}  # walked separately / too noisy

_last_scan: dict[str, dict] = {}  # id -> candidate, keyed for delete() validation
_last_scan_truncated = False


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += (Path(root) / f).lstat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _add(candidates: list, category: str, path: Path, reason: str, size: int):
    candidates.append({
        "id": len(candidates) + 1,
        "category": category,
        "path": str(path),
        "reason": reason,
        "size_bytes": size,
    })


SCAN_TIME_BUDGET_S = 90  # hard cap — returns partial results rather than hanging


def scan() -> list[dict]:
    """Read-only. Returns candidates and caches them (by id) for delete()."""
    global _last_scan
    candidates: list = []
    deadline = time.monotonic() + SCAN_TIME_BUDGET_S
    truncated = False

    # One combined walk per root (checking symlinks + cache cruft + stale
    # logs together) instead of three separate walks over the same tree —
    # the first version walked everything 3x for no reason.
    for root_dir in _SCAN_ROOTS:
        if truncated:
            break
        if not root_dir.exists():
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root_dir):
                if time.monotonic() > deadline:
                    truncated = True
                    break
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]

                for d in list(dirnames):
                    if d == "__pycache__":
                        full = Path(dirpath) / d
                        _add(candidates, "pycache", full,
                             "regenerated automatically by Python", _dir_size(full))
                        dirnames.remove(d)  # don't descend into it too
                    else:
                        p = Path(dirpath) / d
                        if p.is_symlink() and not p.exists():
                            _add(candidates, "broken_symlink", p,
                                 "symlink target doesn't exist", 0)

                for f in filenames:
                    p = Path(dirpath) / f
                    if p.is_symlink() and not p.exists():
                        _add(candidates, "broken_symlink", p,
                             "symlink target doesn't exist", 0)
                        continue
                    if f == ".DS_Store":
                        _add(candidates, "ds_store", p,
                             "Finder metadata, regenerated automatically",
                             p.lstat().st_size if p.exists() else 0)
                    elif f.endswith(".pyc"):
                        _add(candidates, "pyc", p,
                             "compiled bytecode, regenerated automatically",
                             p.lstat().st_size if p.exists() else 0)
                    else:
                        for suffix in _STALE_LOG_SUFFIXES:
                            if f.endswith(suffix) and (f[: -len(suffix)]).endswith(".log"):
                                base = Path(dirpath) / f[: -len(suffix)]
                                if base.exists():
                                    _add(candidates, "stale_log", p,
                                         f"superseded by {base.name}, which still exists",
                                         p.lstat().st_size if p.exists() else 0)
                                break
        except OSError:
            continue

    # ── Old installers in Downloads ──────────────────────────────────────
    downloads = HOME / "Downloads"
    if downloads.exists():
        now = time.time()
        try:
            for p in downloads.iterdir():
                if p.is_file() and p.suffix.lower() in _OLD_INSTALLER_EXTS:
                    age_days = (now - p.stat().st_mtime) / 86400
                    if age_days >= _OLD_INSTALLER_AGE_DAYS:
                        _add(candidates, "old_installer", p,
                             f"installer file, unmodified for {int(age_days)} days",
                             p.stat().st_size)
        except OSError:
            pass

    # ── Trash (reported for visibility, but NOT actually deletable here — ─
    # verified live: macOS protects ~/.Trash beyond normal file permissions,
    # shutil.rmtree raises PermissionError even though this process owns
    # the files. Surfacing the size is still useful; just not via delete().
    trash = HOME / ".Trash"
    if trash.exists():
        size = _dir_size(trash)
        if size > 0:
            _add(candidates, "trash", trash,
                 "already in Trash, but macOS blocks deleting this "
                 "programmatically — empty it yourself via Finder", size)

    global _last_scan_truncated
    _last_scan_truncated = truncated
    _last_scan = {c["id"]: c for c in candidates}
    return candidates


def summarize(candidates: list[dict]) -> str:
    if not candidates:
        base = "Nothing found — no broken symlinks, cache cruft, stale logs, old installers, or Trash contents."
        return base + (f"\n(Scan hit its {SCAN_TIME_BUDGET_S}s time budget and stopped early — this may be incomplete.)" if _last_scan_truncated else "")
    by_cat: dict[str, list] = {}
    for c in candidates:
        by_cat.setdefault(c["category"], []).append(c)
    lines = []
    total = 0
    for cat, items in sorted(by_cat.items()):
        cat_size = sum(i["size_bytes"] for i in items)
        total += cat_size
        lines.append(f"[{cat}] {len(items)} item(s), {_human(cat_size)}")
    lines.append(f"\nTotal reclaimable: {_human(total)}")
    lines.append("Nothing has been deleted — call delete(ids) with specific IDs from this scan to remove any of it.")
    if _last_scan_truncated:
        lines.append(f"(Scan hit its {SCAN_TIME_BUDGET_S}s time budget and stopped early — this list may be incomplete.)")
    return "\n".join(lines)


def delete(ids: list[int]) -> dict:
    """Only removes paths that were IDs in the most recent scan() — an id
    not present there (hallucinated, stale, or from before a re-scan) is
    silently skipped rather than guessed at."""
    if not _last_scan:
        return {"ok": False, "error": "No scan has been run yet — call scan() first."}

    removed = []
    skipped = []
    freed = 0
    for cid in ids:
        candidate = _last_scan.get(cid)
        if candidate is None:
            skipped.append({"id": cid, "reason": "not in the most recent scan"})
            continue
        p = Path(candidate["path"])
        if not p.exists() and not p.is_symlink():
            skipped.append({"id": cid, "path": candidate["path"], "reason": "already gone"})
            continue
        try:
            if p.is_symlink() or p.is_file():
                size = candidate["size_bytes"]
                p.unlink()
            elif p.is_dir():
                import shutil
                size = _dir_size(p)
                shutil.rmtree(p)
            else:
                skipped.append({"id": cid, "path": candidate["path"], "reason": "unknown file type"})
                continue
            freed += size
            removed.append({"id": cid, "path": candidate["path"], "size_bytes": size})
        except OSError as e:
            skipped.append({"id": cid, "path": candidate["path"], "reason": str(e)})

    return {
        "ok": True,
        "removed": removed,
        "skipped": skipped,
        "freed_bytes": freed,
        "freed_human": _human(freed),
    }
