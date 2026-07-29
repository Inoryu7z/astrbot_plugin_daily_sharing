"""智能分享调度器

核心思路：每日 6 点让 LLM 读取 dayflow 当日日程，识别两套穿搭各自的穿着时段（wear_window），
然后在 wear_window 内**随机**选一个分享时间点（带合适时段轻微加权 + 跨人格同 umo 防冲突），
再用这两个时间点注册 date 任务，复用现有 execute_share 链路。
其他一切（aiimg 配图、视觉导演、内容生成、QQ空间调度）完全不动。

失败回退：LLM 超时 / dayflow 未就绪 / 解析失败 → 回退到原 random_periods 随机机制。
"""
import asyncio
import json
import random
import re
from datetime import datetime, timedelta
from typing import Optional

from astrbot.api import logger


# ==================== LLM 提示词 ====================

SMART_SHARE_SYSTEM_PROMPT = "你是专业的日程分析助手，只输出 JSON 对象，不要任何解释或代码块标记。"

SMART_SHARE_USER_PROMPT_TEMPLATE = """你是穿搭时段识别助手。基于以下当日日程，识别两套穿搭各自的穿着时段。

## 当日日程
{schedule_text}

## 任务
1. 从日程和穿搭描述中识别两套穿搭（晨间第一套 和 午后第二套）的穿着时段
2. 给出每套穿搭的 wear_window（穿着时段，格式 HH:MM-HH:MM）
3. wear_window 必须满足：
   - 必须是当天实际穿着该套穿搭的连续时段
   - 起始时间 = 该套穿搭换装完成时间
   - 结束时间 = 该套穿搭被换下时间（或当日最后活动结束时间）
   - 不能跨日（如 22:00-02:00 应改写为 22:00-23:59）
   - 第一套 wear_window 的结束时间应 ≤ 第二套 wear_window 的起始时间

## 注意
- **不需要**选择具体的分享时间点，分享时间由系统在 wear_window 内随机选择
- **不需要**判断哪个时间点"适合分享"，只需准确识别穿着时段

## 输出 JSON
只输出 JSON 对象，不要任何解释或代码块标记：
{{
  "look_1": {{
    "wear_window": "08:00-13:30",
    "reason": "晨间穿搭，从起床换装后到午后换装前"
  }},
  "look_2": {{
    "wear_window": "13:30-22:00",
    "reason": "午后换装后到晚间活动结束"
  }}
}}"""


# ==================== SmartShareScheduler ====================

