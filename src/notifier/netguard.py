"""目标地址防护：拒绝内网与元数据地址，防止本服务成为 SSRF 跳板。

受理时校验一次；投递时（change B）还会再校验，因为 DNS 可能在受理之后发生变化。
"""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

Resolver = Callable[[str], list[str]]


class BlockedTarget(Exception):
    pass


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 含 169.254.169.254
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def check_target(url: str, resolver: Resolver = system_resolver) -> None:
    """目标被禁止时抛出 BlockedTarget。域名解析失败时放行，交给投递侧再判。"""
    host = urlsplit(url).hostname
    if host is None:
        raise BlockedTarget("url 缺少 host")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if is_blocked_ip(literal):
            raise BlockedTarget(f"目标地址 {host} 属于内网或保留地址")
        return

    try:
        addresses = resolver(host)
    except (OSError, UnicodeError):
        # 临时 DNS 故障不应变成业务失败（design D4）
        return
    for addr in addresses:
        if is_blocked_ip(ipaddress.ip_address(addr.split("%", 1)[0])):
            raise BlockedTarget(f"目标域名 {host} 解析到内网或保留地址 {addr}")
