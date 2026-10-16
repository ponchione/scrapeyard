from __future__ import annotations

import fcntl
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
    else {"chains": {"DOCKER-USER": []}, "jumps": [], "interfaces": {}}
)
interfaces = state.setdefault("interfaces", {})
host_jumps = state.setdefault("input", [])
interface = args[args.index("-i") + 1] if "-i" in args else None
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

if len(args) > 1 and args[1] == "INPUT":
    # The host's INPUT chain: a list of [interface, chain] jumps.
    if args[0] == "-S":
        for jump_interface, chain in host_jumps:
            print(f"-A INPUT -i {jump_interface} -j {chain}")
        raise SystemExit(0)
    jump = [interface, args[args.index("-j") + 1]]
    if args[0] == "-C":
        raise SystemExit(0 if jump in host_jumps else 1)
    if args[0] == "-I":
        if jump[1] not in state["chains"]:
            raise SystemExit(1)
        host_jumps.insert(0, jump)
    elif args[0] == "-D":
        if jump not in host_jumps:
            raise SystemExit(1)
        host_jumps.remove(jump)
    else:
        raise SystemExit(f"unsupported shim command: {tool} {' '.join(args)}")
    state_path.write_text(json.dumps(state, sort_keys=True))
    raise SystemExit(0)
if args[:2] == ["-S", "DOCKER-USER"]:
    for chain in state["jumps"]:
        print(f"-A DOCKER-USER -i {interfaces.get(chain, 'br-test')} -j {chain}")
    raise SystemExit(0)
if args[:2] == ["-C", "FORWARD"]:
    # Docker's own jump from FORWARD; a flush of the host's rules removes it.
    raise SystemExit(0 if state.get("forward", True) else 1)
if args and args[0] == "-S":
    if args[1] not in state["chains"]:
        raise SystemExit(1)
    # As iptables lists a chain: addresses in their canonical spelling with a
    # prefix length, and a REJECT with its default answer spelled out.
    import ipaddress
    print(f"-N {args[1]}")
    for rule in state["chains"][args[1]]:
        words = rule.split()
        for index, word in enumerate(words[:-1]):
            if word in ("-s", "-d"):
                words[index + 1] = ipaddress.ip_network(words[index + 1], strict=False).compressed
        if words[-2:] == ["-j", "REJECT"]:
            words += ["--reject-with", "icmp6-port-unreachable" if tool == "ip6tables" else "icmp-port-unreachable"]
        print(f"-A {args[1]} {' '.join(words)}")
    raise SystemExit(0)
if args[:2] == ["-C", "DOCKER-USER"]:
    chain = args[args.index("-j") + 1]
    present = chain in state["jumps"] and interfaces.get(chain, "br-test") == interface
    raise SystemExit(0 if present else 1)
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
    interfaces[chain] = interface
elif args[:2] == ["-D", "DOCKER-USER"]:
    chain = args[args.index("-j") + 1]
    if chain not in state["jumps"] or interfaces.get(chain, "br-test") != interface:
        raise SystemExit(1)
    state["jumps"].remove(chain)
    interfaces.pop(chain, None)
elif args and args[0] == "-F":
    if args[1] not in state["chains"]:
        raise SystemExit(1)
    state["chains"][args[1]] = []
elif args and args[0] == "-X":
    chain = args[1]
    if chain not in state["chains"] or chain in state["jumps"]:
        raise SystemExit(1)
    if any(chain == jumped for _interface, jumped in host_jumps):
        raise SystemExit(1)
    del state["chains"][chain]
else:
    raise SystemExit(f"unsupported shim command: {tool} {' '.join(args)}")
save()
if args[0] in mutations and count == int(os.environ.get("SIGNAL_AT", "0")):
    # A stop that lands just after this command, as systemd's would.
    import signal
    os.kill(os.getppid(), getattr(signal, os.environ["SIGNAL_NAME"]))
