"""URL 安全校验（SSRF 防护）—— 工具抓取外部地址前的门禁。

默认禁止：非 http/https、回环、私有网段、链路本地 / 云元数据、空主机名、
userinfo 欺骗、内网域后缀。

**不解析 DNS**（避免在校验路径引入网络调用），但覆盖 inet_aton 语义的
数字变体（十进制 `2130706433`、十六进制 `0x7f000001`、八进制 `0177.0.0.1`、
缩写 `127.1`——这些在系统解析器眼里都是 127.0.0.1）。
「域名解析后指向内网」（DNS rebinding / 泛解析域）由部署方网络层兜底。
"""

import re
from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import unquote, urlparse


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


def _normalize_host(hostname: str) -> str:
    """归一化主机名：解百分号编码、去尾部点（`localhost.`）、小写。"""
    return unquote(hostname).rstrip(".").lower()


_INET_ATON_CHARS = re.compile(r"[0-9a-fA-FxX.]+")
""" inet_aton 候选字符集：纯数字/点/十六进制记号才可能是数字 IP 变体 """


def _parse_inet_aton(host: str) -> IPv4Address | None:
    """按 inet_aton 语义解析数字 IP 变体；不是数字变体返回 None。

    覆盖十进制（`2130706433`）、十六进制（`0x7f000001`）、八进制（`0177.0.0.1`）、
    少段缩写（`127.1`——末段按剩余字节数填充）。纯 Python 实现，与平台无关
    （Windows 的 socket.inet_aton 会把 255.255.255.255 误判为非法）。
    """
    if not _INET_ATON_CHARS.fullmatch(host):
        return None
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or any(not p for p in parts):
        return None
    nums: list[int] = []
    for p in parts:
        try:
            if p.lower().startswith("0x"):
                nums.append(int(p, 16))
            elif len(p) > 1 and p.startswith("0"):
                nums.append(int(p, 8))
            else:
                nums.append(int(p, 10))
        except ValueError:
            return None
    *head, last = nums
    if any(n > 0xFF for n in head):
        return None
    remaining_bytes = 5 - len(nums)  # 末段占的字节数（全写 1，单段 4）
    if last >= 1 << (8 * remaining_bytes):
        return None
    value = 0
    for n in head:
        value = (value << 8) | n
    value = (value << (8 * remaining_bytes)) | last
    return IPv4Address(value)


def _as_ip(host: str) -> IPv4Address | IPv6Address | None:
    """把主机名当 IP 解析：标准字面量 + inet_aton 数字变体；普通域名返回 None。"""
    try:
        return ip_address(host)
    except ValueError:
        pass
    return _parse_inet_aton(host)


def _is_forbidden_ip(ip: IPv4Address | IPv6Address) -> bool:
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    # 嵌了 IPv4 的 IPv6：实际目的地是里面的 v4，一律拦（URL 里没有正当用途）
    return isinstance(ip, IPv6Address) and (
        ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None
    )


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

    if parsed.username or parsed.password:
        raise UnsafeUrlError(url, "不允许携带 userinfo（认证信息）")

    if not parsed.hostname:
        raise UnsafeUrlError(url, "无法解析主机名")
    hostname = _normalize_host(parsed.hostname)
    if not hostname:
        raise UnsafeUrlError(url, "无法解析主机名")

    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeUrlError(url, f"内网主机名: {hostname}")

    ip = _as_ip(hostname)
    if ip is None:
        return  # 普通域名：字面校验通过（不解析 DNS，见模块 docstring）
    if _is_forbidden_ip(ip):
        raise UnsafeUrlError(url, f"不允许访问的地址: {hostname}")


__all__ = ["UnsafeUrlError", "validate_external_url"]
