# v0.10.1 — federation recursion fix

Patch release on v0.10.0.

## Fixed

- **Infinite recursion on bidirectional peering** (#139, #140): when two L6
  servers each list the other as a peer (the documented two-machine setup),
  every federated query re-entered the peer's own federated path and
  ping-ponged between the servers until file-descriptor exhaustion. Measured
  on a live pair: ~38 s per query at ~129 hops deep, `[Errno 24] Too many
  open files` on the answerer, and — worst — **the federation audit log
  silently dropped writes for the duration of the storm**.
  `list_recent_work` pages also flooded with the same session re-tagged
  `peer:a:peer:b:...` once per round trip, since the depth-varying tag
  defeated the dedupe key.

  Federation is now **depth-1 by contract** (the same idiom
  `export_agents` always used): `RemoteL6Client` sends `federation_hop=1`
  on every fan-out-capable query, and the matching server tools route
  peer-originated calls straight to the local store — a federated query
  never re-fans out. Belt-and-suspenders, the merge paths no longer re-tag
  rows that already carry `peer:` provenance.

If you run peered servers: upgrade **both ends, then restart both** — a
v0.10.1 client treats a v0.10.0 peer as unreachable (graceful local-only
degradation, no storm) until the peer also speaks `federation_hop`. After
the fix, the same live pair answers `list_recent_work` in 0.10 s with
clean single-tag provenance.

Full v0.10.x feature notes: [RELEASE_NOTES_v0.10.0.md](RELEASE_NOTES_v0.10.0.md).
