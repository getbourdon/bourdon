# Bourdon Hybrid Memory Starter

Portable starter kit for projects that want:

- Hybrid memory chains (`workspace` + `global`)
- Bourdon L5 export bridge
- preflight doctor checks
- short-index migration and validation
- short-index regression matrix with fixtures
- MCP smoke assertions (optionally `--isolate-federation-write-smoke` once you sync the Bourdon copy of `mcp_smoke_test.py`)
- CI wiring and JSON reports

## Copy into a new repo

Copy:

- `scripts/build_bourdon_l5.py`
- `scripts/doctor.ps1`
- `scripts/migrate_short_index.py`
- `scripts/validate_short_index.py`
- `scripts/regression_matrix.ps1`
- `scripts/mcp_smoke_test.py`
- `scripts/bootstrap-bourdon-mcp.ps1`
- `scripts/run_memory_cycle.ps1`
- `.github/workflows/memory-cycle.yml`
- `spec/L5_schema.json`
- `tests/fixtures/short-index/*`

Then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap-bourdon-mcp.ps1 -WorkspaceRoot "."
powershell -ExecutionPolicy Bypass -File scripts/run_memory_cycle.ps1 -WorkspaceRoot "." -SchemaPath ".\spec\L5_schema.json"
```
