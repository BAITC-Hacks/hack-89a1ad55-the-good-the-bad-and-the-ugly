# Instructions for AI collaborators

Read `HANDOFF.md` and `README.md` first. This is the HackAlem contractor-matching product, not a general chat bot. Historical step reports in `docs/` are context; current source and handoff describe the implementation.

## Current decisions

- Development and all team revisions belong on `demoVer-1`.
- User selected OpenAI **`gpt-6-sol`**, superseding the earlier Astra preference. `ASTRA_*` remains the legacy environment prefix for the primary provider; do not infer the active model from the variable name.
- User selected **email to the service team** for inquiries. SMTP is implemented; actual sender credentials and live delivery must be configured locally. Telegram is an optional prepared adapter.
- No accounts, payment or booking confirmation are required for this MVP. Inquiries are an explicit product extension, not a requirement of the official PDF.

## Preserve these invariants

- Exact selected city and category; filter order busy → budget → format → duration → language. Each rejection contributes once. Null max_hours passes the duration filter.
- Score and tie-break from README; LLM may not filter, choose or reorder finalists. At most three cards.
- Keep `data/original.csv` byte-identical. Mark all synthetic additions. Never accept real inquiries for synthetic profiles.
- Frozen E5 artifacts are tied to source hashes. If semantic inputs change, explicitly rebuild and validate artifacts; never silently bypass stale-data checks.
- Explanations have a reviewed source quote, fixed factual anchor, closed composition grammar, two sentences and at most45 words. Do not invent differentiators for sparse profiles or claim regex verifies unrestricted prose.
- First accepted explanations, including fallback templates, persist for repeatability. Clearly distinguish configured LLM from actual `explanation_source=openai`.
- Assistant suggestions change exactly one condition and are replayed through all hard filters. Apply only on user selection.
- Inquiry delivery is not booking, a confirmed quote, or proof that a real contractor exists. SMTP acceptance does not prove inbox delivery/read receipt. Unknown delivery must not trigger an automatic resend.

## Secrets and external actions

Never print, commit, upload or quote `.env`, API keys, SMTP passwords, tokens, SQLite content or contact data. Do not request secrets in chat. Keep actual configuration local / in deployment secret storage. Do not weaken TLS. An email recipient in configuration is not by itself authorization to send arbitrary messages: use the user-authorized test or product submission.

The tests isolate providers and delivery. Retain that isolation; tests must not accidentally inherit live `.env` credentials. Log only sanitized failure categories. Receipt tokens belong in an authorization header, not URLs. No public inquiry listing or personal information in validation errors.

## Work and verify

Python3.12+, FastAPI, vanilla HTML/CSS/JS; no Node build required. `start-demo.cmd` is the Windows entry point. Use project venv for `python -m pytest -q`; validate relevant backend behavior and changed user flows in a browser. Keep mobile behavior, stale-result protection and honest disabled states. See `docs/implementation-validation.md` for the actual verification boundary, not assumed success.

For public deployment, follow up with HTTPS, spam/rate controls suitable for a proxy/multiple workers, private persistent storage, backups and a team-defined contact retention policy. The local single-worker demo is not evidence those operational checks passed.

Before Git changes, inspect status, remote refs and graph. `main` previously became an unrelated root commit. Never force-push or discard teammate work. If needed, merge histories on a separate integration branch and review the PR before merging to main. Keep all source changes on demoVer-1 as requested.
