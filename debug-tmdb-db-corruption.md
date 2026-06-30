# [OPEN] tmdb-db-corruption

## Hypotheses
1. Object 16410 is an index and can be fixed with REINDEX.
2. Object 16410 is a table/TOAST relation with heap corruption.
3. Corruption is localized to provider-related rows/pages.
4. Broader storage corruption exists in the TMDB DB.

## Evidence Plan
- Identify relfilenode/object name for 16410
- Determine relkind/table/index/toast
- Inspect dependent indexes/toast/table
- Choose safest repair path based on evidence

## Status
Inspecting PostgreSQL catalog only. No repair executed yet.
