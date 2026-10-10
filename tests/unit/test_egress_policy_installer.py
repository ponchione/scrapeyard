from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

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


_BRIDGE_NETFILTER_LINE = "BRIDGE_NETFILTER_DIR=/proc/sys/net/bridge"
_MODPROBE_SHIM = """#!/bin/sh
echo "$@" >>"$SHIM_STATE_DIR/modprobe.log"
[ -n "$MODPROBE_LOADS_INTO" ] || exit 1
mkdir -p "$MODPROBE_LOADS_INTO"
echo 1 >"$MODPROBE_LOADS_INTO/bridge-nf-call-iptables"
echo 1 >"$MODPROBE_LOADS_INTO/bridge-nf-call-ip6tables"
"""


def _installer(tmp_path: Path) -> Path:
    source = Path("security/install-docker-egress-policy.sh").read_text(encoding="utf-8")
    source = source.replace(
        '((EUID == 0)) || fail "host root privileges are required"',
        ': # root check disabled only in the copied test script',
    )
    assert _BRIDGE_NETFILTER_LINE in source
    source = source.replace(
        _BRIDGE_NETFILTER_LINE, f"BRIDGE_NETFILTER_DIR={tmp_path / 'bridge'}"
    )
    path = tmp_path / "install-docker-egress-policy.sh"
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    _bridge_netfilter(tmp_path, iptables="1", ip6tables="1")
    return path


def _bridge_netfilter(tmp_path: Path, **settings: str) -> Path:
    """Stand in for /proc/sys/net/bridge; no arguments means the module is unloaded."""
    bridge = tmp_path / "bridge"
    shutil.rmtree(bridge, ignore_errors=True)
    if settings:
        bridge.mkdir()
    for family, value in settings.items():
        (bridge / f"bridge-nf-call-{family}").write_text(f"{value}\n")
    return bridge


def _shim_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "iptables"
    shim.write_text(textwrap.dedent(_IPTABLES_SHIM).lstrip(), encoding="utf-8")
    shim.chmod(0o755)
    (bin_dir / "ip6tables").symlink_to(shim)
    modprobe = bin_dir / "modprobe"
    modprobe.write_text(_MODPROBE_SHIM, encoding="utf-8")
    modprobe.chmod(0o755)
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

    def fail_at(failure_point: int) -> tuple[Path, subprocess.CompletedProcess[str]]:
        state_dir = tmp_path / f"failure-{failure_point}"
        shutil.copytree(baseline, state_dir)
        for name in ("mutation-count", "mutations.log"):
            (state_dir / name).unlink(missing_ok=True)
        env = _environment(bin_dir, state_dir)
        env["FAIL_AT"] = str(failure_point)
        return state_dir, _run(installer, env)

    # Each failure point uses its own state directory, and every installer run
    # spawns dozens of shim processes, so run them concurrently.
    with ThreadPoolExecutor() as pool:
        outcomes = list(pool.map(fail_at, sorted(failure_points)))

    for state_dir, result in outcomes:
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


def _fresh_install(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    installer = _installer(tmp_path)
    bin_dir = _shim_bin(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return installer, state_dir, _environment(bin_dir, state_dir)


def test_install_turns_on_bridge_netfilter_that_is_off(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    bridge = _bridge_netfilter(tmp_path, iptables="0", ip6tables="0")

    result = _run(installer, env)

    assert result.returncode == 0, result.stderr
    assert (bridge / "bridge-nf-call-iptables").read_text().strip() == "1"
    assert (bridge / "bridge-nf-call-ip6tables").read_text().strip() == "1"
    assert "Enabled net.bridge.bridge-nf-call-iptables" in result.stdout
    assert not (state_dir / "modprobe.log").exists()
    _assert_old_policy_active(state_dir)


def test_install_loads_the_bridge_netfilter_module_when_it_is_absent(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    bridge = _bridge_netfilter(tmp_path)
    env["MODPROBE_LOADS_INTO"] = os.fspath(bridge)

    result = _run(installer, env)

    assert result.returncode == 0, result.stderr
    assert (state_dir / "modprobe.log").read_text().split() == ["br_netfilter"]
    _assert_old_policy_active(state_dir)


def test_install_refuses_before_firewall_mutation_without_bridge_netfilter(
    tmp_path: Path,
) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    _bridge_netfilter(tmp_path)

    result = _run(installer, env)

    assert result.returncode == 2
    assert "br_netfilter" in result.stderr
    assert "same-bridge traffic would bypass the policy" in result.stderr
    assert not (state_dir / "mutation-count").exists()


def test_install_refuses_when_bridge_netfilter_cannot_be_turned_on(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    bridge = _bridge_netfilter(tmp_path, iptables="0", ip6tables="1")
    (bridge / "bridge-nf-call-iptables").chmod(0o444)
    if os.access(bridge / "bridge-nf-call-iptables", os.W_OK):
        pytest.skip("root can write the read-only stand-in")

    result = _run(installer, env)

    assert result.returncode == 2
    assert "net.bridge.bridge-nf-call-iptables must be 1" in result.stderr
    assert not (state_dir / "mutation-count").exists()


def test_ipv4_only_install_does_not_need_ip6tables_bridge_netfilter(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    bridge = _bridge_netfilter(tmp_path, iptables="1", ip6tables="0")
    env["SCRAPEYARD_EGRESS_SOURCE_V6"] = ""

    result = _run(installer, env)

    assert result.returncode == 0, result.stderr
    assert (bridge / "bridge-nf-call-ip6tables").read_text().strip() == "0"
