""" 命令权限模型：数值越大权限越高，放行要求 调用方等级 >= 命令所需等级 """

from enum import IntEnum


class PermissionLevel(IntEnum):
    """ 四级权限：VISITOR < USER < ADMIN < OWNER """

    VISITOR = 0  # 无法核实身份——最低信任级
    USER = 1     # 普通用户（群成员 / 私聊用户）
    ADMIN = 2    # 平台管理员（QQ 群管理 / 群主）
    OWNER = 3    # bot 主人（默认级：未声明权限的命令只有主人可用）