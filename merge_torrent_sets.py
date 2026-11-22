#!/usr/bin/env python3
"""
Merge two partial BitTorrent (v1) download sets of the SAME torrent into one
folder, keeping whichever copy of each piece matches the torrent's SHA-1 hash.

EXPERIMENTAL. No warranty. Keep backups of both input sets. This script
never modifies the two input folders. Output is a new folder next to input
folder A, named like "<A-folder>-merged" (a numeric suffix is added if that
name already exists).

Credits: the script, tests, and docs were written mostly by Grok Bot (an AI
assistant), with idea, direction, and testing by gr88.

Usage (Ubuntu, stock python3 — standard library only):

    python3 merge_torrent_sets.py
    python3 merge_torrent_sets.py /path/to/file.torrent /path/to/setA /path/to/setB

You will be prompted for any path you omit. Pasted paths may be quoted;
leading/trailing quotes and whitespace are stripped, and ~ is expanded.

After the merge, use the result in BiglyBT (or another v1-compatible client):

    1. Add the same .torrent file (or verify the existing torrent entry).
    2. Set the torrent's data location to the merged folder (the folder
       that contains the content files — for a multi-file torrent this is
       the folder that plays the role of info.name). You can also move or
       rename the merged folder to where the client already expects the data.
    3. Run "Force Recheck" (Force recheck / Re-check files).
    4. The client will mark every piece that hashed correctly as done, and
       will download only the pieces listed as still bad in the report.

A text report is written NEXT TO the merged folder (not inside it, so the
folder stays clean for the client) as "<merged-folder-name>-report.txt".

Supported: single-file and multi-file BitTorrent v1 torrents, including
hybrid v1/v2 torrents that still carry v1 'pieces' hashes (typical of
BiglyBT). Pure v2-only torrents (no v1 'pieces' field) are not supported.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path


# ---------------------------------------------------------------------------
# Bencode (decode only)
# ---------------------------------------------------------------------------

class BencodeError(Exception):
    """Invalid or truncated bencoded data."""


def _bdecode_at(data: bytes, pos: int) -> tuple[object, int]:
    if pos >= len(data):
        raise BencodeError("unexpected end of bencode data")
    ch = data[pos]

    if ch == 0x69:  # 'i' integer
        end = data.find(b"e", pos + 1)
        if end < 0:
            raise BencodeError("unterminated integer")
        raw = data[pos + 1 : end]
        if raw == b"" or raw == b"-" or raw == b"-0":
            raise BencodeError(f"invalid integer: {raw!r}")
        if raw[0:1] == b"-":
            digits = raw[1:]
        else:
            digits = raw
        if digits[0:1] == b"0" and digits != b"0":
            raise BencodeError(f"leading zero in integer: {raw!r}")
        if not digits.isdigit():
            raise BencodeError(f"invalid integer: {raw!r}")
        return int(raw), end + 1

    if ch == 0x6C:  # 'l' list
        pos += 1
        out: list[object] = []
        while True:
            if pos >= len(data):
                raise BencodeError("unterminated list")
            if data[pos] == 0x65:  # 'e'
                return out, pos + 1
            item, pos = _bdecode_at(data, pos)
            out.append(item)

    if ch == 0x64:  # 'd' dict
        pos += 1
        out: dict[str, object] = {}
        while True:
            if pos >= len(data):
                raise BencodeError("unterminated dict")
            if data[pos] == 0x65:  # 'e'
                return out, pos + 1
            key, pos = _bdecode_at(data, pos)
            if not isinstance(key, bytes):
                raise BencodeError("dict key is not a byte string")
            try:
                key_s = key.decode("utf-8")
            except UnicodeDecodeError:
                key_s = key.decode("latin-1")
            value, pos = _bdecode_at(data, pos)
            out[key_s] = value
        return out, pos

    if 0x30 <= ch <= 0x39:  # byte string: <len>:<data>
        colon = data.find(b":", pos)
        if colon < 0:
            raise BencodeError("unterminated string length")
        length_raw = data[pos:colon]
        if length_raw[0:1] == b"0" and length_raw != b"0":
            raise BencodeError(f"leading zero in string length: {length_raw!r}")
        try:
            length = int(length_raw)
        except ValueError as exc:
            raise BencodeError(f"invalid string length: {length_raw!r}") from exc
        if length < 0:
            raise BencodeError("negative string length")
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeError("string overruns bencode data")
        return data[start:end], end

    raise BencodeError(f"invalid bencode type byte 0x{ch:02x} at offset {pos}")


def bdecode(data: bytes) -> object:
    value, pos = _bdecode_at(data, 0)
    if pos != len(data):
        # Trailing junk is unusual; tolerate only trailing whitespace/nulls.
        extra = data[pos:]
        if extra.strip(b"\x00 \t\r\n"):
            raise BencodeError("trailing data after bencode value")
    return value


# ---------------------------------------------------------------------------
# Torrent metadata
# ---------------------------------------------------------------------------

def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"expected bytes or str, got {type(value).__name__}")


def _decode_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("latin-1")
    raise TypeError(f"expected text, got {type(value).__name__}")


def _attr_is_padding(attr: object) -> bool:
    if attr is None:
        return False
    raw = _as_bytes(attr)
    return b"p" in raw


class TorrentFile:
    __slots__ = ("relpath", "length", "offset", "padding")

    def __init__(self, relpath: Path, length: int, offset: int, padding: bool) -> None:
        self.relpath = relpath
        self.length = length
        self.offset = offset
        self.padding = padding


class TorrentMeta:
    def __init__(
        self,
        name: str,
        piece_length: int,
        piece_hashes: list[bytes],
        files: list[TorrentFile],
        multifile: bool,
    ) -> None:
        self.name = name
        self.piece_length = piece_length
        self.piece_hashes = piece_hashes
        self.files = files
        self.multifile = multifile
        self.total_length = files[-1].offset + files[-1].length if files else 0
        self.num_pieces = len(piece_hashes)

    def piece_size(self, index: int) -> int:
        start = index * self.piece_length
        end = min(start + self.piece_length, self.total_length)
        return max(0, end - start)


def parse_torrent(path: Path) -> TorrentMeta:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"Cannot read torrent file {path}: {exc}") from exc
    try:
        decoded = bdecode(data)
    except BencodeError as exc:
        raise SystemExit(f"Invalid .torrent file {path}: {exc}") from exc
    if not isinstance(decoded, dict) or "info" not in decoded:
        raise SystemExit(f"{path} is not a valid .torrent (missing 'info')")
    info = decoded["info"]
    if not isinstance(info, dict):
        raise SystemExit(f"{path} has an invalid 'info' dictionary")

    pieces = info.get("pieces")
    meta_version = info.get("meta version")
    if not pieces:
        if meta_version == 2 or "file tree" in info or "piece layers" in decoded:
            raise SystemExit(
                "This looks like a pure BitTorrent v2 torrent (no v1 'pieces' "
                "SHA-1 hashes). This script only supports v1 and hybrid v1/v2 "
                "torrents that include the v1 'pieces' field. Re-download or "
                "export a v1/hybrid .torrent from your client and try again."
            )
        raise SystemExit(
            f"{path} has no 'pieces' field in its info dict, so piece hashes "
            "cannot be checked. Pure v2-only torrents are not supported."
        )
    pieces_b = _as_bytes(pieces)
    if len(pieces_b) % 20 != 0 or not pieces_b:
        raise SystemExit(
            f"{path}: 'pieces' length {len(pieces_b)} is not a positive "
            "multiple of 20"
        )
    piece_hashes = [pieces_b[i : i + 20] for i in range(0, len(pieces_b), 20)]

    piece_length = info.get("piece length")
    if not isinstance(piece_length, int) or piece_length <= 0:
        raise SystemExit(f"{path}: invalid 'piece length'")

    name = _decode_text(info.get("name.utf-8") or info.get("name") or "torrent-content")

    files: list[TorrentFile] = []
    offset = 0
    if "files" in info:
        file_list = info["files"]
        if not isinstance(file_list, list) or not file_list:
            raise SystemExit(f"{path}: invalid multi-file 'files' list")
        for entry in file_list:
            if not isinstance(entry, dict):
                raise SystemExit(f"{path}: invalid file entry in 'files'")
            length = entry.get("length")
            if not isinstance(length, int) or length < 0:
                raise SystemExit(f"{path}: invalid file length")
            path_key = entry.get("path.utf-8") or entry.get("path")
            if not isinstance(path_key, list) or not path_key:
                raise SystemExit(f"{path}: invalid file path")
            parts = [_decode_text(p) for p in path_key]
            if any(p in ("", ".", "..") or (os.sep in p) or (os.altsep and os.altsep in p) for p in parts):
                raise SystemExit(f"{path}: unsafe file path component {parts!r}")
            rel = Path(*parts)
            padding = _attr_is_padding(entry.get("attr"))
            files.append(TorrentFile(rel, length, offset, padding))
            offset += length
        multifile = True
    elif "length" in info:
        length = info["length"]
        if not isinstance(length, int) or length < 0:
            raise SystemExit(f"{path}: invalid single-file 'length'")
        # Guard against a name that would escape the output folder.
        if name in ("", ".", "..") or os.sep in name or (os.altsep and os.altsep in name):
            raise SystemExit(f"{path}: unsafe torrent name {name!r}")
        files.append(TorrentFile(Path(name), length, 0, False))
        offset = length
        multifile = False
    else:
        # Hybrid v2 might have 'file tree' only — already handled if no pieces.
        raise SystemExit(
            f"{path}: info dict has neither 'files' nor 'length' "
            "(unsupported torrent layout)"
        )

    if files and files[-1].offset + files[-1].length == 0:
        raise SystemExit(f"{path}: torrent has no data (total length 0)")

    expected_pieces = (offset + piece_length - 1) // piece_length if offset else 0
    if expected_pieces != len(piece_hashes):
        raise SystemExit(
            f"{path}: piece count mismatch: 'pieces' has {len(piece_hashes)} "
            f"hashes but total length {offset} with piece length {piece_length} "
            f"requires {expected_pieces}"
        )

    return TorrentMeta(name, piece_length, piece_hashes, files, multifile)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def normalize_user_path(raw: str) -> Path:
    """Strip whitespace and surrounding quotes, then expand ~."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return Path(s).expanduser()


