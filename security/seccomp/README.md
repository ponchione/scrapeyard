# Seccomp profile

Compose starts the application with `chromium.json`. It is the profile every
Docker container gets by default, plus one rule.

| File | What it is |
| --- | --- |
| `moby-default.json` | Moby's default profile, verbatim: `github.com/moby/profiles/seccomp` v0.2.4, as Docker 29.9.0 vendors it (`vendor/github.com/moby/profiles/seccomp/default.json` at tag `docker-v29.9.0`). Never edit it. |
| `chromium.json` | Written by `scripts/derive_seccomp_profile.py` from the file above. Never edit it either. |

## The difference from Moby's default

One rule, at the top of the list:

- `clone`, `setns` and `unshare` are allowed whatever their arguments.
  Chromium's sandbox puts its renderers in new user, PID and network
  namespaces. Moby's default allows these calls with namespace flags only to
  a container that holds `CAP_SYS_ADMIN`, and this one holds no capability.
  What a process inside a new user namespace can then do is bounded by the
  AppArmor profile (`../apparmor/scrapeyard-chromium`), by `no-new-privileges`
  and by the bounding set, which holds `CAP_SYS_CHROOT` alone.

`clone3` is left as the default has it: it answers `ENOSYS`. A seccomp filter
cannot read the flags `clone3` is passed, which is why Moby refuses it, and
glibc falls back to `clone` on `ENOSYS`. Allowing it would add nothing that
`clone` does not already give.

Nothing is removed from the default. In particular the profile does not allow
`io_uring`, or sockets of the `AF_ALG` and `AF_VSOCK` families, all of which
the profile it replaced (Moby's of 2020, by way of Playwright) still did.

## Moving to a newer default

1. Replace `moby-default.json` with the file from the Docker release tag.
2. Run `python3 scripts/derive_seccomp_profile.py`.
3. Update the version above and the checksum in
   `tests/unit/test_browser_runtime_contract.py`.
4. Read the difference between the old and the new default, and have the
   change reviewed as a security change. The container smoke lane and the
   release rehearsal run on a pull request that touches this directory.

Docker reads a rule's `includes` and `excludes` against the container's
bounding set and architecture, and runc skips a system call name its
libseccomp does not know, so a default newer than the host's Docker loads on
it.
