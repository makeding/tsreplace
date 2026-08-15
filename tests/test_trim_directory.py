from __future__ import annotations

import csv
import importlib.util
import io
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.ts_type_d import SparseTypeDDetection

SCRIPT = Path(__file__).parents[1] / "scripts" / "trim_directory.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("trim_directory", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
trim_directory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trim_directory
SPEC.loader.exec_module(trim_directory)


class TrimDirectoryTest(unittest.TestCase):
    def test_media_probe_requires_every_video_stream_to_be_hevc(self) -> None:
        result = types.SimpleNamespace(
            returncode=0,
            stdout=(
                '{"streams":[{"codec_name":"hevc"},{"codec_name":"h264"}],'
                '"format":{"duration":"1800.5"}}'
            ),
            stderr="",
        )
        with mock.patch.object(trim_directory.subprocess, "run", return_value=result):
            media = trim_directory.probe_source_media(Path("show.ts"), "ffprobe")

        self.assertEqual(media.video_codecs, ("hevc", "h264"))
        self.assertEqual(media.duration, 1800.5)
        self.assertFalse(media.is_hevc)

        result.stdout = (
            '{"streams":[{"codec_name":"hevc"}],'
            '"format":{"duration":"NaN"}}'
        )
        with mock.patch.object(trim_directory.subprocess, "run", return_value=result):
            media = trim_directory.probe_source_media(Path("show.ts"), "ffprobe")
        self.assertTrue(media.is_hevc)
        self.assertIsNone(media.duration)

    def test_external_publish_replaces_source_with_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "show.ts"
            processing = root / "archive" / "show.ts.processing"
            output = root / "archive" / "show.ts"
            source.parent.mkdir()
            processing.parent.mkdir()
            source.write_bytes(b"original")
            processing.write_bytes(b"encoded")

            result = trim_directory.install_external_file(source, processing, output)

            self.assertEqual(result, output)
            self.assertTrue(source.is_symlink())
            self.assertEqual(source.readlink(), output)
            self.assertEqual(source.read_bytes(), b"encoded")
            self.assertFalse(processing.exists())

    def test_report_marks_old_trim_but_not_transcode_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "show.ts"
            report = root / "report.tsv"
            source.write_bytes(b"trimmed")
            row = {column: "" for column in trim_directory.REPORT_COLUMNS}
            row.update(
                source_path=str(source),
                output_path=str(source),
                status="ok",
                original_size="100",
                trimmed_size=str(source.stat().st_size),
            )
            with report.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=trim_directory.REPORT_COLUMNS,
                    delimiter="\t",
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(row)

            type_d_handled, transcoded = trim_directory.transcode_state_from_report(
                report
            )

            self.assertEqual(type_d_handled, {source.resolve()})
            self.assertEqual(transcoded, set())

            stdout = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        str(SCRIPT),
                        str(root),
                        "--dry-run",
                        "--report",
                        str(report),
                        "--output-directory",
                        str(root.parent / f"{root.name}-archive"),
                        "--hiraku-address",
                        "192.168.6.230:40773",
                        "--hiraku-secret",
                        "secret",
                        "--no-protect-keywords",
                        "--no-skip-channels",
                    ],
                ),
                mock.patch.object(
                    trim_directory,
                    "probe_source_media",
                    return_value=trim_directory.SourceMediaInfo(("h264",), 1800.0),
                ),
                redirect_stdout(stdout),
            ):
                self.assertEqual(trim_directory.main(), 0)

            command_line = next(
                line for line in stdout.getvalue().splitlines() if "  trim:" in line
            )
            self.assertIn("-e hiraku pipe", command_line)
            self.assertNotIn("--smart-remove-typed", command_line)

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        str(SCRIPT),
                        str(root),
                        "--report",
                        str(report),
                        "--output-directory",
                        str(root.parent / f"{root.name}-archive"),
                        "--hiraku-address",
                        "192.168.6.230:40773",
                        "--hiraku-secret",
                        "secret",
                    ],
                ),
                mock.patch.object(
                    trim_directory,
                    "resolve_executable",
                    side_effect=lambda value, _label: value,
                ),
                mock.patch.object(
                    trim_directory,
                    "check_tsanalyze",
                    return_value="TSDuck test",
                ),
                mock.patch.object(
                    trim_directory,
                    "probe_source_media",
                    return_value=trim_directory.SourceMediaInfo(("h264",), 1800.0),
                ),
                mock.patch.object(trim_directory, "extract_program_info") as extract,
                mock.patch.object(trim_directory, "trim_one", return_value=None) as trim,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(trim_directory.main(), 0)

            extract.assert_not_called()
            self.assertFalse(trim.call_args.kwargs["smart_remove_typed"])

    def test_skipped_report_does_not_claim_type_d_was_trimmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "show.ts"
            report = root / "report.tsv"
            source.write_bytes(b"unchanged")
            row = {column: "" for column in trim_directory.REPORT_COLUMNS}
            row.update(
                source_path=str(source),
                status="skipped_channel",
                original_size=str(source.stat().st_size),
                trimmed_size=str(source.stat().st_size),
                message="built-in channel skip: BS11",
            )
            with report.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=trim_directory.REPORT_COLUMNS,
                    delimiter="\t",
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(row)

            type_d_handled, completed = trim_directory.transcode_state_from_report(
                report
            )

            self.assertEqual(type_d_handled, set())
            self.assertEqual(completed, set())

    def test_published_report_marks_hevc_processing_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "show.ts"
            output = root / "archive" / "show.ts"
            report = root / "report.tsv"
            source.write_bytes(b"source")
            output.parent.mkdir()
            output.write_bytes(b"published")
            row = {column: "" for column in trim_directory.REPORT_COLUMNS}
            row.update(
                source_path=str(source),
                output_path=str(output),
                status=trim_directory.PUBLISHED_REPORT_STATUS,
                original_size=str(source.stat().st_size),
                trimmed_size=str(output.stat().st_size),
            )
            with report.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=trim_directory.REPORT_COLUMNS,
                    delimiter="\t",
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(row)

            type_d_handled, completed = trim_directory.transcode_state_from_report(
                report
            )

            self.assertEqual(type_d_handled, set())
            self.assertEqual(completed, {source.resolve()})

    def test_published_trimmed_report_proves_both_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "show.ts"
            output = root / "archive" / "show.ts"
            report = root / "report.tsv"
            source.write_bytes(b"trimmed")
            output.parent.mkdir()
            output.write_bytes(b"trimmed")
            row = {column: "" for column in trim_directory.REPORT_COLUMNS}
            row.update(
                source_path=str(source),
                output_path=str(output),
                status=trim_directory.PUBLISHED_TRIMMED_REPORT_STATUS,
                original_size="100",
                trimmed_size=str(output.stat().st_size),
            )
            with report.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=trim_directory.REPORT_COLUMNS,
                    delimiter="\t",
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(row)

            type_d_handled, completed = trim_directory.transcode_state_from_report(
                report
            )

            self.assertEqual(type_d_handled, {source.resolve()})
            self.assertEqual(completed, {source.resolve()})

    def test_transcode_without_smart_trim_uses_hiraku_and_links_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            output_root = root / "archive"
            source = source_root / "series" / "show.ts"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"x" * 188)
            commands: list[list[str]] = []

            def run(command: list[str], **_kwargs: object) -> types.SimpleNamespace:
                commands.append(command)
                output = Path(command[command.index("-o") + 1])
                output.write_bytes(source.read_bytes())
                return types.SimpleNamespace(returncode=0)

            with (
                mock.patch.object(trim_directory.subprocess, "run", side_effect=run),
                mock.patch.object(
                    trim_directory,
                    "validate_with_tsduck",
                    return_value=["1"],
                ),
                mock.patch.object(
                    trim_directory,
                    "check_available_space",
                    return_value=(10_000, 376),
                ),
                mock.patch.object(trim_directory, "compare_programs"),
            ):
                result = trim_directory.trim_one(
                    source=source,
                    source_root=source_root,
                    tsreplace="tsreplace",
                    tsanalyze="tsanalyze",
                    ffprobe="ffprobe",
                    processing_suffix=".processing",
                    output_directory=output_root,
                    encoder_command=[
                        "hiraku",
                        "pipe",
                        "192.168.6.230:40773",
                        "secret",
                        "FFMPEG-X265",
                    ],
                    encoder_display_command=[
                        "hiraku",
                        "pipe",
                        "192.168.6.230:40773",
                        "<redacted>",
                        "FFMPEG-X265",
                    ],
                    smart_remove_typed=False,
                    smart_remove_typed_duration=None,
                    copy_source=False,
                    backup_suffix=None,
                    no_replace=False,
                    protected_keywords=[],
                    program_info=trim_directory.ProgramInfo(),
                    minimum_savings_bytes=0,
                    dry_run=False,
                    remove_failed_processing=False,
                )

            self.assertIsInstance(result, trim_directory.TrimResult)
            self.assertNotIn("--smart-remove-typed", commands[0])
            self.assertEqual(
                commands[0][-6:],
                [
                    "-e",
                    "hiraku",
                    "pipe",
                    "192.168.6.230:40773",
                    "secret",
                    "FFMPEG-X265",
                ],
            )
            output = output_root / "series" / "show.ts"
            self.assertTrue(source.is_symlink())
            self.assertEqual(source.readlink(), output)

    def test_source_validation_runs_while_transcoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            output_root = root / "archive"
            source = source_root / "show.ts"
            source.parent.mkdir()
            source.write_bytes(b"x" * 188)
            source_validation_started = threading.Event()
            transcode_started = threading.Event()

            def validate(path: Path, _tsanalyze: str) -> list[str]:
                if path == source:
                    source_validation_started.set()
                    self.assertTrue(transcode_started.wait(1))
                return ["1"]

            def run(command: list[str], **_kwargs: object) -> types.SimpleNamespace:
                self.assertTrue(source_validation_started.wait(1))
                transcode_started.set()
                output = Path(command[command.index("-o") + 1])
                output.write_bytes(source.read_bytes())
                return types.SimpleNamespace(returncode=0)

            with (
                mock.patch.object(trim_directory.subprocess, "run", side_effect=run),
                mock.patch.object(
                    trim_directory, "validate_with_tsduck", side_effect=validate
                ),
                mock.patch.object(
                    trim_directory,
                    "check_available_space",
                    return_value=(10_000, 376),
                ),
                mock.patch.object(trim_directory, "compare_programs"),
            ):
                result = trim_directory.trim_one(
                    source=source,
                    source_root=source_root,
                    tsreplace="tsreplace",
                    tsanalyze="tsanalyze",
                    ffprobe="ffprobe",
                    processing_suffix=".processing",
                    output_directory=output_root,
                    encoder_command=["hiraku", "pipe", "host", "secret", "PIPE"],
                    encoder_display_command=[
                        "hiraku", "pipe", "host", "<redacted>", "PIPE"
                    ],
                    smart_remove_typed=True,
                    smart_remove_typed_duration=1800.0,
                    copy_source=False,
                    backup_suffix=None,
                    no_replace=False,
                    protected_keywords=[],
                    program_info=trim_directory.ProgramInfo(),
                    minimum_savings_bytes=0,
                    dry_run=False,
                    remove_failed_processing=False,
                )

            self.assertIsInstance(result, trim_directory.TrimResult)
            self.assertTrue(source.is_symlink())

    def test_hevc_input_trims_type_d_without_hiraku(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "show.ts"
            source.write_bytes(b"x" * 188)
            stdout = io.StringIO()

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        str(SCRIPT),
                        str(root),
                        "--dry-run",
                        "--hiraku-address",
                        "192.168.6.230:40773",
                        "--hiraku-secret",
                        "secret",
                        "--no-report",
                    ],
                ),
                mock.patch.object(
                    trim_directory,
                    "probe_source_media",
                    return_value=trim_directory.SourceMediaInfo(("hevc",), 1800.0),
                ),
                mock.patch.object(
                    trim_directory,
                    "detect_sparse_smart_trim",
                    return_value=SparseTypeDDetection(False, "not sparse"),
                ),
                redirect_stdout(stdout),
            ):
                self.assertEqual(trim_directory.main(), 0)

            command_line = next(
                line for line in stdout.getvalue().splitlines() if "  trim:" in line
            )
            self.assertIn("--smart-remove-typed", command_line)
            self.assertIn("--smart-remove-typed-duration 1800.000000", command_line)
            self.assertNotIn("-e hiraku", command_line)

    def test_sparse_type_d_detection_skips_trim_but_keeps_needed_transcode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "show.ts"
            source.write_bytes(b"x" * 188)
            stdout = io.StringIO()

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        str(SCRIPT),
                        str(root),
                        "--dry-run",
                        "--hiraku-address",
                        "192.168.6.230:40773",
                        "--hiraku-secret",
                        "secret",
                        "--no-report",
                    ],
                ),
                mock.patch.object(
                    trim_directory,
                    "probe_source_media",
                    return_value=trim_directory.SourceMediaInfo(("h264",), 3600.0),
                ),
                mock.patch.object(
                    trim_directory,
                    "detect_sparse_smart_trim",
                    return_value=SparseTypeDDetection(True, "sparse pattern"),
                ),
                redirect_stdout(stdout),
            ):
                self.assertEqual(trim_directory.main(), 0)

            command_line = next(
                line for line in stdout.getvalue().splitlines() if "  trim:" in line
            )
            self.assertIn("-e hiraku pipe", command_line)
            self.assertNotIn("--smart-remove-typed", command_line)

    def test_copy_only_publishes_hevc_without_running_tsreplace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            output_root = root / "archive"
            source = source_root / "show.ts"
            source.parent.mkdir()
            source.write_bytes(b"x" * 188)

            with (
                mock.patch.object(trim_directory.subprocess, "run") as run,
                mock.patch.object(
                    trim_directory,
                    "validate_with_tsduck",
                    return_value=["1"],
                ),
                mock.patch.object(
                    trim_directory,
                    "check_available_space",
                    return_value=(10_000, 376),
                ),
            ):
                result = trim_directory.trim_one(
                    source=source,
                    source_root=source_root,
                    tsreplace="tsreplace",
                    tsanalyze="tsanalyze",
                    ffprobe="ffprobe",
                    processing_suffix=".processing",
                    output_directory=output_root,
                    encoder_command=None,
                    encoder_display_command=None,
                    smart_remove_typed=False,
                    smart_remove_typed_duration=None,
                    copy_source=True,
                    backup_suffix=None,
                    no_replace=False,
                    protected_keywords=[],
                    program_info=trim_directory.ProgramInfo(),
                    minimum_savings_bytes=0,
                    dry_run=False,
                    remove_failed_processing=False,
                )

            run.assert_not_called()
            self.assertIsInstance(result, trim_directory.TrimResult)
            output = output_root / "show.ts"
            self.assertTrue(source.is_symlink())
            self.assertEqual(source.readlink(), output)
            self.assertEqual(output.read_bytes(), b"x" * 188)

    def test_validation_failure_keeps_source_and_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            output_root = root / "archive"
            source = source_root / "show.ts"
            source.parent.mkdir()
            source.write_bytes(b"x" * 188)

            def run(command: list[str], **_kwargs: object) -> types.SimpleNamespace:
                processing = Path(command[command.index("-o") + 1])
                processing.write_bytes(source.read_bytes())
                return types.SimpleNamespace(returncode=0)

            with (
                mock.patch.object(trim_directory.subprocess, "run", side_effect=run),
                mock.patch.object(
                    trim_directory,
                    "validate_with_tsduck",
                    return_value=["1"],
                ),
                mock.patch.object(
                    trim_directory,
                    "compare_programs",
                    side_effect=RuntimeError("program mismatch"),
                ),
                mock.patch.object(
                    trim_directory,
                    "check_available_space",
                    return_value=(10_000, 376),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "program mismatch"):
                    trim_directory.trim_one(
                        source=source,
                        source_root=source_root,
                        tsreplace="tsreplace",
                        tsanalyze="tsanalyze",
                        ffprobe="ffprobe",
                        processing_suffix=".processing",
                        output_directory=output_root,
                        encoder_command=["hiraku", "pipe", "host", "secret", "PIPE"],
                        encoder_display_command=[
                            "hiraku", "pipe", "host", "<redacted>", "PIPE"
                        ],
                        smart_remove_typed=True,
                        smart_remove_typed_duration=None,
                        copy_source=False,
                        backup_suffix=None,
                        no_replace=False,
                        protected_keywords=[],
                        program_info=trim_directory.ProgramInfo(),
                        minimum_savings_bytes=0,
                        dry_run=False,
                        remove_failed_processing=False,
                    )

            self.assertTrue(source.is_file())
            self.assertFalse(source.is_symlink())
            self.assertEqual(source.read_bytes(), b"x" * 188)
            self.assertFalse((output_root / "show.ts").exists())
            self.assertTrue((output_root / "show.ts.processing").exists())


if __name__ == "__main__":
    unittest.main()
