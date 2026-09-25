"""SSRF 门禁攻击测试：fetch_url 等工具抓取外部地址前的 URL 校验。

攻击面覆盖：经典内网地址、inet_aton 数字变体、尾部点 / 大小写 / 百分号编码、
userinfo 欺骗、IPv6 及其 IPv4 嵌套形式、协议走私。
"""

import pytest

from gensokyoai.utils.url_security import UnsafeUrlError, validate_external_url


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
