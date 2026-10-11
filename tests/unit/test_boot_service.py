from __future__ import annotations

import configparser
import fcntl
import json
import os
import subprocess
import time
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
        self.root = tmp_path
        self.calls = tmp_path / "calls"
        self.env_file = tmp_path / "deploy.env"
        self.releases = tmp_path / "releases"
        self.current = self.releases / "current"
        self.lock_dir = tmp_path / "lock"
        self.script = tmp_path / "manage-release.sh"
        text = MANAGE.read_text(encoding="utf-8")
        for old, new in (
            ("((EUID == 0)) || fail", "true || fail"),
            ("ENV_FILE=/etc/scrapeyard/deploy.env", f"ENV_FILE={self.env_file}"),
            ("CURRENT=/srv/scrapeyard-releases/current", f"CURRENT={self.current}"),
            ("LOCK_DIR=/run/scrapeyard", f"LOCK_DIR={self.lock_dir}"),
            ("TRUSTED_OWNER=0", f"TRUSTED_OWNER={os.getuid()}"),
            ("TRUSTED_ROOT=/", f"TRUSTED_ROOT={tmp_path}"),
        ):
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        self.script.write_text(text, encoding="utf-8")
        self.script.chmod(0o755)
        tmp_path.chmod(0o755)
        self.releases.mkdir(mode=0o755)
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
        self.tool(
            "journalctl",
            'echo "journalctl $*" >> "$CALLS"\n'
            '[[ "$*" != *--show-cursor* ]] || echo "-- cursor: s=abc;i=1"\n',
        )
        # is-active answers from a list of states, one a call, then "inactive".
        self.tool(
            "systemctl",
            'echo "systemctl $*" >> "$CALLS"\n'
            'if [[ "$*" == "is-active --quiet docker.service" ]]; then [[ "${DOCKER_DOWN:-}" != yes ]]; exit; fi\n'
            'state=inactive\n'
            'if [[ -s "$PROMOTION_STATES" ]]; then\n'
            '  state=$(head -n 1 "$PROMOTION_STATES"); sed -i 1d "$PROMOTION_STATES"\n'
            'fi\n'
            'echo "$state"; [[ "$state" == active ]]\n',
        )
        self.tool(
            "docker",
            'echo "docker $*" >> "$CALLS"\n'
            'case "$1" in\n'
            '  ps) [[ "${DOCKER_PS_FAILS:-}" != yes ]] || exit 1\n'
            '      [[ "${NO_CONTAINER:-}" == yes ]] || echo app1 ;;\n'
            '  inspect) echo "${RUNNING_IMAGE:-sha256:old}" ;;\n'
            'esac\n',
        )
        self.states = tmp_path / "promotion-states"
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            CALLS=str(self.calls),
            PROMOTION_STATES=str(self.states),
        )

    def tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def release(self, name: str, wrapper_body: str = "") -> Path:
        release = self.releases / name
        security = release / "source" / "security"
        security.mkdir(parents=True)
        (release / "manifest.json").write_text(
            json.dumps({"images": {"scrapeyard": f"sha256:{name}"}}, indent=2)
        )
        wrapper = security / "deploy-secure-compose.sh"
        wrapper.write_text(
            '#!/bin/bash\necho "wrapper $* project=$COMPOSE_PROJECT_NAME proxy=$SCRAPEYARD_PROXY_URL"'
            ' >> "$CALLS"\n' + wrapper_body + '[[ "${WRAPPER_FAILS:-}" != yes ]]\n'
        )
        wrapper.chmod(0o755)
        return release

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *args], env=dict(self.env, **env), capture_output=True, text=True
        )

    def called(self, prefix: str = "") -> list[str]:
        lines = self.calls.read_text().splitlines() if self.calls.exists() else []
        return [line for line in lines if line.startswith(prefix)]


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _Host(tmp_path)


def test_promotion_deploys_inside_one_unit_and_then_moves_the_link(host):
    old, new = host.release("old"), host.release("new")
    host.current.symlink_to(old)

    result = host.run("promote", str(new))

    assert result.returncode == 0, result.stderr
    assert os.readlink(host.current) == str(new)
    (asked,) = host.called("systemd-run")
    # A stopped oneshot unit is a failure; a stopped simple one reports success.
    assert "--service-type=oneshot" in asked.split()
    assert "--wait" in asked.split()
    assert "--unit scrapeyard-promote" in asked
    # The deployment and the link move are one command inside the unit, so a
    # closed terminal cannot separate them.
    assert asked.endswith(f"{host.script} apply {new}")
    assert host.called("wrapper") == [
        f"wrapper --release {new} project=scrapeyard proxy=https://proxy.example:8443"
    ]
    assert not list(host.releases.glob("current.new*"))
    # Only this promotion's output is printed, not an earlier one's.
    assert host.called("journalctl")[-1].endswith("--after-cursor s=abc;i=1")


