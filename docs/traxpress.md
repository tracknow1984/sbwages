# Traxpress

The admin navigation includes **TRAXPRESS** with Stock take, Handling and Setup.
The existing admin login, CSRF protection and staff directory apply to every route,
including downloads. Employee and customer accounts are not granted new access.

1. In Setup, save AUD excluding GST rates for bulk unloading per hour (forklift
   included), preparation/loading per hour, pallet loading, supplied pallets and
   individual straps. Zero explicitly means no separate charge. Rates start blank.
2. Stock take accepts up to 200 distinct tyre sizes and whole quantities, including
   zero. The work date is separate from the immutable server save timestamp and
   recording administrator. Each count downloads as PDF or CSV.
3. Bulk unload records a delivery-note photo, active SB Empire employee, work date
   and decimal hours. Stock pick records an employee, hours, loaded pallets,
   supplied pallets and straps. Each job has an optional reference and notes.
4. Saved jobs show itemised charges with PDF/CSV downloads. Delivery-note photos
   can be viewed/downloaded separately. The handling register exports all jobs as CSV.

Stock pick total = preparation/loading hours × hourly rate + pallets loaded ×
loading rate + pallets supplied × pallet rate + straps × strapping rate. Each line
rounds half up to cents. Set either hourly or pallet loading rate to zero where one
already includes the other. Time accepts up to two decimal places (1.5 = 90 minutes).

New SQLite tables are created additively. Records and normalised private JPEG
delivery notes reside in the existing durable database. Rates are versioned and
each job stores its itemised amounts, employee name and recorder name. Later rate
or staff-name changes do not rewrite saved history. Signed submission tokens and
unique constraints prevent repeat submissions from duplicating records. All save
timestamps use Australia/Brisbane. PDFs are job/count records, not tax invoices.

Validation: `python -m unittest discover -s tests -p 'test_traxpress.py'`.
