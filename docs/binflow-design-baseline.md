# BinFlow design baseline

BinFlow uses a navy, blue and teal identity across operations, driver, client and public QR screens. The brand combines a bin with a circulation arrow. Keep SB Empire attribution beneath the wordmark.

## Shared assets

- `static/binflow-mark.svg`: original scalable mark, used in page branding and the favicon.
- `static/binflow-touch.png`: 180 px export for phone home-screen bookmarks.
- `templates/binflow/brand.html`: shared accessible wordmark; the adjacent BINFLOW text names the decorative image.
- `templates/binflow/ui_assets.html`: shared theme, icon enhancement and browser icons.
- `static/binflow-ui.css`: scoped presentation rules. Apply `bf-ui` only to BinFlow surfaces.
- `static/binflow-ui.js`: decorative action icons. Labels, destinations, form values and event targets remain unchanged. Dynamic calendar and scanner fragments receive the same treatment.
- `templates/binflow/icons.html`: native SVG icons for the focused driver journey and QR action cards.

## New screens

Use the existing operations, client or QR layout. These provide the product identity, responsive styling and navigation. A direct `base.html` screen must be rendered with the BinFlow tab or endpoint and include any page-specific assets it needs.

Add a short entry in `binflow_guides.GUIDES` for each new full-page template. Guides should explain what to check, what to do and what happens next, using only features that exist. They are collapsed by default and hidden from print. Keep driver journey instructions next to the current action instead of adding extra panels.

For an action that the icon rules cannot identify, set `data-bf-icon` to a named icon in `binflow-ui.js`. Use `data-no-icon` on controls that must remain untouched. Existing SVG/image controls, map controls, state tabs, print labels and POD document contents are not decorated automatically. Retain text labels; never make a destructive action depend on an icon alone.

Keep role and state permissions on the server. Styling and guidance must never grant access or add extra driver actions. Preserve QR quiet zones, scanner video, signed POD evidence and printable document layouts.

## Review checklist

Check desktop and phone widths, keyboard focus, dynamic dialogs, reduced-motion behaviour and print styles. Exercise a full sample driver run after changes to shared controls. Review client and public pages without an office session. Use the isolated sample data rather than changing live stock or schedules for visual testing.
