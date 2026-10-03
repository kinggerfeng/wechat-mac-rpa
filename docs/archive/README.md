# Archived documentation

These documents describe retired implementations and dated reviews. They are
kept as history, not as reference: nothing here describes the current system,
and the file structure they refer to has been reorganised at least twice since.

**Do not follow instructions in this directory.** Several of them describe
approaches that were tried and rejected — the WeChat database-decryption
scheme in `deprecated/` most of all, which the project deliberately does not
use.

For the current system see [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md).

| Directory | What is in it |
|---|---|
| `deprecated/` | Approaches that were implemented and then removed, chiefly the database-decryption route. Superseded by Vision OCR. |
| `reviews/` | Dated full-repository code and structure reviews. Their findings were acted on at the time; the line counts and module layout they quote are long gone. |

A 2026-10-03 cleanup removed 23 further documents that had accumulated in
`docs/`: design proposals for a codebase merge that never happened, a data
model superseded by the three tables that actually shipped, a byte-identical
duplicate of the ReAct + Self-Refine design, completed implementation plans,
and status trackers that were stale the moment they were written. The commit
that removed them lists each one.