"""


_BRIDGE_NETFILTER_LINE = "BRIDGE_NETFILTER_DIR=/proc/sys/net/bridge"
_NETWORK_INTERFACES_LINE = "NETWORK_INTERFACES_DIR=/sys/class/net"
_LOCK_LINE = "LOCK_DIR=/run/scrapeyard"
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
    for line, replacement in (
        (_NETWORK_INTERFACES_LINE, f"NETWORK_INTERFACES_DIR={tmp_path / 'net'}"),
        (_LOCK_LINE, f"LOCK_DIR={tmp_path / 'lock'}"),
    ):
        assert line in source
        source = source.replace(line, replacement)
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


def _run(
    installer: Path, env: dict[str, str], action: str = "install"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.fspath(installer), action],
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


@pytest.mark.parametrize(
    ("tool", "signal_name", "status"),
    [("iptables", "SIGTERM", 143), ("ip6tables", "SIGINT", 130), ("iptables", "SIGHUP", 129)],
)
def test_a_stop_just_after_the_switch_insert_restores_one_jump_and_fails(
    tmp_path: Path, tool: str, signal_name: str, status: int
) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    assert _run(installer, env).returncode == 0
    (state_dir / "mutations.log").unlink()
    (state_dir / "mutation-count").unlink()
    baseline = tmp_path / "baseline"
    shutil.copytree(state_dir, baseline)
    assert _run(installer, env).returncode == 0
    insert = next(
        int(row.split("\t", 1)[0])
        for row in (state_dir / "mutations.log").read_text().splitlines()
        if f"\t{tool}\t-I DOCKER-USER" in row
    )
    shutil.rmtree(state_dir)
    shutil.copytree(baseline, state_dir)

    result = _run(installer, {**env, "SIGNAL_AT": str(insert), "SIGNAL_NAME": signal_name})

    # An interrupted switch never reports success.
    assert result.returncode == status
    assert "prior policy restored" in result.stderr
    _assert_old_policy_active(state_dir)
    for family in ("iptables", "ip6tables"):
        assert json.loads((state_dir / f"{family}.json").read_text())["jumps"] == [
            "SY-EGRESS-test-A"
        ]
    assert _run(installer, env).returncode == 0


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


def _jumps(state_dir: Path, tool: str = "iptables") -> dict[str, str]:
    state = json.loads((state_dir / f"{tool}.json").read_text())
    return {chain: state["interfaces"][chain] for chain in state["jumps"]}


def test_install_replaces_the_jump_left_for_a_removed_deployment_bridge(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    interfaces = tmp_path / "net"
    (interfaces / "br-old").mkdir(parents=True)
    env["SCRAPEYARD_EGRESS_INTERFACE"] = "br-old"
    assert _run(installer, env).returncode == 0
    assert _jumps(state_dir) == {"SY-EGRESS-test-A": "br-old"}

    # `docker compose down` and `up` give the deployment network a new bridge.
    (interfaces / "br-old").rmdir()
    (interfaces / "br-new").mkdir()
    env["SCRAPEYARD_EGRESS_INTERFACE"] = "br-new"
    result = _run(installer, env)

    assert result.returncode == 0, result.stderr
    assert "br-old" in result.stdout
    for tool in ("iptables", "ip6tables"):
        (chain, interface), = _jumps(state_dir, tool).items()
        assert interface == "br-new"
        assert chain.startswith("SY-EGRESS-test-")


@pytest.mark.parametrize("action", ["install", "remove"])
def test_a_policy_chain_another_live_bridge_still_uses_is_left_as_it_is(
    tmp_path: Path, action: str
) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    interfaces = tmp_path / "net"
    (interfaces / "br-old").mkdir(parents=True)
    (interfaces / "br-new").mkdir()
    env["SCRAPEYARD_EGRESS_INTERFACE"] = "br-old"
    assert _run(installer, env).returncode == 0
    before = {
        tool: (state_dir / f"{tool}.json").read_text() for tool in ("iptables", "ip6tables")
    }
    assert "-j REJECT" in before["iptables"]

    env["SCRAPEYARD_EGRESS_INTERFACE"] = "br-new"
    result = subprocess.run(
        [os.fspath(installer), action], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 2
    expected = (
        "has a jump on another interface" if action == "install" else "another interface still jumps"
    )
    assert expected in result.stderr
    # Its jump stays, and so do the rules the jump leads to.
    for tool, state in before.items():
        assert (state_dir / f"{tool}.json").read_text() == state


def test_policy_changes_wait_for_the_lock_before_touching_any_chain(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    source = installer.read_text(encoding="utf-8")
    assert source.count("flock --wait 120 8") == 1
    installer.write_text(source.replace("flock --wait 120 8", "flock --nonblock 8"))
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    with (lock_dir / "egress-policy.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = _run(installer, env)

    assert result.returncode == 2
    assert "another policy installation" in result.stderr
    assert not (state_dir / "mutation-count").exists()
    assert _run(installer, env).returncode == 0


def test_remove_also_clears_the_jump_left_for_a_removed_bridge(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    interfaces = tmp_path / "net"
    (interfaces / "br-old").mkdir(parents=True)
    env["SCRAPEYARD_EGRESS_INTERFACE"] = "br-old"
    assert _run(installer, env).returncode == 0

    (interfaces / "br-old").rmdir()
    env["SCRAPEYARD_EGRESS_INTERFACE"] = "br-new"

    def remove() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [os.fspath(installer), "remove"], env=env, capture_output=True, text=True, check=False
        )

    # Without the named bridge in view, a missing interface proves nothing,
    # so the jump and its rules are left and the command says so.
    refused = remove()
    assert refused.returncode == 2
    assert "another interface still jumps" in refused.stderr
    assert _jumps(state_dir) == {"SY-EGRESS-test-A": "br-old"}
    state = json.loads((state_dir / "iptables.json").read_text())
    assert any("-j REJECT" in rule for rule in state["chains"]["SY-EGRESS-test-A"])

    (interfaces / "br-new").mkdir()
    result = remove()

    assert result.returncode == 0, result.stderr
    for tool in ("iptables", "ip6tables"):
        state = json.loads((state_dir / f"{tool}.json").read_text())
        assert state["jumps"] == []
        assert set(state["chains"]) == {"DOCKER-USER"}


def _host_rules(state_dir: Path, tool: str = "iptables") -> tuple[list[list[str]], dict[str, list[str]]]:
    state = json.loads((state_dir / f"{tool}.json").read_text())
    chains = {name: rules for name, rules in state["chains"].items() if name.startswith("SY-HOST-")}
    return state["input"], chains


def test_install_refuses_the_application_s_connections_to_the_host_itself(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)

    result = _run(installer, env)

    assert result.returncode == 0, result.stderr
    for tool, source in (("iptables", "172.30.0.250"), ("ip6tables", "2001:db8::250")):
        jumps, chains = _host_rules(state_dir, tool)
        # Input to the host from the deployment bridge passes the host chain.
        assert jumps == [["br-test", "SY-HOST-test-A"]]
        # Replies to connections the host opened are admitted; everything else
        # from the application's address is refused; other sources pass on.
        # IPv6 also needs neighbour discovery, which is not connection-tracked:
        # without it the application could not find its gateway.
        discovery = [
            f"-s {source} -p ipv6-icmp --icmpv6-type {kind} -j ACCEPT"
            for kind in ("router-solicitation", "neighbour-solicitation", "neighbour-advertisement")
        ]
        assert chains == {
            "SY-HOST-test-A": [
                f"-s {source} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
                *(discovery if tool == "ip6tables" else []),
                f"-s {source} -j REJECT",
                "-j RETURN",
            ]
        }

    for expected in ("SY-HOST-test-B", "SY-HOST-test-A"):
        assert _run(installer, env).returncode == 0
        jumps, chains = _host_rules(state_dir)
        assert jumps == [["br-test", expected]]
        assert list(chains) == [expected]


def test_host_rules_follow_a_recreated_deployment_bridge(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    interfaces = tmp_path / "net"
    (interfaces / "br-old").mkdir(parents=True)
    env["SCRAPEYARD_EGRESS_INTERFACE"] = "br-old"
    assert _run(installer, env).returncode == 0

    (interfaces / "br-old").rmdir()
    (interfaces / "br-new").mkdir()
    env["SCRAPEYARD_EGRESS_INTERFACE"] = "br-new"
    result = _run(installer, env)

    assert result.returncode == 0, result.stderr
    assert "host jump to SY-HOST-test-A left for the removed interface br-old" in result.stdout
    for tool in ("iptables", "ip6tables"):
        (jump,), chains = _host_rules(state_dir, tool)
        assert jump[0] == "br-new"
        assert list(chains) == [jump[1]]


@pytest.mark.parametrize(
    "failing", ["-N SY-HOST-test-B", "-A SY-HOST-test-B", "-I INPUT", "-D INPUT"]
)
def test_a_failure_in_the_host_rules_leaves_the_forwarded_policy_as_it_was(
    tmp_path: Path, failing: str
) -> None:
    installer = _installer(tmp_path)
    bin_dir = _shim_bin(tmp_path)
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    assert _run(installer, _environment(bin_dir, baseline)).returncode == 0
    for name in ("mutation-count", "mutations.log"):
        (baseline / name).unlink(missing_ok=True)
    before = json.loads((baseline / "iptables.json").read_text())

    # Find where a reinstall makes this change, then fail a second reinstall there.
    completed = tmp_path / "completed"
    shutil.copytree(baseline, completed)
    assert _run(installer, _environment(bin_dir, completed)).returncode == 0
    failure_point = next(
        int(row.split("\t", 1)[0])
        for row in (completed / "mutations.log").read_text().splitlines()
        if "\tiptables\t" in row and failing in row
    )
    state_dir = tmp_path / "failed"
    shutil.copytree(baseline, state_dir)
    env = _environment(bin_dir, state_dir)
    env["FAIL_AT"] = str(failure_point)

    result = _run(installer, env)

    assert result.returncode != 0
    after = json.loads((state_dir / "iptables.json").read_text())
    # The forwarded policy was not touched.
    assert after["jumps"] == before["jumps"] == ["SY-EGRESS-test-A"]
    assert after["chains"]["SY-EGRESS-test-A"] == before["chains"]["SY-EGRESS-test-A"]
    # And input from the application is refused throughout: by the old host
    # chain, or by the new one once INPUT jumps to it.
    assert after["input"], after
    for _interface, chain in after["input"]:
        assert any(rule.endswith("-j REJECT") for rule in after["chains"][chain])

    # Wherever it stopped, even with a jump to each chain left behind, the
    # next install completes without anyone clearing up first.
    retry = _run(installer, _environment(bin_dir, state_dir))
    assert retry.returncode == 0, retry.stderr
    jumps, chains = _host_rules(state_dir)
    assert len(jumps) == 1 and jumps[0][0] == "br-test"
    assert list(chains) == [jumps[0][1]]


def test_remove_takes_the_host_rules_away_with_the_forwarded_policy(tmp_path: Path) -> None:
    installer, state_dir, env = _fresh_install(tmp_path)
    assert _run(installer, env).returncode == 0

    result = subprocess.run(
        [os.fspath(installer), "remove"], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    for tool in ("iptables", "ip6tables"):
        state = json.loads((state_dir / f"{tool}.json").read_text())
        assert state["input"] == [] and state["jumps"] == []
        assert set(state["chains"]) == {"DOCKER-USER"}


def _installed(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    installer = _installer(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _environment(_shim_bin(tmp_path), state_dir)
    assert _run(installer, env).returncode == 0
    return installer, env, state_dir


def _edit(state_dir: Path, tool: str, change) -> None:
    path = state_dir / f"{tool}.json"
    state = json.loads(path.read_text())
    change(state)
    path.write_text(json.dumps(state, sort_keys=True))


def test_check_finds_an_installed_policy_in_place_and_changes_nothing(tmp_path: Path) -> None:
    installer, env, state_dir = _installed(tmp_path)
    before = {path.name: path.read_bytes() for path in state_dir.iterdir()}

    result = _run(installer, env, "check")

    assert (result.returncode, result.stderr) == (0, "")
    assert {path.name: path.read_bytes() for path in state_dir.iterdir()} == before


@pytest.mark.parametrize(
    ("tool", "removal", "named"),
    [
        # What another firewall tool takes when it reloads the host's rules.
        ("iptables", lambda state: state["input"].clear(), "iptables INPUT has no jump for br-test"),
        ("iptables", lambda state: state["jumps"].clear(), "iptables DOCKER-USER has no jump for br-test"),
        ("ip6tables", lambda state: state["input"].clear(), "ip6tables INPUT has no jump for br-test"),
        ("ip6tables", lambda state: state["jumps"].clear(), "ip6tables DOCKER-USER has no jump for br-test"),
        # Docker's jump into DOCKER-USER, which an install cannot put back.
        (
            "iptables",
            lambda state: state.update({"forward": False}),
            "iptables FORWARD does not jump to DOCKER-USER",
        ),
        # A jump to a chain that was flushed filters nothing.
        (
            "iptables",
            lambda state: state["chains"].update({"SY-EGRESS-test-A": ["-j RETURN"]}),
            "iptables chain SY-EGRESS-test-A refuses nothing from 172.30.0.250",
        ),
        (
            "iptables",
            lambda state: state["chains"].update({"SY-HOST-test-A": []}),
            "iptables chain SY-HOST-test-A does not refuse 172.30.0.250",
        ),
    ],
)
def test_check_names_what_is_missing_and_install_puts_it_back(
    tmp_path: Path, tool: str, removal, named: str
) -> None:
    installer, env, state_dir = _installed(tmp_path)
    _edit(state_dir, tool, removal)
    damaged = {path.name: path.read_bytes() for path in state_dir.iterdir()}

    result = _run(installer, env, "check")

    assert result.returncode == 1
    assert named in result.stderr and "not in place: " in result.stderr
    assert {path.name: path.read_bytes() for path in state_dir.iterdir()} == damaged
    assert _run(installer, env).returncode == 0
    # An install puts back everything but Docker's own jump.
    assert _run(installer, env, "check").returncode == (1 if "FORWARD" in named else 0)


def test_check_reports_a_bridge_hook_that_was_switched_off(tmp_path: Path) -> None:
    installer, env, _state_dir = _installed(tmp_path)
    _bridge_netfilter(tmp_path, iptables="0", ip6tables="1")

    result = _run(installer, env, "check")

    assert result.returncode == 1
    assert "net.bridge.bridge-nf-call-iptables is not 1" in result.stderr
    # A check does not switch it back on; an install does.
    assert (tmp_path / "bridge" / "bridge-nf-call-iptables").read_text().strip() == "0"


def test_check_answers_one_for_jumps_an_install_would_refuse_too(tmp_path: Path) -> None:
    installer, env, state_dir = _installed(tmp_path)
    # A second policy chain is jumped to: nobody can say which one filters.
    _edit(
        state_dir,
        "iptables",
        lambda state: (
            state["chains"].update({"SY-EGRESS-test-B": []}),
            state["jumps"].append("SY-EGRESS-test-B"),
            state["interfaces"].update({"SY-EGRESS-test-B": "br-test"}),
        ),
    )

    result = _run(installer, env, "check")

    # Exit 2 is what an installer without this action answers; this one ran.
    assert result.returncode == 1
    assert "iptables DOCKER-USER jumps are not as an install leaves them" in result.stderr


def test_an_action_the_installer_does_not_know_is_a_usage_error(tmp_path: Path) -> None:
    installer, env, state_dir = _installed(tmp_path)

    result = _run(installer, env, "verify")

    assert result.returncode == 2
    assert "usage:" in result.stderr


@pytest.mark.parametrize("spelling", ["2001:DB8:0:0::250", "2001:0db8::0250", "2001:db8:0:0:0:0:0:250"])
def test_check_knows_an_address_by_any_spelling_of_it(tmp_path: Path, spelling: str) -> None:
    # iptables lists 2001:db8::250/128 whatever the settings spell. A check
    # that looked for the settings' spelling would call an installed policy
    # missing every time it ran.
    installer = _installer(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = dict(_environment(_shim_bin(tmp_path), state_dir), SCRAPEYARD_EGRESS_SOURCE_V6=spelling)
    assert _run(installer, env).returncode == 0

    result = _run(installer, env, "check")

    assert (result.returncode, result.stderr) == (0, "")


def test_an_install_leaves_no_bytecode_beside_the_renderer(tmp_path: Path) -> None:
    # In a retained release the renderer's tree is checksummed.
    installer = _installer(tmp_path)
    renderer = tmp_path / "tree" / "render.py"
    renderer.parent.mkdir()
    (renderer.parent / "rules.py").write_text("ROWS = ['4\\tdeny\\t10.0.0.0/8', '6\\tdeny\\tfc00::/7']\n")
    renderer.write_text("import rules\nprint('\\n'.join(rules.ROWS))\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = dict(
        _environment(_shim_bin(tmp_path), state_dir),
        SCRAPEYARD_EGRESS_POLICY_RENDERER=os.fspath(renderer),
    )
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    assert _run(installer, env).returncode == 0
    assert not list(renderer.parent.rglob("__pycache__"))
