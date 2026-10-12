#!/usr/bin/env python3
"""Write security/seccomp/chromium.json from Moby's default seccomp profile.

The profile Compose applies is the one every Docker container gets by default
(security/seccomp/moby-default.json, kept verbatim) plus one rule. Chromium's
sandbox creates user namespaces, and the default allows that only to a
container that holds CAP_SYS_ADMIN. security/seccomp/README.md says where the
default comes from and how to move to a newer one.
"""

from __future__ import annotations

import json
from pathlib import Path

SECCOMP_DIR = Path(__file__).resolve().parent.parent / "security" / "seccomp"

# The whole difference from Moby's default. `clone3` is left as the default
# has it, answering ENOSYS: a filter cannot read the flags it is passed, and
# glibc falls back to `clone`.
USER_NAMESPACE_RULE = {
    "comment": "Chromium's sandbox creates user namespaces without CAP_SYS_ADMIN",
    "names": ["clone", "setns", "unshare"],
    "action": "SCMP_ACT_ALLOW",
}


def derive(moby_default: str) -> str:
    profile = json.loads(moby_default)
    profile["syscalls"].insert(0, USER_NAMESPACE_RULE)
    return json.dumps(profile, indent="\t") + "\n"


def main() -> int:
    moby_default = (SECCOMP_DIR / "moby-default.json").read_text(encoding="utf-8")
    (SECCOMP_DIR / "chromium.json").write_text(derive(moby_default), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
