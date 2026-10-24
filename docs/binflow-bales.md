# Bales and interstate storage handovers

BINFLOW → Bales tracks individually weighed, classified bales separately from bin stock. Each new bale receives a unique BA number and authenticated QR record; the print action produces a 100 × 150 mm label. Creation retries with the same form key do not create duplicate records. Corrections/voids require a reason and are limited to unallocated stored bales. Reprint the label after a correction.

## First-time setup

1. Add warehouses and material classifications through existing Setup.
2. Under Bales → Capacity & access, enter each warehouse's maximum bale count, optional kilogram limit, and carrier-agreed interstate / B-double target kilograms. No payload values are assumed. The default early warning is 90%, adjustable per warehouse.
3. Mark the actual Brisbane QLD receiving depot. This flag describes its operational role; it does not infer Brisbane from a postcode.
4. Give warehouse staff their own existing staff account, MANAGER role and working state. Optionally bind each manager to a specific warehouse. Bales routes enforce both state and warehouse boundaries; admins have national access. Assigned managers entering BINFLOW land on Bales. The direct mobile entry is `/binflow/bales`.

## Workflow and stock accounting

Create and weigh → print label → stored → scan onto truck (loading) → confirm manifest and dispatch → in transit → scan into Brisbane (received) → BlockTexx recipient signs storage handover.

On-site stock includes stored, loading and received bales. Dispatch removes stock from the sending site; each receipt scan adds it to the receiving depot. Signed storage handover removes it from SB Empire warehouse stock and records it once in the BlockTexx storage handover ledger. The ledger records entry into storage, not subsequent BlockTexx withdrawals or a live balance of their storage facility.

The entered weight is net material kilograms. Bin/POD weighing remains separate: this release does not infer which bins went into a bale or consume bin records. Bale weight and classification snapshots are preserved on the manifest and signed delivery note.

## Pickup readiness

Crossing warehouse capacity warning thresholds or the interstate load target automatically creates one `Pickup ready` action. Repeated stock changes update that action without duplicates. Starting a transfer actions the pickup; readiness is reconsidered after cancellation or dispatch. Receiving hubs do not generate onward interstate pickup requests.

This automates internal pickup readiness, not carrier booking or messaging. Staff confirm the carrier, truck/trailer and actual available payload when opening a transfer. Scans cannot exceed that payload. No transport costs or storage rates are assumed.

## Controls and records

All mutations require authentication and CSRF. Admins set limits and warehouse assignments. Source staff load/dispatch; destination staff receive/sign. Scan transactions serialize custody changes, reject foreign-warehouse and off-manifest bales, prevent allocation to two trucks, and preserve original timestamps on duplicate scans. Version checks guard settings, corrections and dispatch/sign-off. Cancelling loading returns the bales to available stock; dispatch locks the manifest.

Every manifested bale must be received before a named BlockTexx recipient supplies a drawn signature. The logged-in manager, server date/time, signature, bale IDs, material names and weights are frozen in the delivery note. Print/save as PDF or export the signed storage-entry ledger as CSV for billing preparation. No invoice or storage charge is automatically issued.

Validation covers stock totals through each custody stage, thresholds and deduplication, payload limits, duplicate form submissions and scans, concurrent allocation guards, invalid weights, incomplete receipt/signature rejection, immutable signed notes, manager state/site restrictions, private labels, and repeat-safe migrations. Mobile browser testing exercises creation through signed handover, real QR decoding, thermal label and A4 note printing, and CSV export.
