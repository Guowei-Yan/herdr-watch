#!/usr/bin/env python3
"""Install herdr-watch into supported personal skills directories."""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys
import tempfile


SKILL_NAME = "herdr-watch"


@dataclass(frozen=True)
class SourceFile:
    path: Path
    data: bytes
    mode: int


def default_destinations(environment, home):
    """Return primary Claude/Codex skills directories plus existing optional agent roots."""
    destinations = []
    claude_dir = Path(environment.get("CLAUDE_CONFIG_DIR") or (home / ".claude")).expanduser()
    destinations.append(claude_dir / "skills")

    codex_home = environment.get("CODEX_HOME")
    if codex_home:
        destinations.append(Path(codex_home).expanduser() / "skills")
    else:
        destinations.append(home / ".codex" / "skills")
        if (home / ".agents").is_dir():
            destinations.append(home / ".agents" / "skills")

    optional_env_profiles = (
        ("ZCODE_CONFIG_DIR", home / ".zcode"),
        ("KIMICODE_CONFIG_DIR", home / ".kimicode"),
    )
    for variable, fallback_dir in optional_env_profiles:
        override = environment.get(variable)
        if override:
            destinations.append(Path(override).expanduser() / "skills")
        elif fallback_dir.is_dir():
            destinations.append(fallback_dir / "skills")

    xdg_config = environment.get("XDG_CONFIG_HOME")
    muse_dir = (Path(xdg_config).expanduser() / "muse") if xdg_config else (home / ".config" / "muse")
    if muse_dir.is_dir():
        destinations.append(muse_dir / "skills")

    for optional_dir in (home / ".gemini" / "config", home / ".pi" / "agent"):
        if optional_dir.is_dir():
            destinations.append(optional_dir / "skills")

    return destinations


def _source_files(source):
    skill_file = source / "SKILL.md"
    scripts_dir = source / "scripts"
    if not skill_file.is_file():
        raise FileNotFoundError("source not found: {}".format(skill_file))
    if not scripts_dir.is_dir():
        raise FileNotFoundError("source not found: {}".format(scripts_dir))
    scripts = sorted(path for path in scripts_dir.glob("*.py") if path.is_file())
    if not scripts:
        raise FileNotFoundError("no Python scripts found in {}".format(scripts_dir))
    references_dir = source / "references"
    references = []
    if references_dir.is_dir():
        references = sorted(path for path in references_dir.glob("*.md") if path.is_file())
    return _snapshot(skill_file), list(map(_snapshot, scripts)), list(map(_snapshot, references))


def _snapshot(source_file):
    return SourceFile(
        path=source_file,
        data=source_file.read_bytes(),
        mode=stat.S_IMODE(source_file.stat().st_mode),
    )


def _validate_python(scripts):
    for script in scripts:
        compile(script.data, str(script.path), "exec")


def _unique_destinations(destinations):
    unique = []
    seen = set()
    for destination in destinations:
        resolved = Path(destination).expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _destination_files(source_files, target):
    skill_file, scripts, references = source_files
    destinations = [(skill_file, target / "SKILL.md")]
    destinations.extend((item, target / "scripts" / item.path.name) for item in scripts)
    destinations.extend((item, target / "references" / item.path.name) for item in references)
    return destinations


def _stage_file(source_file, destination):
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".herdr-watch-stage-", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as staged_file:
            descriptor = None
            staged_file.write(source_file.data)
            staged_file.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(staged_file.fileno(), source_file.mode)
        return temporary, destination
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _ensure_directory(destination):
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise FileExistsError("unsafe directory collision: {}".format(destination))
    destination.mkdir(parents=True, exist_ok=True)


def _prepare_directories(destination_files):
    directories = sorted({item[1].parent for item in destination_files}, key=lambda item: len(item.parts))
    for directory in directories:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise FileExistsError("unsafe directory collision: {}".format(directory))
    for _, destination in destination_files:
        if destination.exists() and destination.is_dir() and not destination.is_symlink():
            raise FileExistsError("unsafe file collision: {}".format(destination))
    created = []
    for directory in directories:
        if not directory.exists():
            directory.mkdir()
            created.append(directory)
    return created


def _stage_files(destination_files):
    staged = []
    try:
        for source_file, destination in destination_files:
            staged.append(_stage_file(source_file, destination))
    except BaseException:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        raise
    return staged


def _backup_path(destination):
    descriptor, name = tempfile.mkstemp(
        prefix=".herdr-watch-backup-", dir=str(destination.parent)
    )
    os.close(descriptor)
    Path(name).unlink()
    return Path(name)


def _rollback(records):
    for destination, backup, had_old in reversed(records):
        if had_old and (backup.exists() or backup.is_symlink()):
            os.replace(str(backup), str(destination))
        elif not had_old:
            destination.unlink(missing_ok=True)


def _commit_files(staged):
    records = []
    try:
        for temporary, destination in staged:
            had_old = destination.exists() or destination.is_symlink()
            backup = _backup_path(destination) if had_old else Path()
            records.append((destination, backup, had_old))
            if had_old:
                os.replace(str(destination), str(backup))
            os.replace(str(temporary), str(destination))
    except BaseException:
        _rollback(records)
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    for _, backup, had_old in records:
        if had_old:
            backup.unlink(missing_ok=True)


def _cleanup_directories(directories):
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            pass


def _stage_target(source_files, skills_dir):
    target = skills_dir / SKILL_NAME
    _ensure_directory(skills_dir)
    destination_files = _destination_files(source_files, target)
    created = _prepare_directories(destination_files)
    try:
        staged = _stage_files(destination_files)
    except BaseException:
        _cleanup_directories(created)
        raise
    return staged, target, created


def _install_one(source_files, skills_dir):
    staged, target, created = _stage_target(source_files, skills_dir)
    try:
        _commit_files(staged)
    except BaseException:
        _cleanup_directories(created)
        raise
    return target


def install(source, destinations):
    """Validate source scripts, then install into each unique skills directory."""
    source_path = Path(source).resolve()
    source_files = _source_files(source_path)
    _validate_python(source_files[1])
    unique_destinations = _unique_destinations(destinations)
    return [
        _install_one(source_files, skills_dir)
        for skills_dir in unique_destinations
    ]


def _parse_args(arguments):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        action="append",
        type=Path,
        metavar="SKILLS_DIR",
        help="install only into this skills directory (repeatable)",
    )
    return parser.parse_args(arguments)


def main(arguments=None):
    args = _parse_args(arguments)
    source = Path(__file__).resolve().parent
    destinations = args.destination or default_destinations(os.environ, Path.home())
    try:
        installed = install(source, destinations)
    except (OSError, SyntaxError, ValueError) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 1
    for target in installed:
        print("synced {}".format(target))
    print("Done - herdr-watch synced to {} profile(s).".format(len(installed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
