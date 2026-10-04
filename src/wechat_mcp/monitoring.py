"""会话关注/忽略列表。

底层监听是全局的（会收到所有会话的消息）。本模块只在 **MCP 工具的读取结果**
层面做过滤，不改变底层监听行为。

语义约定：

- ``mode="allow"``：白名单模式。``allow`` 为空时表示“关注全部会话”。
- ``mode="block"``：黑名单模式。``block`` 中的会话被忽略。
- ``block`` 优先级高于 ``allow``：只要命中黑名单，一律过滤。
- ``add`` 加入当前模式对应的列表；``remove`` 同时从两个列表移除。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import TargetInvalidError

VALID_MODES = ("allow", "block")


def _normalize(items) -> list[str]:
    """去空、去重并保持输入顺序。"""
    result: list[str] = []
    if items is None:
        return result
    source = [items] if isinstance(items, str) else list(items)
    for item in source:
        value = str(item or "").strip()
        if value and value not in result:
            result.append(value)
    return result


@dataclass
class MonitoredChats:
    """关注/忽略列表及其过滤规则。"""

    mode: str = "allow"
    allow: list[str] = field(default_factory=list)
    block: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config) -> "MonitoredChats":
        mode = str(getattr(config, "monitor_mode", "allow")).strip().lower()
        if mode not in VALID_MODES:
            mode = "allow"
        return cls(
            mode=mode,
            allow=_normalize(getattr(config, "monitor_allow", ())),
            block=_normalize(getattr(config, "monitor_block", ())),
        )

    def is_allowed(self, chat: object) -> bool:
        """判断某会话是否允许出现在读取结果中。"""
        name = str(chat or "").strip()
        if not name:
            return True
        if name in self.block:
            return False
        if self.mode == "allow":
            # 白名单为空 = 关注全部
            return not self.allow or name in self.allow
        return True

    def apply(
        self,
        add=None,
        remove=None,
        mode: str | None = None,
    ) -> "MonitoredChats":
        """运行时增删条目或切换模式，返回自身便于链式调用。"""
        if mode is not None:
            value = str(mode).strip().lower()
            if value not in VALID_MODES:
                raise TargetInvalidError(
                    f"mode 只能是 {VALID_MODES[0]} 或 {VALID_MODES[1]}",
                    detail={"mode": str(mode)},
                )
            self.mode = value

        for name in _normalize(add):
            if self.mode == "block":
                if name not in self.block:
                    self.block.append(name)
                if name in self.allow:
                    self.allow.remove(name)
            else:
                if name not in self.allow:
                    self.allow.append(name)
                if name in self.block:
                    self.block.remove(name)

        for name in _normalize(remove):
            if name in self.allow:
                self.allow.remove(name)
            if name in self.block:
                self.block.remove(name)

        return self

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "allow": list(self.allow),
            "block": list(self.block),
        }