# Search results UI

The default search message now uses a compact title/stats header, numbered file
links and a separate size/quality line. Initial results, next/back pages and
quality/language/season filters share the same file-list renderer.

Example (Telegram renders the titles as links):

```text
🎬 marco
━━━━━━━━━━━━━━━━━━
📁 400 files found · ⚡ 5.02s
👤 Yuvi
Minato
━━━━━━━━━━━━━━━━━━
↓ Choose your download

01. Marco 2024 Kannada HDRip Goflix 720p mkv
     1.43 GB · 720P

02. Marco 2024 Kannada HDRip Goflix 1080p mkv
     2.42 GB · 1080P

Tap a title to get the file.

[💎 Ad-free]       [📥 Send all]
[🎞 Quality] [🌐 Language] [📺 Season]
[Page]              [1/40] [Next ›]
```

## Behaviour

- No database migration or new environment variables. Restart/redeploy the bot;
  new searches use the layout automatically.
- Download IDs, premium URLs and callback payloads are unchanged.
- Filenames are escaped for Telegram HTML and shortened for display only (130
  characters). File contents, stored names and download IDs are not modified.
- IMDb/custom header templates remain supported. Text-list mode sends a text
  message, not a poster caption, to avoid the smaller photo-caption limit and
  allow normal text pagination. Poster replies remain available in button mode.
- Green Send all and blue filter/Ad-free buttons use the existing `COLOR_BUTTONS`
  setting and button-style compatibility fallback. Appearance depends on the
  Telegram client; message bubble colours and fonts are controlled by Telegram.
- Numbering consistently uses the zero-based database offset (page two begins
  at 11 for the default ten-result page).

## Offline validation

```sh
python -m unittest discover -s tests -p test_search_results.py -v
python -m compileall -q utils.py plugins/pmfilter.py dreamxbotz/util/search_results.py
```

Tests cover escaping, balanced supported HTML, unchanged deep links, numbering,
button mode, long names on the default page, IMDb fallback/custom headers, and
callback offsets. They do not contact Telegram or MongoDB. Live smoke test after
deployment: search a movie and a series, open a file, use Next/Back, select each
filter, and try Send all in both text and button modes.
