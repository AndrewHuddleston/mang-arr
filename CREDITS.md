# Credits

## Kindle Comic Converter (KCC)

The e-reader converter in `mangarr/convert/` follows conventions worked out
by [Kindle Comic Converter](https://github.com/ciromattia/kcc) (KCC), by
Ciro Mattia Gonano, Paweł Jastrzębski and contributors, ISC licence. No KCC
code is included; these ideas are re-implemented:

- fixed-layout EPUB 3 for comics: one page per image with the viewport at
  the image size, `rendition:spread landscape`, and the two-page slots
  (the halves of a split spread keep their sides, and the page before a
  spread closes a pair; `writers.spread_sides`);
- the `rendition:` prefix on the page-spread slots for Kobo, and KEPUB as
  the same image-only EPUB under a `.kepub.epub` name;
- the page treatment defaults: JPEG quality 85 for e-ink, autocontrast
  skipped on low-contrast pages, margin and page-number cropping, spreads
  split at the fold (right half first for manga) or turned 90 degrees
  counter-clockwise, and webtoon strips cut at blank gaps with an overlap
  when a panel is taller than the screen.