def prompt_path(label: str) -> Path:
    try:
        raw = input(f"{label}: ").strip()
    except EOFError:
        raise SystemExit("No path provided (EOF).") from None
    if not raw:
        raise SystemExit(f"No path entered for {label}.")
    return normalize_user_path(raw)


def unique_output_dir(beside: Path, suffix: str = "-merged") -> Path:
    """Create a name like <beside.name>-merged next to beside; add -2, -3, ... if taken."""
    parent = beside.parent
    base = beside.name + suffix
    candidate = parent / base
    n = 2
    while candidate.exists():
        candidate = parent / f"{base}-{n}"
        n += 1
    return candidate


def _file_present(root: Path, tfile: TorrentFile) -> bool:
    if tfile.padding:
        return False
    path = root / tfile.relpath
    return path.is_file()


def detect_content_root(user_path: Path, meta: TorrentMeta) -> Path:
    """
    Return the directory that directly contains the torrent's content files.

    Multi-file: the user may pass either the top-level folder (info.name) or
    its parent. We pick whichever actually resolves more of the torrent files.
    Single-file: the user may pass the file itself or the directory that
    contains it.
    """
    user_path = user_path.expanduser()
    if not user_path.exists():
        raise SystemExit(f"Input path does not exist: {user_path}")

    if not meta.multifile:
        # User may pass the file itself, the directory that contains it, or
        # (less commonly) a parent of a folder named info.name that holds it.
        if user_path.is_file():
            return user_path.parent
        if (user_path / meta.name).is_file():
            return user_path
        nested = user_path / meta.name
        if nested.is_dir() and (nested / meta.name).is_file():
            return nested
        return user_path

    # Multi-file: compare hits under user_path vs user_path / info.name
    option_direct = user_path
    option_nested = user_path / meta.name

    def score(root: Path) -> int:
        if not root.is_dir():
            return -1
        return sum(1 for f in meta.files if not f.padding and _file_present(root, f))

    score_direct = score(option_direct)
    score_nested = score(option_nested)

    if score_nested > score_direct:
        return option_nested
    if score_direct > score_nested:
        return option_direct

    # Tie (often both zero, when almost everything is missing): use layout hints.
    if option_nested.is_dir() and user_path.name != meta.name:
        return option_nested
    if user_path.is_dir() and user_path.name == meta.name:
        return option_direct
    if option_nested.is_dir():
        return option_nested
    return option_direct


