# mang-arr

Sonarr for manga: add a series once, get every chapter, keep getting new ones.

It sits on top of [Suwayomi](https://github.com/Suwayomi/Suwayomi-Server)
(which does the actual downloading from ~hundreds of sources) and
[Komga](https://komga.org) (which serves the library), and adds the layer both
lack: knowing what a series *is* and what it *should contain*.

## Why this exists

Every off-the-shelf "manga *arr" fails on a real library for the same reasons:

| Problem | What goes wrong |
|---|---|
| Sources name series differently | "Hitting Rewind with You" matches a different series on MangaDex; only the romaji title *Okuremashite Seishun* finds it everywhere |
| Fuzzy matching picks look-alikes | "Let's Play" → *Let's Play a Bunch!* (2 ch); "Gekkan Shoujo Nozaki-kun" → the *Anthology* |
| One source per series | Coverage is split: Weeb Central has 1-9, Manganato 10-18, MangaDex 19-20 |
| Series-level tracking | Gaps hide in the middle (bonus .5 chapters); "complete" means "everything the chosen source had" |
| Existing tools track volumes | A library of `Chapter N.cbz` files reads as 0% owned |

## How it works

1. **Identity comes from AniList.** A series is an AniList ID plus *all* its
   titles (romaji, English, native, synonyms), its country, status and expected
   chapter count. This is the TVDB.
2. **Every source is searched with every title.** A hit is only accepted when
   its title matches one of AniList's titles exactly (after normalising
   punctuation). Prefix and substring matches are rejected - that is how
   anthologies and promos win. Author names are compared as a second check.
3. **Chapters are tracked per chapter, not per series.** The wanted list is the
   union of chapter numbers across every accepted source, minus what is on disk.
4. **Each chapter picks its own source**, preferring healthy, unthrottled ones,
   so a series split across three sites is still complete.
5. **Suwayomi downloads**, paced per source with backoff, so one throttled site
   does not stall the rest.

## Status

Step 1 (in progress): identity, matching, per-chapter resolution, download.
CLI only.

```
python3 -m mangarr search  "Title"        # AniList candidates
python3 -m mangarr resolve "Title"        # dry run: sources, matches, per-chapter plan
python3 -m mangarr add     "Title"        # track it and download what is missing
python3 -m mangarr add     --anilist 175717
```

Planned:

- Step 2: one folder per series for Komga (hard-links; Suwayomi's own folders
  untouched so it never re-downloads).
- Step 3: background worker + nightly timer, Pushover on decisions needed.
- Step 4: web UI (search, confirm cover, Add; have/missing per series).
- Adopt the existing library (parse chapter numbers off `.cbz` names).

## Layout

```
mangarr/
  anilist.py     AniList lookup (series identity)
  suwayomi.py    Suwayomi GraphQL client
  matching.py    title normalisation + strict match rules
  resolver.py    search every source, accept matches, build per-chapter plan
  downloader.py  paced per-source download through Suwayomi
  db.py          SQLite: series, sources, chapters
  __main__.py    CLI
```

No dependencies beyond the Python 3.10 standard library.
