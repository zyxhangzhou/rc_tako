"""目标地址防护：拒绝内网与元数据地址，防止本服务成为 SSRF 跳板。

受理时校验一次；投递时（change B）还会再校验，因为 DNS 可能在受理之后发生变化。
"""

import ipaddress
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import urlsplit

Resolver = Callable[[str], list[str]]

RESOLVE_TIMEOUT_S = 2.0
# getaddrinfo 无法取消，超时后线程会在后台跑完；池有界，DNS 全面变慢时也不会无限堆线程
_resolve_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dns")


class BlockedTarget(Exception):
    pass


class ResolveTimeout(OSError):
    pass


class TargetUnresolvable(Exception):
    """严格模式下解析失败或超时：可重试的临时错误。"""


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


def with_timeout(resolver: Resolver, timeout_s: float = RESOLVE_TIMEOUT_S) -> Resolver:
    def resolve(host: str) -> list[str]:
        future = _resolve_pool.submit(resolver, host)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout:
            raise ResolveTimeout(f"解析 {host} 超过 {timeout_s}s") from None

    return resolve


default_resolver: Resolver = with_timeout(system_resolver)


def check_target(url: str, resolver: Resolver = default_resolver, strict: bool = False) -> None:
    """目标被禁止时抛出 BlockedTarget。

    域名解析失败或超时：受理侧（strict=False）放行，交给投递侧再判；
    投递侧（strict=True）抛出 TargetUnresolvable，按临时失败重试。
    """
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
    except (OSError, UnicodeError) as e:
        if strict:
            raise TargetUnresolvable(f"无法解析 {host}: {e}") from None
        # 临时 DNS 故障不应变成业务失败（change A design D4）
        return
    for addr in addresses:
        if is_blocked_ip(ipaddress.ip_address(addr.split("%", 1)[0])):
            raise BlockedTarget(f"目标域名 {host} 解析到内网或保留地址 {addr}")