# ---------------------------------------------------------------------------
# Efficient piece I/O
# ---------------------------------------------------------------------------

class FileHandleCache:
    """Keep a bounded set of open file handles (read or write)."""

    def __init__(self, mode: str, max_open: int = 256) -> None:
        self.mode = mode
        self.max_open = max_open
        self._handles: OrderedDict[Path, object] = OrderedDict()

    def _open(self, path: Path):
        handle = self._handles.get(path)
        if handle is not None:
            self._handles.move_to_end(path)
            return handle
        if len(self._handles) >= self.max_open:
            old_path, old = self._handles.popitem(last=False)
            try:
                old.close()
            except OSError:
                pass
        flags = os.O_RDONLY
        if "w" in self.mode:
            flags = os.O_RDWR
        fd = os.open(path, flags)
        self._handles[path] = fd
        return fd

    def pread(self, path: Path, offset: int, size: int) -> bytes:
        """Read up to size bytes at offset; short read if the file is shorter."""
        try:
            if not path.is_file():
                return b""
            st = path.stat()
            if offset >= st.st_size:
                return b""
            size = min(size, st.st_size - offset)
            if size <= 0:
                return b""
            fd = self._open(path)
            return os.pread(fd, size, offset)
        except FileNotFoundError:
            return b""
        except OSError:
            return b""

    def pwrite(self, path: Path, offset: int, data: bytes) -> None:
        fd = self._open(path)
        os.pwrite(fd, data, offset)

    def close(self) -> None:
        while self._handles:
            _, fd = self._handles.popitem()
            try:
                os.close(fd)
            except OSError:
                pass


