"""SSRF 门禁攻击测试：fetch_url 等工具抓取外部地址前的 URL 校验。

攻击面覆盖：经典内网地址、inet_aton 数字变体、尾部点 / 大小写 / 百分号编码、
userinfo 欺骗、IPv6 及其 IPv4 嵌套形式、协议走私、Unicode 主机名折叠、
DNS 解析结果复检。
"""

import aiohttp
import pytest

from gensokyoai.tools.fetch_url import _GuardedResolver
from gensokyoai.utils.url_security import (
    UnsafeUrlError,
    is_forbidden_address,
    validate_external_url,
)


def _blocked(urls: list[str]) -> None:
    for url in urls:
        try:
            validate_external_url(url)
        except UnsafeUrlError:
            continue
        pytest.fail(f"应拦截: {url}")


def test_validate_blocks_internal_addresses():
    """回环 / 私有网段 / 链路本地（云元数据）/ 内网域全拦"""
    _blocked(
        [
            "http://127.0.0.1:8080/v1",
            "http://localhost/admin",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data",
            "http://metadata.google.internal/",
            "http://nas.internal/",
            "http://[::1]/",
        ]
    )


def test_validate_blocks_inet_aton_variants():
    """inet_aton 数字变体：十进制 / 十六进制 / 八进制 / 少段缩写（系统解析器眼里都是内网 IP）"""
    _blocked(
        [
            "http://2130706433/",  # 127.0.0.1 十进制
            "http://2130706433:8080/v1/chat",  # 带端口打本地模型服务
            "http://0x7f000001/",  # 127.0.0.1 十六进制
            "http://0x7f.0.0.1/",  # 混合进制
            "http://0177.0.0.1/",  # 127.0.0.1 八进制
            "http://127.1/",  # 少段缩写
            "http://127.0.1/",  # 少段缩写
            "http://0/",  # 0.0.0.0
            "http://0.0.0.0/",
            "http://2852039166/",  # 169.254.169.254 十进制（云元数据）
            "http://4294967295/",  # 255.255.255.255 广播
        ]
    )


def test_validate_blocks_trailing_dot_and_case():
    """尾部点 / 大小写变体：`localhost.` 与 `LOCALHOST` 都是 localhost"""
    _blocked(
        [
            "http://localhost./",
            "http://127.0.0.1./",
            "http://nas.internal./",
            "http://metadata.google.internal./",
            "HTTP://LOCALHOST/",
            "http://LoCaLhOsT/",
        ]
    )


def test_validate_blocks_percent_encoded_host():
    """百分号编码主机名（部分客户端会先解码再解析）"""
    _blocked(
        [
            "http://%31%32%37.0.0.1/",  # 127.0.0.1
            "http://%6c%6f%63%61%6c%68%6f%73%74/",  # localhost
        ]
    )


def test_validate_blocks_userinfo():
    """userinfo 欺骗：认证信息位置的字符串不参与寻址，一律拦"""
    _blocked(
        [
            "http://user@127.0.0.1/",
            "http://127.0.0.1@evil.com/",  # 看着像内网实际打 evil.com，但 userinfo 本身就禁
            "http://user:pass@example.com/",
        ]
    )


def test_validate_blocks_ipv6_variants():
    """IPv6 字面量全形态 + 嵌 IPv4 的过渡地址"""
    _blocked(
        [
            "http://[::1]/",  # 回环
            "http://[0:0:0:0:0:0:0:1]/",  # 回环展开形
            "http://[::]/",  # 未指定
            "http://[fe80::1]/",  # 链路本地
            "http://[ff02::1]/",  # 组播
            "http://[::ffff:127.0.0.1]/",  # IPv4 映射
            "http://[::ffff:7f00:1]/",  # IPv4 映射（纯 v6 记法）
            "http://[2002:7f00:1::]/",  # 6to4（内嵌 127.0.0.1）
        ]
    )


def test_validate_blocks_bad_scheme_and_empty():
    """非 http/https 协议与空串全拦"""
    _blocked(
        [
            "",
            "   ",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://x",
            "dict://127.0.0.1:11211/",
            "http+unix://%2Ftmp%2Fsock/",
            "javascript:alert(1)",
            "http://",
            "https:///path-only",
        ]
    )


def test_validate_allows_normal_sites():
    """普通公网域名与公网 IP 字面量放行"""
    validate_external_url("https://thbwiki.cc/西行寺幽幽子")
    validate_external_url("http://example.com:8080/page?a=1")
    validate_external_url("http://8.8.8.8/dns")


def test_validate_blocks_unicode_host_folding():
    """Unicode 主机名绕过：yarl/aiohttp 做 IDNA（NFKC）编码后会落到回环 / 元数据，
    只校验 urlparse 字面主机名会被放行"""
    _blocked(
        [
            "http://127。0。0。1/",  # 表意句号
            "http://１２７.０.０.１/",  # 全角数字
            "http://①②⑦.0.0.1/",  # 圈号数字
            "http://ｌｏｃａｌｈｏｓｔ/",  # 全角 localhost
            "http://169.254.169.254。/latest/meta-data/",  # 元数据 + 尾部表意句号
        ]
    )


def test_validate_allows_internationalized_domains():
    """正常的国际化域名放行（IDNA 编码后是普通公网域名）"""
    validate_external_url("http://例子.测试/")
    validate_external_url("https://xn--fsqu00a.xn--0zwm56d/")


def test_is_forbidden_address_for_resolved_ips():
    """连接层复检：DNS 解析结果落在内网 / 元数据即禁止，公网放行"""
    assert is_forbidden_address("127.0.0.1")
    assert is_forbidden_address("169.254.169.254")
    assert is_forbidden_address("fe80::1%eth0")
    assert is_forbidden_address("::ffff:10.0.0.1")
    assert not is_forbidden_address("8.8.8.8")
    assert not is_forbidden_address("2606:4700:4700::1111")


async def test_guarded_resolver_drops_internal_results(monkeypatch):
    """公网域名解析到内网（泛解析 / DNS rebinding）：连接层丢弃，全被丢则拦截"""

    async def _fake_resolve(self, host, port=0, family=0):
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            },
        ] + (
            [
                {
                    "hostname": host,
                    "host": "93.184.216.34",
                    "port": port,
                    "family": family,
                    "proto": 0,
                    "flags": 0,
                }
            ]
            if host == "mixed.example"
            else []
        )

    monkeypatch.setattr(aiohttp.ThreadedResolver, "resolve", _fake_resolve)
    resolver = _GuardedResolver()
    with pytest.raises(UnsafeUrlError):
        await resolver.resolve("evil.example", 80)
    mixed = await resolver.resolve("mixed.example", 80)
    assert [r["host"] for r in mixed] == ["93.184.216.34"]
