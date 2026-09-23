# Search results UI

The default search message uses a boxed **CINEMA HUB / SEARCH RESULTS** header,
title/files/time/requested-by metadata, diamond dividers, `🔹 [size]` file links
and a minimal *Powered by Minato* footer. Initial results, next/back pages and
quality/language/season filters share the same renderer.

Example (Telegram renders the titles as links):

```text
╔══════════════════════════╗
🎬 CINEMA HUB 🎬
◆ SEARCH RESULTS ◆
╚══════════════════════════╝

🎬 Title : marco
📂 Total Files : 400
⚡ Time Taken : 5.02s
👤 Requested By : Yuvi

◇◆◇◆◇◆◇◆◇◆◇◆◇
↓ Choose your download

🔹 [1.43 GB] Marco 2024 Kannada HDRip Goflix 720p mkv
🔹 [2.42 GB] Marco 2024 Kannada HDRip Goflix 1080p mkv

Tap a title to get the file.
◇◆◇◆◇◆◇◆◇◆◇◆◇
◆ Powered by Minato ◆

[💎 Ad-free]       [📥 Send all]
[🎞 Quality] [🌐 Language] [📺 Season]
[Page]              [1/40] [Next ›]
```

## Behaviour

- No database migration or new environment variables. Restart/redeploy the bot;
  new searches use the layout automatically.
- Download IDs, premium URLs and callback payloads are unchanged — only the
  presentation templates in `dreamxbotz/util/search_results.py` render
  differently.
- Filenames are escaped for Telegram HTML and shortened for display only (130
  characters). File contents, stored names and download IDs are not modified.
- The Time Taken line appears when the handler measured an execution time; it is
  shown on the initial search and on text-mode pagination/filter edits.
- IMDb/custom header templates remain supported. When a template supplies the
  caption, the cinema-hub header is skipped but the diamond-divider file list
  and footer stay attached, so every mode ends with the same branding. Text-list
  mode sends a text message, not a poster caption, to avoid the smaller
  photo-caption limit and allow normal text pagination. Poster replies remain
  available in button mode.
- Green Send all and blue filter/Ad-free buttons use the existing `COLOR_BUTTONS`
  setting and button-style compatibility fallback. Appearance depends on the
  Telegram client; message bubble colours and fonts are controlled by Telegram.

## Offline validation

```sh
python -m unittest discover -s tests -p test_search_results.py -v
python -m compileall -q utils.py plugins/pmfilter.py dreamxbotz/util/search_results.py
```

Tests cover the boxed header and metadata, escaping, balanced supported HTML,
unchanged deep links and callback payloads, diamond dividers, the
`🔹 [size]` row format, the Powered-by footer, button mode, long names on the
default page, IMDb fallback/custom headers, and callback offsets. They do not
contact Telegram or MongoDB. Live smoke test after deployment: search a movie
and a series, open a file, use Next/Back, select each filter, and try Send all
in both text and button modes.
