"""工具的站点配置 —— `config/tools/<工具名>/sites.yaml`。

不配就是全放行；配了 `allow` 就只认名单里的域名。域名按后缀匹配，
写 `thwiki.cc` 会同时命中 `www.thwiki.cc`。
"""

from pathlib import Path

import msgspec
import yaml


def _repo_root() -> Path:
    """从包目录往上找仓库根（有 pyproject.toml 的那层）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


DEFAULT_ROOT = _repo_root() / "config" / "tools"
""" 默认的工具配置目录 """


class ToolSites(msgspec.Struct, frozen=True):
    """一个工具的域名规则。"""

    allow: tuple[str, ...] = ()
    """ 只认这些域名（空 = 不限） """
    prefer: tuple[str, ...] = ()
    """ 搜到的结果里这些域名排前面 """
    block: tuple[str, ...] = ()
    """ 这些域名一律不要 """

    def matches(self, url: str, names: tuple[str, ...]) -> bool:
        """URL 的域名是否命中名单里的某个后缀。"""
        host = _host_of(url)
        return any(host == name or host.endswith("." + name) for name in names)

    def allows(self, url: str) -> bool:
        """这个 URL 允不允许用。"""
        if self.block and self.matches(url, self.block):
            return False
        if self.allow and not self.matches(url, self.allow):
            return False
        return True

    def prefers(self, url: str) -> bool:
        """是不是优先域名。"""
        return self.rank(url) is not None

    def rank(self, url: str) -> int | None:
        """优先域名里的序号（越靠前越优先）；不是优先域名返回 None。"""
        host = _host_of(url)
        for index, name in enumerate(self.prefer):
            if host == name or host.endswith("." + name):
                return index
        return None


def _host_of(url: str) -> str:
    """取域名（去掉协议、路径、端口）。"""
    rest = url.split("://", 1)[-1]
    host = rest.split("/", 1)[0].split("?", 1)[0]
    return host.split("@")[-1].split(":")[0].lower()


def tools_root(explicit: str | Path | None = None) -> Path:
    """工具配置根目录：显式给的优先，否则仓库根的 config/tools。

    Args:
        explicit: 命令行/环境变量指定的位置

    Returns:
        Path: 配置目录（可能不存在）
    """
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser()
    return DEFAULT_ROOT


def load_sites(tool: str, root: str | Path | None = None) -> ToolSites:
    """读一个工具的站点规则；没有配置文件就全放行。

    Args:
        tool: 工具名（对应 `config/tools/<工具名>/sites.yaml`）
        root: 配置根目录（None = 默认位置）

    Returns:
        ToolSites: 规则

    Raises:
        ValueError: YAML 不是 mapping 或字段类型不对
    """
    path = tools_root(root) / tool / "sites.yaml"
    if not path.exists():
        return ToolSites()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"站点配置不是 mapping: {path}")
    try:
        return msgspec.convert(raw, ToolSites, strict=False)
    except msgspec.ValidationError as err:
        raise ValueError(f"站点配置字段不对（{path}）: {err}") from err
