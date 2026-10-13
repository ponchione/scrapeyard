"""Try, from inside the application's container, what its confinement must refuse.

The container smoke lane pipes this file into the application's container as
the runtime user. It runs the same way against a deployed container:

    docker exec -i <application container> python - < tests/smoke/verify_confinement.py

It opens sockets and closes them again; no packet leaves the process.

`--control` is for a container that runs under a copy of the AppArmor profile
without the lines these checks rest on. There every attempt has to succeed,
or a refusal in the real container would prove nothing about those lines.
`--report` prints what happened and judges nothing.
"""

from __future__ import annotations

import ctypes
import errno
import json
import mmap
import os
import platform
import socket
import struct
import sys

AF_ALG = 38
AF_VSOCK = 40

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def _refusal(number: int) -> str:
    return errno.errorcode.get(number, f"errno {number}")


def direct_socket(family: int, kind: int, protocol: int = 0) -> str:
    """Open a socket with the 64-bit `socket` call, which the seccomp filter reads."""
    descriptor = _libc.syscall(
        ctypes.c_long(41), ctypes.c_long(family), ctypes.c_long(kind), ctypes.c_long(protocol)
    )
    if descriptor < 0:
        return _refusal(ctypes.get_errno())
    os.close(descriptor)
    return "accepted"


class Entry32:
    """The 32-bit system call entry (`int 0x80`), as a 64-bit process reaches it.

    There `socket` can be spelled `socketcall(SYS_SOCKET, arguments)`. A
    seccomp filter sees the pointer and not the family behind it, so Moby's
    default profile, and ours with it, allows every `socketcall`.
    """

    _SOCKETCALL = 102
    _SYS_SOCKET = 1
    _ARGUMENTS_AT = 256

    def __init__(self) -> None:
        # Below 4 GiB, because the entry reads its arguments as 32-bit values.
        self._page = mmap.mmap(
            -1,
            mmap.PAGESIZE,
            flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS | 0x40,  # MAP_32BIT
            prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC,
        )
        # long call(long number, long first, long second)
        self._page.write(
            bytes.fromhex(
                "53"  # push rbx
                "89f8"  # mov eax, edi
                "89f3"  # mov ebx, esi
                "89d1"  # mov ecx, edx
                "cd80"  # int 0x80
                "5b"  # pop rbx
                "4863c0"  # movsxd rax, eax
                "c3"  # ret
            )
        )
        address = ctypes.addressof(ctypes.c_char.from_buffer(self._page))
        self._call = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_long, ctypes.c_long, ctypes.c_long)(
            address
        )
        self._arguments = address + self._ARGUMENTS_AT

    def socket(self, family: int, kind: int, protocol: int = 0) -> str:
        struct.pack_into("<III", self._page, self._ARGUMENTS_AT, family, kind, protocol)
        descriptor = self._call(self._SOCKETCALL, self._SYS_SOCKET, self._arguments)
        if descriptor < 0:
            return _refusal(-descriptor)
        os.close(descriptor)
        return "accepted"


def in_the_container(entry: Entry32) -> dict[str, object]:
    return {
        "TCP socket, 32-bit entry": entry.socket(socket.AF_INET, socket.SOCK_STREAM),
        "AF_ALG socket": direct_socket(AF_ALG, socket.SOCK_SEQPACKET),
        "AF_VSOCK socket": direct_socket(AF_VSOCK, socket.SOCK_STREAM),
        "AF_ALG socket, 32-bit entry": entry.socket(AF_ALG, socket.SOCK_SEQPACKET),
        "AF_VSOCK socket, 32-bit entry": entry.socket(AF_VSOCK, socket.SOCK_STREAM),
    }


# What each attempt must answer in the application's container. A tuple lists
# every acceptable answer.
CONFINED: dict[str, dict[str, object]] = {
    "container": {
        # Without this one a refusal below could mean that the kernel has no
        # 32-bit entry at all.
        "TCP socket, 32-bit entry": "accepted",
        # Seccomp answers EPERM to the call it can read.
        "AF_ALG socket": "EPERM",
        "AF_VSOCK socket": "EPERM",
        # AppArmor answers EACCES, whichever entry the call came through.
        "AF_ALG socket, 32-bit entry": "EACCES",
        "AF_VSOCK socket, 32-bit entry": "EACCES",
    },
}

# And under the control profile, where seccomp is the same and AppArmor does
# not refuse. A kernel without one of the two families answers EAFNOSUPPORT,
# which is as good: it shows the call reached the kernel's own lookup.
CONTROL: dict[str, dict[str, object]] = {
    "container": {
        "TCP socket, 32-bit entry": "accepted",
        "AF_ALG socket": "EPERM",
        "AF_VSOCK socket": "EPERM",
        "AF_ALG socket, 32-bit entry": ("accepted", "EAFNOSUPPORT"),
        "AF_VSOCK socket, 32-bit entry": ("accepted", "EAFNOSUPPORT"),
    },
}


def _mismatches(
    seen: dict[str, dict[str, object]], expected: dict[str, dict[str, object]]
) -> list[str]:
    wrong = []
    for where, checks in expected.items():
        for what, wanted in checks.items():
            got = seen[where].get(what)
            if got not in (wanted if isinstance(wanted, tuple) else (wanted,)):
                wrong.append(f"{where}: {what}: expected {wanted!r}, got {got!r}")
    for where, results in seen.items():
        wrong += [
            f"{where}: {what}: there is no expectation for it"
            for what in results
            if what not in expected.get(where, {})
        ]
    return wrong


def main(arguments: list[str]) -> int:
    if platform.machine() != "x86_64":
        print("verify_confinement knows the x86-64 system call entries only", file=sys.stderr)
        return 2
    seen = {"container": in_the_container(Entry32())}
    print(json.dumps(seen, indent=2, sort_keys=True))
    if "--report" in arguments:
        return 0
    wrong = _mismatches(seen, CONTROL if "--control" in arguments else CONFINED)
    for line in wrong:
        print(f"confinement: {line}", file=sys.stderr)
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
