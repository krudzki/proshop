# Working in this repository

This is the Proshop Poland electronics scanner. It scans category listing pages
with a coherent Chrome-TLS session, records prices in the shared ledger, and
routes only evidence-backed price drops through the shared threshold pipeline.

## Design constraints

- RAM and graphics-card listing pages receive a reserved share of every pass.
- Every discovered electronics leaf remains in the general queue; focus only
  changes visit frequency.
- Listing MPNs come only from the exact suffix that Proshop adds after the clean
  analytics product name. Never guess a code from arbitrary title tokens.
- `*DEMO*` / `...d` products are outlet stock and use the used-condition lane.
- Proshop's displayed “Normalna cena” is not independent evidence and must not
  trigger an alert by itself.
- A 429 or challenge shell aborts the batch. Transient 502/503/504 responses are
  retried at the fetch boundary before failure.
- Dry runs persist nothing and send nothing.

Use `~/fleet-venv/bin/python -m pytest -q` on the minipc for verification.
