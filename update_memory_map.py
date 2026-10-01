#!/usr/bin/env python3
"""
update_memory_map.py
=====================
Run this script after every new `codebase-memory-mcp index` to sync
new project databases into the memory_map repository and commit the changes.

Usage:
    python3 update_memory_map.py [--project PROJECT_NAME]

If --project is omitted, all projects found in the cache directory are synced.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

CACHE_DIR = Path("/opt/data/.cache/codebase-memory-mcp")
MEMORY_MAP_DIR = Path("/opt/data/memory_map")
DBS_DIR = MEMORY_MAP_DIR / "dbs"

# ── Helpers ──────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_db_path(project_name: str) -> Path:
    return CACHE_DIR / f"{project_name}.db"


def get_db_files(project_name: str) -> list[Path]:
    """Return all DB-related files for a project (db, db-shm, db-wal, artifact)."""
    db_path = get_db_path(project_name)
    artifact = None
    if db_path.exists():
        # Find the matching artifact.json from the original repo
        # The artifact is stored alongside the db in the cache or original repo
        # We'll search for it
        for candidate in [
            db_path.parent / f"{project_name}.db",
            db_path,
        ]:
            art = candidate.with_name("artifact.json")
            if art.exists():
                artifact = art
                break
    return [p for p in [db_path] if p.exists()], artifact


def sync_project(project_name: str, commit: bool = True) -> bool:
    """
    Sync a single project's DB files into memory_map/dbs/<project_name>/
    Returns True if changes were made.
    """
    print(f"\n{'─'*60}")
    print(f"  Syncing project: {project_name}")
    print(f"{'─'*60}")

    src_db = get_db_path(project_name)
    if not src_db.exists():
        print(f"  ❌ Source DB not found: {src_db}")
        return False

    dst_dir = DBS_DIR / project_name
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_db = dst_dir / src_db.name

    changed = False

    # Copy DB file if changed/size differs
    src_size = src_db.stat().st_size
    src_hash = sha256_file(src_db)

    if dst_db.exists():
        dst_hash = sha256_file(dst_db)
        dst_size = dst_db.stat().st_size
        if src_hash == dst_hash and src_size == dst_size:
            print(f"  ✓ DB already up to date ({src_size:,} bytes, sha={src_hash[:12]}…)")
        else:
            print(f"  🔄 DB changed — copying ({src_size:,} bytes)")
            shutil.copy2(src_db, dst_db)
            # Remove WAL/SHM if they exist in destination
            for ext in ["-wal", "-shm", "-journal"]:
                (dst_dir / f"{dst_db.name}{ext}").unlink(missing_ok=True)
            changed = True
    else:
        print(f"  📦 New DB — copying ({src_size:,} bytes)")
        shutil.copy2(src_db, dst_db)
        # Remove WAL/SHM
        for ext in ["-wal", "-shm", "-journal"]:
            (dst_dir / f"{dst_db.name}{ext}").unlink(missing_ok=True)
        changed = True

    # Copy artifact.json
    src_artifact = None
    for candidate_dir in [src_db.parent, Path("/opt/data")]:
        candidate = candidate_dir / project_name / ".codebase-memory" / "artifact.json"
        if candidate.exists():
            src_artifact = candidate
            break
        candidate = candidate_dir / f"{project_name}.artifact.json"
        if candidate.exists():
            src_artifact = candidate
            break

    if src_artifact:
        dst_artifact = dst_dir / "artifact.json"
        src_a_hash = sha256_file(src_artifact)
        if dst_artifact.exists():
            dst_a_hash = sha256_file(dst_artifact)
            if src_a_hash == dst_a_hash:
                print(f"  ✓ artifact.json already up to date")
            else:
                shutil.copy2(src_artifact, dst_artifact)
                print(f"  🔄 artifact.json updated")
                changed = True
        else:
            shutil.copy2(src_artifact, dst_artifact)
            print(f"  📦 artifact.json copied")
            changed = True
    else:
        print(f"  ⚠ No artifact.json found for {project_name}")

    if changed and commit:
        commit_changes(project_name)

    return changed


def commit_changes(project_name: str):
    """Commit changes to the memory_map git repo."""
    print(f"\n  📬 Committing changes for {project_name}…")
    try:
        subprocess.run(
            ["git", "add", "dbs/" + project_name],
            cwd=MEMORY_MAP_DIR, check=True, capture_output=True
        )
        # Check if there's anything to commit
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=MEMORY_MAP_DIR, check=True, capture_output=True, text=True
        )
        if not status.stdout.strip():
            print("  ✓ Nothing to commit (no changes)")
            return

        result = subprocess.run(
            ["git", "commit", "-m",
             f"Add/update memory index for {project_name}\n\n"
             f"Indexed DB: {project_name}\n"
             f"Timestamp: {subprocess.run(['date', '-u', '+%Y-%m-%dT%H:%M:%SZ'], "
             f"capture_output=True, text=True).stdout.strip()}\n"
             f"Tool: codebase-memory-mcp index"],
            cwd=MEMORY_MAP_DIR, check=True, capture_output=True, text=True
        )
        print(f"  ✓ Committed: {result.stdout.strip().splitlines()[-1] if result.stdout else 'ok'}")
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Commit failed: {e.stderr}")
        sys.exit(1)


def discover_projects() -> list[str]:
    """Discover all project DBs in the cache directory."""
    projects = []
    if not CACHE_DIR.exists():
        print(f"⚠ Cache directory not found: {CACHE_DIR}")
        return projects
    for db_file in CACHE_DIR.glob("*.db"):
        proj_name = db_file.stem  # strips .db
        # Skip non-project files
        if proj_name.startswith("opt-data-"):
            projects.append(proj_name)
    # Also check dbs directory which might have projects already
    if DBS_DIR.exists():
        for d in DBS_DIR.iterdir():
            if d.is_dir() and (d / "artifact.json").exists():
                if d.name not in projects:
                    projects.append(d.name)
    return sorted(projects)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync codebase-memory index DBs into the memory_map git repo"
    )
    parser.add_argument(
        "--project", "-p",
        help="Specific project name to sync (e.g. opt-data-dontrix). "
             "Omit to sync all discovered projects."
    )
    parser.add_argument(
        "--no-commit", "-n",
        action="store_true",
        help="Copy files but do not commit to git"
    )
    args = parser.parse_args()

    # Ensure git identity is set
    git_config = MEMORY_MAP_DIR / ".git" / "config"
    if git_config.exists():
        # Verify git user is configured
        try:
            result = subprocess.run(
                ["git", "config", "user.name"],
                cwd=MEMORY_MAP_DIR, capture_output=True, text=True
            )
            if not result.stdout.strip():
                print("⚠ Git user.name not set — configuring…")
                subprocess.run(
                    ["git", "config", "user.name", "Terri Turner"],
                    cwd=MEMORY_MAP_DIR, check=True
                )
                subprocess.run(
                    ["git", "config", "user.email", "territurner.ar@gmail.com"],
                    cwd=MEMORY_MAP_DIR, check=True
                )
        except Exception:
            pass

    if args.project:
        sync_project(args.project, commit=not args.no_commit)
    else:
        projects = discover_projects()
        if not projects:
            print("No projects found to sync.")
            print(f"Cache dir: {CACHE_DIR}")
            print(f"Expected: *.db files in {CACHE_DIR}")
            sys.exit(1)
        print(f"\n{'='*60}")
        print(f"  Found {len(projects)} project(s) to sync:")
        for p in projects:
            print(f"    • {p}")
        print(f"{'='*60}\n")
        for proj in projects:
            sync_project(proj, commit=not args.no_commit)

    print(f"\n{'='*60}")
    print(f"  Sync complete!")
    print(f"  Memory map repo: {MEMORY_MAP_DIR}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
