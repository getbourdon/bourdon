# v0.9.1 — registry staleness fix

Patch release on v0.9.0 (same-day).

## Fixed

- **Same-tick revocation invisibility** (#130): the federation registry's
  cross-process staleness check used float `st_mtime`, which quantizes
  coarsely enough (seen on Windows CI) that a `bourdon revoke` written in the
  same timestamp tick as the prior registry write was invisible to a running
  HTTP server — it kept honoring the revoked token until the next unrelated
  registry write. The staleness key is now `(st_mtime_ns, st_size)`.

If you deployed v0.9.0: upgrade. The window is narrow (writes within one
timestamp tick) but the failure mode is exactly what `bourdon revoke` exists
to prevent.

Full v0.9.x feature notes: [RELEASE_NOTES_v0.9.0.md](RELEASE_NOTES_v0.9.0.md).
