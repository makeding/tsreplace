"""Fast PCR-timed sampling of a smart-trimmed Type-D packet layout."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path


TS_PACKET_SIZE = 188
PCR_WRAP = 1 << 33
TYPE_D_STREAM_TYPE = 0x0D
VIDEO_STREAM_TYPES = frozenset({0x01, 0x02, 0x1B, 0x24})
AUDIO_STREAM_TYPES = frozenset({0x03, 0x04, 0x0F, 0x11, 0x81})
STREAM_IDENTIFIER_DESCRIPTOR = 0x52
SMART_KEEP_INTERVAL_SECONDS = 870.0
SMART_KEEP_SECONDS = 60.0
MIDDLE_SAMPLE_INTERVAL_SECONDS = 600.0
MINIMUM_DETECTION_DURATION_SECONDS = 1200.0
DEFAULT_LAYOUT_SCAN_BYTES = 8 * 1024 * 1024
DEFAULT_REGION_SAMPLE_BYTES = 12 * 1024 * 1024
DEFAULT_INDEX_POINTS = 64
DEFAULT_INDEX_SEARCH_BYTES = 512 * 1024
DEFAULT_SEEK_SEARCH_BYTES = 64 * 1024
DEFAULT_SEEK_ITERATIONS = 18
MAX_PCR_GAP_SECONDS = 2.0


@dataclass(frozen=True)
class PacketFormat:
    span: int
    sync_offset: int


@dataclass(frozen=True)
class TypeDLayout:
    pids: frozenset[int]
    persistent_pids: frozenset[int]
    pcr_pids: frozenset[int] = frozenset()
    video_pids: frozenset[int] = frozenset()
    audio_pids: frozenset[int] = frozenset()
    first_av_pts: int | None = None
    packet_format: PacketFormat = PacketFormat(TS_PACKET_SIZE, 0)


@dataclass(frozen=True)
class SparseTypeDDetection:
    handled: bool
    message: str


@dataclass(frozen=True)
class TimeIndexEntry:
    elapsed: float
    offset: int


class _SectionAssembler:
    def __init__(self) -> None:
        self._buffers: dict[int, bytearray] = {}

    @staticmethod
    def _take_complete_sections(data: bytes) -> tuple[list[bytes], bytes]:
        sections: list[bytes] = []
        position = 0
        while position + 3 <= len(data) and data[position] != 0xFF:
            section_length = ((data[position + 1] & 0x0F) << 8) | data[position + 2]
            total = 3 + section_length
            if position + total > len(data):
                break
            sections.append(data[position : position + total])
            position += total
        return sections, data[position:]

    def feed(self, pid: int, payload_start: bool, payload: bytes) -> list[bytes]:
        if not payload:
            return []
        sections: list[bytes] = []
        if payload_start:
            pointer = payload[0]
            if pointer + 1 > len(payload):
                self._buffers.pop(pid, None)
                return []
            prefix = payload[1 : 1 + pointer]
            previous = self._buffers.pop(pid, None)
            if previous is not None:
                previous.extend(prefix)
                completed, _ = self._take_complete_sections(bytes(previous))
                sections.extend(completed)
            completed, remainder = self._take_complete_sections(payload[1 + pointer :])
            sections.extend(completed)
            if remainder and remainder[0] != 0xFF:
                self._buffers[pid] = bytearray(remainder)
            return sections

        previous = self._buffers.get(pid)
        if previous is None:
            return []
        previous.extend(payload)
        completed, remainder = self._take_complete_sections(bytes(previous))
        sections.extend(completed)
        if completed:
            self._buffers.pop(pid, None)
            if remainder and remainder[0] != 0xFF:
                self._buffers[pid] = bytearray(remainder)
        return sections


def _detect_packet_format(path: Path) -> PacketFormat | None:
    with path.open("rb") as stream:
        data = stream.read(64 * 1024)
    best: tuple[int, PacketFormat] | None = None
    for packet_format in (PacketFormat(188, 0), PacketFormat(192, 4)):
        available = (len(data) - packet_format.sync_offset) // packet_format.span
        checked = min(available, 32)
        if checked < 5:
            continue
        matches = sum(
            data[packet_format.sync_offset + index * packet_format.span] == 0x47
            for index in range(checked)
        )
        if matches < checked - 1:
            continue
        if best is None or matches > best[0]:
            best = matches, packet_format
    return best[1] if best is not None else None


def _iter_packets(data: bytes, packet_format: PacketFormat):
    for position in range(0, len(data) - packet_format.span + 1, packet_format.span):
        sync = position + packet_format.sync_offset
        packet = data[sync : sync + TS_PACKET_SIZE]
        if len(packet) == TS_PACKET_SIZE:
            yield position, packet


def _packet_payload(packet: bytes) -> tuple[int, bool, bytes] | None:
    if len(packet) != TS_PACKET_SIZE or packet[0] != 0x47 or packet[1] & 0x80:
        return None
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    payload_start = bool(packet[1] & 0x40)
    adaptation_control = (packet[3] >> 4) & 0x03
    if adaptation_control not in {1, 3}:
        return pid, payload_start, b""
    position = 4
    if adaptation_control == 3:
        if position >= len(packet):
            return None
        position += 1 + packet[position]
    if position > len(packet):
        return None
    return pid, payload_start, packet[position:]


def _mpeg_crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


def _valid_current_psi(section: bytes, table_id: int) -> bool:
    return (
        len(section) >= 12
        and section[0] == table_id
        and bool(section[1] & 0x80)
        and bool(section[5] & 0x01)
        and _mpeg_crc32(section) == 0
    )


def _packet_pcr(packet: bytes) -> int | None:
    if (
        len(packet) != TS_PACKET_SIZE
        or packet[0] != 0x47
        or packet[1] & 0x80
    ):
        return None
    adaptation_control = (packet[3] >> 4) & 0x03
    if adaptation_control not in {2, 3} or packet[4] < 7 or not packet[5] & 0x10:
        return None
    return (
        (packet[6] << 25)
        | (packet[7] << 17)
        | (packet[8] << 9)
        | (packet[9] << 1)
        | (packet[10] >> 7)
    )


def _packet_has_discontinuity(packet: bytes) -> bool:
    adaptation_control = (packet[3] >> 4) & 0x03
    return (
        adaptation_control in {2, 3}
        and packet[4] >= 1
        and bool(packet[5] & 0x80)
    )


def _payload_pts(payload: bytes) -> int | None:
    if len(payload) < 14 or payload[:3] != b"\x00\x00\x01":
        return None
    if ((payload[7] >> 6) & 0x03) not in {2, 3}:
        return None
    pts = payload[9:14]
    if not (pts[0] & 0x01 and pts[2] & 0x01 and pts[4] & 0x01):
        return None
    return (
        ((pts[0] >> 1) & 0x07) << 30
        | (pts[1] << 22)
        | ((pts[2] >> 1) << 15)
        | (pts[3] << 7)
        | (pts[4] >> 1)
    )


def _parse_pat(section: bytes) -> set[int]:
    if not _valid_current_psi(section, 0x00):
        return set()
    pmt_pids: set[int] = set()
    for position in range(8, len(section) - 4, 4):
        if position + 4 > len(section) - 4:
            break
        program_number = (section[position] << 8) | section[position + 1]
        if program_number:
            pmt_pids.add(((section[position + 2] & 0x1F) << 8) | section[position + 3])
    return pmt_pids


def _parse_pmt(section: bytes) -> TypeDLayout:
    if not _valid_current_psi(section, 0x02) or len(section) < 16:
        return TypeDLayout(frozenset(), frozenset())
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    position = 12 + program_info_length
    end = len(section) - 4
    pids: set[int] = set()
    persistent_pids: set[int] = set()
    video_pids: set[int] = set()
    audio_pids: set[int] = set()
    while position + 5 <= end:
        stream_type = section[position]
        pid = ((section[position + 1] & 0x1F) << 8) | section[position + 2]
        info_length = ((section[position + 3] & 0x0F) << 8) | section[position + 4]
        info_end = position + 5 + info_length
        if info_end > end:
            break
        if stream_type == TYPE_D_STREAM_TYPE:
            pids.add(pid)
            descriptor_position = position + 5
            while descriptor_position + 2 <= info_end:
                length = section[descriptor_position + 1]
                descriptor_end = descriptor_position + 2 + length
                if descriptor_end > info_end:
                    break
                if (
                    section[descriptor_position] == STREAM_IDENTIFIER_DESCRIPTOR
                    and length >= 1
                    and section[descriptor_position + 2] in {0x40, 0x80}
                ):
                    persistent_pids.add(pid)
                descriptor_position = descriptor_end
        if stream_type in VIDEO_STREAM_TYPES:
            video_pids.add(pid)
        if stream_type in AUDIO_STREAM_TYPES:
            audio_pids.add(pid)
        position = info_end
    return TypeDLayout(
        frozenset(pids),
        frozenset(persistent_pids),
        frozenset({pcr_pid}),
        frozenset(video_pids),
        frozenset(audio_pids),
    )


def inspect_type_d_layout(
    path: Path, scan_bytes: int = DEFAULT_LAYOUT_SCAN_BYTES
) -> TypeDLayout:
    packet_format = _detect_packet_format(path)
    if packet_format is None:
        return TypeDLayout(frozenset(), frozenset())
    assembler = _SectionAssembler()
    pmt_pids: set[int] = set()
    pmt_layouts: dict[int, TypeDLayout] = {}
    first_av_pts: int | None = None
    clock_pcr_pid: int | None = None
    clock_pmt_pid: int | None = None
    clock_changed = False
    with path.open("rb") as stream:
        data = stream.read(scan_bytes - (scan_bytes % packet_format.span))
    for _, packet in _iter_packets(data, packet_format):
        parsed = _packet_payload(packet)
        if parsed is None:
            continue
        pid, payload_start, payload = parsed
        known_av_pids = frozenset().union(
            *(
                layout.video_pids | layout.audio_pids
                for layout in pmt_layouts.values()
            )
        )
        if first_av_pts is None and payload_start and pid in known_av_pids:
            first_av_pts = _payload_pts(payload)
            if first_av_pts is not None:
                clock_layout = next(
                    layout
                    for layout in pmt_layouts.values()
                    if pid in layout.video_pids or pid in layout.audio_pids
                )
                clock_pcr_pid = next(iter(clock_layout.pcr_pids), None)
                clock_pmt_pid = next(
                    pmt_pid
                    for pmt_pid, layout in pmt_layouts.items()
                    if pid in layout.video_pids or pid in layout.audio_pids
                )
        if pid != 0 and pid not in pmt_pids:
            continue
        for section in assembler.feed(pid, payload_start, payload):
            if pid == 0:
                if _valid_current_psi(section, 0x00):
                    pmt_pids = _parse_pat(section)
            elif _valid_current_psi(section, 0x02):
                updated_layout = _parse_pmt(section)
                if clock_pmt_pid == pid and clock_pcr_pid not in updated_layout.pcr_pids:
                    clock_changed = True
                pmt_layouts[pid] = updated_layout
    active_layouts = [
        layout for pid, layout in pmt_layouts.items() if pid in pmt_pids
    ]
    if clock_changed:
        active_pcr_pids = frozenset()
    elif clock_pcr_pid is not None:
        active_pcr_pids = frozenset({clock_pcr_pid})
    else:
        active_pcr_pids = frozenset().union(
            *(layout.pcr_pids for layout in active_layouts)
        )
    return TypeDLayout(
        frozenset().union(*(layout.pids for layout in active_layouts)),
        frozenset().union(
            *(layout.persistent_pids for layout in active_layouts)
        ),
        active_pcr_pids,
        frozenset().union(*(layout.video_pids for layout in active_layouts)),
        frozenset().union(*(layout.audio_pids for layout in active_layouts)),
        first_av_pts,
        packet_format,
    )


def _packet_window(
    path: Path,
    center: int,
    byte_count: int,
    packet_format: PacketFormat,
):
    size = path.stat().st_size
    span_count = max(1, byte_count // packet_format.span)
    read_size = span_count * packet_format.span
    offset = max(0, min(max(0, size - read_size), center - read_size // 2))
    offset -= offset % packet_format.span
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(read_size)
    for relative_offset, packet in _iter_packets(data, packet_format):
        yield offset + relative_offset, packet


def _elapsed_seconds(timestamp: int, first_av_pts: int) -> float:
    difference = (timestamp - first_av_pts) & (PCR_WRAP - 1)
    if difference >= PCR_WRAP // 2:
        difference -= PCR_WRAP
    return difference / 90000.0


def _build_time_index(
    path: Path,
    layout: TypeDLayout,
    points: int = DEFAULT_INDEX_POINTS,
    search_bytes: int = DEFAULT_INDEX_SEARCH_BYTES,
) -> tuple[TimeIndexEntry, ...]:
    if layout.first_av_pts is None or not layout.pcr_pids:
        return ()
    size = path.stat().st_size
    entries: dict[int, TimeIndexEntry] = {}
    for index in range(points):
        center = int((size - 1) * index / max(1, points - 1))
        closest: tuple[int, TimeIndexEntry] | None = None
        for offset, packet in _packet_window(
            path, center, search_bytes, layout.packet_format
        ):
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            if pid not in layout.pcr_pids:
                continue
            if _packet_has_discontinuity(packet):
                return ()
            pcr = _packet_pcr(packet)
            if pcr is None:
                continue
            candidate = TimeIndexEntry(
                _elapsed_seconds(pcr, layout.first_av_pts), offset
            )
            distance = abs(offset - center)
            if closest is None or distance < closest[0]:
                closest = distance, candidate
        if closest is not None:
            entries[closest[1].offset] = closest[1]
    ordered = tuple(entries[offset] for offset in sorted(entries))
    if len(ordered) < 4 or any(
        later.elapsed < earlier.elapsed
        for earlier, later in zip(ordered, ordered[1:])
    ):
        return ()
    return ordered


def _pcr_entry_near(
    path: Path,
    center: int,
    layout: TypeDLayout,
    search_bytes: int = DEFAULT_SEEK_SEARCH_BYTES,
) -> TimeIndexEntry | None:
    closest: tuple[int, TimeIndexEntry] | None = None
    for offset, packet in _packet_window(
        path, center, search_bytes, layout.packet_format
    ):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid not in layout.pcr_pids:
            continue
        if _packet_has_discontinuity(packet):
            return None
        pcr = _packet_pcr(packet)
        if pcr is None or layout.first_av_pts is None:
            continue
        entry = TimeIndexEntry(_elapsed_seconds(pcr, layout.first_av_pts), offset)
        distance = abs(offset - center)
        if closest is None or distance < closest[0]:
            closest = distance, entry
    return closest[1] if closest is not None else None


def _offset_for_time(
    path: Path,
    layout: TypeDLayout,
    index: tuple[TimeIndexEntry, ...],
    timestamp: float,
) -> int | None:
    elapsed = [entry.elapsed for entry in index]
    position = bisect_left(elapsed, timestamp)
    if position == 0:
        return index[0].offset if timestamp >= index[0].elapsed - 1.0 else None
    if position == len(index):
        return index[-1].offset if timestamp <= index[-1].elapsed + 1.0 else None
    before = index[position - 1]
    after = index[position]
    for _ in range(DEFAULT_SEEK_ITERATIONS):
        if after.elapsed <= before.elapsed or after.offset <= before.offset:
            return None
        ratio = (timestamp - before.elapsed) / (after.elapsed - before.elapsed)
        center = int(before.offset + ratio * (after.offset - before.offset))
        candidate = _pcr_entry_near(path, center, layout)
        if candidate is None:
            return None
        if abs(candidate.elapsed - timestamp) <= 0.5:
            return candidate.offset
        if candidate.elapsed < timestamp:
            before = candidate
        else:
            after = candidate
    closest = min((before, after), key=lambda entry: abs(entry.elapsed - timestamp))
    return closest.offset if abs(closest.elapsed - timestamp) <= 0.5 else None


def _sample_has_pid(
    path: Path,
    center: int,
    timestamp: float,
    region_start: float,
    region_end: float,
    pids: frozenset[int],
    chunk_size: int,
    layout: TypeDLayout,
) -> bool | None:
    covered = False
    previous_pcr_elapsed: float | None = None
    pending_type_d = False
    for _, packet in _packet_window(path, center, chunk_size, layout.packet_format):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid in layout.pcr_pids:
            if _packet_has_discontinuity(packet):
                return None
            pcr = _packet_pcr(packet)
            if pcr is not None and layout.first_av_pts is not None:
                current_elapsed = _elapsed_seconds(pcr, layout.first_av_pts)
                if previous_pcr_elapsed is not None:
                    gap = current_elapsed - previous_pcr_elapsed
                    if gap < 0 or gap > MAX_PCR_GAP_SECONDS:
                        return None
                    covered = covered or (
                        previous_pcr_elapsed <= timestamp <= current_elapsed
                    )
                    if pending_type_d:
                        if (
                            region_start <= previous_pcr_elapsed
                            and current_elapsed <= region_end
                        ):
                            return True
                        if not (
                            current_elapsed < region_start
                            or previous_pcr_elapsed > region_end
                        ):
                            return None
                previous_pcr_elapsed = current_elapsed
                pending_type_d = False
        elif pid in pids and previous_pcr_elapsed is not None:
            pending_type_d = True
    if pending_type_d or not covered:
        return None
    return False


def _region_has_pid(
    path: Path,
    region_start: float,
    region_end: float,
    pids: frozenset[int],
    sample_bytes: int,
    layout: TypeDLayout,
    time_index: tuple[TimeIndexEntry, ...],
) -> bool | None:
    chunk_size = max(layout.packet_format.span, sample_bytes // 3)
    unknown = False
    for ratio in (1.0 / 6.0, 0.5, 5.0 / 6.0):
        timestamp = region_start + (region_end - region_start) * ratio
        center = _offset_for_time(path, layout, time_index, timestamp)
        if center is None:
            unknown = True
            continue
        sample_present = _sample_has_pid(
            path,
            center,
            timestamp,
            region_start,
            region_end,
            pids,
            chunk_size,
            layout,
        )
        if sample_present is True:
            return True
        unknown = unknown or sample_present is None
    return None if unknown else False


def detect_sparse_smart_trim(
    path: Path,
    duration: float | None,
    sample_bytes: int = DEFAULT_REGION_SAMPLE_BYTES,
) -> SparseTypeDDetection:
    if duration is None or duration < MINIMUM_DETECTION_DURATION_SECONDS:
        return SparseTypeDDetection(False, "recording is shorter than 20 minutes")
    layout = inspect_type_d_layout(path)
    sampled_pids = layout.pids - layout.persistent_pids
    if not sampled_pids:
        return SparseTypeDDetection(False, "no non-persistent Type-D PID found")
    time_index = _build_time_index(path, layout)
    if not time_index:
        return SparseTypeDDetection(False, "no continuous PCR/A-V PTS index found")

    start_present = _region_has_pid(
        path,
        0.0,
        SMART_KEEP_SECONDS,
        sampled_pids,
        sample_bytes,
        layout,
        time_index,
    )
    end_present = _region_has_pid(
        path,
        duration - SMART_KEEP_SECONDS,
        duration,
        sampled_pids,
        sample_bytes,
        layout,
        time_index,
    )
    periodic_present = False
    keep_start = SMART_KEEP_INTERVAL_SECONDS
    while keep_start + SMART_KEEP_SECONDS < duration - SMART_KEEP_SECONDS:
        periodic_present = periodic_present or _region_has_pid(
            path,
            keep_start,
            keep_start + SMART_KEEP_SECONDS,
            sampled_pids,
            sample_bytes,
            layout,
            time_index,
        )
        keep_start += SMART_KEEP_INTERVAL_SECONDS

    middle_samples = 0
    middle_with_type_d = 0
    middle_unknown = False
    middle_start = MIDDLE_SAMPLE_INTERVAL_SECONDS
    while middle_start + SMART_KEEP_SECONDS < duration - SMART_KEEP_SECONDS:
        phase = middle_start % SMART_KEEP_INTERVAL_SECONDS
        if (
            phase > SMART_KEEP_SECONDS
            and phase + SMART_KEEP_SECONDS < SMART_KEEP_INTERVAL_SECONDS
        ):
            middle_samples += 1
            middle_present = _region_has_pid(
                path,
                middle_start,
                middle_start + SMART_KEEP_SECONDS,
                sampled_pids,
                sample_bytes,
                layout,
                time_index,
            )
            if middle_present is None:
                middle_unknown = True
            elif middle_present:
                middle_with_type_d += 1
        middle_start += MIDDLE_SAMPLE_INTERVAL_SECONDS

    if start_present is not True:
        return SparseTypeDDetection(False, "opening keep window has no Type-D packets")
    if not (end_present is True or periodic_present is True):
        return SparseTypeDDetection(
            False, "no second Type-D keep window was observed"
        )
    if middle_unknown:
        return SparseTypeDDetection(False, "a sampled middle window has no PCR mapping")
    if middle_samples == 0 or middle_with_type_d != 0:
        return SparseTypeDDetection(
            False,
            f"middle windows with Type-D: {middle_with_type_d}/{middle_samples}",
        )
    return SparseTypeDDetection(
        True,
        "sparse Type-D pattern detected: opening and another keep window contain "
        f"data; all {middle_samples} sampled middle windows are empty",
    )
