# Smart Contractor Matching

Hackathon team repository for The Good The Bad and The Ugly.

The goal is to recommend event contractors from structured requirements while keeping the first matching layer deterministic and auditable. The current baseline accepts a client query with city, category, event format, event date, budget, guest count, and requested skills, then filters out contractors that cannot safely serve the request.

## Current baseline

- Loads contractor profiles from JSON.
- Validates the query shape with small Python dataclasses.
- Applies hard filters for city, category, format, budget, capacity, and busy dates.
- Keeps rejection reasons machine-readable so the next ranking layer can explain why a candidate was removed.
- Ranks eligible contractors deterministically by rating, experience, skill fit, and budget fit.
- Produces a full audit report with accepted candidates and rejected contractors.

## Matching flow

1. Load contractor profiles from `data/contractors.json`.
2. Remove candidates that fail non-negotiable requirements.
3. Rank eligible contractors with a transparent weighted score.
4. Return recommendations plus rejection reasons for candidates that were filtered out.

## Project layout

```text
app/contractor_matching/  matching package
data/                     sample contractor dataset
tests/                    regression tests for the matching pipeline
```

## Run checks

```bash
python -m unittest discover -s tests
```
