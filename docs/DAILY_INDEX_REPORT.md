# Daily Index Report

> **Roz subah, kal index hui saari files ki poori list — details ke saath — seedha LOG_CHANNEL me.**

```
plugins/index.py  (manual /index)  ─┐
                                   ├──► database.ia_filterdb.save_file()
plugins/channel.py (auto-index)  ──┘          │
                                              │ success
                                              ▼
                              database/index_log_db.py
                              (Mongo collection: index_log)
                                              │
                     roz 08:00 IST (configurable) │ startup catch-up
                                              ▼
              dreamxbotz/util/index_report.py  →  LOG_CHANNEL
```

## Kya hota hai

1. **Tracking** — `save_file()` me ek tiny hook har successfully-indexed file ko
   log karta hai (original file name, size, type, kaunsi DB me save hui,
   `/index` se aayi ya channel auto-index se, aur IST time). Yeh hook
   strictly best-effort hai — kabhi indexing ko slow ya break **nahi** karega.
2. **Daily delivery** — ek background task har roz `DAILY_INDEX_REPORT_TIME`
   (default **08:00 IST**) par **pichhle din** ki report LOG_CHANNEL me bhejta hai:
   - **Summary card:** total files, total size, manual-vs-channel split,
     type-wise breakdown (video/audio/document), DB-wise breakdown.
   - **Detail list:** har file ek line me —
     `7. File.Name.2023.1080p.mkv │ 1.45 GB │ video │ Primary │ manual │ 14:32`
   - ≤ 30 files → list inline messages me; usse zyada → `.txt` document attach
     hota hai (kitni bhi lambi list, no flooding).
3. **Kabhi miss nahi hota** — meta-document yaad rakhta hai kaunsi date ki
   report gayi. Bot agar scheduled time par down tha, to agle boot par missed
   report turant bhej di jati hai; dobara kabhi duplicate nahi jati.
4. **Housekeeping** — report ke baad `index_log` ke purane entries auto-prune
   ho jate hain (10 din se purane), isliye Mongo kabhi ful nahi hota.

## Admin command — turant test karein

Roz subah ka intezaar karne ki zaroorat nahi:

| Command | Output |
| ------- | ------ |
| `/indexreport` | Aaj ab tak index hui saari files ki report |
| `/indexreport yesterday` | Kal ki poori report |
| `/indexreport 2026-10-04` | Kisi bhi specific date ki report |

Command jis chat me di jati hai, report wahi preview hoti hai (LOG_CHANNEL
me nahi jati). Sirf `ADMINS` use kar sakte hain.

## Environment variables

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `DAILY_INDEX_REPORT` | `True` | Feature on/off. `False` karne se tracking aur delivery dono band. |
| `DAILY_INDEX_REPORT_TIME` | `08:00` | Report time, `HH:MM` format, **Asia/Kolkata** timezone. |
| `INDEX_LOG_COLLECTION` | `index_log` | Mongo collection (bot ke existing `DATABASE_URI` ke andar — koi naya DB nahi chahiye). |

## Notes

- Sirf **main bot** report bhejta hai; user-created clones (`docs/CLONE_BOTS.md`)
  nahi — warna duplicate reports lagti.
- Day-boundaries IST midnight par hoti hain (00:00–23:59 IST = "ek din").
- Duplicate files (jo pehle se DB me hain) save_file ke skip-path me jaate hain;
  woh report me nahi aati — sirf naye successful saves aate hain.
- Bot restart-safe: reports `LOG_CHANNEL` par post hone ke baad mark hoti hain.
