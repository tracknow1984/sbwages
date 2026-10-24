# POD finalisation and traceability

A job reaching complete means operational handover and weighing are complete. A final POD additionally requires pod_signed_at, a validated warehouse signature, authenticated signer identity and material classifications on every collected bin. Empty-delivery-only records require a signature but no material/weight.

Setup manages active material types. A POD retains its selected name when the catalogue is renamed or deactivated. Office users classify per collected bin; both classification and sign-off use the existing POD version for stale-form protection. Signature time comes from the server. There is no automatic signature or backdating.

Existing jobs receive unique, stable random POD tokens. Existing completed jobs remain unsigned until reviewed; old recipient signatures are not warehouse approval. Previously deleted records may be restored for review. Final client listings, PDFs and email/filing actions require signed approval.

The QR opens /binflow/pod/<token> and still requires staff permissions or a scoped client account. It is not a public bearer link. The client login returns to the scanned record. Signature, QR image and journey routes apply the same permissions and deletion checks.

Movement records now snapshot custody names when recorded. The journey stops at this docket's last event, and final sign-off stores a snapshot so subsequent rotations cannot alter it. Actual changes of custody determine durations; reservations, approval notes and same-location events do not reset a stay. Missing arrivals are labelled unknown. Historical names without a stored label may reflect the current location name at the time of signing.

Office users can inspect recorded history up to this collection. Drivers and clients see the relevant collection cycle only; prior-client source labels are restricted. PDF bin identifiers link to these authorised journeys.

Driver day completion and calendar pickup completion continue to describe operational collection progress; they do not claim the POD is signed.
