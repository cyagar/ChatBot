"""Hand-built minimal OLE2 (Compound File Binary Format) writer, for P1-09
regression tests. olefile (already a dependency, used by extract_legacy_doc)
only reads OLE files -- there's no writer available, and no genuine sample
.doc ships with it or with the project, so this builds the smallest valid
compound file that satisfies olefile's own reader: one FAT sector, one
directory sector (Root Entry + a single named stream), and the stream's data
sectors. Good enough to prove sniff_file_type()/_is_word_ole() actually look
at stream names, not just the outer OLE signature.
"""

from __future__ import annotations

import struct

SECTOR_SIZE = 512
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD


def make_ole_file(stream_name: str, stream_data: bytes) -> bytes:
    """A compound file with exactly one stream, at the given name, holding
    the given bytes. stream_data is padded up to SECTOR_SIZE alignment."""
    data_sector_count = max(1, (len(stream_data) + SECTOR_SIZE - 1) // SECTOR_SIZE)
    padded_data = stream_data.ljust(data_sector_count * SECTOR_SIZE, b"\x00")

    # Sector 0 = FAT, sector 1 = directory, sectors 2.. = stream data.
    fat_sector_index = 0
    dir_sector_index = 1
    first_data_sector = 2

    header = bytearray(SECTOR_SIZE)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    header[8:24] = b"\x00" * 16  # CLSID
    struct.pack_into("<H", header, 24, 0x003E)  # minor version
    struct.pack_into("<H", header, 26, 0x0003)  # major version (512-byte sectors)
    struct.pack_into("<H", header, 28, 0xFFFE)  # byte order mark
    struct.pack_into("<H", header, 30, 9)  # sector shift (2^9 = 512)
    struct.pack_into("<H", header, 32, 6)  # mini sector shift (2^6 = 64)
    struct.pack_into("<6x", header, 34)  # reserved
    struct.pack_into("<I", header, 40, 0)  # number of directory sectors (0 for v3)
    struct.pack_into("<I", header, 44, 1)  # number of FAT sectors
    struct.pack_into("<I", header, 48, dir_sector_index)  # first directory sector
    struct.pack_into("<I", header, 52, 0)  # transaction signature
    struct.pack_into("<I", header, 56, 0x1000)  # mini stream cutoff size (4096)
    struct.pack_into("<I", header, 60, ENDOFCHAIN)  # first mini FAT sector
    struct.pack_into("<I", header, 64, 0)  # number of mini FAT sectors
    struct.pack_into("<I", header, 68, ENDOFCHAIN)  # first DIFAT sector
    struct.pack_into("<I", header, 72, 0)  # number of DIFAT sectors
    # DIFAT: 109 entries, first points at our one FAT sector, rest unused.
    difat = [FREESECT] * 109
    difat[0] = fat_sector_index
    struct.pack_into("<109I", header, 76, *difat)
    assert len(header) == SECTOR_SIZE

    fat = bytearray(SECTOR_SIZE)
    fat_entries = [FREESECT] * 128
    fat_entries[fat_sector_index] = FATSECT
    fat_entries[dir_sector_index] = ENDOFCHAIN
    for i in range(data_sector_count):
        sector = first_data_sector + i
        fat_entries[sector] = (first_data_sector + i + 1) if i < data_sector_count - 1 else ENDOFCHAIN
    struct.pack_into("<128I", fat, 0, *fat_entries)

    def dir_entry(name: str, obj_type: int, child_id: int, start_sector: int, size: int) -> bytes:
        entry = bytearray(128)
        name_utf16 = name.encode("utf-16-le") + b"\x00\x00"
        entry[0:len(name_utf16)] = name_utf16
        struct.pack_into("<H", entry, 64, len(name_utf16))
        entry[66] = obj_type
        entry[67] = 1  # color flag: black
        struct.pack_into("<I", entry, 68, FREESECT)  # left sibling
        struct.pack_into("<I", entry, 72, FREESECT)  # right sibling
        struct.pack_into("<I", entry, 76, child_id)
        # CLSID (16 bytes) + state bits (4) + 2 timestamps (16) already zero.
        struct.pack_into("<I", entry, 116, start_sector)
        struct.pack_into("<Q", entry, 120, size)
        return bytes(entry)

    directory = bytearray(SECTOR_SIZE)
    root = dir_entry("Root Entry", 5, 1, ENDOFCHAIN, 0)
    stream = dir_entry(stream_name, 2, FREESECT, first_data_sector, len(stream_data))
    directory[0:128] = root
    directory[128:256] = stream
    # Remaining two 128-byte slots stay zeroed (object type 0 = unused).

    return bytes(header) + bytes(fat) + bytes(directory) + padded_data


def word_stream_bytes(lines: list[str], min_bytes: int = 4096) -> bytes:
    """UTF-16LE text, matching what extract_legacy_doc's byte-scan looks for
    (runs of printable-ASCII-range UTF-16LE code units).

    min_bytes defaults to the CFBF Mini Stream Cutoff Size (4096): a stream
    declared SMALLER than that MUST live in the mini-FAT, which this minimal
    writer doesn't implement (only regular FAT sectors) -- so real content is
    padded with distinct filler lines (not repeats -- extract_legacy_doc
    dedupes identical lines) up to that size."""
    all_lines = list(lines)
    i = 0
    while len(("\r".join(all_lines)).encode("utf-16-le")) < min_bytes:
        all_lines.append(f"Filler padding line {i} to reach the minimum regular-stream size.")
        i += 1
    return "\r".join(all_lines).encode("utf-16-le")
