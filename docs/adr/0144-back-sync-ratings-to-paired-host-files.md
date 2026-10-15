# ADR-0144: Back Sync ratings to paired Host files

- Status: Accepted
- Date: 2026-10-15
- Extends: ADR-0005, ADR-0065, ADR-0076 and ADR-0099

## Context

Sync preserves an existing iPod Track's rating during Host metadata and media
updates. Without Host tag write-back, ratings edited on the iPod never reach the
paired Host file. The historical Back Sync claims in `sync-media-policy.md` do not
implement that behavior in the current source. External library services and
recording-copy aggregation are outside this application's responsibility.

## Decision

After verified Sync publication, the Application Layer performs rating Back Sync
for selected paired Tracks. Rating-only changes appear as selectable Updates in
Review. They require no encoder or replacement iPod media. Ambiguous, unmatched,
removed, excluded, and unsuccessfully prepared Tracks cannot authorize Host edits.

Sync Details retain optional Host and iPod rating baselines separately. New Adds
record both after the successful commit. Existing pairs without rating baselines
establish them during their next successful Sync involving selected changes; that
first observation does not write Host ratings. Reassociation to another Host path
also establishes fresh baselines.

An iPod rating writes back only when it changed from its baseline, differs from
the current Host rating, and the Host rating still equals its own baseline. The
Host wins when both changed, with a diagnostic. Zero iPod ratings never clear Host
ratings: a missing initial rating must not erase a rated Host file. This also
means deliberate clears are not supported by this initial Back Sync policy.
No clocks decide rating conflicts.

Supported Host files are MP3 and MP4 (`m4a`, `m4b`, `mp4`, `m4v`). MP3 writes POPM
using values 0, 1, 64, 128, 196 and 255 for zero through five stars. Existing POPM
owners receive the same rating while retaining their counts; files without POPM
receive an `iOpenPod` owner. MP4 writes UTF-8 decimal 0-100 to
`----:com.apple.iTunes:RATING`; it never uses the content-advisory atom `rtng`.
Other tags and audio payloads are retained. ID3v2.3/v2.4 versions are retained;
legacy ID3v2.2 changes fail rather than implicitly converting unknown frames.

Storage owns generic Host replacement. It validates the reviewed regular file and
pins its parents, copies to private staging beside the source, runs the verified
rating transform, flushes, checks the source and device again, and atomically
replaces that file. It rejects links and changed files. The transform also checks
the captured native Host rating against the scan. Host updates do not participate
in the already committed iPod transaction and cannot roll that transaction back.

Each successful Host update reports its path and old/new star values. Failed Host
updates produce a partial Sync result and retain previous rating baselines for a
fresh scan and retry. Successful helper publication refreshes Host file facts and
rating baselines. If the helper write fails after Host publication, a retry sees
the same rating on both sides and does not repeat the Host write.

Host Media Scan Cache v15 invalidates pre-v15 MP4 entries once so the newly
supported freeform rating cannot remain hidden by an older cached projection.
Other cache entries retain their existing migration and reuse behavior.

## Consequences

The particular Host source paired by Review receives rating updates. No recording
copies, upstream services, credentials, or external databases are accessed.
Listening History remains unchanged. Host files must be writable, and replacing
one path does not update other hard links to its prior inode. Physical device and
native Windows/network filesystem acceptance remain separate from virtual Storage
and authored media fixture coverage.
