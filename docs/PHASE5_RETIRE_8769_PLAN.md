# Retirement of test service 8769 (revision 2, after Codex HOLD on ccab3e2)

Author: Claude. NOT executed anywhere except a local rehearsal on test ports.
Running it on Oracle requires (1) Codex approval of this revision and (2) the
owner's explicit authorization. Production 8765 and preview 8768 are only
observed (identity, and 8768's `/healthz`); 8767 is out of scope.

Script: `deploy/oracle/retire_service_8769.sh`
Tests:  `tests/test_phase5_retire_8769.py` (static guarantees + real rehearsal)

## Evidence that 8769 is no longer needed (unchanged)

- PID 475993, `/home/opc/kalshi-gap-repair-stage`, shell-launched, commit
  `7ba1763` (codex/opening-gap-repair), journal `repair-test-8769.db`.
- `7ba1763` is an ancestor of the running 8768 build `6c19623` and of every
  Phase 5 build: its change is already live on 8768.
- No repository file references port 8769 or its directory.
- 22.2% of the single CPU and 571 MiB on a host 1.7 GB into swap.

## Usage (on Oracle, as opc)

```bash
bash retire_service_8769.sh check                          # read-only; prints the approval token
bash retire_service_8769.sh retire --approve RETIRE-8769-PID475993-START<n>
bash retire_service_8769.sh rollback <state dir printed by retire> --approve ROLLBACK-8769
```

Exit codes: 0 ok; 2 a gate failed or usage/approval wrong (nothing changed);
3 SIGTERM sent but the process did not exit in 30 s (never force-killed;
manual decision); 4 post-check or rollback verification failed.

## Codex's six defects and their fixes

| # | Defect (ccab3e2) | Fix |
|---|---|---|
| 1 | Invalid rollback assignment `R=<directory printed in step B>` | Rollback takes the state directory as an argument; the script validates the directory (mode 700) and every state file before acting. |
| 2 | Time-of-check / time-of-stop race | After capturing state, the script re-checks listener PID **and process start time**, clients, journal users, other-process references, parent and protected identities immediately before the signal. The signal is sent through a **pidfd** opened before a final start-time check, so the PID cannot be reused between check and signal. |
| 3 | Narrow supervisor check | Searches installed system units (`/etc`, `/usr/lib`, `/lib`, `/run` systemd dirs), system/user units and timers (`systemctl list-units/list-timers`, system and `--user`), `~/.config/systemd/user`, `/etc/crontab`, `/etc/cron.*`, `/var/spool/cron`, `/etc/anacrontab`, user crontab, supervisord, `rc.local`, nginx/caddy/httpd/haproxy/cloudflared configs, `~/.cloudflared`, `~/.ssh/config`, shell profiles, `~/bin`, launch scripts in home and the service directory; every process's argv for the port, journal or directory (tunnels, proxies, readers, restart loops); and the service's parent (a living non-init parent may be a restart loop). |
| 4 | Manual protected-service comparison | `protected_snapshot` records PID, start time, cwd and argv hash for 8765 and 8768 before; it is compared automatically before the signal and after the stop (and before/after a rollback). Both must each have exactly one readable listener or the check fails. |
| 5 | Rollback not executable/verified | `rollback` relaunches with the captured argv, cwd and environment (`env -i`, `setsid nohup`), then verifies: exactly one listener, argv identical argument by argument, cwd, git commit, signals-only (no account flags), `/healthz` HEALTHY with workers healthy and `signals_only`, `/readyz` READY, and unchanged protected services. It refuses to start if the port is in use, the journal is missing or locked, or the checkout moved. |
| 6 | Did not fail closed | Every gate fails closed: missing tool, `ss` failure, timeout (`timeout` on every external command), unreadable location without passwordless sudo, grep read errors, fuser timeout, journal accessible to other users when other users' processes cannot be inspected, wrong commit/cwd/journal/flags, more than one listener, or any client/reader/reference. |

Also: exclusive `flock` on `retire.lock` and, when present, the preview-8768
deployment lock (so no deployment can run concurrently); a token that binds
the approval to the exact PID and start time seen by `check`; launch state
stored 0700/0600 and never printed; journal files are never deleted, moved,
truncated or opened for writing by the script.

## Residual races (documented, cannot be fully removed without changing the host)

1. A new client could connect in the milliseconds between the final client
   re-check and the SIGTERM. The stop would end that session. Removing this
   would require a firewall change, which is out of scope.
2. A configuration file could be written between the final supervisor search
   (in the gate pass) and the signal; the final re-check repeats the process
   references and parent check but not the full file search (it takes
   seconds). Low risk: nothing on the host is known to write these files.
3. Rollback cannot recreate open client sessions; clients reconnect.

## Local rehearsal (sandbox, test ports; never Oracle)

A real signals server in a git worktree played "8769"; an HTTP server and a
second signals server played the protected services. Rehearsal-only
overrides: readiness not required (no market feed in the sandbox; the output
says so explicitly), the sandbox init process accepted as parent, and a
stand-in `crontab` reporting an empty user crontab.

- `check` passed all gates and printed the token.
- `retire` with a wrong token: refused, service untouched.
- `retire` with the token: state captured (0700/0600), final re-check
  passed, SIGTERM via pidfd, graceful exit, port freed, journal preserved,
  protected identities unchanged, environment never printed.
- `rollback` with a wrong approval: refused.
- `rollback`: relaunched; argv identical (14 arguments), cwd, commit,
  signals-only, HEALTHY with workers healthy, protected unchanged.
- A second `rollback` while running: refused (port in use).
- Fail-closed cases: live client, another journal reader, a restart script
  referencing the port, a missing protected listener, a wrong commit, and
  missing tools each blocked the operation with nothing changed.

## Instructions for Codex's independent review

1. `git fetch origin claude/phase5-deploy-failure-repair` and check out the
   commit named in the handoff message.
2. Read `deploy/oracle/retire_service_8769.sh` in full. Verify against the
   six defects above, in particular: the pidfd stop path (`pidfd_term`), the
   final re-check block before it, `gate_supervisors`/`search_refs`
   fail-closed behaviour, `protected_snapshot` comparisons, and the rollback
   verification block.
3. Run `bash -n` and `shellcheck -S warning` on the script.
4. Run `python -m pytest -q tests/test_phase5_retire_8769.py` (needs `ss`,
   `fuser`, `git`, `setsid`; the rehearsal starts local servers on free test
   ports and removes its temporary worktree).
5. Confirm or refute: Oracle has passwordless sudo for opc (otherwise
   unreadable system cron/proxy locations make `check` fail closed, which is
   intended), and `fuser` is installed (otherwise a bounded descriptor scan
   is used, and its failure also fails closed).
6. Record PASS/HOLD with findings in a commit on your branch. Nothing should
   be run on Oracle until the owner authorizes it after your PASS.