def test_failed_promotion_leaves_the_link_and_says_what_is_running(host):
    old, new = host.release("old"), host.release("new")
    host.current.symlink_to(old)

    result = host.run("promote", str(new), WRAPPER_FAILS="yes")

    assert result.returncode == 2
    assert os.readlink(host.current) == str(old)
    assert f"was not promoted and the boot service still starts {old}" in result.stderr
    assert "the application is still running the earlier release" in result.stderr
    assert not list(host.releases.glob("current.new*"))
    for env, message in (
        ({"NO_CONTAINER": "yes"}, "the application is stopped; promote a retained release"),
        ({"RUNNING_IMAGE": "sha256:new"}, "is running the release that was not promoted"),
        ({"DOCKER_PS_FAILS": "yes"}, "the application's state is unknown"),
    ):
        assert message in host.run("promote", str(new), WRAPPER_FAILS="yes", **env).stderr


def test_promoting_the_promoted_release_again_deploys_it_and_reports_truthfully(host):
    release = host.release("new")
    host.current.symlink_to(release)

    result = host.run("promote", str(release))

    assert result.returncode == 0, result.stderr
    assert os.readlink(host.current) == str(release)
    assert len(host.called("wrapper")) == 1
    assert not list(host.releases.glob("current.new*"))

    failed = host.run("promote", str(release), WRAPPER_FAILS="yes", RUNNING_IMAGE="sha256:new")
    assert failed.returncode == 2
    # It is the promoted release and it is running: neither may be denied.
    assert "was already the release the boot service starts" in failed.stderr
    assert "the application is running it" in failed.stderr
    assert "was not promoted" not in failed.stderr
    assert "next boot would replace" not in failed.stderr


def test_promotion_goes_ahead_when_the_journal_cannot_be_read(host):
    old, new = host.release("old"), host.release("new")
    host.current.symlink_to(old)
    host.tool("journalctl", 'echo "journalctl $*" >> "$CALLS"\nexit 1\n')

    result = host.run("promote", str(new))

    assert result.returncode == 0, result.stderr
    assert os.readlink(host.current) == str(new)
    assert "--since @" in host.called("journalctl")[-1]


def test_promotion_is_not_believed_unless_the_link_moved(host):
    old, new = host.release("old"), host.release("new")
    host.current.symlink_to(old)

    # The unit reports success without having run, as a stopped Type=simple unit would.
    result = host.run("promote", str(new), SYSTEMD_RUN_SKIPS="yes")

    assert result.returncode == 2
    assert "was not promoted" in result.stderr
    assert os.readlink(host.current) == str(old)


def test_promotion_waits_for_a_stopped_unit_to_finish_before_judging(host):
    old, new = host.release("old"), host.release("new")
    host.current.symlink_to(old)
    # Not under way when asked; still stopping the application when systemd-run returns.
    host.states.write_text("inactive\ndeactivating\ndeactivating\n")

    result = host.run("promote", str(new), WRAPPER_FAILS="yes")

    assert result.returncode == 2
    calls = host.called()
    polls = [i for i, call in enumerate(calls) if call == "systemctl is-active scrapeyard-promote.service"]
    assert len(polls) == 4
    # Nothing is read from Docker or the journal until the unit is gone.
    judged = min(i for i, call in enumerate(calls) if call.startswith(("docker ps", "journalctl --no-pager")))
    assert judged > polls[-1]


def test_second_promotion_is_refused_while_one_is_under_way(host):
    new = host.release("new")
    host.states.write_text("activating\n")

    result = host.run("promote", str(new))

    assert result.returncode == 2
    assert "a promotion is under way" in result.stderr
    assert host.called("systemd-run") == []


def test_boot_start_deploys_the_linked_release_with_the_file_settings(host):
    release = host.release("new")
    host.current.symlink_to(release)

    result = host.run("start", SCRAPEYARD_PROXY_URL="https://from-the-environment.example")

    assert result.returncode == 0, result.stderr
    assert host.called() == [
        f"wrapper --release {release} project=scrapeyard proxy=https://proxy.example:8443"
    ]
    host.current.unlink()
    assert host.run("start").returncode == 2


