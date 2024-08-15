import ctypes.util
import os
import socket
from sys import platform

from ctypes import (
    CDLL,
    POINTER,
    Structure,
    c_char,
    c_char_p,
    c_int,
    c_uint,
    c_uint8,
    c_ushort,
    c_void_p,
    pointer,
)

if platform == "linux" or platform == "linux2":
    class Sockaddr(ctypes.Structure):
        _fields_ = [
            ('sa_family', ctypes.c_uint16),
            ('sa_data', ctypes.c_uint8 * 14)
        ]
    ipv4_addr_data_offset = 2
elif platform == "darwin":
    class Sockaddr(Structure):
        _fields_ = [
            ("sa_len", c_uint8),
            ("sa_family", c_uint8),
            ("sa_data", c_uint8*14),
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


def get_interfaces():
    libc = CDLL(
        ctypes.util.find_library("socket" if os.uname()[0] == "SunOS" else "c"),
        use_errno=True,
    )
    libc.getifaddrs.restype = c_int
    ifaddr_p = pointer(Ifaddrs())
    ret = libc.getifaddrs(pointer((ifaddr_p)))
    if ret != 0:
        raise ValueError("getifaddrs nonzero return code")
    interfaces = set()
    head = ifaddr_p
    while ifaddr_p:
        interfaces.add(ifaddr_p.contents.ifa_name)
        netmask = None
        broadaddr = None
        addr = None
        fam = -1
        if ifaddr_p.contents.ifa_broadaddr:
            broadaddr = bytes(ifaddr_p.contents.ifa_broadaddr.contents.sa_data)
        if ifaddr_p.contents.ifa_netmask:
            netmask = bytes(ifaddr_p.contents.ifa_netmask.contents.sa_data)
        if ifaddr_p.contents.ifa_addr:
            addr = bytes(ifaddr_p.contents.ifa_addr.contents.sa_data)
            fam = ifaddr_p.contents.ifa_addr.contents.sa_family
        #print(
        #    f"{ifaddr_p.contents.ifa_name}  fam={fam} addr={addr} bcast={broadaddr} mask={netmask}"
        #)
        if fam == 2:
            addr = socket.inet_ntoa(addr[2:6])
            netmask = socket.inet_ntoa(netmask[2:6])
            broadaddr = socket.inet_ntoa(broadaddr[2:6])

            print(
                f"{ifaddr_p.contents.ifa_name} IPV4 fam={fam} addr={addr} bcast={broadaddr} mask={netmask}"
            )

        ifaddr_p = ifaddr_p.contents.ifa_next
    libc.freeifaddrs(head)
    return interfaces


if __name__ == "__main__":
    print(get_interfaces())
