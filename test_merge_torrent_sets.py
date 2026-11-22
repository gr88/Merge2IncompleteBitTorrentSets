#!/usr/bin/env python3
"""Tests for merge_torrent_sets.py — creates sample torrents and partial sets."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
SCRIPT = THIS_DIR / "merge_torrent_sets.py"

sys.path.insert(0, str(THIS_DIR))
import merge_torrent_sets as m  # noqa: E402


PIECE = 16 * 1024  # 16 KiB, so pieces will span files of odd sizes


def sha1(data: bytes) -> bytes:
    try:
        return hashlib.sha1(data, usedforsecurity=False).digest()
    except TypeError:
        return hashlib.sha1(data).digest()


def bencode(obj) -> bytes:
    if isinstance(obj, bool):
        raise TypeError("bool is not valid bencode")
    if isinstance(obj, int):
        return f"i{obj}e".encode("ascii")
    if isinstance(obj, bytes):
        return str(len(obj)).encode("ascii") + b":" + obj
    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        return str(len(raw)).encode("ascii") + b":" + raw
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = []
        keys = sorted(
            obj.keys(),
            key=lambda k: k if isinstance(k, bytes) else k.encode("utf-8"),
        )
        for key in keys:
            key_b = key if isinstance(key, bytes) else key.encode("utf-8")
            items.append(bencode(key_b) + bencode(obj[key]))
        return b"d" + b"".join(items) + b"e"
    raise TypeError(type(obj))


def concat_files(files: list[tuple[str, bytes, bool]]) -> bytes:
    """files: list of (relpath, data, is_padding). Padding data should be zeros."""
    return b"".join(data for _, data, _ in files)


def piece_hashes(blob: bytes, piece_length: int) -> bytes:
    out = []
    for i in range(0, len(blob), piece_length):
        out.append(sha1(blob[i : i + piece_length]))
    return b"".join(out)


def write_torrent(
    dest: Path,
    name: str,
    files: list[tuple[str, bytes, bool]],
    piece_length: int = PIECE,
    single_file: bool = False,
) -> bytes:
    blob = concat_files(files)
    info: dict = {
        "name": name,
        "piece length": piece_length,
        "pieces": piece_hashes(blob, piece_length),
    }
    if single_file:
        assert len(files) == 1 and not files[0][2]
        info["length"] = len(files[0][1])
    else:
        flist = []
        for rel, data, padding in files:
            entry = {
                "length": len(data),
                "path": rel.split("/"),
            }
            if padding:
                entry["attr"] = "p"
            flist.append(entry)
        info["files"] = flist
    dest.write_bytes(bencode({"announce": "http://example.test/announce", "info": info}))
    return blob


def write_set(root: Path, files: list[tuple[str, bytes, bool]], skip_padding: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, data, padding in files:
        if padding and skip_padding:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def tree_fingerprint(root: Path) -> dict[str, tuple[int, bytes]]:
    """Relative path -> (size, sha1 of contents) for every file under root."""
    out = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            data = path.read_bytes()
            out[rel] = (len(data), sha1(data))
    return out


def corrupt_byte(data: bytes, index: int = 0, xor: int = 0xFF) -> bytes:
    b = bytearray(data)
    if not b:
        return data
    b[index % len(b)] ^= xor
    return bytes(b)


def apply_piece_corruption(blob: bytes, piece_length: int, piece_index: int) -> bytes:
    start = piece_index * piece_length
    if start >= len(blob):
        return blob
    # Flip a byte in the middle of the piece (or at start if the last piece is tiny).
    pos = start + min(100, max(0, (min(start + piece_length, len(blob)) - start) // 2))
    return blob[:pos] + bytes([blob[pos] ^ 0xFF]) + blob[pos + 1 :]


def split_blob_into_files(blob: bytes, files: list[tuple[str, bytes, bool]]) -> list[tuple[str, bytes, bool]]:
    out = []
    pos = 0
    for rel, original, padding in files:
        n = len(original)
        out.append((rel, blob[pos : pos + n], padding))
        pos += n
    return out


class PathHelpersTests(unittest.TestCase):
    def test_normalize_strips_quotes_whitespace_and_expands_home(self):
        home = str(Path.home())
        self.assertEqual(m.normalize_user_path('  "/tmp/foo bar"  '), Path("/tmp/foo bar"))
        self.assertEqual(m.normalize_user_path("'/tmp/x'"), Path("/tmp/x"))
        self.assertEqual(m.normalize_user_path("  ~/Downloads  ").expanduser(), Path(home) / "Downloads")
        self.assertEqual(m.normalize_user_path('~/Downloads'), Path(home) / "Downloads")


class BencodeTests(unittest.TestCase):
    def test_roundtrip_typical_values(self):
        payload = bencode(
            {
                "announce": "http://example.test/announce",
                "info": {
                    "name": "X",
                    "piece length": 16384,
                    "pieces": b"\x00" * 20,
                    "length": 10,
                },
            }
        )
        decoded = m.bdecode(payload)
        self.assertEqual(decoded["announce"], b"http://example.test/announce")
        self.assertEqual(decoded["info"]["name"], b"X")
        self.assertEqual(decoded["info"]["piece length"], 16384)
        self.assertEqual(decoded["info"]["pieces"], b"\x00" * 20)
        self.assertEqual(decoded["info"]["length"], 10)

    def test_rejects_truncated(self):
        with self.assertRaises(m.BencodeError):
            m.bdecode(b"i123")
        with self.assertRaises(m.BencodeError):
            m.bdecode(b"5:abcd")


class MergeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="torrent-merge-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_script(self, torrent: Path, a: Path, b: Path, extra_args=None) -> subprocess.CompletedProcess:
        cmd = [sys.executable, str(SCRIPT), str(torrent), str(a), str(b)]
        if extra_args:
            cmd.extend(extra_args)
        return subprocess.run(
            cmd,
            cwd=str(THIS_DIR),
            capture_output=True,
            text=True,
            check=False,
        )

    def run_script_quoted_args(self, torrent: Path, a: Path, b: Path) -> subprocess.CompletedProcess:
        # Simulate a user pasting quoted paths as argv (the script must strip them).
        cmd = [
            sys.executable,
            str(SCRIPT),
            f'"{torrent}"',
            f"'{a}'",
            f'"{b}"',
        ]
        return subprocess.run(cmd, cwd=str(THIS_DIR), capture_output=True, text=True, check=False)


def make_odd_multifile() -> list[tuple[str, bytes, bool]]:
    """Odd sizes so several 16 KiB pieces span file boundaries; include a subdir and an empty file."""
    rng_data = hashlib.sha256(b"seed-multifile").digest()
    # Stretch a deterministic stream of pseudo-random bytes.
    def block(n: int, salt: bytes) -> bytes:
        out = bytearray()
        i = 0
        while len(out) < n:
            out.extend(hashlib.sha256(salt + i.to_bytes(8, "big")).digest())
            i += 1
        return bytes(out[:n])

    files = [
        ("a.bin", block(10000, b"a"), False),
        ("b.bin", block(25000, b"b"), False),
        ("sub/c.bin", block(8000, b"c"), False),
        ("d.bin", block(32768, b"d"), False),
        ("e.bin", block(100, b"e"), False),
        ("empty.dat", b"", False),
    ]
    # Sanity: 10000+25000+8000+32768+100+0 = 75868; 75868/16384 = 4 pieces + 10332
    total = sum(len(d) for _, d, _ in files)
    assert total == 75868, total
    return files


class MultiFileMergeTests(MergeTestBase):
    def build(self):
        name = "MyShow"
        files = make_odd_multifile()
        torrent = self.tmp / "myshow.torrent"
        blob = write_torrent(torrent, name, files)

        # Layout on disk: <set>/<name>/files...
        orig_root = self.tmp / "original" / name
        write_set(orig_root, files)

        # Piece assignment (16 KiB):
        #  0: good in A only (B corrupted)
        #  1: good in B only (A corrupted)
        #  2: good in both
        #  3: bad in both
        #  4: good in A; B truncated so the last piece is incomplete
        blob_a = apply_piece_corruption(blob, PIECE, 1)
        blob_a = apply_piece_corruption(blob_a, PIECE, 3)
        blob_b = apply_piece_corruption(blob, PIECE, 0)
        blob_b = apply_piece_corruption(blob_b, PIECE, 3)

        files_a = split_blob_into_files(blob_a, files)
        files_b = split_blob_into_files(blob_b, files)

        # Truncate e.bin in B (last 100 bytes of piece 4) and also shorten d.bin
        # so B cannot have a complete last piece. Piece 4 starts at 65536;
        # d.bin occupies 43000-75767, so keep d.bin only up to offset 70000
        # in the torrent stream: 70000-43000 = 27000 bytes (truncate d.bin).
        files_b_adj = []
        for rel, data, padding in files_b:
            if rel == "e.bin":
                continue  # missing file in B
            if rel == "d.bin":
                data = data[:27000]  # truncated
            files_b_adj.append((rel, data, padding))

        set_a = self.tmp / "setA" / name
        set_b = self.tmp / "setB" / name
        write_set(set_a, files_a)
        write_set(set_b, files_b_adj)
        return {
            "name": name,
            "files": files,
            "torrent": torrent,
            "blob": blob,
            "orig": orig_root,
            "setA": set_a,
            "setB": set_b,
            "setA_parent": set_a.parent,
            "setB_parent": set_b.parent,
        }

    def _assert_successful_merge(self, proc, ctx, out_dir: Path):
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)
        self.assertTrue(out_dir.is_dir(), proc.stdout)
        report = out_dir.parent / f"{out_dir.name}-report.txt"
        self.assertTrue(report.is_file(), "report must sit next to, not inside, the output folder")
        # Output folder must contain only torrent content (no report, no padding).
        out_names = {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file()}
        expected_names = {rel for rel, data, padding in ctx["files"] if not padding}
        self.assertEqual(out_names, expected_names)

        text = report.read_text() + "\n" + proc.stdout
        self.assertIn("Pieces total:      5", text)
        self.assertIn("Good from A:       3", text)  # pieces 0, 2, 4
        self.assertIn("Good from B only:  1", text)  # piece 1
        self.assertIn("Good in both:      1", text)  # piece 2
        self.assertIn("Bad in both:       1", text)  # piece 3
        self.assertIn("80.00%", text)

        # Every good piece must be byte-identical to the original stream.
        # Reconstruct the merged stream, treating missing padding as zeros (none here).
        merged_blob = bytearray()
        for rel, original, padding in ctx["files"]:
            path = out_dir / rel
            if padding:
                merged_blob.extend(b"\x00" * len(original))
                continue
            self.assertTrue(path.is_file(), rel)
            data = path.read_bytes()
            self.assertEqual(len(data), len(original), f"{rel} should be pre-sized to full length")
            merged_blob.extend(data)
        merged_blob = bytes(merged_blob)
        self.assertEqual(len(merged_blob), len(ctx["blob"]))

        for i in range(5):
            start = i * PIECE
            end = min(start + PIECE, len(ctx["blob"]))
            orig_piece = ctx["blob"][start:end]
            got_piece = merged_blob[start:end]
            if i == 3:
                # Both bad: script prefers A's (corrupted) bytes, which must
                # differ from the original and must match set A's piece.
                self.assertNotEqual(got_piece, orig_piece)
                self.assertEqual(sha1(got_piece), sha1(apply_piece_corruption(ctx["blob"], PIECE, 3)[start:end]))
            else:
                self.assertEqual(got_piece, orig_piece, f"piece {i} should match the original")
                self.assertEqual(sha1(got_piece), sha1(orig_piece))

        # empty.dat exists and is empty
        self.assertEqual((out_dir / "empty.dat").read_bytes(), b"")

    def test_merge_pointing_at_top_level_folders(self):
        ctx = self.build()
        fp_a_before = tree_fingerprint(ctx["setA"])
        fp_b_before = tree_fingerprint(ctx["setB"])
        proc = self.run_script(ctx["torrent"], ctx["setA"], ctx["setB"])
        out_dir = ctx["setA"].parent / "MyShow-merged"
        self._assert_successful_merge(proc, ctx, out_dir)
        self.assertEqual(tree_fingerprint(ctx["setA"]), fp_a_before, "input set A must not change")
        self.assertEqual(tree_fingerprint(ctx["setB"]), fp_b_before, "input set B must not change")
        # Output is next to folder A, named after it.
        self.assertEqual(out_dir.name, "MyShow-merged")
        self.assertEqual(out_dir.parent, ctx["setA"].parent)

    def test_merge_pointing_at_parent_folders(self):
        ctx = self.build()
        proc = self.run_script(ctx["torrent"], ctx["setA_parent"], ctx["setB_parent"])
        # User passed the parent (named setA), so output is setA-merged next to setA.
        out_dir = ctx["setA_parent"].parent / "setA-merged"
        self._assert_successful_merge(proc, ctx, out_dir)
        # Content files live directly in the merged folder (not nested under MyShow).
        self.assertTrue((out_dir / "a.bin").is_file())
        self.assertFalse((out_dir / "MyShow" / "a.bin").exists())

    def test_existing_output_gets_numeric_suffix(self):
        ctx = self.build()
        first = ctx["setA"].parent / "MyShow-merged"
        first.mkdir()
        (first / "stale.txt").write_text("do not overwrite me")
        proc = self.run_script(ctx["torrent"], ctx["setA"], ctx["setB"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((ctx["setA"].parent / "MyShow-merged-2").is_dir())
        self.assertEqual((first / "stale.txt").read_text(), "do not overwrite me")
        self.assertIn("MyShow-merged-2", proc.stdout)

    def test_quoted_paths_on_argv(self):
        ctx = self.build()
        proc = self.run_script_quoted_args(ctx["torrent"], ctx["setA"], ctx["setB"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((ctx["setA"].parent / "MyShow-merged").is_dir())


class SingleFileMergeTests(MergeTestBase):
    def build(self):
        # 3 pieces + a short last piece: 16 KiB * 3 + 5000 = 54152
        data = hashlib.sha256(b"seed-single").digest() * 2000
        data = data[:54152]
        files = [("movie.bin", data, False)]
        torrent = self.tmp / "movie.torrent"
        blob = write_torrent(torrent, "movie.bin", files, single_file=True)

        blob_a = apply_piece_corruption(blob, PIECE, 1)  # piece 1 bad in A
        blob_b = apply_piece_corruption(blob, PIECE, 0)  # piece 0 bad in B
        # piece 2 good in both; piece 3 (last) missing/truncated in A, good in B

        set_a_dir = self.tmp / "A"
        set_b_dir = self.tmp / "B"
        set_a_dir.mkdir()
        set_b_dir.mkdir()
        # A has a truncated file that cuts off the last piece (starts at 49152).
        (set_a_dir / "movie.bin").write_bytes(blob_a[:50000])
        (set_b_dir / "movie.bin").write_bytes(blob_b)

        return {
            "torrent": torrent,
            "blob": blob,
            "setA": set_a_dir,
            "setB": set_b_dir,
            "fileA": set_a_dir / "movie.bin",
        }

    def test_single_file_via_directories(self):
        ctx = self.build()
        fp_a = tree_fingerprint(ctx["setA"])
        fp_b = tree_fingerprint(ctx["setB"])
        proc = self.run_script(ctx["torrent"], ctx["setA"], ctx["setB"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out_dir = self.tmp / "A-merged"
        self.assertTrue((out_dir / "movie.bin").is_file())
        merged = (out_dir / "movie.bin").read_bytes()
        self.assertEqual(len(merged), len(ctx["blob"]))

        # piece 0: A good, used A
        # piece 1: A bad, B good, used B
        # piece 2: both good, used A
        # piece 3: A truncated (bad), B good, used B
        for i in range(4):
            start = i * PIECE
            end = min(start + PIECE, len(ctx["blob"]))
            self.assertEqual(merged[start:end], ctx["blob"][start:end], f"piece {i}")

        text = proc.stdout + (out_dir.parent / "A-merged-report.txt").read_text()
        self.assertIn("Pieces total:      4", text)
        self.assertIn("Good from A:       2", text)  # 0 and 2
        self.assertIn("Good from B only:  2", text)  # 1 and 3
        self.assertIn("Good in both:      1", text)  # 2
        self.assertIn("Bad in both:       0", text)
        self.assertIn("100.00%", text)
        self.assertEqual(tree_fingerprint(ctx["setA"]), fp_a)
        self.assertEqual(tree_fingerprint(ctx["setB"]), fp_b)
        # Report is not inside the output folder
        self.assertFalse((out_dir / "A-merged-report.txt").exists())
        self.assertTrue((out_dir.parent / "A-merged-report.txt").is_file())

    def test_single_file_via_file_path_itself(self):
        ctx = self.build()
        proc = self.run_script(ctx["torrent"], ctx["fileA"], ctx["setB"] / "movie.bin")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # Output is named after the file stem and sits next to the file.
        out_dir = ctx["setA"] / "movie-merged"
        self.assertTrue(out_dir.is_dir(), proc.stdout)
        merged = (out_dir / "movie.bin").read_bytes()
        self.assertEqual(merged, ctx["blob"])


class PaddingAndV2Tests(MergeTestBase):
    def test_padding_files_are_skipped_but_counted_in_offsets(self):
        # file1 (10000) + pad (6384) = one full piece of 16384; then file2 (20000).
        def block(n, salt):
            out = bytearray()
            i = 0
            while len(out) < n:
                out.extend(hashlib.sha256(salt + i.to_bytes(8, "big")).digest())
                i += 1
            return bytes(out[:n])

        files = [
            ("doc/part1.bin", block(10000, b"p1"), False),
            (".pad/6384", b"\x00" * 6384, True),
            ("doc/part2.bin", block(20000, b"p2"), False),
        ]
        total = sum(len(d) for _, d, _ in files)
        self.assertEqual(total, 36384)
        # pieces: 2 full + 3616
        torrent = self.tmp / "padded.torrent"
        blob = write_torrent(torrent, "PaddedSet", files)

        # A: corrupt piece 1 (inside part2). B: corrupt piece 2 (last piece).
        blob_a = apply_piece_corruption(blob, PIECE, 1)
        blob_b = apply_piece_corruption(blob, PIECE, 2)
        set_a = self.tmp / "padA" / "PaddedSet"
        set_b = self.tmp / "padB" / "PaddedSet"
        write_set(set_a, split_blob_into_files(blob_a, files), skip_padding=True)
        write_set(set_b, split_blob_into_files(blob_b, files), skip_padding=True)

        proc = self.run_script(torrent, set_a, set_b)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out_dir = set_a.parent / "PaddedSet-merged"
        self.assertTrue((out_dir / "doc/part1.bin").is_file())
        self.assertTrue((out_dir / "doc/part2.bin").is_file())
        # Padding file must not be written.
        self.assertFalse((out_dir / ".pad").exists())
        self.assertFalse((out_dir / ".pad/6384").exists())

        rebuilt = (
            (out_dir / "doc/part1.bin").read_bytes()
            + b"\x00" * 6384
            + (out_dir / "doc/part2.bin").read_bytes()
        )
        # piece 0 good in both (includes implicit zero padding)
        # piece 1: A bad, B good
        # piece 2: A good, B bad
        for i in range(3):
            start = i * PIECE
            end = min(start + PIECE, len(blob))
            self.assertEqual(rebuilt[start:end], blob[start:end], f"piece {i}")

        text = proc.stdout
        self.assertIn("Good from A:       2", text)  # 0 and 2
        self.assertIn("Good from B only:  1", text)  # 1
        self.assertIn("Bad in both:       0", text)
        self.assertIn("padding", text.lower())

    def test_pure_v2_torrent_is_rejected(self):
        # Minimal v2-like torrent: meta version 2, file tree, no 'pieces'.
        info = {
            "name": "v2only",
            "piece length": 16384,
            "meta version": 2,
            "file tree": {"": {"length": 10}},
        }
        path = self.tmp / "v2.torrent"
        path.write_bytes(bencode({"announce": "http://example.test/announce", "info": info}))
        a = self.tmp / "a"
        b = self.tmp / "b"
        a.mkdir()
        b.mkdir()
        proc = self.run_script(path, a, b)
        self.assertNotEqual(proc.returncode, 0)
        msg = proc.stdout + proc.stderr
        self.assertTrue(
            "v2" in msg.lower() or "not supported" in msg.lower(),
            msg,
        )
        self.assertFalse(any(self.tmp.glob("a-merged*")), "should not create an output folder on failure")


class HelpAndPromptTests(MergeTestBase):
    def test_help(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Force Recheck", proc.stdout)
        self.assertIn("BiglyBT", proc.stdout)

    def test_prompts_when_args_missing(self):
        ctx_dir = self.tmp
        # Build a tiny single-file torrent so a prompted run can succeed.
        data = os.urandom(PIECE + 50)
        files = [("x.bin", data, False)]
        torrent = ctx_dir / "x.torrent"
        write_torrent(torrent, "x.bin", files, single_file=True)
        a = ctx_dir / "pa"
        b = ctx_dir / "pb"
        a.mkdir()
        b.mkdir()
        (a / "x.bin").write_bytes(data)
        (b / "x.bin").write_bytes(data)
        prompt_input = f'  "{torrent}"  \n{a}\n  {b}  \n'
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=prompt_input,
            capture_output=True,
            text=True,
            cwd=str(THIS_DIR),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((ctx_dir / "pa-merged" / "x.bin").is_file())
        self.assertEqual((ctx_dir / "pa-merged" / "x.bin").read_bytes(), data)


class DetectRootUnitTests(MergeTestBase):
    def test_detect_multifile_parent_vs_toplevel(self):
        files = make_odd_multifile()
        torrent = self.tmp / "t.torrent"
        write_torrent(torrent, "MyShow", files)
        meta = m.parse_torrent(torrent)
        top = self.tmp / "downloads" / "MyShow"
        write_set(top, files)
        parent = top.parent
        self.assertEqual(m.detect_content_root(top, meta), top)
        self.assertEqual(m.detect_content_root(parent, meta), top)


if __name__ == "__main__":
    unittest.main(verbosity=2)
