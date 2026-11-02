# Oracle readiness — 2026-11-02

Production cutover is blocked. PR #1, production run #31, and the live Oracle
service must remain untouched until the owner explicitly approves production.

The development preview uses public GET-only market data, no account client,
and a separate `logs/private-preview.db` journal. Historical root databases and
credentials are preserved. Port 8765 was verified private with the Codespaces
API. This preview ends when the Codespace stops; it is not persistent hosting.

## Verified locally

- Actual public Kalshi and BTC fallback feeds are fresh; 1,440 closed candles
  loaded and the live contract rolled over at 19:00 UTC.
- No active trained registry version exists. Last settlement research had
  127/170 required official labels; scalp research had 18/90 required contracts.
  Experimental estimates do not establish an edge. Combined AI has no trained
  ensemble and must show WAIT / NO VERIFIED EDGE.
- The preserved `.oracle-pipeline-work` checkout contains Oracle systemd,
  release, rollback, preflight and SQLite backup tooling. Its 19 deployment
  tests passed locally. That checkout has existing local changes and remains
  untouched; it is not copied wholesale into this development branch.
- `verify_restore.py` verifies a supplied SHA-256 manifest, opens the backup
  read-only, creates a new mode-0600 destination exclusively, checks integrity
  and table row counts, and refuses existing destinations. It never replaces
  a live database or calls systemd. Example after independently transferring
  a backup and its `.sha256` sidecar:

  `python3 deploy/oracle/verify_restore.py /staging/backup.db /staging/rehearsal.db`

- A local online backup and restore of the historical account journal passed
  SHA-256 verification, restored integrity, and row-count comparison across
  all 10 tables. The rehearsal used `/tmp`, which is not off-host storage.

## Required evidence before production

1. Establish trusted read-only SSH access. The attempt using the existing
   default SSH identity was rejected. Do not disable host verification or print,
   copy, or commit private keys. Verify service user, unit sandboxing, private
   listener, key permissions, broker read-only scope and environment protection.
2. Configure an owner-approved encrypted off-host backup destination and retention.
   Same-disk backups or `/tmp` rehearsals do not prove off-host durability.
   Transfer consistent SQLite online backups, model versions and manifests;
   exclude credentials. Download to a separate staging location and run the
   restore verifier. Record object identity, checksums, integrity, row counts,
   recovery time and retention evidence. Never restore over the live journal.
3. Review the preserved Oracle source changes and reconcile its database/model
   paths. Test the exact release in isolation using copies of verified backups;
   confirm fresh feeds, full preload, rollover and read-only failure behavior.
4. Rehearse code rollback against the actual service configuration in isolation.
   Keep databases outside versioned code releases. Schema changes require a
   separate reviewed migration and data rollback plan. A script/unit test alone
   does not establish production rollback readiness.
5. Verify systemd restarts, boot persistence, private ingress, monitoring, disk
   headroom and an off-host restore schedule. Workspace disk was 93% full;
   production capacity must be inspected rather than inferred.
6. Present the exact tested commit, backup/restore evidence and rollback plan for
   explicit owner approval. Do not merge PR #1, approve run #31, activate a
   baseline, restart Oracle or deploy while this gate is closed.

Coinbase currently reports sequence-gap reconnects; fresh Kraken/REST fallback
keeps the preview live. Recheck transport continuity during the isolated Oracle
soak. No model validation claim is supported by this session.
