import ctypes.util
import logging
import os
import socket
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    c_char_p,
    c_int,
    c_uint,
    c_uint8,
    c_void_p,
    pointer,
)
from sys import platform
from typing import Any, Dict, List, Optional

# return MAC addresses under our own constant, because
# on macos socket.AF_PACKET is not defined, and the value
# differs by platform
AF_PACKET = 17

if platform == "linux" or platform == "linux2":

    class Sockaddr(ctypes.Structure):
        _fields_ = [
            ("sa_family", ctypes.c_uint16),
            ("sa_data", ctypes.c_uint8 * (2 + 4 + 16)),
        ]

    ipv4_addr_data_offset = 2

elif platform == "darwin":

    class Sockaddr(Structure):  # type: ignore [no-redef]
        _fields_ = [
            ("sa_len", c_uint8),
            ("sa_family", c_uint8),
            ("sa_data", c_uint8 * 24),
        ]

    ipv4_addr_data_offset = 2
else:
    raise ValueError("Unsupported platform")


class Ifaddrs(Structure):
    pass


Ifaddrs._fields_ = [
    ("ifa_next", POINTER(Ifaddrs)),
    ("ifa_name", c_char_p),
    ("ifa_flags", c_uint),
    ("ifa_addr", POINTER(Sockaddr)),
    ("ifa_netmask", POINTER(Sockaddr)),
    ("ifa_broadaddr", POINTER(Sockaddr)),
    ("ifa_data", c_void_p),
]


def getifaddrs(
    ifname: Optional[str] = None, family: Optional[int] = None
) -> List[Dict[str, Any]]:
    libc = CDLL(
        ctypes.util.find_library("socket" if os.uname()[0] == "SunOS" else "c"),
        use_errno=True,
    )
    libc.getifaddrs.restype = c_int
    ifaddr_p = pointer(Ifaddrs())
    ret = libc.getifaddrs(pointer((ifaddr_p)))
    if ret != 0:
        raise ValueError("getifaddrs nonzero return code")
    addrs = []
    head = ifaddr_p
    while ifaddr_p:
        name = str(ifaddr_p.contents.ifa_name.decode())
        netmask = None
        broadaddr = None
        addr = None
        fam = ifaddr_p.contents.ifa_addr.contents.sa_family
        d = {"name": name, "family": fam}
        if ifaddr_p.contents.ifa_broadaddr:
            broadaddr = bytes(ifaddr_p.contents.ifa_broadaddr.contents.sa_data)
        if ifaddr_p.contents.ifa_netmask:
            netmask = bytes(ifaddr_p.contents.ifa_netmask.contents.sa_data)
        if ifaddr_p.contents.ifa_addr:
            addr = bytes(ifaddr_p.contents.ifa_addr.contents.sa_data)

        if fam == socket.AF_INET:
            if addr:
                d["addr"] = socket.inet_ntoa(addr[2:6])
            if netmask:
                d["netmask"] = socket.inet_ntoa(netmask[2:6])
            if broadaddr:
                d["broadaddr"] = socket.inet_ntoa(broadaddr[2:6])
        elif fam == socket.AF_INET6:
            if addr:
                d["addr"] = socket.inet_ntop(fam, addr[6:22])
            if netmask:
                d["netmask"] = socket.inet_ntop(fam, netmask[6:22])
        elif fam == 17:  # linux MAC addr
            if addr:
                d["addr"] = addr[10:16].hex()
            if broadaddr:
                d["broadaddr"] = broadaddr[10:16].hex()
            fam = AF_PACKET
        elif fam == 18:  # macos MAC addr
            if addr:
                d["addr"] = addr[9:15].hex()
            fam = AF_PACKET
        logging.debug(f"getifaddrs {d}")

        ifaddr_p = ifaddr_p.contents.ifa_next

        if ifname is None or ifname in name:
            if family is None or fam == family:
                addrs.append(d)

    libc.freeifaddrs(head)
    return addrs


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print(getifaddrs())
