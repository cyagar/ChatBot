# Tester onboarding, privacy and offboarding

Items marked **OWNER DECISION** are not settled by the code; the project owner
must fill them in and approve this document before it is given to testers.

## Accounts

- One account per tester. Never share a login; feedback, audit history,
  revocation and incident response all depend on it.
- An administrator creates each account from `/admin` → Invitations. The invite
  is an email-bound, single-use, expiring link that the administrator sends to the
  tester; the tester sets a password on the invite page and then signs in on
  the Android app.
- Any account that was ever used with a seeded or demo password must have that
  password reset before testing (`python scripts/reset_password.py --email ...`).
- Testers get the technician role only.

## What testers are told

- The assistant answers from the manufacturer's manuals and cites the page. It
  can be wrong. Verify every instruction against the cited page and follow the
  employer's safety procedures before acting on a machine.
- Report anything incorrect with the in-app feedback buttons ("Incorrect" or
  "Missing info") and, for urgent problems, the support contact below.

## Data flow (what leaves the organization)

| Data | Where it goes | Why |
|---|---|---|
| Questions and conversation history | Backend database (Neon). With `AI_PROVIDER=anthropic`, the current question, recent turns and the retrieved manual excerpts are also sent to Anthropic to generate the answer. | Answering |
| Manual files | Google Drive (source), backend local storage (originals, page images) | Corpus and evidence display |
| Account email, password hash, audit events | Backend database | Authentication, accountability |
| Crash and usage telemetry | None is collected today | — |

**OWNER DECISION:** Anthropic account terms and data-retention setting (zero
retention or default), permitted regions, conversation and log retention period,
who can read conversations and logs, tester consent wording, and the legal
review of this notice. Until decided, use `AI_PROVIDER=local_extractive` for
testers, which sends nothing to a third party.

## Support and incidents

**OWNER DECISION:** named incident owner, rollback owner, support contact
(email or phone), hours, and the stop-testing criteria (for example: any answer
that contradicts the cited page on a safety topic).

## Offboarding

1. Administrator disables the account in `/admin` → Accounts. This invalidates
   every session token issued to it.
2. Ask the tester to uninstall the app.
3. **OWNER DECISION:** whether the tester's conversations are retained, exported
   or deleted, and by whom. There is no self-service deletion workflow yet.

## Password recovery

There is no self-service reset. An operator with backend access runs
`python scripts/reset_password.py --email <address>`, which sets a new password,
invalidates the account's existing sessions and writes an audit event. A copied
session cookie otherwise stays valid until its eight-hour expiry unless the
account is disabled.
