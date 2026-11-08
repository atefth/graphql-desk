"""Prove the Ubuntu package verification command starts without network egress."""

import argparse
import errno
import os
import socket
from ipaddress import IPv4Address
from pathlib import Path

REGISTRY = "registry.npmjs.org"
PORT = 443
NET_NAMESPACE = "/proc/self/ns/net"
CAPABILITY_SETS = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")


def _public_address(value: str) -> IPv4Address:
    address = IPv4Address(value)
    if not address.is_global:
        raise ValueError("The npm registry control must use a public IPv4 address")
    return address


def _connect(address: IPv4Address) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect((str(address), PORT))


def _observe() -> None:
    address = _public_address(socket.gethostbyname(REGISTRY))
    _connect(address)
    print(os.readlink(NET_NAMESPACE), address)


def _verify_isolation(
    original_namespace: str, address: IPv4Address, uid: int, gid: int
) -> None:
    namespace = os.readlink(NET_NAMESPACE)
    if namespace == original_namespace:
        raise ValueError("Package verification still has the runner network namespace")
    status = {
        key: value.strip()
        for key, _, value in (
            line.partition(":")
            for line in Path("/proc/self/status")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    }
    if (
        uid <= 0
        or gid <= 0
        or status["Uid"].split() != [str(uid)] * 4
        or status["Gid"].split() != [str(gid)] * 4
        or status["Groups"]
        or status["NoNewPrivs"] != "1"
        or any(int(status[key], 16) for key in CAPABILITY_SETS)
    ):
        raise ValueError("Package verification did not drop runner privileges")
    if [name for _, name in socket.if_nameindex()] != ["lo"]:
        raise ValueError("Package verification has a network interface beyond loopback")
    try:
        _connect(address)
    except OSError as error:
        if error.errno != errno.ENETUNREACH:
            raise
    else:
        raise ValueError("The npm registry remains reachable inside the namespace")
    print(
        f"Verified isolated package run in {namespace} (was {original_namespace}) "
        f"as {uid}:{gid} with no capabilities "
        f"or new privileges. {REGISTRY} ({address} port {PORT}) is unreachable.",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("observe", help="Verify the registry is reachable on the runner")
    isolated = modes.add_parser("run", help="Verify isolation before running a command")
    isolated.add_argument("namespace")
    isolated.add_argument("address", type=_public_address)
    isolated.add_argument("uid", type=int)
    isolated.add_argument("gid", type=int)
    isolated.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.mode == "observe":
        _observe()
        return
    if not args.command or not os.path.isabs(args.command[0]):
        parser.error("An absolute verification command is required")
    _verify_isolation(args.namespace, args.address, args.uid, args.gid)
    os.execv(args.command[0], args.command)


if __name__ == "__main__":
    main()