def _sha1(data: bytes) -> bytes:
    try:
        return hashlib.sha1(data, usedforsecurity=False).digest()
    except TypeError:  # pragma: no cover - older Python
        return hashlib.sha1(data).digest()  # noqa: S324


def _first_file_index_at(files: list[TorrentFile], offset: int) -> int:
    """Binary search: first file whose range extends past offset."""
    lo, hi = 0, len(files)
    while lo < hi:
        mid = (lo + hi) // 2
        f = files[mid]
        if f.offset + f.length <= offset:
            lo = mid + 1
        else:
            hi = mid
    return lo


def iter_piece_regions(meta: TorrentMeta, piece_index: int):
    """Yield (tfile, offset_in_file, length, offset_in_piece) for a piece."""
    start = piece_index * meta.piece_length
    remaining = meta.piece_size(piece_index)
    pos = start
    off_in_piece = 0
    i = _first_file_index_at(meta.files, start)
    while remaining > 0 and i < len(meta.files):
        tfile = meta.files[i]
        take_start = pos - tfile.offset
        if take_start < 0 or take_start >= tfile.length:
            # Should not happen when files are contiguous from offset 0.
            i += 1
            continue
        take = min(tfile.length - take_start, remaining)
        if take > 0:
            yield tfile, take_start, take, off_in_piece
            pos += take
            off_in_piece += take
            remaining -= take
        i += 1


def read_piece(
    root: Path, meta: TorrentMeta, piece_index: int, cache: FileHandleCache
) -> tuple[bytes, bool]:
    """
    Read one piece from a content root. Missing or short files contribute
    zero bytes (so the hash will fail unless those bytes really are zero).

    Returns (data, had_data). had_data is True if at least one non-padding
    byte was actually read from a file on disk (not filled in as a zero
    because the file was missing or truncated).
    """
    size = meta.piece_size(piece_index)
    buf = bytearray(size)  # already zero-filled
    had_data = False
    for tfile, file_off, length, piece_off in iter_piece_regions(meta, piece_index):
        if tfile.padding:
            # Padding is defined as zeros; leave the zero fill in place.
            continue
        got = cache.pread(root / tfile.relpath, file_off, length)
        if got:
            buf[piece_off : piece_off + len(got)] = got
            had_data = True
        # If got is short, the rest of this region stays zeros.
    return bytes(buf), had_data


def write_piece(
    root: Path, meta: TorrentMeta, piece_index: int, data: bytes, cache: FileHandleCache
) -> None:
    for tfile, file_off, length, piece_off in iter_piece_regions(meta, piece_index):
        if tfile.padding or length == 0:
            continue
        chunk = data[piece_off : piece_off + length]
        cache.pwrite(root / tfile.relpath, file_off, chunk)