class SmartShareScheduler:
    """智能分享调度器：让 LLM 基于 dayflow 日程找出两套穿搭各自的最佳分享时间点"""

    SMART_STATE_KEY_TEMPLATE = "smart_{persona}"

    def __init__(self, plugin):
        self.plugin = plugin
        self.db = plugin.db
        self.ctx_service = plugin.ctx_service
        self.task_manager = plugin.task_manager
        # 正在运行智能调度的 persona 集合，防止 6 点 cron 与 _smart_init_today 并发
        self._running_personas = set()
        # 跨人格注册时的全局锁：防止多 persona 同时 register_smart_jobs 导致同 umo 时间冲突
        self._registration_lock = asyncio.Lock()

    # ============ 配置读取 ============

    def _is_smart_share_enabled(self, persona_name: str) -> bool:
        return bool(self.plugin.get_persona_config_value(
            persona_name, "persona_smart_share_conf", "enable_smart_share", False
        ))

    def _get_smart_provider_id(self, persona_name: str) -> str:
        """获取智能调度专用 LLM provider id，留空回退到人格默认 LLM"""
        smart_pid = self.plugin.get_persona_config_value(
            persona_name, "persona_smart_share_conf", "smart_share_llm_provider_id", ""
        )
        if smart_pid:
            return smart_pid
        # 回退到人格默认 LLM
        return self.plugin.get_persona_config_value(
            persona_name, "persona_llm_conf", "llm_provider_id", ""
        )

    def _get_trigger_time(self, persona_name: str) -> str:
        return self.plugin.get_persona_config_value(
            persona_name, "persona_smart_share_conf", "smart_share_trigger_time", "06:00"
        )

    def _get_wait_timeout(self, persona_name: str) -> int:
        return int(self.plugin.get_persona_config_value(
            persona_name, "persona_smart_share_conf", "smart_share_wait_dayflow_timeout", 30
        ))

    def _get_fallback_to_random(self, persona_name: str) -> bool:
        return bool(self.plugin.get_persona_config_value(
            persona_name, "persona_smart_share_conf", "smart_share_fallback_to_random", True
        ))

    def _get_state_key(self, persona_name: str) -> str:
        return self.SMART_STATE_KEY_TEMPLATE.format(persona=persona_name)

    async def _mark_smart_failed(self, persona_name: str):
        """标记今日智能调度已失败，防止 _smart_init_today 重载后重复触发

        失败回退随机后，smart state 写入 date=今日 + failed=True，
        _smart_init_today 检查 date=今日 即 return，不再重复触发。
        """
        try:
            await self.db.update_state_dict(self._get_state_key(persona_name), {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "failed": True
            })
        except Exception as e:
            logger.warning(f"[SmartShare] [{persona_name}] 标记 failed 失败: {e}")

    # ============ dayflow 日程就绪检查 ============

    def _find_dayflow_plugin(self):
        """查找 dayflow 插件实例"""
        try:
            plugin = self.ctx_service._find_life_plugin()
            if plugin and hasattr(plugin, "service"):
                return plugin
        except Exception as e:
            logger.warning(f"[SmartShare] 查找 dayflow 插件失败: {e}")
        return None

    async def _check_dayflow_ready(self, persona_name: str) -> Optional[dict]:
        """检查 dayflow 当日日程是否已生成，返回日程数据或 None"""
        dayflow = self._find_dayflow_plugin()
        if not dayflow:
            return None

        try:
            today = datetime.now().strftime("%Y-%m-%d")
            store_key = dayflow.service.normalize_persona_key(persona_name)
            data = dayflow.service.store.get_schedule_for_date(store_key, today)
            if data and not data.get("meta", {}).get("error"):
                return data
        except Exception as e:
            logger.warning(f"[SmartShare] 检查 dayflow 日程就绪失败: {e}")
        return None

    async def _trigger_dayflow_generation(self, persona_name: str) -> bool:
        """触发 dayflow 生成当日日程

        直接调用 dayflow.service.generate_schedule(event=None, ...) 触发完整生成流程，
        会触发 dayflow 自己的 push_schedule_to_targets 推送（如果配置了 push_targets）。
        注意：enter_generation/exit_generation/save_generated 内部会 normalize persona_name，
        所以这里传 raw persona_name，不要传 store_key。
        """
        dayflow = self._find_dayflow_plugin()
        if not dayflow:
            logger.warning("[SmartShare] dayflow 插件未安装，无法触发生成")
            return False

        try:
            # 尝试获取生成锁（service.enter_generation 内部会 normalize）
            ok = await dayflow.service.enter_generation(persona_name)
            if not ok:
                # dayflow 正在生成中（可能是 dayflow 自己的定时任务），等待其完成
                logger.info(f"[SmartShare] dayflow 正在生成中 [{persona_name}]，等待其完成")
                for _ in range(6):
                    await asyncio.sleep(10)
                    data = await self._check_dayflow_ready(persona_name)
                    if data:
                        return True
                return False

            try:
                # generate_schedule 内部会 _resolve_persona_context_internal，无需预先解析
                data = await dayflow.service.generate_schedule(
                    event=None,
                    persona_name=persona_name,
                    target_date=datetime.now().strftime("%Y-%m-%d"),
                    auto_retry=False,
                )
                if not data.get("meta", {}).get("error"):
                    await dayflow.service.save_generated(persona_name, data)
                    logger.info(f"[SmartShare] dayflow 日程生成成功 [{persona_name}]")
                    return True
                else:
                    logger.warning(f"[SmartShare] dayflow 生成失败: {data.get('memo', '')}")
                    return False
            finally:
                await dayflow.service.exit_generation(persona_name)
        except Exception as e:
            logger.error(f"[SmartShare] 触发 dayflow 生成异常: {e}")
            return False

    async def ensure_dayflow_ready(self, persona_name: str) -> Optional[dict]:
        """确保 dayflow 当日日程就绪，返回日程数据或 None"""
        # 先检查是否已就绪
        data = await self._check_dayflow_ready(persona_name)
        if data:
            return data

        # 未就绪，主动触发生成
        timeout = self._get_wait_timeout(persona_name)
        logger.info(f"[SmartShare] dayflow 日程未就绪 [{persona_name}]，主动触发生成（超时 {timeout} 分钟）")

        success = await self._trigger_dayflow_generation(persona_name)
        if success:
            return await self._check_dayflow_ready(persona_name)

        # 触发失败，等待 dayflow 自己的定时任务生成
        logger.info(f"[SmartShare] dayflow 主动触发失败 [{persona_name}]，等待 dayflow 自己生成")
        for _ in range(max(1, timeout // 5)):
            await asyncio.sleep(300)  # 每 5 分钟检查一次
            data = await self._check_dayflow_ready(persona_name)
            if data:
                return data

        return None

    # ============ LLM 分析日程 ============

    def _build_schedule_text(self, dayflow_data: dict) -> str:
        """从 dayflow 日程数据构建 LLM 输入文本"""
        parts = []

        outfit = dayflow_data.get("outfit", "")
        if outfit:
            parts.append(f"【今日穿搭】\n{outfit}")

        timeline = dayflow_data.get("timeline", [])
        if timeline:
            lines = ["【今日时间轴】"]
            for item in timeline:
                time_str = item.get("time", "")
                activity = item.get("activity", "")
                status = item.get("status", "")
                line = f"{time_str} {activity}"
                if status:
                    line += f"（{status}）"
                lines.append(line)
            parts.append("\n".join(lines))

        schedule = dayflow_data.get("schedule", "")
        if schedule:
            parts.append(f"【日程详情】\n{schedule}")

        weather = dayflow_data.get("weather", "")
        if weather:
            parts.append(f"【天气】{weather}")

        return "\n\n".join(parts)

    def _build_analyze_prompt(self, dayflow_data: dict) -> str:
        """构建 LLM 分析提示词"""
        schedule_text = self._build_schedule_text(dayflow_data)
        return SMART_SHARE_USER_PROMPT_TEMPLATE.format(schedule_text=schedule_text)

    def _parse_llm_response(self, response: str) -> Optional[dict]:
        """解析 LLM 输出的 JSON"""
        if not response:
            return None

        text = response.strip()

        # 移除 markdown 代码块
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        if match:
            text = match.group(1).strip()

        # 找到第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None

        try:
            data = json.loads(text[start:end + 1])
            if "look_1" in data and "look_2" in data:
                return data
        except Exception:
            pass

        return None

    def _parse_hhmm(self, time_str: str) -> Optional[tuple]:
        try:
            h, m = map(int, str(time_str).split(":"))
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except Exception:
            pass
        return None

    def _parse_window(self, window_str: str) -> Optional[tuple]:
        """解析 HH:MM-HH:MM 格式"""
        try:
            start, end = str(window_str).split("-")
            start_parsed = self._parse_hhmm(start.strip())
            end_parsed = self._parse_hhmm(end.strip())
            if start_parsed and end_parsed:
                return start_parsed, end_parsed
        except Exception:
            pass
        return None

    def _validate_look_times(self, look_times: dict) -> bool:
        """验证 LLM 输出的 wear_window 是否合理

        新流程：LLM 只返回 wear_window，share_time 由代码在 wear_window 内随机选择。
        因此只校验 wear_window 的格式和合理性，不再校验 share_time。
        """
        try:
            for key in ("look_1", "look_2"):
                look = look_times.get(key)
                if not isinstance(look, dict):
                    return False
                wear_window = look.get("wear_window", "")
                if not wear_window or not self._parse_window(wear_window):
                    logger.warning(f"[SmartShare] {key} wear_window 无效: {wear_window}")
                    return False

            # 校验 look_1 的 wear_window 结束时间 ≤ look_2 的起始时间（不重叠）
            w1 = self._parse_window(look_times["look_1"]["wear_window"])
            w2 = self._parse_window(look_times["look_2"]["wear_window"])
            if w1 and w2:
                (_, _), (e1h, e1m) = w1
                (s2h, s2m), _ = w2
                end1_mins = e1h * 60 + e1m
                start2_mins = s2h * 60 + s2m
                # 允许 5 分钟容差（LLM 可能输出 13:30-13:30 这种边界）
                if end1_mins > start2_mins + 5:
                    logger.warning(
                        f"[SmartShare] look_1 wear_window 结束({look_times['look_1']['wear_window'].split('-')[1]})"
                        f" 晚于 look_2 起始({look_times['look_2']['wear_window'].split('-')[0]})，存在重叠"
                    )
                    return False

            # 校验每个 wear_window 至少 30 分钟（太短的窗口无法随机选时）
            for key in ("look_1", "look_2"):
                wear_window = look_times[key]["wear_window"]
                (sh, sm), (eh, em) = self._parse_window(wear_window)
                start_mins = sh * 60 + sm
                end_mins = eh * 60 + em
                if end_mins - start_mins < 30:
                    logger.warning(f"[SmartShare] {key} wear_window {wear_window} 不足 30 分钟，无法选时")
                    return False

            return True
        except Exception:
            return False

    async def analyze_schedule(self, persona_name: str, dayflow_data: dict) -> Optional[dict]:
        """调用 LLM 分析日程，返回两套穿搭的分享时间点"""
        provider_id = self._get_smart_provider_id(persona_name)
        prompt = self._build_analyze_prompt(dayflow_data)

        logger.info(f"[SmartShare] 调用 LLM 分析日程 [{persona_name}], provider={provider_id or 'default'}")

        response = await self.plugin._call_llm_wrapper(
            prompt=prompt,
            system_prompt=SMART_SHARE_SYSTEM_PROMPT,
            timeout=60,
            max_retries=2,
            persona_name=persona_name,
            provider_id=provider_id
        )

        if not response:
            logger.warning(f"[SmartShare] LLM 无响应 [{persona_name}]")
            return None

        look_times = self._parse_llm_response(response)
        if not look_times:
            logger.warning(f"[SmartShare] LLM 输出解析失败 [{persona_name}]: {response[:200]}")
            return None

        if not self._validate_look_times(look_times):
            logger.warning(f"[SmartShare] LLM 输出时间点不合理 [{persona_name}]: {look_times}")
            return None

        logger.info(
            f"[SmartShare] LLM 分析成功 [{persona_name}]: "
            f"look_1 wear_window={look_times['look_1'].get('wear_window','')}, "
            f"look_2 wear_window={look_times['look_2'].get('wear_window','')}"
        )
        return look_times

    # ============ 随机选时与跨人格协调 ============

    # 同一 umo 下两人格分享时间最小间隔（分钟）
    MIN_CROSS_PERSONA_GAP_MIN = 120
    # "合适分享时段"（轻微加权用），分钟数
    SUITABLE_RANGES = [
        (9 * 60, 11 * 60),    # 09:00-11:00
        (14 * 60, 17 * 60),   # 14:00-17:00
        (19 * 60, 21 * 60),   # 19:00-21:00
    ]

    def _get_umo_fingerprint(self, persona_name: str) -> str:
        """获取人格分享目标的 umo 指纹（用于跨人格冲突检测）

        同一 umo（相同的 adapter + groups + users）下的多人格需要避免相近时间分享。
        不同 umo 之间互不影响。
        """
        receiver = self.plugin.get_persona_receiver(persona_name)
        adapter_id = (receiver.get("adapter_id") or "").strip()
        groups = sorted(receiver.get("groups") or [])
        users = sorted(receiver.get("users") or [])
        return f"{adapter_id}|g:{','.join(groups)}|u:{','.join(users)}"

    def _get_conflict_times_for_umo(self, persona_name: str) -> list:
        """获取同一 umo 下其他人格已注册的智能分享时间点

        扫描调度器中所有 persona_*_smart_share_* date 任务，
        提取与当前人格共享同一 umo 的其他人格的分享时间（HH, MM）。
        """
        try:
            current_umo = self._get_umo_fingerprint(persona_name)
        except Exception:
            return []

        conflict_times = []
        try:
            jobs = self.task_manager.scheduler.get_jobs()
        except Exception:
            return []

        for job in jobs:
            jid = job.id
            if not jid.startswith("persona_") or "_smart_" not in jid:
                continue
            # 跳过 6 点 cron 调度任务（id 形如 persona_xxx_smart_scheduler）
            if jid.endswith("_smart_scheduler"):
                continue
            # 解析 persona_name: persona_{name}_smart_share_{idx}
            # persona_name 可能含下划线，用 rfind 定位最后的 _smart_
            rest = jid[len("persona_"):]
            idx_pos = rest.rfind("_smart_")
            if idx_pos <= 0:
                continue
            other_persona = rest[:idx_pos]
            if other_persona == persona_name:
                continue

            # 检查 umo 是否相同
            try:
                other_umo = self._get_umo_fingerprint(other_persona)
            except Exception:
                continue
            if other_umo != current_umo:
                continue

            # 提取 date 任务的运行时间
            run_date = getattr(getattr(job, "trigger", None), "run_date", None)
            if run_date:
                try:
                    conflict_times.append((run_date.hour, run_date.minute))
                except Exception:
                    continue

        return conflict_times

    def _pick_random_time_in_window(self, wear_window: str, conflict_times: list) -> Optional[str]:
        """在 wear_window 内随机选择一个分享时间点

        - 以完全随机为主，"合适时段"（09-11 / 14-17 / 19-21）轻微加权（权重 1.5，其余 1.0）
        - 避开 conflict_times 中每个时间点 ±MIN_CROSS_PERSONA_GAP_MIN 的范围
        - 若整个窗口都被冲突覆盖，则退化为纯随机（忽略冲突），保证总能选出时间
        - 步长 5 分钟，返回 HH:MM 格式
        """
        window = self._parse_window(wear_window)
        if not window:
            return None
        (sh, sm), (eh, em) = window
        start_mins = sh * 60 + sm
        end_mins = eh * 60 + em
        if end_mins - start_mins < 30:
            return None

        def in_suitable(m):
            for (a, b) in self.SUITABLE_RANGES:
                if a <= m <= b:
                    return True
            return False

        def in_conflict(m):
            for (ch, cm) in conflict_times:
                cmins = ch * 60 + cm
                if (cmins - self.MIN_CROSS_PERSONA_GAP_MIN) <= m <= (cmins + self.MIN_CROSS_PERSONA_GAP_MIN):
                    return True
            return False

        def build_candidates(ignore_conflict: bool):
            cands, weights = [], []
            m = start_mins
            # 对齐到 5 分钟
            if m % 5 != 0:
                m = m + (5 - m % 5)
            while m <= end_mins:
                blocked = (not ignore_conflict) and in_conflict(m)
                if not blocked:
                    cands.append(m)
                    weights.append(1.5 if in_suitable(m) else 1.0)
                m += 5
            return cands, weights

        candidates, weights = build_candidates(ignore_conflict=False)
        if not candidates:
            # 全部被冲突覆盖：退化为纯随机，保证能选出时间
            candidates, weights = build_candidates(ignore_conflict=True)
        if not candidates:
            return None

        chosen = random.choices(candidates, weights=weights, k=1)[0]
        return f"{chosen // 60:02d}:{chosen % 60:02d}"

    # ============ 任务注册 ============

    def _make_smart_task_wrapper(self, persona_name: str, look_key: str):
        """构建智能分享任务 wrapper：执行后再标记 look 为已执行，防止重载后重复注册

        复用 task_manager._make_task_wrapper 的全部 execute_share 链路，
        仅在执行后更新 smart state 标记该 look 已执行。

        注意：不能在调用前标记 executed，否则若 _make_delayed_task 因防抖/锁跳过，
        executed 已标记 True 但分享未执行，该套穿搭会被 recover_smart_state 永久跳过。
        """
        async def wrapper():
            if self.plugin._is_terminated: return
            # 先调用原始任务链路（DB清理 + execute_share）
            await self.task_manager._make_task_wrapper(persona_name)()
            # 执行后再标记该 look 为已执行（防止插件重载后 recover_smart_state 重复注册）
            try:
                state_key = self._get_state_key(persona_name)
                state = await self.db.get_state(state_key, {})
                look_state = state.get(look_key)
                if isinstance(look_state, dict):
                    look_state["executed"] = True
                    await self.db.update_state_dict(state_key, state)
            except Exception as e:
                logger.warning(f"[SmartShare] [{persona_name}] 标记 {look_key} 已执行失败: {e}")
        return wrapper

    async def register_smart_jobs(self, persona_name: str, look_times: dict):
        """用 LLM 输出的 wear_window 随机选时并注册 date 任务

        新流程：
        1. LLM 只返回 wear_window，不返回 share_time
        2. 在 wear_window 内随机选 share_time（合适时段轻微加权 + 避开同 umo 冲突）
        3. 跨人格协调：通过 _registration_lock 串行化注册，
           先注册的人格的时间点会加入冲突列表，后注册的人格自动避开
        4. 复用 _make_smart_task_wrapper → _make_task_wrapper，execute_share 链路完全不变

        注意：任务 ID 使用 _smart_share_ 前缀，与 cron 的 _smart_scheduler 区分，
        防止清理旧任务时误删 cron。
        """
        # 全局锁：串行化多人格注册，确保同 umo 下后注册者能看到先注册者的时间点
        async with self._registration_lock:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            # 清理旧的智能分享任务和随机任务
            # 使用 _smart_share_ 前缀而非 _smart_，避免误删 smart_scheduler cron
            # 同时清理 random_ 前缀：防止智能调度失败回退随机后，6 点 cron 成功导致两套任务并发重复分享
            task_prefix = f"persona_{persona_name}_smart_share_"
            random_prefix = f"persona_{persona_name}_random_"
            for job in self.task_manager.scheduler.get_jobs():
                if job.id.startswith(task_prefix) or job.id.startswith(random_prefix):
                    self.task_manager.scheduler.remove_job(job.id)

            # 清理随机模式残留的延迟分享恢复任务和 state：
            # 随机模式失败回退时会在 global_{persona} 写入 pending_delay_job（如 9:00），
            # 若不清理，插件重载后 _recover_pending_jobs 会恢复出 9:00 的额外分享
            resume_job_id = f"resume_auto_share_{persona_name}"
            try:
                if self.task_manager.scheduler.get_job(resume_job_id):
                    self.task_manager.scheduler.remove_job(resume_job_id)
            except Exception:
                pass
            try:
                await self.db.update_state_dict(f"global_{persona_name}", {"pending_delay_job": None})
            except Exception as e:
                logger.warning(f"[SmartShare] [{persona_name}] 清理 global pending_delay_job 失败: {e}")

            # 收集同 umo 其他人格已注册的分享时间（冲突列表）
            conflict_times = self._get_conflict_times_for_umo(persona_name)
            if conflict_times:
                logger.info(
                    f"[SmartShare] [{persona_name}] 检测到同 umo 冲突时间: "
                    f"{[f'{h:02d}:{m:02d}' for (h, m) in conflict_times]}"
                )

            # 为每套穿搭在 wear_window 内随机选时
            picked_times = {}
            for look_key in ("look_1", "look_2"):
                look = look_times.get(look_key, {})
                wear_window = look.get("wear_window", "")
                # 把本人格前一套穿搭的已选时间也加入冲突，避免两套靠太近
                share_time = self._pick_random_time_in_window(wear_window, conflict_times)
                if not share_time:
                    logger.warning(
                        f"[SmartShare] [{persona_name}] {look_key} 在 wear_window={wear_window} 内选时失败"
                    )
                    share_time = wear_window.split("-")[0] if wear_window else "12:00"
                picked_times[look_key] = share_time
                # 把本套时间加入冲突列表，供后续 look / 其他人格避让
                parsed = self._parse_hhmm(share_time)
                if parsed:
                    conflict_times.append(parsed)
                logger.info(
                    f"[SmartShare] [{persona_name}] {look_key} 在 wear_window={wear_window} 内"
                    f"随机选定 share_time={share_time}"
                )

            # 准备状态
            state_key = self._get_state_key(persona_name)
            state = {
                "date": today_str,
                "look_1": {
                    "share_time": picked_times["look_1"],
                    "wear_window": look_times["look_1"].get("wear_window", ""),
                    "executed": False
                },
                "look_2": {
                    "share_time": picked_times["look_2"],
                    "wear_window": look_times["look_2"].get("wear_window", ""),
                    "executed": False
                }
            }

            # 注册任务
            for idx, look_key in enumerate(["look_1", "look_2"]):
                share_time = picked_times[look_key]
                wear_window = look_times[look_key].get("wear_window", "")

                parsed = self._parse_hhmm(share_time)
                if not parsed:
                    continue
                h, m = parsed

                run_time = now.replace(hour=h, minute=m, second=0, microsecond=0)

                # 时间点已过：检查是否仍在穿搭时段内
                if run_time <= now:
                    in_window = False
                    if wear_window:
                        window = self._parse_window(wear_window)
                        if window:
                            (sh, sm), (eh, em) = window
                            start_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
                            end_dt = now.replace(hour=eh, minute=em, second=59, microsecond=0)
                            if start_dt <= now <= end_dt:
                                in_window = True

                    if in_window:
                        # 仍在穿搭时段内，立即补偿触发
                        run_time = now + timedelta(seconds=10)
                        logger.info(
                            f"[SmartShare] [{persona_name}] {look_key} 时间点 {share_time} 已过但在穿搭时段内，补偿触发"
                        )
                    else:
                        # 已超出穿搭时段，跳过（避免画错穿搭）
                        logger.info(
                            f"[SmartShare] [{persona_name}] {look_key} 时间点 {share_time} 已过且超出穿搭时段，跳过"
                        )
                        state[look_key]["executed"] = True
                        state[look_key]["skipped"] = True
                        continue

                job_id = f"{task_prefix}{idx}"
                self.task_manager.scheduler.add_job(
                    self._make_smart_task_wrapper(persona_name, look_key),
                    'date',
                    run_date=run_time,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"[SmartShare] [{persona_name}] {look_key} 已安排在 {run_time.strftime('%H:%M:%S')} 执行"
                    f"（穿搭时段: {wear_window}）"
                )

            # 保存状态
            await self.db.update_state_dict(state_key, state)

            # 防御性检查：确保 cron 未被误删（register_smart_jobs 清理由 _smart_share_ 前缀保护，
            # 但以防其他路径意外移除，注册完任务后验证 cron 是否存活）
            scheduler_jid = f"persona_{persona_name}_smart_scheduler"
            if not self.task_manager.scheduler.get_job(scheduler_jid):
                logger.warning(f"[SmartShare] [{persona_name}] cron 意外丢失，自动重新注册")
                self.setup_smart_cron(persona_name)

    # ============ 主入口 ============

    async def run_smart_schedule(self, persona_name: str) -> bool:
        """6 点触发的智能调度主入口，返回是否成功

        失败场景（dayflow 未就绪 / LLM 超时 / 解析失败）返回 False，
        由调用方决定是否回退到原 random_periods 随机机制。

        并发保护：通过 _running_personas 防止 6 点 cron 与 _smart_init_today
        并发执行（如插件在 5:50 重载，_smart_init_today 立即触发并等待 dayflow，
        6 点 cron 又触发）。返回 True 表示"已处理/正在处理"，不触发回退。
        """
        if self.plugin._is_terminated:
            return False

        # 并发去重：如果该 persona 的智能调度正在运行，跳过本次触发
        if persona_name in self._running_personas:
            logger.info(f"[SmartShare] [{persona_name}] 智能调度正在运行中，跳过本次触发")
            return True

        self._running_personas.add(persona_name)
        try:
            logger.info(f"[SmartShare] 开始智能调度 [{persona_name}]")

            # 1. 确保 dayflow 日程就绪
            dayflow_data = await self.ensure_dayflow_ready(persona_name)
            if not dayflow_data:
                logger.warning(f"[SmartShare] dayflow 日程未就绪 [{persona_name}]")
                return False

            # 2. 调用 LLM 分析日程
            look_times = await self.analyze_schedule(persona_name, dayflow_data)
            if not look_times:
                logger.warning(f"[SmartShare] LLM 分析失败 [{persona_name}]")
                return False

            # 3. 注册任务
            await self.register_smart_jobs(persona_name, look_times)

            logger.info(f"[SmartShare] 智能调度成功 [{persona_name}]")
            return True
        finally:
            self._running_personas.discard(persona_name)

    # ============ 调度入口（供 tasks.py 注册 cron） ============

    def make_smart_cron_wrapper(self, persona_name: str):
        """构建 6 点触发的智能调度 cron wrapper

        成功：使用 LLM 输出的两个时间点
        失败：回退到原 _make_persona_daily_random_scheduler
        """
        async def wrapper():
            if self.plugin._is_terminated:
                return

            task = asyncio.current_task()
            self.plugin._bg_tasks.add(task)
            try:
                success = False
                try:
                    success = await self.run_smart_schedule(persona_name)
                except Exception as e:
                    logger.error(f"[SmartShare] [{persona_name}] 智能调度异常: {e}")
                if success:
                    return

                # 失败回退到原随机机制
                if self._get_fallback_to_random(persona_name):
                    logger.info(f"[SmartShare] [{persona_name}] 智能调度失败，回退到随机机制")
                    # 标记今日智能调度已失败，防止插件重载后 _smart_init_today 重复触发
                    await self._mark_smart_failed(persona_name)
                    random_scheduler = self.task_manager._make_persona_daily_random_scheduler(persona_name)
                    await random_scheduler()
                else:
                    logger.warning(f"[SmartShare] [{persona_name}] 智能调度失败且未启用回退，今日跳过分享")
                    await self._mark_smart_failed(persona_name)
            finally:
                self.plugin._bg_tasks.discard(task)

        return wrapper

    def setup_smart_cron(self, persona_name: str):
        """注册 6 点触发的智能调度 cron 任务"""
        trigger_time = self._get_trigger_time(persona_name)
        parsed = self._parse_hhmm(trigger_time)
        if not parsed:
            logger.warning(f"[SmartShare] [{persona_name}] 触发时间格式错误: {trigger_time}，使用默认 06:00")
            parsed = (6, 0)

        hour, minute = parsed
        job_id = f"persona_{persona_name}_smart_scheduler"

        try:
            if self.task_manager.scheduler.get_job(job_id):
                self.task_manager.scheduler.remove_job(job_id)
        except Exception:
            pass

        self.task_manager.scheduler.add_job(
            self.make_smart_cron_wrapper(persona_name),
            'cron',
            hour=hour, minute=minute,
            id=job_id,
            replace_existing=True,
            max_instances=1
        )
        logger.info(f"[SmartShare] [{persona_name}] 智能调度 cron 已注册：每日 {hour:02d}:{minute:02d} 触发")

    async def recover_smart_state(self, persona_name: str):
        """插件重载后恢复：检查当日智能调度状态，必要时重新注册任务

        场景：6 点已触发智能调度成功，但插件在分享时间点之前被重载，
        原本的 date 任务会丢失，需要根据 state 重新注册。
        """
        if self.plugin._is_terminated:
            return

        state_key = self._get_state_key(persona_name)
        state = await self.db.get_state(state_key, {})
        if not state:
            return

        today_str = datetime.now().strftime("%Y-%m-%d")
        if state.get("date") != today_str:
            # 状态过期，清理
            await self.db.update_state_dict(state_key, {})
            return

        # 今日智能调度已失败（已回退随机），不恢复智能任务
        if state.get("failed"):
            return

        now = datetime.now()
        # 使用 _smart_share_ 前缀区分任务与 cron（cron id 为 _smart_scheduler，不匹配此前缀）
        task_prefix = f"persona_{persona_name}_smart_share_"
        existing_jobs = [j for j in self.task_manager.scheduler.get_jobs() if j.id.startswith(task_prefix)]

        # 如果任务还存在，不处理
        if existing_jobs:
            return

        # 任务丢失，根据 state 重新注册
        re_registered = 0
        for idx, look_key in enumerate(["look_1", "look_2"]):
            look_state = state.get(look_key, {})
            if look_state.get("executed") or look_state.get("skipped"):
                continue

            share_time = look_state.get("share_time", "")
            wear_window = look_state.get("wear_window", "")
            parsed = self._parse_hhmm(share_time)
            if not parsed:
                continue
            h, m = parsed
            run_time = now.replace(hour=h, minute=m, second=0, microsecond=0)

            if run_time <= now:
                # 时间点已过，检查是否在穿搭时段内
                in_window = False
                if wear_window:
                    window = self._parse_window(wear_window)
                    if window:
                        (sh, sm), (eh, em) = window
                        start_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
                        end_dt = now.replace(hour=eh, minute=em, second=59, microsecond=0)
                        if start_dt <= now <= end_dt:
                            in_window = True

                if in_window:
                    run_time = now + timedelta(seconds=10)
                    logger.info(f"[SmartShare] [{persona_name}] 恢复 {look_key} 补偿触发")
                else:
                    logger.info(f"[SmartShare] [{persona_name}] 恢复时 {look_key} 已超出穿搭时段，跳过")
                    look_state["executed"] = True
                    look_state["skipped"] = True
                    continue

            job_id = f"{task_prefix}{idx}"
            self.task_manager.scheduler.add_job(
                self._make_smart_task_wrapper(persona_name, look_key),
                'date',
                run_date=run_time,
                id=job_id,
                replace_existing=True
            )
            re_registered += 1
            logger.info(f"[SmartShare] [{persona_name}] 恢复 {look_key} 任务: {run_time.strftime('%H:%M:%S')}")

        # 始终保存 state（即使 re_registered=0，也需要保存 skipped 标记，避免重载后重复处理）
        await self.db.update_state_dict(state_key, state)
