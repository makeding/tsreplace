from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ts_type_d


class SparseTypeDDetectionTest(unittest.TestCase):
    @staticmethod
    def _ts_packet(pid: int, payload: bytes) -> bytes:
        packet = bytes(
            [0x47, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10, 0x00]
        ) + payload
        return packet.ljust(ts_type_d.TS_PACKET_SIZE, b"\xff")

    @staticmethod
    def _psi_section(table_id: int, body: bytes) -> bytes:
        section_length = len(body) + 4
        section = bytes(
            [
                table_id,
                0xB0 | ((section_length >> 8) & 0x0F),
                section_length & 0xFF,
            ]
        ) + body
        return section + ts_type_d._mpeg_crc32(section).to_bytes(4, "big")

    @staticmethod
    def _pts_packet(pid: int, pts: int) -> bytes:
        encoded_pts = bytes(
            [
                0x21 | (((pts >> 30) & 0x07) << 1),
                (pts >> 22) & 0xFF,
                0x01 | (((pts >> 15) & 0x7F) << 1),
                (pts >> 7) & 0xFF,
                0x01 | ((pts & 0x7F) << 1),
            ]
        )
        payload = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + encoded_pts
        packet = bytes(
            [0x47, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10]
        ) + payload
        return packet.ljust(ts_type_d.TS_PACKET_SIZE, b"\xff")

    @staticmethod
    def _pcr_packet(pid: int, pcr: int) -> bytes:
        encoded_pcr = bytes(
            [
                (pcr >> 25) & 0xFF,
                (pcr >> 17) & 0xFF,
                (pcr >> 9) & 0xFF,
                (pcr >> 1) & 0xFF,
                ((pcr & 0x01) << 7) | 0x7E,
                0x00,
            ]
        )
        packet = bytes(
            [0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x20, 0x07, 0x10]
        ) + encoded_pcr
        return packet.ljust(ts_type_d.TS_PACKET_SIZE, b"\xff")

    def _program_tables(self) -> tuple[bytes, bytes]:
        pat = self._psi_section(
            0x00,
            bytes([0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01, 0xE1, 0x00]),
        )
        pmt = self._psi_section(
            0x02,
            bytes(
                [
                    0x00,
                    0x01,
                    0xC1,
                    0x00,
                    0x00,
                    0xE1,
                    0x01,
                    0xF0,
                    0x00,
                    0x1B,
                    0xE1,
                    0x01,
                    0xF0,
                    0x00,
                    0x0D,
                    0xE2,
                    0x00,
                    0xF0,
                    0x03,
                    0x52,
                    0x01,
                    0x40,
                    0x0D,
                    0xE2,
                    0x01,
                    0xF0,
                    0x00,
                ]
            ),
        )
        return pat, pmt

    def test_layout_reads_type_d_and_excludes_entry_component(self) -> None:
        pat, pmt = self._program_tables()
        packets = [
            self._ts_packet(0x0000, pat),
            self._ts_packet(0x0100, pmt),
            self._ts_packet(0x1FFF, b""),
            self._ts_packet(0x0000, pat),
            self._ts_packet(0x0100, pmt),
            self._ts_packet(0x1FFF, b""),
        ]

        for m2ts in (False, True):
            with self.subTest(m2ts=m2ts), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "show.ts"
                path.write_bytes(
                    b"".join(
                        (b"\x00\x00\x00\x00" + packet) if m2ts else packet
                        for packet in packets
                    )
                )

                layout = ts_type_d.inspect_type_d_layout(path)

                self.assertEqual(layout.pids, frozenset({0x0200, 0x0201}))
                self.assertEqual(layout.persistent_pids, frozenset({0x0200}))
                self.assertEqual(layout.pcr_pids, frozenset({0x0101}))
                self.assertEqual(layout.video_pids, frozenset({0x0101}))
                self.assertEqual(layout.packet_format.span, 192 if m2ts else 188)

    def test_pcr_timed_sparse_detection_reads_real_packet_windows(self) -> None:
        pat, pmt = self._program_tables()
        type_d_seconds = {10, 30, 50, 880, 900, 920, 1150, 1170, 1190}
        packets = [
            self._ts_packet(0x0000, pat),
            self._ts_packet(0x0100, pmt),
            self._pts_packet(0x0101, 0),
        ]
        for second in range(1201):
            packets.append(self._pcr_packet(0x0101, second * 90000))
            if second in type_d_seconds:
                packets.append(self._ts_packet(0x0201, b"type-d"))
            null_count = 1 if second < 400 else 20 if second < 800 else 3
            packets.extend(
                self._ts_packet(0x1FFF, b"") for _ in range(null_count)
            )

        for m2ts in (False, True):
            with self.subTest(m2ts=m2ts), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "show.ts"
                path.write_bytes(
                    b"".join(
                        (b"\x00\x00\x00\x00" + packet) if m2ts else packet
                        for packet in packets
                    )
                )

                layout = ts_type_d.inspect_type_d_layout(path)
                time_index = ts_type_d._build_time_index(path, layout)
                periodic_present = ts_type_d._region_has_pid(
                    path,
                    870.0,
                    930.0,
                    frozenset({0x0201}),
                    150 * ts_type_d.TS_PACKET_SIZE,
                    layout,
                    time_index,
                )
                self.assertTrue(periodic_present)

                result = ts_type_d.detect_sparse_smart_trim(
                    path, 1200.0, sample_bytes=150 * ts_type_d.TS_PACKET_SIZE
                )

                self.assertTrue(result.handled, result.message)

    def test_current_pmt_replaces_old_type_d_layout_and_audio_can_start_clock(self) -> None:
        pat, old_pmt = self._program_tables()
        current_pmt = self._psi_section(
            0x02,
            bytes(
                [
                    0x00,
                    0x01,
                    0xC3,
                    0x00,
                    0x00,
                    0xE1,
                    0x01,
                    0xF0,
                    0x00,
                    0x0F,
                    0xE1,
                    0x02,
                    0xF0,
                    0x00,
                    0x1B,
                    0xE1,
                    0x01,
                    0xF0,
                    0x00,
                ]
            ),
        )
        packets = [
            self._ts_packet(0x0000, pat),
            self._ts_packet(0x0100, old_pmt),
            self._ts_packet(0x0100, current_pmt),
            self._pts_packet(0x0102, 0),
            self._pts_packet(0x0101, 90000),
            self._ts_packet(0x1FFF, b""),
        ]

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "show.ts"
            path.write_bytes(b"".join(packets))

            layout = ts_type_d.inspect_type_d_layout(path)

        self.assertEqual(layout.pids, frozenset())
        self.assertEqual(layout.audio_pids, frozenset({0x0102}))
        self.assertEqual(layout.first_av_pts, 0)

    def test_pcr_pid_change_after_clock_start_is_inconclusive(self) -> None:
        pat, old_pmt = self._program_tables()
        changed_pmt = self._psi_section(
            0x02,
            bytes(
                [
                    0x00,
                    0x01,
                    0xC3,
                    0x00,
                    0x00,
                    0xE1,
                    0x03,
                    0xF0,
                    0x00,
                    0x1B,
                    0xE1,
                    0x01,
                    0xF0,
                    0x00,
                    0x0D,
                    0xE2,
                    0x01,
                    0xF0,
                    0x00,
                ]
            ),
        )
        packets = [
            self._ts_packet(0x0000, pat),
            self._ts_packet(0x0100, old_pmt),
            self._pts_packet(0x0101, 0),
            self._ts_packet(0x0100, changed_pmt),
            self._ts_packet(0x1FFF, b""),
            self._ts_packet(0x1FFF, b""),
        ]

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "show.ts"
            path.write_bytes(b"".join(packets))
            layout = ts_type_d.inspect_type_d_layout(path)

        self.assertEqual(layout.pcr_pids, frozenset())

    def test_type_d_without_a_close_following_pcr_is_inconclusive(self) -> None:
        packets = [
            self._pcr_packet(0x0101, 0),
            self._ts_packet(0x0201, b"type-d"),
            self._pcr_packet(0x0101, 200 * 90000),
            self._ts_packet(0x1FFF, b""),
            self._ts_packet(0x1FFF, b""),
        ]
        layout = ts_type_d.TypeDLayout(
            frozenset({0x0201}),
            frozenset(),
            frozenset({0x0101}),
            frozenset({0x0101}),
            frozenset(),
            0,
        )
        index = (
            ts_type_d.TimeIndexEntry(0.0, 0),
            ts_type_d.TimeIndexEntry(200.0, 2 * ts_type_d.TS_PACKET_SIZE),
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "show.ts"
            path.write_bytes(b"".join(packets))
            with mock.patch.object(ts_type_d, "_offset_for_time", return_value=0):
                present = ts_type_d._region_has_pid(
                    path,
                    0.0,
                    60.0,
                    frozenset({0x0201}),
                    len(packets) * ts_type_d.TS_PACKET_SIZE * 3,
                    layout,
                    index,
                )

        self.assertIsNone(present)

    def test_opening_and_end_with_empty_middle_is_handled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "show.ts"
            path.write_bytes(b"x" * 188)
            layout = ts_type_d.TypeDLayout(
                frozenset({0x200, 0x201}), frozenset({0x200})
            )

            def region_has_pid(
                _path: Path,
                start: float,
                end: float,
                pids: frozenset[int],
                _sample_bytes: int,
                _layout: ts_type_d.TypeDLayout,
                _time_index: tuple[ts_type_d.TimeIndexEntry, ...],
            ) -> bool:
                self.assertEqual(pids, frozenset({0x201}))
                return start == 0.0 or end == 3600.0

            with (
                mock.patch.object(
                    ts_type_d, "inspect_type_d_layout", return_value=layout
                ),
                mock.patch.object(
                    ts_type_d,
                    "_build_time_index",
                    return_value=tuple(
                        ts_type_d.TimeIndexEntry(float(value), value)
                        for value in (0, 1200, 2400, 3600)
                    ),
                ),
                mock.patch.object(
                    ts_type_d, "_region_has_pid", side_effect=region_has_pid
                ),
            ):
                result = ts_type_d.detect_sparse_smart_trim(path, 3600.0)

            self.assertTrue(result.handled)
            self.assertIn("all", result.message)

    def test_type_d_in_a_middle_window_is_not_handled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "show.ts"
            path.write_bytes(b"x" * 188)
            layout = ts_type_d.TypeDLayout(frozenset({0x201}), frozenset())

            def region_has_pid(
                _path: Path,
                start: float,
                end: float,
                _pids: frozenset[int],
                _sample_bytes: int,
                _layout: ts_type_d.TypeDLayout,
                _time_index: tuple[ts_type_d.TimeIndexEntry, ...],
            ) -> bool:
                return start == 0.0 or end == 3600.0 or start == 600.0

            with (
                mock.patch.object(
                    ts_type_d, "inspect_type_d_layout", return_value=layout
                ),
                mock.patch.object(
                    ts_type_d,
                    "_build_time_index",
                    return_value=tuple(
                        ts_type_d.TimeIndexEntry(float(value), value)
                        for value in (0, 1200, 2400, 3600)
                    ),
                ),
                mock.patch.object(
                    ts_type_d, "_region_has_pid", side_effect=region_has_pid
                ),
            ):
                result = ts_type_d.detect_sparse_smart_trim(path, 3600.0)

            self.assertFalse(result.handled)
            self.assertIn("middle windows with Type-D", result.message)


if __name__ == "__main__":
    unittest.main()
