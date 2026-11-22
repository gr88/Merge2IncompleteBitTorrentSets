# Merge2IncompleteBitTorrentSets

**Experimental.** This tool is an experiment for combining two incomplete
download sets of the **same** BitTorrent torrent. There is **no warranty**.
**Keep backups** of both input folders before you run it. The script never
modifies those input folders; it writes a new merged folder instead.

The script, tests, and docs were written mostly by [Grok Bot](https://grok.com)
(an AI assistant), with idea, direction, and testing by gr88. See
[Credits](#credits).

## The problem

Sometimes the same torrent is downloaded twice, at two different times (or
on two machines, disks, or client sessions). Each set has some of the
pieces, but neither set is complete. Copying one folder over the other is
risky: a partial or corrupt file can overwrite good data. This script
merges the two sets *piece by piece* using the hashes stored in the
`.torrent` file, so only verified data is kept.

## How it works

The `.torrent` file (BitTorrent v1) stores a SHA-1 hash for every piece.
For each piece the script:

1. Checks set **A**. If that copy matches the hash, it is written to the
   output.
2. Otherwise it checks set **B**. If B matches, B's copy is written.
3. If neither copy matches, the piece is still written (preferring
   whichever side has any data, A first, or zeros if both are missing) and
   **marked bad** so your BitTorrent client will re-download it after a
   recheck.

Set B is read **only when A's piece fails** its hash, so a "good in both"
count is not computed.

Pieces that span more than one file, missing files, and truncated files are
handled. Padding files (attribute `p`) are treated as zeros. They count
toward piece offsets but do **not** need to be on disk and are not written
into the output folder.

If a file is not at its normal torrent path, the script also looks in the
places BiglyBT uses for incomplete or skipped files, and prints which
fallback it used:

- `<filename>.az!` — "Rename Incomplete Files"
- `<parent>/.dnd_az!/<filename>` — "Do not download"
- any `*<filename>` in that `.dnd_az!` folder — "Do not download" with a
  possible prefix on the name

## Requirements

- **Python 3.8+**, standard library only (no pip packages).
- Tested for **Ubuntu 26.04 LTS** with the stock `python3`. It should also
  work on macOS and Windows, as long as you use a matching Python 3 and
  pass paths the way that OS expects (the script expands `~` and strips
  quotes around pasted paths).

## Usage

From a terminal, in this directory (or with a full path to the script):

```bash
python3 merge_torrent_sets.py
```

You will be prompted for:

1. The path to the `.torrent` file
2. Input folder **A** (first partial download set)
3. Input folder **B** (second partial download set)

You can also pass those paths as arguments and skip the prompts:

```bash
python3 merge_torrent_sets.py /path/to/file.torrent /path/to/setA /path/to/setB
```

Pasted paths may be quoted; surrounding quotes and whitespace are stripped,
and `~` is expanded.

For a **multi-file** torrent you may point at either the torrent's
top-level folder (usually named after `info.name` in the torrent) or the
parent of that folder. The script detects which layout you used. For a
**single-file** torrent you may pass the file itself or the directory that
contains it.

```bash
python3 merge_torrent_sets.py --help
```

prints the same overview, including the client follow-up steps below.

## Output folder and report

The merged folder is created **next to input folder A**, named like A's
folder name plus `-merged`. For example, if A is `MyShow`, the output is
`MyShow-merged` beside it. If that name already exists, a numeric suffix is
added (`MyShow-merged-2`, `MyShow-merged-3`, …) so nothing is overwritten.
The script prints the output path.

A short text report is written **next to** the merged folder (not inside
it, so the folder stays clean for your client) as
`<merged-folder-name>-report.txt`. The same summary is printed on screen:

- pieces total
- good from A (A hashed; set B was not read for these pieces)
- good from B only
- bad in both (still need to be downloaded)
- percent complete

The merged folder is pre-sized to the torrent's **full length**, so you
need that much free disk space (a full copy). Sparse files are used where
the OS allows, but plan for the full size.

Input folders are left untouched.

## Before you merge (BiglyBT)

- **Stop the torrent first:** right-click > **Stop**. This flushes
  BiglyBT's disk cache so the files on disk are up to date.
- Use **Linear** storage (BiglyBT's default). **Compact** and **Reorder**
  storage modes do not store bytes at the torrent's real file offsets, so
  this script will read those pieces as bad. If a set was downloaded in
  Compact or Reorder mode, accept that those pieces will need to be
  re-downloaded.
- Incomplete files may appear as `<filename>.az!`, and "Do not download"
  files may live under `.dnd_az!` (sometimes with a prefix). The script
  looks there automatically.

## Next steps in BiglyBT

Menu names below are BiglyBT's exact wording.

1. If the torrent is still running, right-click > **Stop**.
2. Point BiglyBT at the merged data. Either:
   - Move the old download set aside, then rename the merged folder to the
     torrent's original name in the original location, **or**
   - Right-click > **"Search For Existing Data Files..."** and choose the
     folder **containing** the merged folder (the parent of
     `Something-merged`). Avoid **"Move Data Files..."**.
3. Right-click > **"Force Re-check"**.
4. Start the torrent. BiglyBT will keep every piece that hashes correctly
   and download only the pieces that were still bad.
5. If some files were previously set to **"Do not download"**, they stay
   skipped after a recheck. Set those files back to **Normal** if you want
   them downloaded or used.

### Other clients

The same idea works in other clients, including **qBittorrent** and
**Transmission**: point the torrent at the merged folder, then use that
client's **recheck** / **verify local data** command, and start the torrent
so it fills in the remaining pieces.

## Limitations

- **v1 piece hashes only.** The script uses the classic `pieces` SHA-1
  field. Hybrid v1/v2 torrents are fine if they still include that field
  (typical of BiglyBT). **Pure v2-only** torrents (no v1 `pieces` field)
  are not supported.
- Both sets must come from the **identical torrent** (same info-hash / same
  `.torrent`). Do not merge sets from a different release, remux, or
  reissued torrent with a different info dictionary.
- The output folder is pre-sized to the torrent's **full length**, so you
  need disk space for a **full copy** (sparse files are used where the OS
  allows, but plan for the full size).
- **Compact / Reorder** storage in BiglyBT is not readable as a normal
  file layout; those pieces will be treated as bad and re-downloaded.
- A bad piece that neither set has correctly will still need to be
  downloaded from the swarm.

## Running the tests

From this repository, with the same `python3`:

```bash
python3 test_merge_torrent_sets.py
```

The tests build small single-file and multi-file torrents, create two
partial sets with missing, truncated, and corrupted pieces, and check that
the merge keeps the hash-verified data and leaves the inputs unchanged.

## Alternative approaches

You do not have to use this script. Other options:

- **File-level copy, then recheck.** Copy files that are *complete* in set
  B into set A, then in BiglyBT right-click > **"Force Re-check"**. This
  does **not** help files that are only partial in both sets — a whole-file
  copy can overwrite good pieces in A with bad ones from B, or miss B's
  good pieces inside a file that already exists in A.
- **"Search For Existing Data Files..."** in BiglyBT, if files were
  renamed or moved. Right-click the torrent and choose that command, then
  pick the folder that contains the data. (This is also the safe way to
  attach the merged folder; avoid **"Move Data Files..."**.)
- **Two clients over localhost.** Run two clients with separate
  profiles/ports — for example BiglyBT and DownLoadControlBT — add the
  same torrent in both, and on the Peers tab add the other client as a
  peer at `127.0.0.1:<port>`. Each can then feed the other the pieces it
  already has. Caveat: DownLoadControlBT's peer-ban and rarest-only rules
  may block this; disable those temporarily and re-enable them afterward.
- **qBittorrent / Transmission recheck.** Those clients (like BiglyBT) can
  recheck data you already have once the save path points at it. That is
  the right last step after any merge, including a manual one.
- **`rsync --ignore-existing`.** This can fill in files that are missing
  from A, but it will **not** repair a file that already exists in A and is
  only partially or wrongly downloaded: `--ignore-existing` skips that file
  entirely, so B's good pieces inside it never get used. A byte-level copy
  the other way can also overwrite A's good pieces with B's bad ones. Prefer
  a hash-based merge (this script or a client recheck) over a plain rsync.
- **Just re-download.** Often the simplest option. If the swarm is healthy
  and the amount still missing is small, let the client finish the job and
  skip the merge.

## Credits

The script (`merge_torrent_sets.py`), the tests
(`test_merge_torrent_sets.py`), and this documentation were written
**mostly by Grok Bot**, an AI assistant. The idea, direction, and testing
were provided by **gr88**.

## License

[MIT](LICENSE). Copyright (c) 2026 gr88.
