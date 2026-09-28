# Pre-Move Scanner — v0.7 working repository

| Path | What it is |
|---|---|
| `pre_move_scanner_v06_history/` | Untouched reference copy of v0.6 (read-only; do not edit) |
| `pre_move_scanner_v07_work/` | v0.7 development folder — all changes happen here |
| `v06_manifest.sha256` | SHA-256 of every v0.6 file, taken right after extraction |

Verify v0.6 is unchanged:

```bash
cd pre_move_scanner_v06_history && sha256sum -c ../v06_manifest.sha256
```

Current phase: audit / architecture proposal — see
`pre_move_scanner_v07_work/docs/V07_AUDIT_AND_ARCHITECTURE.md`.
