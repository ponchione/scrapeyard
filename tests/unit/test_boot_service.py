from __future__ import annotations

import configparser
import os
import subprocess
from pathlib import Path

import pytest

UNIT = Path("security/systemd/scrapeyard.service")
MANAGE = Path("security/manage-release.sh")
CURRENT = "/srv/scrapeyard-releases/current"
SETTINGS = "COMPOSE_PROJECT_NAME='scrapeyard'\nSCRAPEYARD_PROXY_URL='https://proxy.example:8443'\n"


def _unit() -> configparser.ConfigParser:
    # systemd keys are case-sensitive and a value may hold `%` or `$`.
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(UNIT.read_text(encoding="utf-8"))
    return parser


def test_boot_service_runs_an_installed_script_not_one_from_the_release():
    unit = _unit()
    service = unit["Service"]
    assert service["Type"] == "oneshot"
    assert service["RemainAfterExit"] == "yes"
    # A rollback may promote a release from before this script existed, and
    # the unit must still be able to start and stop it.
    assert service["ExecStart"] == "/usr/local/sbin/scrapeyard-release start"
    assert service["ExecStop"] == "/usr/local/sbin/scrapeyard-release stop"
    # Without a promoted release there is nothing to start, which is not a failure.
    assert unit["Unit"]["ConditionPathExists"] == f"{CURRENT}/manifest.json"
    # systemd logs a settings line it cannot parse, value included, so the
    # script reads the file and the unit never names it.
    assert "EnvironmentFile" not in service
    assert "Environment" not in service
    assert "SCRAPEYARD_" not in UNIT.read_text(encoding="utf-8").split("[Unit]", 1)[1]
    assert int(service["TimeoutStopSec"]) > 90


def test_boot_service_follows_docker_without_being_cancelled_by_its_failures():
    unit = _unit()["Unit"]
    assert "docker.service" in unit["After"].split()
    # A Requires= would cancel this unit's start when Docker's first start fails.
    assert "Requires" not in unit
    assert "docker.service" in unit["Wants"].split()
    # Stops and restarts of Docker reach this unit; starts reach it through WantedBy.
    assert unit["PartOf"] == "docker.service"
    assert _unit()["Install"]["WantedBy"].split() == ["multi-user.target", "docker.service"]


def test_boot_service_retries_a_failed_start_ever_more_slowly_and_never_refuses_a_start():
    unit = _unit()
    service = unit["Service"]
    assert service["Restart"] == "on-failure"
    assert int(service["RestartSec"]) >= 60
    assert int(service["RestartSteps"]) >= 3
    assert int(service["RestartMaxDelaySec"]) >= 1800
    assert int(service["TimeoutStartSec"]) >= 300
    # A start limit also counts successful starts, so the sixth Docker restart
    # in its window would be refused and leave the application down.
    assert unit["Unit"]["StartLimitIntervalSec"] == "0"
    assert "StartLimitBurst" not in unit["Unit"]