def preallocate_output(root: Path, meta: TorrentMeta) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for tfile in meta.files:
        if tfile.padding:
            continue
        dest = root / tfile.relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Open/create and extend to the full length. On Linux this is sparse
        # (holes until we write real data), which is fine for large torrents.
        flags = os.O_RDWR | os.O_CREAT
        fd = os.open(dest, flags, 0o644)
        try:
            os.ftruncate(fd, tfile.length)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

class MergeStats:
    def __init__(self) -> None:
        self.total = 0
        self.good_from_a = 0  # A hashed OK (used A); includes "both good"
        self.good_from_b_only = 0
        self.good_in_both = 0
        self.bad_in_both = 0
        self.bad_indexes: list[int] = []


def merge_sets(meta: TorrentMeta, root_a: Path, root_b: Path, out_root: Path) -> MergeStats:
    stats = MergeStats()
    stats.total = meta.num_pieces
    preallocate_output(out_root, meta)

    cache_a = FileHandleCache("r")
    cache_b = FileHandleCache("r")
    cache_out = FileHandleCache("w")
    started = time.monotonic()
    last_print = started
    progress_every = max(1, meta.num_pieces // 100)  # ~1% steps, plus time-based

    try:
        for i in range(meta.num_pieces):
            data_a, had_a = read_piece(root_a, meta, i, cache_a)
            data_b, had_b = read_piece(root_b, meta, i, cache_b)
            expected = meta.piece_hashes[i]
            ok_a = _sha1(data_a) == expected
            ok_b = _sha1(data_b) == expected

            if ok_a and ok_b:
                stats.good_in_both += 1
            if ok_a:
                stats.good_from_a += 1
                chosen = data_a
            elif ok_b:
                stats.good_from_b_only += 1
                chosen = data_b
            else:
                stats.bad_in_both += 1
                stats.bad_indexes.append(i)
                # Prefer whichever source actually has bytes, A first.
                if had_a:
                    chosen = data_a
                elif had_b:
                    chosen = data_b
                else:
                    chosen = data_a  # zeros (or whatever A produced)

            write_piece(out_root, meta, i, chosen, cache_out)

            now = time.monotonic()
            if i + 1 == meta.num_pieces or (i + 1) % progress_every == 0 or (now - last_print) >= 2.0:
                done = i + 1
                pct = 100.0 * done / meta.num_pieces
                elapsed = now - started
                rate = done / elapsed if elapsed > 0 else 0.0
                good = stats.good_from_a + stats.good_from_b_only
                print(
                    f"  … piece {done}/{meta.num_pieces} ({pct:.1f}%), "
                    f"{good} good so far, {stats.bad_in_both} still bad, "
                    f"{rate:.0f} pieces/s",
                    flush=True,
                )
                last_print = now
    finally:
        cache_a.close()
        cache_b.close()
        cache_out.close()

    return stats


def format_report(
    torrent_path: Path,
    root_a: Path,
    root_b: Path,
    out_root: Path,
    meta: TorrentMeta,
    stats: MergeStats,
) -> str:
    good = stats.good_from_a + stats.good_from_b_only
    pct = (100.0 * good / stats.total) if stats.total else 100.0
    lines = [
        "BitTorrent set merge report",
        "===========================",
        f"Torrent:           {torrent_path}",
        f"Torrent name:      {meta.name}",
        f"Layout:            {'multi-file' if meta.multifile else 'single-file'}",
        f"Piece length:      {meta.piece_length} bytes",
        f"Total size:        {meta.total_length} bytes",
        f"Input set A:       {root_a}",
        f"Input set B:       {root_b}",
        f"Merged output:     {out_root}",
        "",
        f"Pieces total:      {stats.total}",
        f"Good from A:       {stats.good_from_a}  (A hashed OK; used A's copy, including pieces also good in B)",
        f"Good from B only:  {stats.good_from_b_only}  (A failed, B hashed OK; used B's copy)",
        f"Good in both:      {stats.good_in_both}",
        f"Bad in both:       {stats.bad_in_both}  (still need to be downloaded)",
        f"Percent complete:  {pct:.2f}%  ({good}/{stats.total} pieces have a correct hash)",
        "",
    ]
    if stats.bad_indexes:
        lines.append("Bad piece indexes (0-based, for Force Recheck / re-download):")
        # Compact runs: 3-7, 10, 12-13
        runs: list[str] = []
        start = prev = stats.bad_indexes[0]
        for idx in stats.bad_indexes[1:]:
            if idx == prev + 1:
                prev = idx
                continue
            runs.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = idx
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        # Wrap long lists.
        line = "  "
        for run in runs:
            add = run if line == "  " else ", " + run
            if len(line) + len(add) > 100:
                lines.append(line)
                line = "  " + run
            else:
                line += add
        if line.strip():
            lines.append(line)
        lines.append("")
    else:
        lines.append("All pieces hashed correctly. Force Recheck should show 100% complete.")
        lines.append("")
    lines.append("Next step in BiglyBT:")
    lines.append("  Add this torrent (or open the existing one), set the data location to")
    lines.append(f"  {out_root}")
    lines.append("  then run Force Recheck. Only the 'bad in both' pieces will be re-downloaded.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def gather_paths(argv: list[str]) -> tuple[Path, Path, Path]:
    torrent = normalize_user_path(argv[1]) if len(argv) > 1 and argv[1].strip() else None
    folder_a = normalize_user_path(argv[2]) if len(argv) > 2 and argv[2].strip() else None
    folder_b = normalize_user_path(argv[3]) if len(argv) > 3 and argv[3].strip() else None

    if torrent is None:
        torrent = prompt_path("Path to the .torrent file")
    if folder_a is None:
        folder_a = prompt_path("Path to input folder A (first partial download set)")
    if folder_b is None:
        folder_b = prompt_path("Path to input folder B (second partial download set)")
    return torrent, folder_a, folder_b


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(__doc__.strip())
        return 0

    torrent_path, folder_a, folder_b = gather_paths(argv)

    if not torrent_path.is_file():
        print(f"Torrent file not found: {torrent_path}", file=sys.stderr)
        return 1
    if not folder_a.exists():
        print(f"Input folder A not found: {folder_a}", file=sys.stderr)
        return 1
    if not folder_b.exists():
        print(f"Input folder B not found: {folder_b}", file=sys.stderr)
        return 1

    print(f"Reading torrent: {torrent_path}")
    meta = parse_torrent(torrent_path)
    n_files = sum(1 for f in meta.files if not f.padding)
    n_pad = sum(1 for f in meta.files if f.padding)
    pad_note = f", {n_pad} padding" if n_pad else ""
    print(
        f"  name={meta.name!r}, "
        f"{'multi-file' if meta.multifile else 'single-file'}, "
        f"{n_files} file(s){pad_note}, "
        f"{meta.num_pieces} pieces × {meta.piece_length} bytes, "
        f"total {meta.total_length} bytes"
    )

    root_a = detect_content_root(folder_a, meta)
    root_b = detect_content_root(folder_b, meta)
    print(f"  set A content root: {root_a}")
    print(f"  set B content root: {root_b}")

    content_files = [f for f in meta.files if not f.padding]
    found_a = sum(1 for f in content_files if _file_present(root_a, f))
    found_b = sum(1 for f in content_files if _file_present(root_b, f))
    print(f"  files found: A {found_a}/{len(content_files)}, B {found_b}/{len(content_files)}")
    if found_a == 0 and found_b == 0:
        print(
            "  WARNING: no torrent files were found in either set. Check that "
            "you pointed at the download folder (or its parent, for a "
            "multi-file torrent). The merge will still run, but every piece "
            "will be marked bad.",
            flush=True,
        )

    # Output is named after the folder the user passed as A, and created
    # next to it. If they passed a single-file path, use that file's parent
    # as the "folder A" for naming (so we create a directory, not a file).
    beside = folder_a
    if beside.is_file():
        beside = beside.parent / beside.stem
        # Put the merged folder next to the file, named <stem>-merged.
        out_root = unique_output_dir(beside)
    else:
        out_root = unique_output_dir(folder_a)

    out_root.mkdir(parents=True, exist_ok=False)
    print(f"Merged output folder: {out_root}")

    print("Merging (checking every piece against the torrent's SHA-1 hashes)…")
    stats = merge_sets(meta, root_a, root_b, out_root)

    report = format_report(torrent_path, root_a, root_b, out_root, meta, stats)
    print()
    print(report)

    report_path = out_root.parent / f"{out_root.name}-report.txt"
    report_path.write_text(report, encoding="utf-8")
    print(f"Report saved to: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
