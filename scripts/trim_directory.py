#!/usr/bin/env python3
"""Smart-trim TS files in a directory and atomically replace validated files."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_EXTENSIONS = (".m2ts", ".ts")
DEFAULT_PROTECTED_KEYWORDS = ("紅白歌合戦", "開票速報")
EIT_PID = 0x0012
EIT_PRESENT_FOLLOWING_ACTUAL_TABLE_ID = 0x4E


@dataclass(frozen=True)
class SourceState:
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def read(cls, path: Path) -> "SourceState":
        stat = path.stat()
        return cls(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


@dataclass(frozen=True)
class TrimResult:
    source: Path
    output: Path
    original_size: int
    trimmed_size: int


def parse_args() -> argparse.Namespace:
    repository_tsreplace = Path(__file__).resolve().parent.parent / "tsreplace"
    parser = argparse.ArgumentParser(
        description=(
            "Smart-trim TS files into sibling .processing files, validate them "
            "with TSDuck, and atomically replace the originals."
        ),
    )
    parser.add_argument("directory", nargs="?", type=Path, default=Path("."))
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="also process matching files in subdirectories",
    )
    parser.add_argument(
        "--extensions",
        default=",".join(DEFAULT_EXTENSIONS),
        help="comma-separated filename extensions (default: .m2ts,.ts)",
    )
    parser.add_argument(
        "--tsreplace",
        default=str(repository_tsreplace),
        help=f"tsreplace executable (default: {repository_tsreplace})",
    )
    parser.add_argument(
        "--tsanalyze",
        default="tsanalyze",
        help="TSDuck tsanalyze executable (default: tsanalyze from PATH)",
    )
    parser.add_argument(
        "--tscharset",
        default="tscharset",
        help="TSDuck tscharset executable (default: tscharset from PATH)",
    )
    parser.add_argument(
        "--protect-keyword",
        action="append",
        default=[],
        help=(
            "also skip files whose EIT contains this program-name keyword; "
            "may be repeated"
        ),
    )
    parser.add_argument(
        "--no-protect-keywords",
        action="store_true",
        help="disable the default EIT keyword protection",
    )
    parser.add_argument(
        "--processing-suffix",
        default=".processing",
        help="temporary output suffix (default: .processing)",
    )
    parser.add_argument(
        "--backup-suffix",
        help="keep each original using this suffix, for example .original",
    )
    parser.add_argument(
        "--no-replace",
        action="store_true",
        help="keep the source and publish the result as <name>-trimed.<suffix>",
    )
    parser.add_argument(
        "--remove-failed-processing",
        action="store_true",
        help="delete .processing output after a failed trim or validation",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop after the first failed file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list files and commands without changing anything",
    )
    return parser.parse_args()


def parse_extensions(value: str) -> set[str]:
    extensions: set[str] = set()
    for item in value.split(","):
        item = item.strip().lower()
        if not item:
            continue
        extensions.add(item if item.startswith(".") else f".{item}")
    if not extensions:
        raise ValueError("at least one extension is required")
    return extensions


def find_files(directory: Path, extensions: set[str], recursive: bool) -> list[Path]:
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        (
            path
            for path in iterator
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in extensions
            and not path.stem.casefold().endswith("-trimed")
        ),
        key=lambda path: str(path).casefold(),
    )


def resolve_executable(value: str, label: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.parent != Path(".") or candidate.is_absolute():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        raise FileNotFoundError(f"{label} is not executable: {candidate}")
    resolved = shutil.which(value)
    if resolved is None:
        raise FileNotFoundError(f"{label} not found in PATH: {value}")
    return resolved


def command_text(command: list[str]) -> str:
    return shlex.join(command)


def check_tsanalyze(tsanalyze: str) -> str:
    result = subprocess.run(
        [tsanalyze, "--version"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"tsanalyze --version failed with exit code {result.returncode}"
            + (f":\n{detail}" if detail else "")
        )
    return (result.stdout or result.stderr).strip().splitlines()[0]


def encode_arib_keywords(tscharset: str, keywords: list[str]) -> dict[str, bytes]:
    encoded: dict[str, bytes] = {}
    for keyword in keywords:
        result = subprocess.run(
            [tscharset, "--japan", "--encode", keyword],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"tscharset could not encode protected keyword {keyword!r}"
                + (f":\n{detail}" if detail else "")
            )
        octets = re.findall(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}(?![0-9A-Fa-f])", result.stdout)
        if not octets:
            raise RuntimeError(
                f"tscharset returned no ARIB bytes for protected keyword {keyword!r}"
            )
        encoded[keyword] = bytes.fromhex("".join(octets))
    return encoded


def reserve_processing_file(path: Path) -> None:
    with path.open("xb"):
        pass


def check_available_space(source: Path, source_size: int) -> tuple[int, int]:
    free = shutil.disk_usage(source.parent).free
    required = source_size * 2
    if free < required:
        raise RuntimeError(
            "insufficient free space: "
            f"{free} bytes available, {required} bytes required "
            f"(2 x source size) on {source.parent}"
        )
    return free, required


def detect_ts_packet_layout(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        probe = stream.read(204 * 8)
    for packet_size, sync_offset in ((188, 0), (192, 4), (204, 0)):
        if len(probe) >= sync_offset + packet_size * 5 and all(
            probe[sync_offset + packet_size * index] == 0x47 for index in range(5)
        ):
            return packet_size, sync_offset
    raise RuntimeError("could not detect TS, M2TS, or RS204 packet layout")


def find_next_packet_start(
    data: bytes,
    start: int,
    packet_size: int,
    sync_offset: int,
) -> int | None:
    position = start
    required_syncs = 4
    last_candidate = len(data) - sync_offset - packet_size * (required_syncs - 1)
    while position < last_candidate:
        sync = data.find(b"\x47", position + sync_offset, last_candidate + sync_offset)
        if sync < 0:
            return None
        candidate = sync - sync_offset
        if all(
            data[candidate + sync_offset + packet_size * index] == 0x47
            for index in range(1, required_syncs)
        ):
            return candidate
        position = candidate + 1
    return None


def find_protected_keyword(path: Path, patterns: dict[str, bytes]) -> str | None:
    if not patterns:
        return None
    packet_size, sync_offset = detect_ts_packet_layout(path)
    packets_per_read = 8192
    pending = b""
    section_data = bytearray()
    last_continuity: int | None = None

    def inspect_complete_sections() -> str | None:
        while len(section_data) >= 3:
            if section_data[0] == 0xFF:
                section_data.clear()
                return None
            section_length = ((section_data[1] & 0x0F) << 8) | section_data[2]
            if section_length > 4093:
                del section_data[0]
                continue
            total_size = 3 + section_length
            if len(section_data) < total_size:
                return None
            section = bytes(section_data[:total_size])
            del section_data[:total_size]
            if section[0] != EIT_PRESENT_FOLLOWING_ACTUAL_TABLE_ID:
                continue
            for keyword, pattern in patterns.items():
                if pattern in section:
                    return keyword
        return None

    with path.open("rb") as stream:
        while chunk := stream.read(packet_size * packets_per_read):
            data = pending + chunk
            offset = 0
            while offset + sync_offset + 188 <= len(data):
                if data[offset + sync_offset] != 0x47:
                    next_offset = find_next_packet_start(
                        data, offset + 1, packet_size, sync_offset
                    )
                    if next_offset is None:
                        break
                    offset = next_offset
                    section_data.clear()
                    last_continuity = None
                packet = data[offset + sync_offset : offset + sync_offset + 188]
                pid = ((packet[1] & 0x1F) << 8) | packet[2]
                adaptation_control = (packet[3] >> 4) & 0x03
                offset += packet_size
                if pid != EIT_PID or adaptation_control not in (1, 3):
                    continue
                if packet[1] & 0x80:
                    section_data.clear()
                    last_continuity = None
                    continue
                payload_offset = 4
                if adaptation_control == 3:
                    payload_offset += 1 + packet[4]
                if payload_offset >= len(packet):
                    continue
                continuity = packet[3] & 0x0F
                if last_continuity is not None and continuity != (last_continuity + 1) & 0x0F:
                    section_data.clear()
                last_continuity = continuity
                payload = packet[payload_offset:]
                if packet[1] & 0x40:
                    pointer = payload[0]
                    if pointer + 1 > len(payload):
                        section_data.clear()
                        continue
                    if section_data:
                        section_data.extend(payload[1 : 1 + pointer])
                        match = inspect_complete_sections()
                        if match is not None:
                            return match
                    section_data.clear()
                    payload = payload[1 + pointer :]
                section_data.extend(payload)
                match = inspect_complete_sections()
                if match is not None:
                    return match
            pending = data[offset:]
            if len(pending) > packet_size * 16:
                pending = pending[-packet_size * 16 :]
                section_data.clear()
                last_continuity = None
    return None


def validate_with_tsduck(path: Path, tsanalyze: str) -> list[str]:
    size = path.stat().st_size
    if size == 0:
        raise RuntimeError("trim output is empty")
    if size % 188 != 0:
        raise RuntimeError(f"trim output is not 188-byte aligned: {size} bytes")

    command = [tsanalyze, "--deterministic", "--no-pager", "--service-list", str(path)]
    print(f"  validate: {command_text(command)}")
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"tsanalyze failed with exit code {result.returncode}"
            + (f":\n{detail}" if detail else "")
        )

    service_ids = re.findall(r"(?<![\w])(?:0x[0-9a-fA-F]+|[0-9]+)(?![\w])", result.stdout)
    if not service_ids:
        detail = result.stdout.strip()
        raise RuntimeError(
            "tsanalyze found no services"
            + (f":\n{detail}" if detail else "")
        )
    return service_ids


def install_trimmed_file(
    source: Path,
    processing: Path,
    backup_suffix: str | None,
    no_replace: bool,
) -> Path:
    if no_replace:
        output = source.with_name(f"{source.stem}-trimed{source.suffix}")
        if output.exists():
            raise FileExistsError(f"trimmed output already exists: {output}")
        processing.rename(output)
        return output

    if backup_suffix is None:
        os.replace(processing, source)
        return source

    backup = source.with_name(source.name + backup_suffix)
    if backup.exists():
        raise FileExistsError(f"backup already exists: {backup}")
    source.rename(backup)
    try:
        os.replace(processing, source)
    except BaseException:
        if not source.exists() and backup.exists():
            backup.rename(source)
        raise
    return source


def trim_one(
    source: Path,
    tsreplace: str,
    tsanalyze: str,
    processing_suffix: str,
    backup_suffix: str | None,
    no_replace: bool,
    protected_patterns: dict[str, bytes],
    dry_run: bool,
    remove_failed_processing: bool,
) -> TrimResult | None:
    processing = source.with_name(source.name + processing_suffix)
    output = source.with_name(f"{source.stem}-trimed{source.suffix}") if no_replace else source
    command = [
        tsreplace,
        "-i",
        str(source),
        "-o",
        str(processing),
        "--smart-remove-typed",
    ]

    print(f"\n[{source}]")
    print(f"  trim:     {command_text(command)}")
    print(f"  temporary:{processing}")
    if no_replace:
        print(f"  output:   {output}")
    if no_replace and output.exists():
        raise FileExistsError(f"trimmed output already exists: {output}")
    if processing.exists():
        raise FileExistsError(
            f"processing file already exists: {processing}; inspect or remove it first"
        )

    before = SourceState.read(source)
    if not dry_run and protected_patterns:
        print("  EIT scan: protected program-name keywords")
        protected_keyword = find_protected_keyword(source, protected_patterns)
        if protected_keyword is not None:
            print(f"  skipped:  protected EIT keyword {protected_keyword!r}")
            return None
    free, required = check_available_space(source, before.size)
    print(f"  free space: {free} bytes (required: {required} bytes)")
    if dry_run:
        return None
    reserve_processing_file(processing)
    try:
        result = subprocess.run(command)
        if result.returncode != 0:
            raise RuntimeError(f"tsreplace failed with exit code {result.returncode}")

        services = validate_with_tsduck(processing, tsanalyze)
        if SourceState.read(source) != before:
            raise RuntimeError("source changed while trimming; it may still be recording")

        shutil.copystat(source, processing, follow_symlinks=False)
        trimmed_size = processing.stat().st_size
        output = install_trimmed_file(source, processing, backup_suffix, no_replace)
        saved = before.size - trimmed_size
        percent = saved * 100.0 / before.size if before.size else 0.0
        print(f"  services: {', '.join(services)}")
        print(
            f"  {'created' if no_replace else 'replaced'}: {output} "
            f"({before.size} -> {trimmed_size} bytes, "
            f"saved {saved} bytes, {percent:.2f}%)"
        )
        return TrimResult(source, output, before.size, trimmed_size)
    except BaseException:
        if remove_failed_processing:
            processing.unlink(missing_ok=True)
        else:
            print(f"  kept failed output: {processing}", file=sys.stderr)
        raise


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        raise NotADirectoryError(directory)
    if not args.processing_suffix:
        raise ValueError("--processing-suffix must not be empty")
    if args.backup_suffix == "":
        raise ValueError("--backup-suffix must not be empty")
    if args.no_replace and args.backup_suffix is not None:
        raise ValueError("--no-replace and --backup-suffix cannot be used together")

    extensions = parse_extensions(args.extensions)
    protected_keywords = [] if args.no_protect_keywords else [
        *DEFAULT_PROTECTED_KEYWORDS,
        *args.protect_keyword,
    ]
    sources = find_files(directory, extensions, args.recursive)
    print(f"directory: {directory}")
    print(f"files:     {len(sources)}")
    if not sources:
        return 0

    if args.dry_run:
        tsreplace = args.tsreplace
        tsanalyze = args.tsanalyze
        protected_patterns: dict[str, bytes] = {}
    else:
        tsreplace = resolve_executable(args.tsreplace, "tsreplace")
        tsanalyze = resolve_executable(args.tsanalyze, "tsanalyze")
        tsanalyze_version = check_tsanalyze(tsanalyze)
        print(f"tsreplace: {tsreplace}")
        print(f"tsanalyze: {tsanalyze} ({tsanalyze_version})")
        protected_patterns = {}
        if protected_keywords:
            tscharset = resolve_executable(args.tscharset, "tscharset")
            protected_patterns = encode_arib_keywords(tscharset, protected_keywords)
            print(f"tscharset: {tscharset}")
            print(f"protected: {', '.join(protected_patterns)}")

    succeeded: list[TrimResult] = []
    skipped = 0
    failed: list[tuple[Path, str]] = []
    for source in sources:
        try:
            result = trim_one(
                source=source,
                tsreplace=tsreplace,
                tsanalyze=tsanalyze,
                processing_suffix=args.processing_suffix,
                backup_suffix=args.backup_suffix,
                no_replace=args.no_replace,
                protected_patterns=protected_patterns,
                dry_run=args.dry_run,
                remove_failed_processing=args.remove_failed_processing,
            )
            if result is not None:
                succeeded.append(result)
            elif not args.dry_run:
                skipped += 1
        except Exception as error:
            failed.append((source, str(error)))
            print(f"  FAILED: {error}", file=sys.stderr)
            if args.fail_fast:
                break

    if args.dry_run:
        print(f"\ndry-run complete: {len(sources)} file(s)")
        return 0

    total_before = sum(item.original_size for item in succeeded)
    total_after = sum(item.trimmed_size for item in succeeded)
    print(
        f"\ncomplete: ok={len(succeeded)} skipped={skipped} failed={len(failed)} "
        f"saved={total_before - total_after} bytes"
    )
    for source, error in failed:
        print(f"  {source}: {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted; original file was not replaced", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
