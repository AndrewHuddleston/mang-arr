# E-reader conversion

mang-arr can make a copy of every chapter as an e-book, for reader apps and
devices. It is off until you switch it on. The library itself is only read.

## What it makes

| Format | For |
|---|---|
| EPUB (fixed layout) | Any app or device that opens EPUB: Apple Books, Google Play Books, KOReader, Moon+ Reader, Calibre, Kobo, PocketBook, Boox, Tolino |
| Kobo KEPUB | Kobo devices (offered for the Kobo profiles) |
| CBZ, resized | Readers that open comic archives, such as KOReader |
| PDF | reMarkable and other PDF readers |

**Kindle:** a Kindle does not open EPUB files directly. Send the EPUB with
Amazon's Send to Kindle, which converts it, or read the CBZ in KOReader.
MOBI and AZW3 need Amazon's kindlegen, which mang-arr cannot include.

## Switching it on

Settings → Media Management → E-reader Conversion → **Convert Chapters**,
then Save. The first time, this adds the target **Generic EPUB (any
reader)**: colour kept, pages only made smaller when they are larger than a
big tablet screen (1600x2560), nothing enlarged.

From then on every chapter that arrives gets a copy. The chapters you
already have are converted when you press **Convert existing chapters** on
the target. The button's tooltip says how long that takes and how much disk
space it may need for your library.

## Targets

A target is what to make and where to put it: a device profile, a format and
a folder. You can have up to 8. Each has a folder of its own inside the
output folder, with one folder per series:

```
/data/converted/epub/Daytime Shooting Star/Daytime Shooting Star - Chapter 064.5 - Extra.epub
```

The series is in every file name and in every book's metadata (the EPUB 3
collection and Calibre's series fields), so apps that ignore folders still
group the chapters of a series and keep them in order.

The output folder is `MANGARR_CONVERTED` (`/data/converted` in Docker). It
must be outside the library: Komga would otherwise show every copy as a
second book. To put one target on another disk or share, mount that place
at the target's folder, for example `/data/converted/epub`.

## Getting the copies onto a device

- Copy the target's folder over USB.
- Share it with Syncthing. Add `.mangarr-*` to `.stignore`: those are
  unfinished files.
- Point Calibre-Web, KOReader's OPDS catalogue or another server at it.
- Download one chapter from its series page: every finished copy is a link
  next to the chapter's file.

## Reading direction and layout

Decided per series, and changeable on its page under **E-reader copies**.

| Series | Direction |
|---|---|
| From Japan | Right to left |
| From Korea, China or another known country | Left to right |
| Origin unknown | Right to left, like a manga |
| A webtoon | Always left to right |

Layout is decided from the pages: a chapter of tall strips is a webtoon and
is cut into screens at the gaps between panels. Set Layout to Pages or
Webtoon strip when the guess is wrong.

## Renames

When library files are renamed (Preview Rename), their copies are renamed
with them, and so is the series' folder in every target. A reader app that
tracks progress by file path may start those chapters over.

## Cost

About 3 seconds of computing for a 20 page chapter. Conversions run one at
a time in the background, in a process of their own with a memory limit and
a time limit, so a bad page cannot take mang-arr down. The queue pauses by
itself while the output folder has less free space than Minimum Free Space,
and goes on when there is room again.

## When a chapter fails

It is tried again after 10 minutes, 1 hour, 6 hours and a day, then stays
failed. A chapter that cannot be converted as it is (a damaged page) fails
at once and is not tried again until its file changes. Activity → E-reader
conversions lists the failures with their reasons and has a button to try
them again.

## Command line

```
mangarr convert            # make the copies that are due, in the foreground
mangarr convert --again    # make the existing copies again too
```

## Without Docker

`pip install 'mang-arr[convert]'` installs Pillow, which the conversion
needs. The Docker image has it.
