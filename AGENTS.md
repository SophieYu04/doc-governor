# Doc Governor repository instructions

Doc Governor governs Markdown; it should not create Markdown merely to report that it ran.

- Use only the five core document types: `contract`, `state`, `procedure`, `evidence`, and `decision`.
- Register governed documents, dependencies, owners, TTLs, and protected paths in `.docgov/catalog.yaml`.
- Treat `.docgov/ledger.jsonl` as append-only evidence. A date is not refreshed by changing text alone.
- Every generated document must have a canonical destination or be rejected as an orphan/misclassified file.
- Model output may propose semantic findings, but it cannot directly authorize content updates or stale-status changes. A model-enabled run must call the bounded `repository_snapshot` tool exactly once, and its public trace may contain only tool and model identifiers.
- Never delete an existing document from `main`. Automatic deletion is limited to an exact duplicate newly added by the current PR, with no inbound links; human approval is required for a protected duplicate.
- Keep Supabase source/config inventory and contract documents consistent. A mismatch is a blocker, not an invitation to invent documentation.
- Changes to CLI contracts, the GitHub Action, document taxonomy, or lifecycle rules require updates to `README.md`, tests, and the relevant Catalog examples.
- Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v` before handing off changes.
