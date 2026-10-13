# Seccomp profile

Compose starts the application with `chromium.json`. It is the profile every
Docker container gets by default, plus one rule.

| File | What it is |
| --- | --- |
| `moby-default.json` | Moby's default profile, verbatim: `github.com/moby/profiles/seccomp` v0.2.4, as Docker 29.9.0 vendors it (`vendor/github.com/moby/profiles/seccomp/default.json` at tag `docker-v29.9.0`). Never edit it. |
| `chromium.json` | Written by `scripts/derive_seccomp_profile.py` from the file above. Never edit it either. |

## The difference from Moby's default

One rule, at the top of the list:

- `clone` and `unshare` are allowed whatever their arguments. Chromium's and
  Firefox's sandboxes create a user namespace and put their child processes
  in further new namespaces inside it. Moby's default allows these calls
  with namespace flags only to a container that holds `CAP_SYS_ADMIN`, and
  this one holds no capability. Narrowing the rule to the user, PID and
  network flags was tried and hangs Firefox, which also needs the IPC flag.

`setns`, which the profile Playwright publishes also allows, is not in the
rule: both browsers run sandboxed without it.

The container's bounding set does not limit a user namespace. The kernel
gives a process every capability inside a namespace it creates. What it can
do there is bounded by this filter and by the AppArmor profile
(`../apparmor/scrapeyard-chromium`). Docker decides the filter's
capability-gated rules once, against the container's own bounding set, so
`mount`, `umount2`, `sethostname`, `bpf` and the mount calls stay refused
inside the namespace, and `pivot_root` and `keyctl` are in no rule at all.
What would open there is the network stack of a new network namespace:
packet and raw sockets, netfilter, links and traffic control. A filter
cannot close that without refusing the namespaces the sandboxes need, so the
AppArmor profile does: it denies `CAP_NET_ADMIN` and `CAP_NET_RAW`, which
AppArmor applies inside a user namespace as well, and the packet family.

`cap_add: SYS_CHROOT` in Compose exists for the filter alone. Docker keeps
the `chroot` rule only for a container whose bounding set holds that
capability, and the sandboxes call `chroot` inside their namespace.

`clone3` is left as the default has it: it answers `ENOSYS`. A seccomp filter
cannot read the flags `clone3` is passed, which is why Moby refuses it, and
glibc falls back to `clone` on `ENOSYS`. Allowing it would add nothing that
`clone` does not already give.

Nothing is removed from the default. In particular the profile does not allow
`io_uring`, and it refuses `socket` for the `AF_ALG` and `AF_VSOCK`
families, all of which the profile it replaced (an older default, by way of
Playwright) allowed. The filter sees the direct `socket` call only. A 64-bit
process can still reach `socketcall` through the 32-bit entry, so the
AppArmor profile denies both families as well, as Moby's own does. The
container smoke lane opens both through that entry and requires AppArmor's
refusal (`tests/smoke/verify_confinement.py`).

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
