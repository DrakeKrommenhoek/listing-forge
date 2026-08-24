# data-archive

A one-time CSV export of the six `estate_*` tables from the production
database, taken 2026-08-23 before the tables were dropped from Operation
D.R.A.K.E. Historical record only — this data was never wired back into
anything and the pipeline that produced it is retired (see the root README).

| File | Rows | Notes |
|---|---|---|
| `estate_items.csv` | 5 | No completed sales recorded (buyer/final_sale_price empty on every row); `submission_owner` redacted |
| `estate_photos.csv` | 17 | `local_path` and `telegram_file_id` redacted to `<path-redacted>` / `<redacted>` |
| `estate_comps.csv` | 0 | Schema only |
| `estate_submissions.csv` | 5 | `telegram_user_id` redacted to `<redacted-telegram-id>` |
| `estate_events.csv` | 99 | Audit trail; `actor` and embedded `detail` paths redacted where they carried the same Telegram ID / VPS path |
| `estate_inquiries.csv` | 0 | The public catalogue's inquiry endpoint was never used — no buyer contact data exists anywhere in this export |

The submitter's real Telegram user ID and the production VPS's absolute
filesystem paths were redacted to placeholders after an initial commit
included them unredacted; that commit was rewritten and force-pushed. See
git history for the redaction commit if you need the exact diff shape.
