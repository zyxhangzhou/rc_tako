import socket

import pytest

from notifier.netguard import BlockedTarget, check_target


def resolver_returning(*addrs):
    def resolve(host):
        return list(addrs)
    return resolve


def failing_resolver(host):
    raise socket.gaierror("temporary failure in name resolution")


def must_not_resolve(host):
    raise AssertionError("IP 字面量不应触发 DNS 解析")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
    ],
)
def test_blocked_ip_literals(url):
    with pytest.raises(BlockedTarget):
        check_target(url, resolver=must_not_resolve)


def test_public_ip_literal_allowed():
    check_target("https://93.184.216.34/hook", resolver=must_not_resolve)


def test_domain_resolving_to_private_rejected():
    with pytest.raises(BlockedTarget):
        check_target("https://internal.example.com/", resolver=resolver_returning("10.0.0.5"))


def test_domain_with_any_private_address_rejected():
    with pytest.raises(BlockedTarget):
        check_target("https://mixed.example.com/", resolver=resolver_returning("93.184.216.34", "10.0.0.5"))


def test_domain_resolving_to_mapped_private_ipv6_rejected():
    with pytest.raises(BlockedTarget):
        check_target("https://v6.example.com/", resolver=resolver_returning("::ffff:127.0.0.1"))


def test_domain_resolution_failure_allowed():
    check_target("https://flaky.example.com/", resolver=failing_resolver)


def test_domain_resolving_to_public_only_allowed():
    check_target(
        "https://api.example.com/", resolver=resolver_returning("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    )
