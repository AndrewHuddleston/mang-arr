"""What the converter makes pages for: the reader's screen, and whether it
shows colour and is e-ink. Standard library only (the Settings page lists
these without Pillow installed).

The default, "generic", is an EPUB for any reader app or device that opens
EPUB (Apple Books, Google Play Books, KOReader, Moon+ Reader, Calibre, Kobo,
PocketBook, Boox, Tolino ...): colour kept, no e-ink tone changes, pages
only shrunk when larger than a big tablet screen. The e-ink profiles are
optional tuning for one screen size: grey pages sized to the panel, with
contrast stretched for e-ink. Profiles are named by their screen, which is
shared by many makers' readers of that size.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    width: int                  # pixels, portrait
    height: int
    colour: bool                # colour pages stay colour (grey pages are always stored grey)
    eink: bool                  # grey pages get autocontrast and the gamma option
    vendor: str                 # "kobo": Kobo's quirks (rendition: prefix on spread slots, KEPUB); "" otherwise
    default_format: str
    formats: tuple[str, ...]
    quality: int = 85           # JPEG quality unless the target sets one
    group: str = ""             # the heading it is listed under


DEFAULT_PROFILE = "generic"

_ANY = ("epub", "cbz", "pdf")
_KOBO = ("kepub", "epub", "cbz", "pdf")

PROFILES = {p.key: p for p in (
    # 1600x2560: a large tablet. Phones and smaller tablets scale the page
    # down themselves, and nothing is ever enlarged.
    Profile("generic", "Generic EPUB (any reader)", 1600, 2560, colour=True, eink=False, vendor="",
            default_format="epub", formats=_ANY, quality=90, group="Any reader"),
    Profile("eink-1072", "E-ink, 6 inch (1072x1448)", 1072, 1448, colour=False, eink=True, vendor="",
            default_format="epub", formats=_ANY, group="E-ink by screen size"),
    Profile("eink-1264", "E-ink, 7 inch (1264x1680)", 1264, 1680, colour=False, eink=True, vendor="",
            default_format="epub", formats=_ANY, group="E-ink by screen size"),
    Profile("eink-1404", "E-ink, 7.8 or 10.3 inch (1404x1872)", 1404, 1872, colour=False, eink=True, vendor="",
            default_format="epub", formats=_ANY, group="E-ink by screen size"),
    Profile("eink-1440", "E-ink, 8 inch (1440x1920)", 1440, 1920, colour=False, eink=True, vendor="",
            default_format="epub", formats=_ANY, group="E-ink by screen size"),
    Profile("kobo-clara", "Kobo Clara HD / 2E / BW", 1072, 1448, colour=False, eink=True, vendor="kobo",
            default_format="kepub", formats=_KOBO, group="Kobo"),
    Profile("kobo-clara-colour", "Kobo Clara Colour", 1072, 1448, colour=True, eink=True, vendor="kobo",
            default_format="kepub", formats=_KOBO, group="Kobo"),
    Profile("kobo-libra", "Kobo Libra H2O / 2", 1264, 1680, colour=False, eink=True, vendor="kobo",
            default_format="kepub", formats=_KOBO, group="Kobo"),
    Profile("kobo-libra-colour", "Kobo Libra Colour", 1264, 1680, colour=True, eink=True, vendor="kobo",
            default_format="kepub", formats=_KOBO, group="Kobo"),
    Profile("kobo-sage", "Kobo Sage", 1440, 1920, colour=False, eink=True, vendor="kobo",
            default_format="kepub", formats=_KOBO, group="Kobo"),
    Profile("kobo-elipsa", "Kobo Elipsa / 2E", 1404, 1872, colour=False, eink=True, vendor="kobo",
            default_format="kepub", formats=_KOBO, group="Kobo"),
    Profile("remarkable-2", "reMarkable 2", 1404, 1872, colour=False, eink=True, vendor="",
            default_format="pdf", formats=_ANY, group="Other"),
)}

FORMAT_LABELS = {
    "epub": "EPUB (fixed layout)",
    "kepub": "Kobo KEPUB (.kepub.epub)",
    "cbz": "CBZ (resized for the reader)",
    "pdf": "PDF",
}

# There is no Kindle profile: Kindles do not open EPUB files themselves.
# This is the help text for the Settings page and the docs.
KINDLE_NOTE = ("Kindle: a Kindle does not open EPUB files directly. Send the EPUB with Amazon's "
               "Send to Kindle, which converts it, or read the CBZ in KOReader. MOBI and AZW3 need "
               "Amazon's kindlegen, which mang-arr cannot include.")
