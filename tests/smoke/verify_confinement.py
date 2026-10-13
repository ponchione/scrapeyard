"""Try, from inside the application's container, what its confinement must refuse.

The container smoke lane pipes this file into the application's container as
the runtime user. It runs the same way against a deployed container:

    docker exec -i <application container> python - < tests/smoke/verify_confinement.py

It opens sockets and closes them again; no packet leaves the process. What
it creates (a link, a queueing discipline and a netfilter table in a network
namespace of its own) ends with the child process that made the namespace.
One thing outlasts it: where an attempt is accepted, the kernel may load the
module behind it (`vsock`, `af_alg`, `dummy` and the like), and a module
stays loaded until the host restarts. Under the application's profile each
of those attempts is refused before that; the one module the attempts ask
for regardless is `nfnetlink`, which a host with Docker's bridge networks
has loaded already. Under a profile that lacks a denial, the attempts that
denial would have refused succeed, whatever arguments this file is given:
it tries everything first and judges afterwards.

`--control` is for a container that runs under a copy of the AppArmor profile
without the lines these checks rest on. There every attempt has to succeed,
or a refusal in the real container would prove nothing about those lines.
`--report` prints what happened and judges nothing.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import mmap
import os
import platform
import socket
import struct
import sys
from collections.abc import Callable

AF_ALG = 38
AF_VSOCK = 40
NETLINK_ROUTE = 0
NETLINK_NETFILTER = 12
CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
CAP_NET_ADMIN = 12
CAP_NET_RAW = 13

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def _refusal(number: int) -> str:
    return errno.errorcode.get(number, f"errno {number}")


def _attempt(action: Callable[[], object]) -> str:
    try:
        answer = action()
    except OSError as error:
        return _refusal(error.errno or 0)
    return answer if isinstance(answer, str) else "accepted"


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


def _netlink(protocol: int, messages: list[tuple[int, int, bytes]]) -> str:
    """Send netlink messages and say how the kernel answered the first of them."""
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, protocol) as channel:
        channel.settimeout(5)
        channel.bind((0, 0))
        request = b""
        for sequence, (kind, flags, payload) in enumerate(messages, start=1):
            length = 16 + len(payload)
            request += struct.pack("=IHHII", length, kind, flags, sequence, 0) + payload
            request += b"\0" * (-length % 4)
        channel.send(request)
        reply = channel.recv(65536)
    _length, kind, _flags, _sequence, _port = struct.unpack_from("=IHHII", reply)
    if kind != 2:  # NLMSG_ERROR carries a refusal, or 0 for an acknowledgement
        return "accepted"
    (code,) = struct.unpack_from("=i", reply, 16)
    return "accepted" if code == 0 else _refusal(-code)


def _attribute(kind: int, value: bytes) -> bytes:
    length = 4 + len(value)
    return struct.pack("=HH", length, kind) + value + b"\0" * (-length % 4)


def _create_netfilter_table() -> str:
    request, acknowledge, create = 0x1, 0x4, 0x400
    nftables = 10  # NFNL_SUBSYS_NFTABLES
    batch = struct.pack("=BBH", socket.AF_UNSPEC, 0, socket.htons(nftables))
    table = struct.pack("=BBH", 1, 0, 0) + _attribute(1, b"confinement-probe\0")  # NFPROTO_INET
    return _netlink(
        NETLINK_NETFILTER,
        [
            (0x10, request, batch),  # NFNL_MSG_BATCH_BEGIN
            (nftables << 8, request | acknowledge | create, table),  # NFT_MSG_NEWTABLE
            (0x11, request, batch),  # NFNL_MSG_BATCH_END
        ],
    )


def _create_link() -> str:
    request, acknowledge, create, exclusive = 0x1, 0x4, 0x400, 0x200
    link = struct.pack("=BxHiII", socket.AF_UNSPEC, 0, 0, 0, 0)
    link += _attribute(3, b"probe0\0")  # IFLA_IFNAME
    link += _attribute(18, _attribute(1, b"dummy\0"))  # IFLA_LINKINFO { IFLA_INFO_KIND }
    return _netlink(
        NETLINK_ROUTE, [(16, request | acknowledge | create | exclusive, link)]  # RTM_NEWLINK
    )


def _add_queueing_discipline() -> str:
    request, acknowledge, create, exclusive = 0x1, 0x4, 0x400, 0x200
    loopback, root, handle = 1, 0xFFFFFFFF, 0x00010000
    discipline = struct.pack("=BxHiIII", socket.AF_UNSPEC, 0, loopback, handle, root, 0)
    discipline += _attribute(1, b"pfifo\0")  # TCA_KIND
    return _netlink(
        NETLINK_ROUTE, [(36, request | acknowledge | create | exclusive, discipline)]  # RTM_NEWQDISC
    )


def _list_links() -> str:
    request, dump = 0x1, 0x300
    link = struct.pack("=BxHiII", socket.AF_UNSPEC, 0, 0, 0, 0)
    return _netlink(NETLINK_ROUTE, [(18, request | dump, link)])  # RTM_GETLINK


def _raise_loopback() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        name = struct.pack("16sH22x", b"lo", 0)
        (flags,) = struct.unpack_from("H", fcntl.ioctl(control, 0x8913, name), 16)  # SIOCGIFFLAGS
        fcntl.ioctl(control, 0x8914, struct.pack("16sH22x", b"lo", flags | 1))  # SIOCSIFFLAGS


def _holds(capability: int) -> bool:
    with open("/proc/self/status", encoding="utf-8") as status:
        effective = next(line.split()[1] for line in status if line.startswith("CapEff:"))
    return bool(int(effective, 16) >> capability & 1)


def _in_own_namespaces(entry: Entry32) -> dict[str, object]:
    seen: dict[str, object] = {}
    if _libc.unshare(CLONE_NEWUSER | CLONE_NEWNET) != 0:
        seen["own user and network namespace"] = _refusal(ctypes.get_errno())
        return seen
    seen["own user and network namespace"] = "accepted"
    # The kernel does hand them over. A host that gave the namespace nothing
    # would refuse what follows by itself.
    seen["holds CAP_NET_ADMIN and CAP_NET_RAW there"] = _holds(CAP_NET_ADMIN) and _holds(
        CAP_NET_RAW
    )
    seen["packet socket"] = direct_socket(socket.AF_PACKET, socket.SOCK_RAW)
    seen["packet socket, 32-bit entry"] = entry.socket(socket.AF_PACKET, socket.SOCK_RAW)
    seen["raw IP socket"] = direct_socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    seen["netfilter: create a table"] = _attempt(_create_netfilter_table)
    seen["route netlink: create a link"] = _attempt(_create_link)
    seen["traffic control: add a queueing discipline"] = _attempt(_add_queueing_discipline)
    seen["raise the loopback interface"] = _attempt(_raise_loopback)
    # What has to keep working there: the browsers read the route table, and
    # their sandboxes call chroot inside the namespace.
    seen["route netlink: list the links"] = _attempt(_list_links)
    seen["chroot"] = _attempt(lambda: os.chroot("/"))
    return seen


def in_own_namespaces(entry: Entry32) -> dict[str, object]:
    """What a process can do once it has given itself a user and a network namespace.

    The kernel gives it every capability there, whatever the container's
    bounding set holds, and the namespace's network stack is its own. Run in
    a child process, so the namespaces end with it.
    """
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        seen: dict[str, object] = {}
        try:
            seen = _in_own_namespaces(entry)
        except BaseException as error:  # the parent has to hear of it
            seen = {"the attempts themselves": repr(error)}
        finally:
            os.write(writer, json.dumps(seen).encode())
            os._exit(0)
    os.close(writer)
    with os.fdopen(reader, "rb") as pipe:
        payload = pipe.read()
    os.waitpid(child, 0)
    answers: dict[str, object] = json.loads(payload)
    return answers


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
    "own_namespaces": {
        "own user and network namespace": "accepted",
        "holds CAP_NET_ADMIN and CAP_NET_RAW there": True,
        # AppArmor denies the family by name, whichever entry asks.
        "packet socket": "EACCES",
        "packet socket, 32-bit entry": "EACCES",
        # And it denies the two capabilities, which the kernel reports as EPERM.
        "raw IP socket": "EPERM",
        "netfilter: create a table": "EPERM",
        "route netlink: create a link": "EPERM",
        "traffic control: add a queueing discipline": "EPERM",
        "raise the loopback interface": "EPERM",
        "route netlink: list the links": "accepted",
        "chroot": "accepted",
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
    "own_namespaces": {
        "own user and network namespace": "accepted",
        "holds CAP_NET_ADMIN and CAP_NET_RAW there": True,
        "packet socket": "accepted",
        "packet socket, 32-bit entry": "accepted",
        "raw IP socket": "accepted",
        "netfilter: create a table": "accepted",
        # EOPNOTSUPP is a kernel without the dummy link type, asked after the
        # capability was accepted.
        "route netlink: create a link": ("accepted", "EOPNOTSUPP"),
        "traffic control: add a queueing discipline": "accepted",
        "raise the loopback interface": "accepted",
        "route netlink: list the links": "accepted",
        "chroot": "accepted",
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
    entry = Entry32()
    seen = {"container": in_the_container(entry), "own_namespaces": in_own_namespaces(entry)}
    print(json.dumps(seen, indent=2, sort_keys=True))
    if "--report" in arguments:
        return 0
    wrong = _mismatches(seen, CONTROL if "--control" in arguments else CONFINED)
    for line in wrong:
        print(f"confinement: {line}", file=sys.stderr)
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