class _Host:
    """A copy of the script with its fixed paths moved, and stand-ins for the host tools."""

    def __init__(self, tmp_path: Path) -> None:
        self.calls = tmp_path / "calls"
        self.env_file = tmp_path / "deploy.env"
        self.current = tmp_path / "releases" / "current"
        self.script = tmp_path / "manage-release.sh"
        text = MANAGE.read_text(encoding="utf-8")
        for old, new in (
            ("((EUID == 0)) || fail", "true || fail"),
            ("ENV_FILE=/etc/scrapeyard/deploy.env", f"ENV_FILE={self.env_file}"),
            ("CURRENT=/srv/scrapeyard-releases/current", f"CURRENT={self.current}"),
            ("TRUSTED_OWNER=0", f"TRUSTED_OWNER={os.getuid()}"),
        ):
            assert text.count(old) == 1
            text = text.replace(old, new)
        self.script.write_text(text, encoding="utf-8")
        self.script.chmod(0o755)
        self.env_file.write_text(SETTINGS)
        self.env_file.chmod(0o600)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        # systemd-run records how it was asked, then runs the command in place.
        self.tool(
            "systemd-run",
            'echo "systemd-run $*" >> "$CALLS"\n'
            '[[ "${SYSTEMD_RUN_SKIPS:-}" != yes ]] || exit 0\n'
            'while [[ "$1" == --* ]]; do [[ "$1" == --unit || "$1" == --property ]] && shift; shift; done\n'
            'exec "$@"\n',
        )
        self.tool("journalctl", "true\n")
        self.tool("systemctl", 'echo "systemctl $*" >> "$CALLS"\n[[ "${DOCKER_DOWN:-}" != yes ]]\n')
        self.tool(
            "docker",
            'echo "docker $*" >> "$CALLS"\n[[ "$1" != ps || "${NO_CONTAINER:-}" == yes ]] || echo app1\n',
        )
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", CALLS=str(self.calls))

    def tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def release(self, name: str) -> Path:
        release = self.current.parent / name
        security = release / "source" / "security"
        security.mkdir(parents=True)
        (release / "manifest.json").write_text("{}")
        wrapper = security / "deploy-secure-compose.sh"
        wrapper.write_text(
            '#!/bin/bash\necho "wrapper $* project=$COMPOSE_PROJECT_NAME proxy=$SCRAPEYARD_PROXY_URL"'
            ' >> "$CALLS"\n[[ "${WRAPPER_FAILS:-}" != yes ]]\n'
        )
        wrapper.chmod(0o755)
        return release

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *args], env=dict(self.env, **env), capture_output=True, text=True
        )

    def called(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _Host(tmp_path)


def test_promotion_deploys_inside_one_unit_and_then_moves_the_link(host):
    old, new = host.release("scrapeyard-release-old"), host.release("scrapeyard-release-new")
    host.current.symlink_to(old)

    result = host.run("promote", str(new))

    assert result.returncode == 0, result.stderr
    assert os.readlink(host.current) == str(new)
    asked, ran = host.called()
    # A stopped oneshot unit is a failure; a stopped simple one reports success.
    assert "--service-type=oneshot" in asked.split()
    assert "--wait" in asked.split()
    assert "--unit scrapeyard-promote" in asked
    # The deployment and the link move are one command inside the unit, so a
    # closed terminal cannot separate them.
    assert asked.endswith(f"{host.script} apply {new}")
    assert ran == f"wrapper --release {new} project=scrapeyard proxy=https://proxy.example:8443"
    assert not list(host.current.parent.glob("current.new.*"))


def test_failed_promotion_leaves_the_link_and_says_what_is_running(host):
    old, new = host.release("scrapeyard-release-old"), host.release("scrapeyard-release-new")
    host.current.symlink_to(old)

    result = host.run("promote", str(new), WRAPPER_FAILS="yes")

    assert result.returncode == 2
    assert os.readlink(host.current) == str(old)
    assert f"was not promoted and the boot service still starts {old}" in result.stderr
    assert "the application is running" in result.stderr
    stopped = host.run("promote", str(new), WRAPPER_FAILS="yes", NO_CONTAINER="yes")
    assert "the application is stopped; promote a retained release to start it" in stopped.stderr


def test_promotion_is_not_believed_unless_the_link_moved(host):
    old, new = host.release("scrapeyard-release-old"), host.release("scrapeyard-release-new")
    host.current.symlink_to(old)

    # The unit reports success without having run, as a stopped Type=simple unit would.
    result = host.run("promote", str(new), SYSTEMD_RUN_SKIPS="yes")

    assert result.returncode == 2
    assert "was not promoted" in result.stderr
    assert os.readlink(host.current) == str(old)


def test_boot_start_deploys_the_linked_release_with_the_file_settings(host):
    release = host.release("scrapeyard-release-new")
    host.current.symlink_to(release)

    result = host.run("start", SCRAPEYARD_PROXY_URL="https://from-the-environment.example")

    assert result.returncode == 0, result.stderr
    assert host.called() == [
        f"wrapper --release {release} project=scrapeyard proxy=https://proxy.example:8443"
    ]
    host.current.unlink()
    assert host.run("start").returncode == 2


def test_stop_asks_docker_only_while_docker_is_running(host):
    assert host.run("stop").returncode == 0
    assert host.called() == [
        "systemctl is-active --quiet docker.service",
        "docker ps -q --filter label=com.docker.compose.project=scrapeyard"
        " --filter label=com.docker.compose.service=scrapeyard",
        # No --time: the container's own grace period applies.
        "docker stop app1",
    ]
    host.calls.unlink()
    # After a Docker crash the CLI would wait on a daemon that cannot start yet.
    assert host.run("stop", DOCKER_DOWN="yes").returncode == 0
    assert host.called() == ["systemctl is-active --quiet docker.service"]


@pytest.mark.parametrize(
    "line",
    [
        "export SCRAPEYARD_ENCRYPTION_KEYS='hunter2-secret'",
        "SCRAPEYARD_ENCRYPTION_KEYS=hunter2-secret",
        'SCRAPEYARD_ENCRYPTION_KEYS="hunter2-secret"',
        "SCRAPEYARD_ENCRYPTION-KEYS='hunter2-secret'",
        "SCRAPEYARD_ENCRYPTION_KEYS='hunter2-secret",
        "SCRAPEYARD_ENCRYPTION_KEYS='hunter2-secret' # rotated",
        "SCRAPEYARD_ENCRYPTION_KEYS='hunter2'-secret'",
        "LD_PRELOAD='hunter2-secret'",
        "PATH='hunter2-secret'",
    ],
)
@pytest.mark.parametrize("action", ["promote", "start", "stop"])
def test_a_malformed_settings_line_is_refused_without_repeating_it(host, line, action):
    release = host.release("scrapeyard-release-new")
    host.current.symlink_to(release)
    host.env_file.write_text(SETTINGS + "# a comment\n\n" + line + "\n")

    result = host.run(action, *([str(release)] if action == "promote" else []))

    assert result.returncode == 2
    assert "deploy.env line 5 is not a SCRAPEYARD_ or COMPOSE_ setting" in result.stderr
    assert "hunter2" not in result.stderr + result.stdout
    # Nothing was started, stopped or handed to systemd.
    assert host.called() == []


@pytest.mark.parametrize("problem", ["mode", "link", "no-project"])
def test_settings_file_must_be_private_and_name_the_project(host, problem):
    release = host.release("scrapeyard-release-new")
    if problem == "mode":
        host.env_file.chmod(0o640)
    elif problem == "link":
        real = host.env_file.with_name("real.env")
        host.env_file.rename(real)
        host.env_file.symlink_to(real)
    else:
        host.env_file.write_text("SCRAPEYARD_PROXY_URL='https://proxy.example:8443'\n")

    result = host.run("promote", str(release))

    assert result.returncode == 2
    assert host.called() == []


@pytest.mark.parametrize("problem", ["group-writable", "not-a-release", "current-is-a-directory"])
def test_promotion_refuses_a_tree_root_should_not_run(host, problem):
    release = host.release("scrapeyard-release-new")
    target = release
    if problem == "group-writable":
        (release / "source" / "security" / "deploy-secure-compose.sh").chmod(0o775)
    elif problem == "not-a-release":
        target = release.parent
    else:
        host.current.mkdir()

    result = host.run("promote", str(target))

    assert result.returncode == 2
    assert "scrapeyard release:" in result.stderr
    assert host.called() == []
    if problem == "group-writable":
        assert "tar --no-same-owner" in result.stderr
        # The boot start checks the tree again: it may have changed since promotion.
        host.current.symlink_to(release)
        assert host.run("start").returncode == 2
        assert host.called() == []
