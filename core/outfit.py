"""dayflow 日程穿搭解析

dayflow 的穿搭约定（见 dayflow core/constants.py）：
- 顶层 ``outfit`` 字段 = **晨起第一套**穿搭（从头到脚完整描述）
- timeline 各时段的 ``outfit_change`` = 该时段换上的那套（不换装时为 null）
- 带换装的时段里，**最后一个**是夜间居家装（第三套）；只有一个换装时段时说明当天没有第三套

本模块把上述结构解析为"某个时刻 / 某一套"生效的穿搭描述，
供文案生成、配图视觉导演、智能分享调度共用。
"""
import datetime
from typing import List, Optional

# 智能分享使用的 look 身份标识（与 smart_share.py 的 look_1/look_2/look_3 一致）
LOOK_KEYS = ("look_1", "look_2", "look_3")


def parse_hhmm(value) -> Optional[int]:
    """把 "08:30" 解析为一天中的分钟数；解析失败返回 None"""
    try:
        h, m = map(int, str(value).strip().split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return None


def collect_outfit_changes(timeline) -> List[dict]:
    """按 time_start 升序返回所有换装时段（仅含 outfit_change 非空的项）

    每项为 ``{"minutes", "index", "time_start", "time_end", "title", "outfit"}``。
    time_start 缺失或无法解析的项排在最后（按原顺序稳定）。
    """
    changes = []
    for idx, item in enumerate(timeline or []):
        if not isinstance(item, dict):
            continue
        outfit = str(item.get("outfit_change") or "").strip()
        if not outfit:
            continue
        changes.append({
            "index": idx,
            "minutes": parse_hhmm(item.get("time_start")),
            "time_start": str(item.get("time_start") or "").strip(),
            "time_end": str(item.get("time_end") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "outfit": outfit,
        })
    changes.sort(key=lambda c: (c["minutes"] is None, c["minutes"] or 0))
    return changes


def resolve_outfit_by_look(data: dict, look_key: str) -> str:
    """按 look 身份取穿搭（智能分享用，不受执行时刻漂移影响）

    - look_1：晨间第一套（顶层 outfit）
    - look_2：第一个换装时段的那套
    - look_3：最后一个换装时段的那套（需至少两个换装时段，与 dayflow 夜间款判定一致）
    """
    data = data or {}
    base = str(data.get("outfit") or "").strip()
    changes = collect_outfit_changes(data.get("timeline"))

    if look_key == "look_1":
        return base
    if look_key == "look_2":
        return changes[0]["outfit"] if changes else base
    if look_key == "look_3":
        if len(changes) >= 2:
            return changes[-1]["outfit"]
        return changes[0]["outfit"] if changes else base
    return base


def resolve_current_outfit(data: dict, look_key: str = None, now=None) -> str:
    """解析"此刻 / 指定套"生效的穿搭

    look_key 有效时按身份取（智能分享路径，补偿触发导致真实时间漂移也不会穿错套）；
    否则按当前时间推断：取最后一个 time_start <= now 的换装，
    若当前时间早于任何换装则回退晨间第一套（顶层 outfit）。
    """
    if look_key in LOOK_KEYS:
        return resolve_outfit_by_look(data, look_key)

    data = data or {}
    base = str(data.get("outfit") or "").strip()
    changes = collect_outfit_changes(data.get("timeline"))
    if not changes:
        return base

    now = now or datetime.datetime.now()
    now_mins = now.hour * 60 + now.minute
    picked = ""
    for change in changes:  # 已按 time_start 升序，最后一次命中即为当前生效的那套
        if change["minutes"] is not None and change["minutes"] <= now_mins:
            picked = change["outfit"]
    return picked or base


def find_current_slot(timeline, now=None) -> Optional[dict]:
    """找当前所处的时间轴时段

    优先用 ``time_start <= now < time_end`` 区间匹配；若时间落在所有区间之外
    （例如 time_end 缺失或跨日），回退为"最后一个 time_start <= now"的项。
    凌晨等早于首段的时刻返回 None。
    """
    now = now or datetime.datetime.now()
    now_mins = now.hour * 60 + now.minute

    fallback = None
    for item in timeline or []:
        if not isinstance(item, dict):
            continue
        start = parse_hhmm(item.get("time_start"))
        end = parse_hhmm(item.get("time_end"))
        if start is None:
            continue
        if start <= now_mins:
            fallback = item
        if end is not None and start <= now_mins < end:
            return item
    return fallback
