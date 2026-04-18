# Third-party notices

Sentry WMS bundles third-party assets under their respective licenses.
The project itself is MIT-licensed; see `LICENSE`.

## Fonts (admin panel)

Both fonts are redistributed under the SIL Open Font License, Version 1.1.
The license text travels with each font file at `admin/public/fonts/`.

- **Instrument Sans** (variable font, covering weights 400-700 and widths
  75%-125%): © 2022 The Instrument Sans Project Authors.
  Source: https://github.com/Instrument/instrument-sans
  License: `admin/public/fonts/OFL-InstrumentSans.txt`

- **JetBrains Mono** (variable font, weight axis): © 2020 The JetBrains
  Mono Project Authors.
  Source: https://github.com/JetBrains/JetBrainsMono
  License: `admin/public/fonts/OFL-JetBrainsMono.txt`

Prior to v1.4 these fonts were loaded from Google Fonts. V-110 moved
them in-tree to eliminate third-party requests from the admin panel
and to let the CSP drop `fonts.googleapis.com` / `fonts.gstatic.com`.
