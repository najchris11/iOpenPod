# ADR-0143: Default F1061 to padded 55-pixel artwork

- Status: Accepted
- Date: 2026-10-14
- Amends: ADR-0121 and ADR-0124

## Context

Issue #178 requests the iTunes F1061 default: 55x55 RGB565 pixels with a
112-byte row stride, occupying 6,160 bytes. The previous catalog default was
56x56 and occupied 6,272 bytes. Existing iOpenPod libraries can contain that
older layout, including mixtures with 55-row images.

## Decision

Fresh F1061 artwork uses 55x55 with one pixel of row padding. Existing libraries
retain ADR-0124's evidence-based slot-size selection. Retained MHNI widths up to
56 remain valid with the known stride and 6,160/6,272-byte raster sizes. New
images preserve 56 columns when retained dimensions establish that width;
otherwise they use the new default. A recognized MHIF size remains the fallback
when there are no retained images. A tied raster-size vote now favors 6,160 bytes.

Reading uses retained dimensions bounded by the known stride and raster length,
so the new default does not crop an old 56-column image. Preparation and output
verification share the same F1061 recognition rule. Unknown, overlapping,
truncated or otherwise unverifiable retained ranges still reject preparation
with `preparation.cannot_encode`; size correction requires validated evidence.

## Consequences

Valid 6,272-byte libraries can receive refreshed artwork without migration or
rewriting retained thumbnail bytes. Stale MHIF declarations can still be
corrected from validated MHNI evidence. Unchanged databases and unrelated edits
remain lossless. Synthetic tests establish preparation and decoding behavior;
physical firmware validation of the new default and mixed layouts remains open.