def test_a_start_that_waited_for_a_promotion_deploys_what_was_promoted(host):
    old, new = host.release("old"), host.release("new")
    host.current.symlink_to(old)
    host.lock_dir.mkdir(mode=0o700)

    # A promotion holds the lock, as `apply` does until the link has moved.
    with (host.lock_dir / "release.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        waiting = subprocess.Popen([str(host.script), "start"], env=host.env)
        time.sleep(0.5)
        assert waiting.poll() is None and host.called() == []
        host.current.unlink()
        host.current.symlink_to(new)
    assert waiting.wait(timeout=20) == 0

    # The link is read after the wait, so the old release is not brought back.
    assert host.called("wrapper") == [
        f"wrapper --release {new} project=scrapeyard proxy=https://proxy.example:8443"
    ]


def test_a_link_that_cannot_be_moved_stops_what_was_deployed(host):
    old = host.release("old")
    # The deployment succeeds, and then the link's place is taken.
    new = host.release("new", 'rm -f "$TAKEN" && mkdir "$TAKEN" && touch "$TAKEN/in-the-way"\n')
    host.current.symlink_to(old)

    result = host.run("apply", str(new), TAKEN=str(host.current))

    assert result.returncode == 2
    assert "could not be moved; the application was stopped" in result.stderr
    assert host.called("docker")[-1] == "docker stop app1"
    assert not list(host.releases.glob("current.new*"))


def test_a_link_that_cannot_be_moved_says_so_when_the_stop_fails_too(host):
    old = host.release("old")
    new = host.release("new", 'rm -f "$TAKEN" && mkdir "$TAKEN" && touch "$TAKEN/in-the-way"\n')
    host.current.symlink_to(old)

    result = host.run("apply", str(new), TAKEN=str(host.current), DOCKER_PS_FAILS="yes")

    assert result.returncode == 2
    assert "could not be moved, and the application could not be stopped" in result.stderr
    assert "the application was stopped" not in result.stderr


def test_a_promotion_is_refused_before_deploying_when_its_link_cannot_be_written(host):
    old, new = host.release("old"), host.release("new")
    host.current.symlink_to(old)
    (host.releases / "current.new").mkdir()

    result = host.run("apply", str(new))

    assert result.returncode == 2
    assert host.called("wrapper") == []
    assert os.readlink(host.current) == str(old)


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


def test_stop_does_not_report_success_when_it_could_not_ask_docker(host):
    result = host.run("stop", DOCKER_PS_FAILS="yes")

    assert result.returncode == 2
    assert "it may still be running" in result.stderr
    text = MANAGE.read_text(encoding="utf-8")
    # A hung daemon must not hold a stop until systemd gives up on it.
    assert "timeout 20 docker ps" in text and "timeout 90 docker stop" in text


def test_stop_needs_only_the_project_name_from_the_settings(host):
    host.env_file.write_text("export SCRAPEYARD_ENCRYPTION_KEYS=hunter2\n" + SETTINGS)

    result = host.run("stop")

    # A mistyped line elsewhere must not leave the application running.
    assert result.returncode == 0, result.stderr
    assert host.called("docker")[-1] == "docker stop app1"
    assert "hunter2" not in result.stderr + result.stdout
    host.env_file.write_text("SCRAPEYARD_PROXY_URL='https://proxy.example:8443'\n")
    assert host.run("stop").returncode == 2


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
        # Compose would read this second file and print what it cannot parse.
        "COMPOSE_ENV_FILES='/etc/scrapeyard/hunter2-secret.env'",
        "COMPOSE_FILE='hunter2-secret.yml'",
        "   ",
    ],
)
@pytest.mark.parametrize("action", ["promote", "start"])
def test_a_malformed_settings_line_is_refused_without_repeating_it(host, line, action):
    release = host.release("new")
    host.current.symlink_to(release)
    host.env_file.write_text(SETTINGS + "# a comment\n\n" + line + "\n")

    result = host.run(action, *([str(release)] if action == "promote" else []))

    assert result.returncode == 2
    assert "deploy.env line 5 is not a SCRAPEYARD_ setting or COMPOSE_PROJECT_NAME" in result.stderr
    assert "hunter2" not in result.stderr + result.stdout
    # Nothing was deployed or handed to systemd.
    assert host.called("wrapper") == host.called("systemd-run") == host.called("docker") == []


@pytest.mark.parametrize("problem", ["mode", "link", "no-project"])
def test_settings_file_must_be_private_and_name_the_project(host, problem):
    release = host.release("new")
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


@pytest.mark.parametrize(
    "problem",
    [
        "group-writable",
        "symlink-inside",
        "writable-parent",
        "linked-from-a-writable-directory",
        "not-a-release",
        "current-is-a-directory",
    ],
)
@pytest.mark.parametrize("action", ["promote", "start"])
def test_a_tree_root_should_not_run_is_refused(host, problem, action):
    release = host.release("new")
    target = release
    if problem == "group-writable":
        (release / "source" / "security" / "deploy-secure-compose.sh").chmod(0o775)
    elif problem == "symlink-inside":
        (release / "source" / "elsewhere").symlink_to("/etc")
    elif problem == "writable-parent":
        # Whoever can write to the directory can swap the release after the check.
        host.releases.chmod(0o777)
    elif problem == "linked-from-a-writable-directory":
        # The releases are root's alone, but they are reached through a link
        # in a directory someone else can write to, who could repoint it.
        public = host.root / "public"
        public.mkdir(mode=0o777)
        public.chmod(0o777)
        (public / "releases").symlink_to(host.releases)
        script = host.script.read_text()
        host.script.write_text(
            script.replace(f"CURRENT={host.current}", f"CURRENT={public / 'releases' / 'current'}")
        )
    elif problem == "not-a-release":
        target = host.releases
    else:
        host.current.mkdir()
    if problem != "current-is-a-directory":
        host.current.symlink_to(target)

    result = host.run(action, *([str(target)] if action == "promote" else []))

    assert result.returncode == 2
    assert "scrapeyard release:" in result.stderr
    assert host.called("wrapper") == host.called("systemd-run") == []
    if problem == "group-writable":
        assert "tar --no-same-owner" in result.stderr
