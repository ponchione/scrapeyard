from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path


_IPTABLES_SHIM = r"""
#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["SHIM_STATE_DIR"])
state_path = root / f"{tool}.json"
state = (
    json.loads(state_path.read_text())
    if state_path.exists()
    else {"chains": {"DOCKER-USER": []}, "jumps": []}
)
mutations = {"-N", "-A", "-I", "-D", "-F", "-X"}
if args and args[0] in mutations:
    counter_path = root / "mutation-count"
    count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
    counter_path.write_text(str(count))
    with (root / "mutations.log").open("a") as log:
        log.write(f"{count}\t{tool}\t{' '.join(args)}\n")
    if count == int(os.environ.get("FAIL_AT", "0")):
        raise SystemExit(91)

def save():
    state_path.write_text(json.dumps(state, sort_keys=True))

if args[:2] == ["-S", "DOCKER-USER"]:
    for chain in state["jumps"]:
        print(f"-A DOCKER-USER -i br-test -j {chain}")
    raise SystemExit(0)
if args and args[0] == "-S":
    raise SystemExit(0 if args[1] in state["chains"] else 1)
if args[:2] == ["-C", "DOCKER-USER"]:
    chain = args[args.index("-j") + 1]
    raise SystemExit(0 if chain in state["jumps"] else 1)
if args and args[0] == "-N":
    chain = args[1]
    if chain in state["chains"]:
        raise SystemExit(1)
    state["chains"][chain] = []
elif args and args[0] == "-A":
    chain = args[1]
    if chain not in state["chains"]:
        raise SystemExit(1)
    state["chains"][chain].append(" ".join(args[2:]))
elif args[:2] == ["-I", "DOCKER-USER"]:
    chain = args[args.index("-j") + 1]
    if chain not in state["chains"]:
        raise SystemExit(1)
    state["jumps"].insert(0, chain)
elif args[:2] == ["-D", "DOCKER-USER"]:
    chain = args[args.index("-j") + 1]
    if chain not in state["jumps"]:
        raise SystemExit(1)
    state["jumps"].remove(chain)
elif args and args[0] == "-F":
    if args[1] not in state["chains"]:
        raise SystemExit(1)
    state["chains"][args[1]] = []
elif args and args[0] == "-X":
    chain = args[1]
    if chain not in state["chains"] or chain in state["jumps"]:
        raise SystemExit(1)
    del state["chains"][chain]
else:
    raise SystemExit(f"unsupported shim command: {tool} {' '.join(args)}")
save()
"""


def _installer(tmp_path: Path) -> Path:
    source = Path("security/install-docker-egress-policy.sh").read_text(encoding="utf-8")
    source = source.replace(
        '((EUID == 0)) || fail "host root privileges are required"',
        ': # root check disabled only in the copied test script',
    )
    path = tmp_path / "install-docker-egress-policy.sh"
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _shim_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "iptables"
    shim.write_text(textwrap.dedent(_IPTABLES_SHIM).lstrip(), encoding="utf-8")
    shim.chmod(0o755)
    (bin_dir / "ip6tables").symlink_to(shim)
    return bin_dir


def _environment(bin_dir: Path, state_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    # pytest-cov enables coverage in every Python subprocess. The iptables
    # shims intentionally spawn once per host-tool command, so instrumenting
    # them makes the rollback matrix depend on unrelated suite load.
    for name in tuple(environment):
        if name.startswith("COV_CORE_") or name == "COVERAGE_PROCESS_START":
            environment.pop(name)
    environment.update(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SHIM_STATE_DIR": os.fspath(state_dir),
            "SCRAPEYARD_EGRESS_INTERFACE": "br-test",
            "SCRAPEYARD_EGRESS_POLICY_ID": "test",
            "SCRAPEYARD_EGRESS_SOURCE_V6": "2001:db8::250",
            "SCRAPEYARD_EGRESS_POLICY_RENDERER": os.fspath(
                Path("security/render-egress-policy.py").resolve()
            ),
        }
    )
    return environment


def _run(installer: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.fspath(installer), "install"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_old_policy_active(state_dir: Path) -> None:
    for tool, source in (
        ("iptables", "172.30.0.250"),
        ("ip6tables", "2001:db8::250"),
    ):
        state = json.loads((state_dir / f"{tool}.json").read_text())
        assert "SY-EGRESS-test-A" in state["jumps"]
        rules = state["chains"]["SY-EGRESS-test-A"]
        established = next(
            index
            for index, rule in enumerate(rules)
            if "--ctstate ESTABLISHED,RELATED -j ACCEPT" in rule
        )
        rejected = next(
            index for index, rule in enumerate(rules) if "-j REJECT" in rule
        )
        assert rules[established].startswith(f"-s {source} -m conntrack ")
        assert established < rejected


def test_policy_replacement_restores_prior_deny_path_at_every_mutation_stage(
    tmp_path: Path,
) -> None:
    installer = _installer(tmp_path)
    bin_dir = _shim_bin(tmp_path)
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    baseline_env = _environment(bin_dir, baseline)
    assert _run(installer, baseline_env).returncode == 0
    _assert_old_policy_active(baseline)

    successful = tmp_path / "successful"
    shutil.copytree(baseline, successful)
    for name in ("mutation-count", "mutations.log"):
        (successful / name).unlink(missing_ok=True)
    assert _run(installer, _environment(bin_dir, successful)).returncode == 0
    log_rows = (successful / "mutations.log").read_text().splitlines()

    def mutation_index(tool: str, fragment: str, *, last: bool = False) -> int:
        matches = [
            int(row.split("\t", 1)[0])
            for row in log_rows
            if f"\t{tool}\t" in row and fragment in row
        ]
        return matches[-1] if last else matches[0]

    failure_points = {
        mutation_index("iptables", "-N SY-EGRESS-test-B"),
        mutation_index("iptables", "-A SY-EGRESS-test-B"),
        mutation_index("iptables", "-A SY-EGRESS-test-B", last=True),
        mutation_index("iptables", "-I DOCKER-USER"),
        mutation_index("iptables", "-D DOCKER-USER"),
        mutation_index("ip6tables", "-N SY-EGRESS-test-B"),
        mutation_index("ip6tables", "-A SY-EGRESS-test-B"),
        mutation_index("ip6tables", "-A SY-EGRESS-test-B", last=True),
        mutation_index("ip6tables", "-I DOCKER-USER"),
        mutation_index("ip6tables", "-D DOCKER-USER"),
    }

    for failure_point in sorted(failure_points):
        state_dir = tmp_path / f"failure-{failure_point}"
        shutil.copytree(baseline, state_dir)
        for name in ("mutation-count", "mutations.log"):
            (state_dir / name).unlink(missing_ok=True)
        env = _environment(bin_dir, state_dir)
        env["FAIL_AT"] = str(failure_point)

        result = _run(installer, env)

        assert result.returncode != 0
        assert "prior policy restored" in result.stderr
        _assert_old_policy_active(state_dir)


def test_wrong_address_family_fails_before_firewall_mutation(tmp_path: Path) -> None:
    installer = _installer(tmp_path)
    bin_dir = _shim_bin(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _environment(bin_dir, state_dir)
    env["SCRAPEYARD_EGRESS_SOURCE"] = "2001:db8::250"

    result = _run(installer, env)

    assert result.returncode != 0
    assert not (state_dir / "mutation-count").exists()
