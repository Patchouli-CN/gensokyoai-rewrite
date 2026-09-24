"""SSRF 门禁测试：fetch_url 等工具抓取外部地址前的 URL 校验"""

import pytest

from gensokyoai.utils.url_security import UnsafeUrlError, validate_external_url


def test_validate_blocks_internal_addresses():
    """回环 / 私有网段 / 链路本地（云元数据）/ 内网域全拦"""
    for url in [
        "http://127.0.0.1:8080/v1",
        "http://localhost/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/",
        "http://nas.internal/",
        "http://[::1]/",
    ]:
        with pytest.raises(UnsafeUrlError):
            validate_external_url(url)


def test_validate_blocks_bad_scheme_and_empty():
    """非 http/https 协议与空串全拦"""
    for url in ["", "   ", "file:///etc/passwd", "ftp://example.com/x", "gopher://x"]:
        with pytest.raises(UnsafeUrlError):
            validate_external_url(url)


def test_validate_allows_normal_sites():
    """普通公网域名放行"""
    validate_external_url("https://thbwiki.cc/西行寺幽幽子")
    validate_external_url("http://example.com:8080/page?a=1")
