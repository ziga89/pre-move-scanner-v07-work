# Pre-Move Scanner — working repository

| Path | What it is |
|---|---|
| `pre_move_scanner_v07_work/` | The current application: **v0.8.0**. The folder name is kept from v0.7 so that existing installations and scripts keep working. All development happens here. |
| `pre_move_scanner_v06_history/` | Untouched reference copy of v0.6. Read-only; do not edit. |
| `v06_manifest.sha256` | SHA-256 of every v0.6 file, taken right after extraction. |

Start with [`pre_move_scanner_v07_work/README.md`](pre_move_scanner_v07_work/README.md). Upgrading from v0.7.x:
[`docs/MIGRATION_V080.md`](pre_move_scanner_v07_work/docs/MIGRATION_V080.md).

Verify that v0.6 is unchanged (CI does this on every push):

```bash
cd pre_move_scanner_v06_history && sha256sum -c ../v06_manifest.sha256
```

Build a clean release ZIP (source only: no `data/`, `config.json`, `.venv/`, databases or keys):

```bash
cd pre_move_scanner_v07_work && python tools/make_release.py --out dist
```

On Windows, run `run_windows.bat release`.
