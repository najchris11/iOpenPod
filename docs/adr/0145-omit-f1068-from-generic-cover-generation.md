# ADR-0145: Omit F1068 from generic cover generation

- Status: Accepted
- Date: 2026-10-14
- Amends: ADR-0139's unchanged cover-format policy

## Context

Issue #178 reports that iTunes omits F1068 for audio and associates one F1068
record with the sole video Track in a Classic 6.5 library. F1068 is a documented
128x128 RGB565LE representation on Classic 6/6.5/7 and Nano 3/4, but historical
writers disagree about when to generate it. libgpod lists it among cover formats;
rePear omits it. foo_dop uses device-supplied media associations to filter formats.
These sources do not establish a universal firmware rule for F1068.

## Decision

Remove F1068 from these profiles' generic cover formats as explicitly requested.
The shared pixel descriptor remains in the catalog. Media-specific generation
needs a separately reviewed policy: shared artwork identities and output
verification make a per-Track filter more than a catalog-only change.

## Consequences

New ordinary and timed artwork no longer generates F1068 for audio or video.
Video poster completeness remains a limitation of this conservative omission.
Existing records and thumbnails remain untouched by unrelated edits. Refreshing
a zero-time cover that already contains F1068 still rejects preparation with
`preparation.cannot_encode` rather than silently retaining stale pixels. This
branch does not migrate such covers or delete their files. Other known
representations remain available for ordinary previewing; F1068 is no longer
included in application read formats. Tests cover generation on all five
generations, lossless retention and the explicit refresh refusal.

## Evidence

- [Issue readback](https://github.com/TheRealSavi/iOpenPod/issues/178#issuecomment-6113260630)
- [libgpod format tables](https://github.com/gtkpod/libgpod/blob/7982c5554f78dde47fd006afbeff659201d6db3d/src/itdb_device.c#L467-L621)
- [rePear format tables](https://github.com/worstje/repear/blob/7ca8bbd704ee247e15cf9bccdb85a94b1d53d768/iTunesDB.py#L451-L470)
- [foo_dop format eligibility](https://github.com/reupen/ipod_manager/blob/08e0657b5ee09bd05cdb60273e1a139205d4d3f6/foo_dop/photodb.cpp#L780-L822)
