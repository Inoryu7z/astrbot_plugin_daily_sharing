"""人格配置共享工具。"""

from typing import Any


class PersonaConfigMixin:
    """为依赖 self.config 的组件提供统一的人格配置解析能力。"""

    config: dict

    def _normalize_persona_name(self, persona_name: str | None) -> str | None:
        if persona_name is None:
            return None
        value = str(persona_name).strip()
        return value or None

    def _normalize_persona_token(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return "".join(text.lower().split())

    def _persona_entries(self) -> list[dict[str, Any]]:
        raw = self.config.get("personas", [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _persona_aliases(self, item: dict[str, Any]) -> list[str]:
        aliases: list[str] = []
        for candidate in [
            item.get("persona_name"),
            item.get("alias"),
            item.get("aliases"),
            item.get("persona_id"),
            item.get("display_name"),
            item.get("select_persona"),
            item.get("name"),
        ]:
            if isinstance(candidate, list):
                aliases.extend([str(x).strip() for x in candidate if str(x).strip()])
            elif candidate and str(candidate).strip():
                aliases.append(str(candidate).strip())
        dedup: list[str] = []
        seen: set[str] = set()
        for alias in aliases:
            token = self._normalize_persona_token(alias)
            if token and token not in seen:
                dedup.append(alias)
                seen.add(token)
        return dedup

    def _find_persona_config(self, persona_name: str | None) -> dict[str, Any] | None:
        target = self._normalize_persona_token(persona_name)
        if not target:
            return None
        for item in self._persona_entries():
            for alias in self._persona_aliases(item):
                if self._normalize_persona_token(alias) == target:
                    return item
        return None

    def _canonical_persona_name(self, persona_name: str | None) -> str | None:
        item = self._find_persona_config(persona_name)
        if item is None:
            return self._normalize_persona_name(persona_name)
        return self._normalize_persona_name(item.get("persona_name") or item.get("name") or item.get("select_persona"))

    def _persona_value(self, persona_name: str | None, key: str, default=None):
        item = self._find_persona_config(persona_name)
        if item is not None and key in item and item.get(key) is not None:
            return item.get(key)
        return self.config.get(key, default)


__all__ = ["PersonaConfigMixin"]
