"""URL 安全校验（SSRF 防护）—— 工具抓取外部地址前的门禁。

默认禁止：非 http/https、回环、私有网段、链路本地 / 云元数据、空主机名。

只做字面校验，**不解析 DNS**（避免在校验路径引入网络调用）；「域名解析后
指向内网」的场景由部署方网络层兜底（老项目的 350 行全量版含异步 DNS 解析，
这里按重写版的体量只保留核心门禁）。
"""

from ipaddress import ip_address
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """URL 未通过安全校验"""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"{reason}: {url!r}")


_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",  # GCP 元数据服务
    }
)

_BLOCKED_SUFFIXES = (".internal", ".local", ".lan", ".home.arpa")
""" 内网常用域后缀 """


def validate_external_url(url: str) -> None:
    """校验 URL 是否允许抓取。

    Args:
        url: 待校验 URL

    Raises:
        UnsafeUrlError: 校验失败（reason 说明原因）
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError(str(url), "URL 不能为空")

    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError(url, f"不支持的协议: {scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeUrlError(url, "无法解析主机名")
    hostname = hostname.lower()

    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeUrlError(url, f"内网主机名: {hostname}")

    try:
        ip = ip_address(hostname)
    except ValueError:
        return  # 普通域名：字面校验通过（不解析 DNS，见模块 docstring）
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise UnsafeUrlError(url, f"不允许访问的地址: {hostname}")


__all__ = ["UnsafeUrlError", "validate_external_url"]
