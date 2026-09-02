# AGENTS.md

## Language policy

All first-party source code, identifiers, comments, docstrings, tests, logs,
documentation, and commit messages must be written in English. Localized shop
strings and existing external contracts may remain Polish.

## Runtime boundaries

This repository owns only the Proshop feed, parser, queue policy, and service
files. Shared persistence, matching, thresholds, routing, and delivery belong
to `deal-pipeline`.

Never commit runtime databases, `.env`, webhook maps, credentials, or proxy
configuration. Production data lives under `~/dane` on the minipc.

A green test is not evidence until the relevant guard has been shown able to
fail. Prove parser, focus reservation, condition separation, and refusal tests
red before delivery.

Verified against deal-pipeline ee9503b (condition-aware median).
