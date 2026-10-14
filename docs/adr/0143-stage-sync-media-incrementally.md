# ADR-0143: Stage Sync media incrementally

- Status: Accepted
- Date: 2026-10-14
- Extends: ADR-0029, ADR-0076 and ADR-0117

Retaining every encoder workspace until the Library transaction finishes makes
Host temporary storage grow with the whole Sync batch. Linux commonly mounts
`/tmp` as tmpfs, so those retained files consume memory. Moving the entire batch
to Host disk would preserve the same unbounded preparation footprint.

Storage supplies a connection-bound Transaction Staging handle. Verified media
streams into private incoming files under one existing Operation Journal namespace;
public targets remain unchanged. A rolling worker window limits retained Host
preparations to the configured job count. Results are consumed in Review order,
preserving serial filename allocation, and their Host contexts close after staging.
Podcast downloads and encoding contexts close after staging each Episode as well.
Artwork is captured and optionally embedded before a Track's Host file is released.

The final issued Library transaction adopts that same journal. Storage validates
the handle, journal fingerprint, original preconditions and immutable stage
versions, moves selected versions into numbered transaction stages, and uses the
existing verification, protected publication, removal, commit and recovery path.
Capacity validation counts adopted files as already allocated while retaining
original-backup and restoration reserves. No Backup Snapshot preference changes
these protections.

Lyrics and Rockbox rewrites also stage one completed output at a time, including
metadata-only writes over retained media. Versions are immutable: a failed Library
preparation releases only the versions it created, so cover fallback can reread its
original incoming media. Unused versions stay private until review replacement or
transaction cleanup.

A live, unchanged, unadopted STAGING handle can discard its private namespace
without changing any public target, including an unrelated external edit. After
adoption, ordinary transaction recovery owns cleanup. A disconnect, changed journal
or unsafe cleanup entry preserves recovery evidence. After a crash, the existing
STAGING recovery path still rechecks the complete recorded observation before
restoring or cleaning; foreign journals never receive an exemption. Sync temporarily
admits only its exact owned STAGING journal through the recovery gate.

This trades early device I/O and journal updates for bounded Host media staging.
Complete transaction staging, retained originals and recovery still require device
space. The worker window bounds media-file retention rather than total process
memory or individual media-file size; database models, decoded artwork and Photo
preparation retain their separate budgets and limits.

## Crashed Host workspaces

New media workspaces carry an ownership marker and a cross-process lease. POSIX
tool launches inherit the relevant lease descriptors, including the separate
FFprobe launcher, so an encoder/probe surviving application SIGKILL protects its
workspace. Windows records child evidence before launching and clears it only after
reaping; a live, inaccessible or ambiguous child prevents reclamation. Windows
automatic cleanup is restricted to the user's private temporary tree; customized
shared temporary roots remain untouched.

Startup cleanup skips links/reparse points, foreign ownership, shared POSIX modes,
unknown markers and active leases. Pre-fix Linux directories have no lease. Their
expected private file layout is reclaimed only when same-user process observations
prove quiescence; another Python/iOpenPod/media-tool process or unreadable evidence
defers cleanup. Legacy macOS/Windows directories remain untouched without equivalent
liveness evidence. Normal context exit still cleans on every supported platform.
Cleanup never touches device recovery namespaces.

Linux tests exercise actual orphaned processes for both launchers. Native Windows
and macOS behavior still requires validation on those Hosts; conservative deferral
is preferable to deleting a directory with ambiguous ownership or liveness.
