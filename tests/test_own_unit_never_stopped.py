"""A systemd unit that contains the caller is never treated as the target's manager.

CI incident 2026-11-20: on a GitHub runner every process lives inside the
runner's own .service unit. The retirement rehearsal therefore saw its stand-in
"old bot" as systemd-managed by that unit and ran `systemctl disable --now` on
it, which stopped the runner itself ("The runner has received a shutdown
signal") partway through the suite. The same would happen to crond.service if
the scripts ever ran from cron. The restart capture likewise refused every
stand-in as "systemd-managed".
"""
import re
from pathlib import Path

import pytest

from src.signals_display import production_restart as pr_

ROOT = Path(__file__).resolve().parents[1]
RUNNER = "0::/system.slice/actions.runner.repo.runner-1.service\n"
CROND = "0::/system.slice/crond.service\n"
SESSION = "0::/user.slice/user-1000.slice/session-7561.scope\n"
OTHER = "0::/system.slice/kalshi-signals.service\n"


@pytest.fixture
def cgroups(monkeypatch):
    table = {}
    real = Path.read_text

    def fake(self, *a, **k):
        key = str(self).replace("\\", "/")
        if key in table:
            value = table[key]
            if isinstance(value, Exception):
                raise value
            return value
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", fake)
    return table


@pytest.mark.parametrize("own,target,expected", [
    (RUNNER, RUNNER, False),     # stand-in inside the CI runner's own unit
    (CROND, CROND, False),       # cron job and its child share crond.service
    (SESSION, OTHER, True),      # a real service started by systemd: refuse
    (CROND, OTHER, True),
    (SESSION, SESSION, False),   # manual launch from an SSH session (live 8768 today)
    (RUNNER, OTHER + RUNNER, True),
])
def test_only_a_foreign_service_unit_counts_as_systemd_managed(cgroups, own, target, expected):
    cgroups["/proc/self/cgroup"] = own
    cgroups["/proc/4242/cgroup"] = target
    assert pr_._cgroup_is_systemd(4242) is expected


def test_unreadable_target_cgroup_still_refuses(cgroups):
    cgroups["/proc/self/cgroup"] = SESSION
    cgroups["/proc/4242/cgroup"] = OSError("gone")
    assert pr_._cgroup_is_systemd(4242) is True


def test_retire_script_never_disables_its_own_unit():
    code = (ROOT / "deploy/oracle/retire_old_bots.sh").read_text(encoding="utf-8")
    assert re.search(r'SELF_UNIT=\$\(grep -oE .*"/proc/\$\$/cgroup"', code)
    guard = code.index('$unit == "$SELF_UNIT"')
    assert guard < code.index("$SYSTEMCTL --user disable --now")
    assert 'unit=""' in code[guard:guard + 600]
