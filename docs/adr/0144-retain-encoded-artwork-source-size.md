# ADR-0144: Retain encoded artwork source size

- Status: Accepted
- Date: 2026-10-14
- Extends: ADR-0033 and ADR-0138

## Context

MHII `source_image_size` was populated from the decoded RGB888 buffer length.
Resizing a compressed source therefore invented a different source byte count.
Issue #178 requests the encoded image's byte count instead.

## Decision

`ArtworkPixels` carries a keyword-only, unsigned 32-bit `source_image_size`.
Host file imports use the captured file size; embedded artwork uses the image
payload size rather than its enclosing media file size. Sync and Podcast covers
retain their validated/downloaded image payload size through decoding and resizing.
The grayscale Rockbox transformation preserves that provenance.
The GUI crop after a Host import also preserves the original file's byte count.

New MHII records use the zero-time frame's source size, and MHIT `artwork_size`
continues to mirror MHII. Retained artwork group references keep the original
MHII value. Pixel-only generated, pasted or decoded-thumbnail images
have no encoded source byte count and use zero, never the RGB buffer length.
Existing MHII values are not retroactively inferred or repaired from thumbnails.

## Consequences

Compressed source bytes remain distinct from pixel-buffer memory accounting and
device thumbnail allocation sizes. The API remains compatible with existing
pixel-only callers, whose new source-size metadata is now zero. Synthetic tests
cover compressed sources, resizing, retained groups and invalid field values.
Physical firmware behavior with unknown source size remains unverified.
