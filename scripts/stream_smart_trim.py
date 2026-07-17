#!/usr/bin/env python3
"""Feed a TS file to tsreplace through stdin and save stdout as a TS file."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read an input TS in Python, pipe it to `tsreplace -i - -o -`, "
            "and atomically publish the resulting output file."
        ),
    )
    parser.add_argument("input", type=Path, help="input TS/M2TS file")
    parser.add_argument("output", type=Path, help="output TS/M2TS file")
    parser.add_argument(
        "--tsreplace",
        default="./tsreplace",
        help="tsreplace executable (default: ./tsreplace)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        help="optional planned end in seconds; also preserves the final minute",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1024 * 1024,
        help="stdin write size in bytes (default: 1048576)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=0,
        help="feed speed relative to real time; 1 is real time, 10 is 10x, 0 is unlimited",
    )
    parser.add_argument(
        "--keep-partial",
        action="store_true",
        help="keep the .part output when tsreplace fails",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.chunk_size <= 0:
        raise ValueError("--chunk-size must be greater than zero")
    if args.speed < 0:
        raise ValueError("--speed must not be negative")
    if not args.input.is_file():
        raise FileNotFoundError(args.input)

    duration = args.duration
    if duration is not None and duration <= 0:
        raise ValueError("--duration must be greater than zero")
    if args.speed > 0 and duration is None:
        raise ValueError("--speed requires --duration")

    input_size = args.input.stat().st_size
    partial_output = args.output.with_name(args.output.name + ".part")
    partial_output.parent.mkdir(parents=True, exist_ok=True)

    command = [
        args.tsreplace,
        "-i",
        "-",
        "-o",
        "-",
        "--smart-remove-typed",
    ]
    if duration is not None:
        command.extend(["--smart-remove-typed-duration", f"{duration:.6f}"])

    print(f"input:    {args.input} ({input_size} bytes)", file=sys.stderr)
    print(f"output:   {args.output}", file=sys.stderr)
    if duration is not None:
        print(f"duration: {duration:.6f} sec", file=sys.stderr)
    print(f"command:  {shlex_join(command)}", file=sys.stderr)

    started_at = time.monotonic()
    sent = 0
    process: subprocess.Popen[bytes] | None = None

    try:
        with args.input.open("rb") as source, partial_output.open("wb") as destination:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=destination,
                stderr=None,
            )
            if process.stdin is None:
                raise RuntimeError("failed to open tsreplace stdin")

            while chunk := source.read(args.chunk_size):
                process.stdin.write(chunk)
                sent += len(chunk)

                if args.speed > 0 and input_size > 0 and duration is not None:
                    expected_elapsed = (sent / input_size) * duration / args.speed
                    remaining = expected_elapsed - (time.monotonic() - started_at)
                    if remaining > 0:
                        time.sleep(remaining)

            process.stdin.close()
            return_code = process.wait()

        elapsed = time.monotonic() - started_at
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, command)

        os.replace(partial_output, args.output)
        output_size = args.output.stat().st_size
        print(
            f"done: sent={sent} bytes output={output_size} bytes elapsed={elapsed:.3f} sec",
            file=sys.stderr,
        )
        return 0
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if not args.keep_partial:
            partial_output.unlink(missing_ok=True)
        raise


def shlex_join(command: list[str]) -> str:
    try:
        import shlex

        return shlex.join(command)
    except AttributeError:
        return " ".join(command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
