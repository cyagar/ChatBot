# Comment style

Write comments that describe the current system, not its history.

- No review/finding attribution ("P1-15 (external review, 2026-09-21): ...", "independent review concern #N"). If a comment needs to justify a non-obvious choice, state the reasoning itself, not who flagged it or when.
- No "used to do X, now does Y" / "this replaced the old Z" / changelog-style narration. Describe only what the code does now.
- No dates, commit references, or version-bump callouts inside comments unless the date/version is itself operationally load-bearing (rare).
- Keep comments that explain a real, current, non-obvious invariant or gotcha (e.g. "PgBouncer transaction-mode pooling drops session state between statements, so this must use the unpooled connection"). That's the bar: would a reader be confused or make a mistake without this comment, given only the code as it exists today.
- Default to no comment. Well-named code doesn't need one. When in doubt, prefer shorter.

Applies to all comments and docstrings across backend, Android, and scripts.
