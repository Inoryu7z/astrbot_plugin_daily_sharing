import asyncio
import random
import re
import sys
import aiofiles
from datetime import datetime, timedelta
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Record, Video 

from ..config import TimePeriod, SharingType, SHARING_TYPE_SEQUENCES, QZONE_SHARING_TYPE_SEQUENCES, CRON_TEMPLATES, NEWS_SOURCE_MAP
from .constants import CMD_CN_MAP, SOURCE_CN_MAP

class TaskManager:
    def __init__(self, plugin):
        self.plugin = plugin
        self.scheduler = plugin.scheduler
        self.db = plugin.db
        self.ctx_service = plugin.ctx_service
        self.news_service = plugin.news_service
        self.image_service = plugin.image_service
        self.content_service = plugin.content_service
        self._get_lock = plugin._get_lock
        
        self.basic_conf = plugin.basic_conf
        self.extra_shares_conf = plugin.extra_shares_conf
        self.qzone_conf = plugin.qzone_conf
        self.image_conf = plugin.image_conf
        self.tts_conf = plugin.tts_conf
        self.context_conf = plugin.context_conf
        self.receiver_conf = plugin.receiver_conf

    def setup_tasks(self):
        personas = self.plugin.get_enabled_personas()

        if personas:
            for persona_entry in personas:
                pname = persona_entry.get("persona_name") or persona_entry.get("name") or persona_entry.get("select_persona", "")
                if not pname:
                    continue
                canonical = self.plugin._canonical_persona_name(pname) or pname
                self._setup_persona_tasks(canonical, persona_entry)

            if self.plugin.config.get("enable_auto_sharing", False):
                has_global_targets = bool(self.receiver_conf.get("groups") or self.receiver_conf.get("users"))
                if has_global_targets:
                    trigger_mode = self.basic_conf.get("trigger_mode", "random_period")
                    if trigger_mode == "cron":
                        cron = self.basic_conf.get("sharing_cron", "0 8,20 * * *")
                        self.setup_cron(cron, persona_name=None)
                    else:
                        daily_sched_id = "daily_random_scheduler"
                        self._setup_cron_job_custom(daily_sched_id, "0 0 * * *", self._make_persona_daily_random_scheduler(None))
                        asyncio.create_task(self._make_persona_daily_random_scheduler(None)())
                    self.setup_custom_target_crons(persona_name=None)
                    logger.debug(f"[DailySharing] 全局分享任务已启动 (与人格任务并行, 模式: {trigger_mode})")
        else:
            if self.plugin.config.get("enable_auto_sharing", False):
                trigger_mode = self.basic_conf.get("trigger_mode", "random_period")
                if trigger_mode == "cron":
                    cron = self.basic_conf.get("sharing_cron", "0 8,20 * * *")
                    self.setup_cron(cron, persona_name=None)
                else:
                    daily_sched_id = "daily_random_scheduler"
                    self._setup_cron_job_custom(daily_sched_id, "0 0 * * *", self._make_persona_daily_random_scheduler(None))
                    asyncio.create_task(self._make_persona_daily_random_scheduler(None)())
                self.setup_custom_target_crons(persona_name=None)
                logger.debug(f"[DailySharing] 单人格模式：分享内容定时任务已启动 (模式: {trigger_mode})")

        enable_60s = self.extra_shares_conf.get("enable_60s_news", False)
        enable_ai = self.extra_shares_conf.get("enable_ai_news", False)
        
        if enable_60s or enable_ai:
            cron_briefing = self.extra_shares_conf.get("cron_briefing", "0 8 * * *")
            self._setup_cron_job_custom("share_briefing", cron_briefing, self._task_wrapper_briefing)
            logger.debug(f"[DailySharing] 早报定时任务已启动 ({cron_briefing})")

        asyncio.create_task(self._recover_pending_jobs())

    def _setup_persona_tasks(self, persona_name: str, persona_entry: dict):
        if not self.plugin.config.get("enable_auto_sharing", False):
            return

        persona_receiver = self.plugin.get_persona_receiver(persona_name)
        has_targets = bool(persona_receiver.get("groups") or persona_receiver.get("users"))

        if not has_targets:
            logger.debug(f"[DailySharing] 人格 [{persona_name}] 未配置接收对象，跳过定时任务")
            return

        persona_basic = persona_entry.get("persona_basic_conf", {})
        trigger_mode = persona_basic.get("trigger_mode", "") or self.basic_conf.get("trigger_mode", "random_period")

        if trigger_mode == "cron":
            cron = persona_basic.get("sharing_cron", "") or self.basic_conf.get("sharing_cron", "0 8,20 * * *")
            self.setup_cron(cron, persona_name=persona_name)
        elif trigger_mode == "random_period":
            job_id_prefix = f"persona_{persona_name}_"
            daily_sched_id = f"{job_id_prefix}daily_random_scheduler"
            self._setup_cron_job_custom(daily_sched_id, "0 0 * * *", self._make_persona_daily_random_scheduler(persona_name))
            asyncio.create_task(self._make_persona_daily_random_scheduler(persona_name)())
            logger.debug(f"[DailySharing] 人格 [{persona_name}] 已启用多时间段随机生成模式")

        self.setup_custom_target_crons(persona_name=persona_name)

        persona_qzone = persona_entry.get("persona_qzone_conf", {})
        qzone_enabled = persona_qzone.get("enable_qzone", "")
        if isinstance(qzone_enabled, str):
            qzone_enabled = qzone_enabled.lower() == "true" if qzone_enabled else self.qzone_conf.get("enable_qzone", False)
        if qzone_enabled:
            self.setup_qzone_cron(persona_name=persona_name)

        logger.debug(f"[DailySharing] 人格 [{persona_name}] 定时任务已挂载")

    def setup_custom_target_crons(self, persona_name=None):
        receiver_conf = self.plugin.get_persona_receiver(persona_name) if persona_name else self.receiver_conf
        default_adapter_id = self._resolve_adapter_id("setup_custom_target_crons", receiver_conf=receiver_conf)

        r_groups = self._parse_targets_config(receiver_conf.get("groups", []))
        r_users = self._parse_targets_config(receiver_conf.get("users", []))

        prefix = f"persona_{persona_name}_" if persona_name else "custom_share_"
        job_ids = [job.id for job in self.scheduler.get_jobs() if job.id.startswith(prefix)]
        for jid in job_ids:
            self.scheduler.remove_job(jid)

        def add_custom_job(target_id, is_group, cron_str):
            p = f"persona_{persona_name}_" if persona_name else ""
            job_id = f"{p}custom_share_{target_id}"
            target_umo = f"{default_adapter_id}:{'GroupMessage' if is_group else 'FriendMessage'}:{target_id}"
            
            async def delayed_custom_execute():
                if self.plugin._is_terminated: return
                task = asyncio.current_task()
                self.plugin._bg_tasks.add(task)
                try:
                    await self.db.update_state_dict(f"target_{target_id}", {"pending_delay_job": None})
                    lock = self._get_lock(persona_name)
                    if lock.locked():
                        logger.warning(f"[DailySharing] 独立任务 {target_id} 触发，系统繁忙排队中...")
                    async with lock:
                        logger.debug(f"[DailySharing] 独立时间到达，开始执行独立分享任务: {target_id}")
                        await self.execute_share(specific_target=target_umo, persona_name=persona_name)
                finally:
                    self.plugin._bg_tasks.discard(task)

            async def custom_wrapper():
                if self.plugin._is_terminated: return
                
                random_delay_min = 0
                try:
                    random_delay_min = int(self.basic_conf.get("cron_random_delay", 0))
                except Exception: 
                    pass

                if random_delay_min > 0:
                    delay_seconds = random.randint(0, random_delay_min * 60)
                    if delay_seconds > 0:
                        target_time = datetime.now() + timedelta(seconds=delay_seconds)
                        time_str = target_time.strftime('%H:%M:%S')
                        
                        await self.db.update_state_dict(f"target_{target_id}", {
                            "pending_delay_job": {"target_time": target_time.timestamp()}
                        })
                        
                        self.scheduler.add_job(
                            delayed_custom_execute, 'date',
                            run_date=target_time,
                            id=f"{p}delayed_custom_share_{target_id}",
                            replace_existing=True
                        )
                        logger.debug(f"[DailySharing] 独立任务 [{target_id}] 已触发，将随机延迟 {delay_seconds/60:.1f} 分钟，预计于 {time_str} 执行...")
                        return
                
                await delayed_custom_execute()

            actual_cron = CRON_TEMPLATES.get(cron_str, cron_str)
            parts = actual_cron.split()
            if len(parts) == 5:
                self.scheduler.add_job(
                    custom_wrapper, 'cron',
                    minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4],
                    id=job_id, replace_existing=True, max_instances=1
                )
                logger.debug(f"[DailySharing] 独立群聊、私聊任务 [{target_id}] 已挂载独立定时: {actual_cron}")
            else:
                logger.error(f"[DailySharing] 独立群聊、私聊任务 [{target_id}] 无效的Cron表达式: {cron_str}")

        for gid, conf in r_groups.items():
            if isinstance(conf, dict) and conf.get("cron"):
                add_custom_job(gid, True, conf["cron"])
                
        for uid, conf in r_users.items():
            if isinstance(conf, dict) and conf.get("cron"):
                add_custom_job(uid, False, conf["cron"])

    async def _recover_pending_jobs(self):
        """恢复因重启中断的延迟任务"""
        if self.plugin._is_terminated: return
        
        now = datetime.now()
        now_ts = now.timestamp()
        
        # 主任务恢复 (全局 + 人格级)
        state_keys_to_check = ["global"]
        for persona_entry in self.plugin.get_enabled_personas():
            pname = persona_entry.get("persona_name") or persona_entry.get("name") or persona_entry.get("select_persona", "")
            if pname:
                canonical = self.plugin._canonical_persona_name(pname) or pname
                state_keys_to_check.append(f"global_{canonical}")

        for state_key in state_keys_to_check:
            persona_name_for_job = None
            if state_key != "global":
                persona_name_for_job = state_key.replace("global_", "", 1)
            
            g_state = await self.db.get_state(state_key, {})
            pending = g_state.get("pending_delay_job")
            if pending:
                target_ts = pending.get("target_time", 0)
                job_id = f"resume_auto_share_{persona_name_for_job}" if persona_name_for_job else "resume_auto_share"
                if target_ts > now_ts:
                    run_time = datetime.fromtimestamp(target_ts)
                    self.scheduler.add_job(
                        self._make_delayed_task(persona_name_for_job), 'date', run_date=run_time, id=job_id, replace_existing=True
                    )
                    logger.debug(f"[DailySharing] 已恢复未完成的延迟分享任务{'['+persona_name_for_job+']' if persona_name_for_job else ''}，将在 {run_time.strftime('%H:%M:%S')} 执行")
                elif 0 <= now_ts - target_ts < 3600:  
                    run_time = now + timedelta(seconds=5)
                    self.scheduler.add_job(
                        self._make_delayed_task(persona_name_for_job), 'date', run_date=run_time, id=job_id, replace_existing=True
                    )
                    logger.debug(f"[DailySharing] 检测到近期错过的延迟分享任务{'['+persona_name_for_job+']' if persona_name_for_job else ''}，即将执行补偿分享")
                else:
                    await self.db.update_state_dict(state_key, {"pending_delay_job": None})

        # QQ空间任务恢复 (全局 + 人格级)
        qzone_keys_to_check = ["qzone"]
        for persona_entry in self.plugin.get_enabled_personas():
            pname = persona_entry.get("persona_name") or persona_entry.get("name") or persona_entry.get("select_persona", "")
            if pname:
                canonical = self.plugin._canonical_persona_name(pname) or pname
                qzone_keys_to_check.append(f"qzone_{canonical}")

        for qzone_key in qzone_keys_to_check:
            qzone_pname = None
            if qzone_key != "qzone":
                qzone_pname = qzone_key.replace("qzone_", "", 1)

            qzone_state = await self.db.get_state(qzone_key, {})
            q_pending = qzone_state.get("pending_delay_job")
            if q_pending:
                target_ts = q_pending.get("target_time", 0)
                job_id = f"resume_qzone_share_{qzone_pname}" if qzone_pname else "resume_qzone_share"
                if target_ts > now_ts:
                    run_time = datetime.fromtimestamp(target_ts)
                    self.scheduler.add_job(
                        self._make_qzone_task_wrapper(qzone_pname), 'date', run_date=run_time, id=job_id, replace_existing=True
                    )
                    logger.debug(f"[DailySharing] 已恢复未完成的QQ空间延迟任务{'['+qzone_pname+']' if qzone_pname else ''}，将在 {run_time.strftime('%H:%M:%S')} 执行")
                elif 0 <= now_ts - target_ts < 3600:
                    run_time = now + timedelta(seconds=10)
                    self.scheduler.add_job(
                        self._make_qzone_task_wrapper(qzone_pname), 'date', run_date=run_time, id=job_id, replace_existing=True
                    )
                    logger.debug(f"[DailySharing] 检测到近期错过的QQ空间延迟任务{'['+qzone_pname+']' if qzone_pname else ''}，即将执行补偿分享")
                else:
                    await self.db.update_state_dict(qzone_key, {"pending_delay_job": None})

        # 独立群聊、私聊任务的延迟恢复
        # 使用统一方法解析适配器 ID
        default_adapter_id = self._resolve_adapter_id("_recover_pending_jobs")

        r_groups = self._parse_targets_config(self.receiver_conf.get("groups", []))
        r_users = self._parse_targets_config(self.receiver_conf.get("users", []))
        all_targets = [(gid, True) for gid in r_groups.keys() if gid] + [(uid, False) for uid in r_users.keys() if uid]
        
        def recover_custom_job(tid, is_group):
            target_umo = f"{default_adapter_id}:{'GroupMessage' if is_group else 'FriendMessage'}:{tid}"
            async def delayed_recover():
                if self.plugin._is_terminated: return
                await self.db.update_state_dict(f"target_{tid}", {"pending_delay_job": None})
                async with self._get_lock():
                    logger.debug(f"[DailySharing] 补偿恢复，执行独立分享任务: {tid}")
                    await self.execute_share(specific_target=target_umo)
            return delayed_recover

        for tid, is_group in all_targets:
            t_state = await self.db.get_state(f"target_{tid}", {})
            t_pending = t_state.get("pending_delay_job")
            if t_pending:
                target_ts = t_pending.get("target_time", 0)
                if target_ts > now_ts:
                    run_time = datetime.fromtimestamp(target_ts)
                    self.scheduler.add_job(
                        recover_custom_job(tid, is_group), 'date', run_date=run_time, 
                        id=f"resume_custom_share_{tid}", replace_existing=True
                    )
                elif 0 <= now_ts - target_ts < 3600:
                    run_time = now + timedelta(seconds=random.randint(10, 30))
                    self.scheduler.add_job(
                        recover_custom_job(tid, is_group), 'date', run_date=run_time, 
                        id=f"resume_custom_share_{tid}", replace_existing=True
                    )
                else:
                    await self.db.update_state_dict(f"target_{tid}", {"pending_delay_job": None})

    def setup_cron(self, cron_str, persona_name=None):
        trigger_mode = self.basic_conf.get("trigger_mode", "random_period")
        if persona_name:
            item = self.plugin._find_persona_config(persona_name)
            if item:
                persona_basic = item.get("persona_basic_conf", {})
                trigger_mode = persona_basic.get("trigger_mode", "") or trigger_mode
        
        if trigger_mode == "cron":
            job_id = f"auto_share_{persona_name}" if persona_name else "auto_share"
            self._setup_cron_job_custom(job_id, cron_str, self._make_task_wrapper(persona_name))
        elif trigger_mode == "random_period":
            sched_id = f"daily_random_scheduler_{persona_name}" if persona_name else "daily_random_scheduler"
            self._setup_cron_job_custom(sched_id, "0 0 * * *", self._make_persona_daily_random_scheduler(persona_name))
            asyncio.create_task(self._make_persona_daily_random_scheduler(persona_name)())
            logger.debug(f"[DailySharing] {'人格 ['+persona_name+'] ' if persona_name else ''}已启用多时间段随机生成模式")

    def setup_qzone_cron(self, persona_name=None):
        qzone_conf = self.qzone_conf
        if persona_name:
            item = self.plugin._find_persona_config(persona_name)
            if item:
                persona_qzone = item.get("persona_qzone_conf", {})
                qzone_conf = {**self.qzone_conf, **{k: v for k, v in persona_qzone.items() if v not in ("", [], -1)}}

        trigger_mode = qzone_conf.get("qzone_trigger_mode", "random_period")
        
        if trigger_mode == "cron":
            q_cron = qzone_conf.get("qzone_cron", "0 20 * * *")
            actual_q_cron = CRON_TEMPLATES.get(q_cron, q_cron)
            job_id = f"qzone_share_{persona_name}" if persona_name else "qzone_share"
            self._setup_cron_job_custom(job_id, actual_q_cron, self._make_qzone_task_wrapper(persona_name))
            logger.debug(f"[DailySharing] {'人格 ['+persona_name+'] ' if persona_name else ''}QQ空间定时任务已启动 ({actual_q_cron})")
        elif trigger_mode == "random_period":
            sched_id = f"daily_qzone_random_scheduler_{persona_name}" if persona_name else "daily_qzone_random_scheduler"
            self._setup_cron_job_custom(sched_id, "0 0 * * *", self._make_persona_qzone_random_scheduler(persona_name))
            asyncio.create_task(self._make_persona_qzone_random_scheduler(persona_name)())
            logger.debug(f"[DailySharing] {'人格 ['+persona_name+'] ' if persona_name else ''}QQ空间已启用多时间段随机生成模式")

    def _make_task_wrapper(self, persona_name=None):
        async def wrapper():
            if self.plugin._is_terminated: return
            try:
                days_limit = self.content_service.dedup_days
                await self.db.clean_expired_data(days_limit)
            except Exception as e:
                logger.warning(f"[DailySharing] 数据库清理失败: {e}")

            trigger_mode = self.basic_conf.get("trigger_mode", "random_period")
            if persona_name:
                item = self.plugin._find_persona_config(persona_name)
                if item:
                    persona_basic = item.get("persona_basic_conf", {})
                    trigger_mode = persona_basic.get("trigger_mode", "") or trigger_mode

            random_delay_min = 0
            if trigger_mode == "cron":
                try:
                    random_delay_min = int(self.basic_conf.get("cron_random_delay", 0))
                except Exception:
                    pass

            if random_delay_min > 0:
                delay_seconds = random.randint(0, random_delay_min * 60)
                if delay_seconds > 0:
                    target_time = datetime.now() + timedelta(seconds=delay_seconds)
                    state_key = f"global_{persona_name}" if persona_name else "global"
                    await self.db.update_state_dict(state_key, {"pending_delay_job": {"target_time": target_time.timestamp()}})
                    job_id = f"delayed_auto_share_{persona_name}" if persona_name else "delayed_auto_share"
                    self.scheduler.add_job(
                        self._make_delayed_task(persona_name), 'date',
                        run_date=target_time, id=job_id, replace_existing=True
                    )
                    return

            await self._make_delayed_task(persona_name)()
        return wrapper

    def _make_delayed_task(self, persona_name=None):
        async def delayed():
            if self.plugin._is_terminated: return
            task = asyncio.current_task()
            self.plugin._bg_tasks.add(task)
            try:
                state_key = f"global_{persona_name}" if persona_name else "global"
                await self.db.update_state_dict(state_key, {"pending_delay_job": None})
                now = datetime.now()
                debounce_key = persona_name or "_default"
                last_time = self.plugin._last_share_time.get(debounce_key)
                if last_time:
                    if (now - last_time).total_seconds() < 60:
                        logger.debug(f"[DailySharing] 检测到近期已执行任务{'[人格: '+persona_name+']' if persona_name else ''}，跳过本次触发。")
                        return
                lock = self._get_lock(persona_name)
                if lock.locked():
                    logger.warning(f"[DailySharing] 上一个任务正在进行中{'[人格: '+persona_name+']' if persona_name else ''}，跳过本次触发。")
                    return
                async with lock:
                    self.plugin._last_share_time[debounce_key] = now
                    logger.info(f"[DailySharing] 开始执行分享任务{' [人格: '+persona_name+']' if persona_name else ''}...")
                    await self.execute_share(persona_name=persona_name)
            finally:
                self.plugin._bg_tasks.discard(task)
        return delayed

    def _make_qzone_task_wrapper(self, persona_name=None):
        async def wrapper():
            if self.plugin._is_terminated: return
            task = asyncio.current_task()
            self.plugin._bg_tasks.add(task)
            try:
                await self.execute_qzone_share(persona_name=persona_name)
            finally:
                self.plugin._bg_tasks.discard(task)
        return wrapper

    def _make_persona_daily_random_scheduler(self, persona_name=None):
        async def scheduler():
            if self.plugin._is_terminated: return
            prefix = f"persona_{persona_name}_random_" if persona_name else "random_share_"
            job_ids = [job.id for job in self.scheduler.get_jobs() if job.id.startswith(prefix)]
            for jid in job_ids:
                self.scheduler.remove_job(jid)

            periods = self.plugin.get_persona_config_value(persona_name, "persona_basic_conf", "random_periods", None)
            if not periods:
                periods = self.basic_conf.get("random_periods", ["08:00-10:00", "19:00-21:00"])

            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            state_key = f"global_{persona_name}" if persona_name else "global"
            state = await self.db.get_state(state_key, {})
            random_schedule = state.get("random_schedule", {})

            is_modified = False
            if random_schedule.get("date") != date_str:
                random_schedule = {"date": date_str, "jobs": {}}
                is_modified = True

            jobs = random_schedule.get("jobs", {})
            stale_periods = [p for p in jobs.keys() if p not in periods]
            for p in stale_periods:
                del jobs[p]
                is_modified = True

            for period_str in periods:
                if period_str not in jobs:
                    try:
                        start_str, end_str = period_str.split('-')
                        start_h, start_m = map(int, start_str.split(':'))
                        end_h, end_m = map(int, end_str.split(':'))
                        start_dt = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
                        end_dt = now.replace(hour=end_h, minute=end_m, second=59, microsecond=0)
                        if end_dt <= start_dt: continue
                        random_seconds = random.randint(0, int((end_dt - start_dt).total_seconds()))
                        run_time = start_dt + timedelta(seconds=random_seconds)
                        jobs[period_str] = run_time.timestamp()
                        is_modified = True
                    except Exception as e:
                        logger.error(f"[DailySharing] 解析时间段 {period_str} 失败: {e}")

            adjusted_jobs = self._coordinate_random_times(jobs, now)
            if adjusted_jobs != jobs:
                for p_str, new_ts in adjusted_jobs.items():
                    old_ts = jobs.get(p_str)
                    if old_ts is not None and abs(new_ts - old_ts) > 60:
                        old_t = datetime.fromtimestamp(old_ts).strftime('%H:%M:%S')
                        new_t = datetime.fromtimestamp(new_ts).strftime('%H:%M:%S')
                        logger.info(f"[DailySharing] {'人格 ['+persona_name+'] ' if persona_name else ''}防冲突调整: [{p_str}] {old_t} → {new_t}")
                jobs = adjusted_jobs
                is_modified = True

            if is_modified:
                random_schedule["jobs"] = jobs
                await self.db.update_state_dict(state_key, {"random_schedule": random_schedule})

            for idx, (period_str, timestamp) in enumerate(jobs.items()):
                run_time = datetime.fromtimestamp(timestamp)
                if run_time > now:
                    job_id = f"{prefix}{idx}"
                    self.scheduler.add_job(
                        self._make_task_wrapper(persona_name), 'date',
                        run_date=run_time, id=job_id, replace_existing=True
                    )
                    logger.debug(f"[DailySharing] {'人格 ['+persona_name+'] ' if persona_name else ''}今日随机任务 [{period_str}] 已安排在: {run_time.strftime('%H:%M:%S')} 执行")
        return scheduler

    def _make_persona_qzone_random_scheduler(self, persona_name=None):
        async def scheduler():
            if self.plugin._is_terminated: return
            prefix = f"persona_{persona_name}_qzone_random_" if persona_name else "qzone_random_share_"
            job_ids = [job.id for job in self.scheduler.get_jobs() if job.id.startswith(prefix)]
            for jid in job_ids:
                self.scheduler.remove_job(jid)

            periods = self.plugin.get_persona_config_value(persona_name, "persona_qzone_conf", "qzone_random_periods", None)
            if not periods:
                periods = self.qzone_conf.get("qzone_random_periods", ["08:00-10:00", "19:00-21:00"])

            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            state_key = f"qzone_{persona_name}" if persona_name else "qzone"
            state = await self.db.get_state(state_key, {})
            qzone_random_schedule = state.get("random_schedule", {})

            is_modified = False
            if qzone_random_schedule.get("date") != date_str:
                qzone_random_schedule = {"date": date_str, "jobs": {}}
                is_modified = True

            jobs = qzone_random_schedule.get("jobs", {})
            stale_periods = [p for p in jobs.keys() if p not in periods]
            for p in stale_periods:
                del jobs[p]
                is_modified = True

            for period_str in periods:
                if period_str not in jobs:
                    try:
                        start_str, end_str = period_str.split('-')
                        start_h, start_m = map(int, start_str.split(':'))
                        end_h, end_m = map(int, end_str.split(':'))
                        start_dt = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
                        end_dt = now.replace(hour=end_h, minute=end_m, second=59, microsecond=0)
                        if end_dt <= start_dt: continue
                        random_seconds = random.randint(0, int((end_dt - start_dt).total_seconds()))
                        run_time = start_dt + timedelta(seconds=random_seconds)
                        jobs[period_str] = run_time.timestamp()
                        is_modified = True
                    except Exception as e:
                        logger.error(f"[DailySharing] 解析QQ空间时间段 {period_str} 失败: {e}")

            if is_modified:
                qzone_random_schedule["jobs"] = jobs
                await self.db.update_state_dict(state_key, {"random_schedule": qzone_random_schedule})

            for idx, (period_str, timestamp) in enumerate(jobs.items()):
                run_time = datetime.fromtimestamp(timestamp)
                if run_time > now:
                    job_id = f"{prefix}{idx}"
                    self.scheduler.add_job(
                        self._make_qzone_task_wrapper(persona_name), 'date',
                        run_date=run_time, id=job_id, replace_existing=True
                    )
                    logger.debug(f"[DailySharing] {'人格 ['+persona_name+'] ' if persona_name else ''}今日QQ空间随机任务 [{period_str}] 已安排在: {run_time.strftime('%H:%M:%S')} 执行")
        return scheduler


    def _setup_cron_job_custom(self, job_id: str, cron_str: str, func):
        """通用 Cron 设置方法"""
        if self.plugin._is_terminated: return
        try:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)

            actual_cron = CRON_TEMPLATES.get(cron_str, cron_str)
            parts = actual_cron.split()
            
            if len(parts) == 5:
                self.scheduler.add_job(
                    func, 'cron',
                    minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4],
                    id=job_id,
                    replace_existing=True,
                    max_instances=1
                )
                logger.debug(f"[DailySharing] 任务[{job_id}]已设定: {actual_cron}")
            else:
                logger.error(f"[DailySharing] 任务[{job_id}]无效的 Cron 表达式: {cron_str}")
        except Exception as e:
            logger.error(f"[DailySharing] 任务[{job_id}]设置失败: {e}")


    async def _task_wrapper_briefing(self):
        """早报任务回调"""
        if self.plugin._is_terminated: return
        task = asyncio.current_task()
        self.plugin._bg_tasks.add(task)
        try:
            await self.execute_briefing_share()
        finally:
            self.plugin._bg_tasks.discard(task)


    def _coordinate_random_times(self, jobs: dict, now: datetime) -> dict:
        if len(jobs) < 2:
            return jobs

        parsed = {}
        for period_str, ts in jobs.items():
            try:
                start_str, end_str = period_str.split('-')
                start_h, start_m = map(int, start_str.split(':'))
                end_h, end_m = map(int, end_str.split(':'))
                start_dt = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
                end_dt = now.replace(hour=end_h, minute=end_m, second=59, microsecond=0)
                if end_dt <= start_dt:
                    continue
                parsed[period_str] = {
                    "timestamp": ts,
                    "start_ts": start_dt.timestamp(),
                    "end_ts": end_dt.timestamp(),
                }
            except Exception:
                continue

        if len(parsed) < 2:
            return jobs

        all_starts = [v["start_ts"] for v in parsed.values()]
        all_ends = [v["end_ts"] for v in parsed.values()]
        union_start = min(all_starts)
        union_end = max(all_ends)
        union_span = union_end - union_start

        if union_span <= 0:
            return jobs

        num = len(parsed)
        ideal_gap = union_span / (num + 1)
        min_gap = max(1800, ideal_gap * 0.5)

        for _round in range(5):
            sorted_items = sorted(parsed.items(), key=lambda x: x[1]["timestamp"])
            conflict_found = False

            for i in range(len(sorted_items) - 1):
                p1, d1 = sorted_items[i]
                p2, d2 = sorted_items[i + 1]
                gap = d2["timestamp"] - d1["timestamp"]

                if gap >= min_gap:
                    continue

                conflict_found = True
                needed = min_gap - gap

                push_later_ok = (d2["timestamp"] + needed) <= d2["end_ts"]
                pull_earlier_ok = (d1["timestamp"] - needed) >= d1["start_ts"]

                if push_later_ok and pull_earlier_ok:
                    later_room = d2["end_ts"] - d2["timestamp"] - needed
                    earlier_room = d1["timestamp"] - d1["start_ts"] - needed
                    if later_room >= earlier_room:
                        d2["timestamp"] += needed
                    else:
                        d1["timestamp"] -= needed
                elif push_later_ok:
                    d2["timestamp"] += needed
                elif pull_earlier_ok:
                    d1["timestamp"] -= needed
                else:
                    reduced_gap = d2["end_ts"] - d1["start_ts"]
                    if reduced_gap > 0:
                        mid = (d1["start_ts"] + d2["end_ts"]) / 2
                        d1_new = mid - reduced_gap / 2
                        d2_new = mid + reduced_gap / 2
                        d1["timestamp"] = max(d1["start_ts"], min(d1_new, d1["end_ts"]))
                        d2["timestamp"] = max(d2["start_ts"], min(d2_new, d2["end_ts"]))
                    else:
                        logger.warning(f"[DailySharing] 窗口 [{p1}] 和 [{p2}] 完全重叠且过窄，无法拉开间隔")

            if not conflict_found:
                break

        result = {}
        for period_str, data in parsed.items():
            result[period_str] = data["timestamp"]

        return result

    def get_curr_period(self) -> TimePeriod:
        h = datetime.now().hour
        if 0 <= h < 6: return TimePeriod.DAWN
        if 6 <= h < 9: return TimePeriod.MORNING
        if 9 <= h < 12: return TimePeriod.FORENOON
        if 12 <= h < 16: return TimePeriod.AFTERNOON
        if 16 <= h < 19: return TimePeriod.EVENING
        if 19 <= h < 22: return TimePeriod.NIGHT
        return TimePeriod.LATE_NIGHT

    def get_period_range_str(self, period: TimePeriod) -> str:
        """获取时段对应的时间范围字符串"""
        return {
            TimePeriod.DAWN: "00:00-06:00",            
            TimePeriod.MORNING: "06:00-09:00",
            TimePeriod.FORENOON: "09:00-12:00",
            TimePeriod.AFTERNOON: "12:00-16:00",
            TimePeriod.EVENING: "16:00-19:00",
            TimePeriod.NIGHT: "19:00-22:00",
            TimePeriod.LATE_NIGHT: "22:00-24:00"
        }.get(period, "")

    def _resolve_adapter_id(self, context_source: str = "unknown", receiver_conf=None) -> str:
        rc = receiver_conf or self.receiver_conf
        configured_id = rc.get("adapter_id", "").strip()
        if configured_id:
            if self.plugin._cached_adapter_id != configured_id:
                self.plugin._cached_adapter_id = configured_id
                logger.debug(f"[DailySharing] 已使用配置的适配器: {configured_id} ({context_source})")
            return configured_id

        if self.plugin._cached_adapter_id:
            return self.plugin._cached_adapter_id

        try:
            if hasattr(self.plugin.context, "platform_manager"):
                insts = self.plugin.context.platform_manager.get_insts()
                for inst in insts:
                    if hasattr(inst, "metadata") and inst.metadata.id:
                        self.plugin._cached_adapter_id = inst.metadata.id
                        logger.debug(f"[DailySharing] 自动发现适配器: {inst.metadata.id} ({context_source})")
                        return inst.metadata.id
        except Exception as e:
            logger.warning(f"[DailySharing] 自动发现适配器失败: {e}")

        fallback_id = "aiocqhttp"
        logger.warning(f"[DailySharing] 无法确定适配器 ID，使用兜底值 '{fallback_id}' ({context_source})")
        return fallback_id

    async def decide_type_with_state(self, current_period: TimePeriod, is_qzone: bool = False, target_id: str = None, specific_type: str = "auto", persona_name: str = None) -> SharingType:
        if is_qzone:
            state_key = f"qzone_{persona_name}" if persona_name else "qzone"
        else:
            state_key = f"target_{target_id}" if target_id else "global"
            
        state = await self.db.get_state(state_key, {})

        if specific_type and specific_type.lower() != "auto":
            seq_str = specific_type.replace("，", ",")
            custom_seq = [s.strip().lower() for s in seq_str.split(",") if s.strip()]
            
            if custom_seq and custom_seq != ["auto"]:
                idx_key = "custom_sequence_index"
                idx = state.get(idx_key, 0)
                if idx >= len(custom_seq): idx = 0
                
                selected_str = custom_seq[idx]
                next_idx = (idx + 1) % len(custom_seq)
                
                await self.db.update_state_dict(state_key, {
                    idx_key: next_idx, 
                    "last_timestamp": datetime.now().isoformat()
                })
                
                if selected_str != "auto":
                    try: 
                        return SharingType(selected_str)
                    except ValueError:
                        pass

        conf_node = self.qzone_conf if is_qzone else self.basic_conf
        prefix = "qzone_" if is_qzone else ""
        config_key_map = {
            TimePeriod.MORNING: f"{prefix}morning_sequence",
            TimePeriod.FORENOON: f"{prefix}forenoon_sequence",
            TimePeriod.AFTERNOON: f"{prefix}afternoon_sequence",
            TimePeriod.EVENING: f"{prefix}evening_sequence",
            TimePeriod.NIGHT: f"{prefix}night_sequence",
            TimePeriod.LATE_NIGHT: f"{prefix}late_night_sequence",
            TimePeriod.DAWN: f"{prefix}dawn_sequence"
        }
        
        config_key = config_key_map.get(current_period)
        seq = []

        if persona_name:
            persona_conf_key = "persona_qzone_conf" if is_qzone else "persona_basic_conf"
            persona_seq_key = f"{prefix}{current_period.name.lower()}_sequence"
            seq = self.plugin.get_persona_config_value(persona_name, persona_conf_key, persona_seq_key, None) or []

        if not seq:
            seq = conf_node.get(config_key, [])
        
        if not seq:
            fallback = QZONE_SHARING_TYPE_SEQUENCES if is_qzone else SHARING_TYPE_SEQUENCES
            seq = fallback.get(current_period, [SharingType.GREETING.value])

        last_type = state.get("last_type", "")

        mood_boosted = await self._get_mood_driven_candidates(seq, persona_name=persona_name)
        candidates = seq

        weights = []
        for t in candidates:
            if t == last_type:
                weights.append(1)
            elif t in mood_boosted:
                weights.append(5)
            else:
                weights.append(2)

        total = sum(weights)
        if total == 0:
            selected = candidates[0] if candidates else SharingType.GREETING.value
        else:
            r = random.random() * total
            cumulative = 0
            selected = candidates[0]
            for t, w in zip(candidates, weights):
                cumulative += w
                if r <= cumulative:
                    selected = t
                    break

        await self.db.update_state_dict(state_key, {
            "last_period": current_period.value,
            "last_timestamp": datetime.now().isoformat(),
            "last_type": selected
        })
        
        try: return SharingType(selected)
        except: return SharingType.GREETING

    async def _get_mood_driven_candidates(self, fallback_seq: list, persona_name: str = None) -> set:
        """根据 DayMind 心情数据返回应被加权提升的分享类型集合（不替换候选池）"""
        try:
            content_svc = self.content_service
            if not content_svc:
                return set()

            mood_data = await content_svc._get_daymind_mood(persona_name=persona_name)
            if not mood_data:
                return set()

            label = mood_data.get("label", "")
            if not label:
                return set()

            positive_moods = {"开心", "放松", "期待", "安心"}
            neutral_moods = {"平静"}
            negative_moods = {"烦躁", "紧张", "委屈", "低落", "疲惫"}
            dream_moods = {"疲惫", "低落", "平静"}

            candidates = set()

            if label in positive_moods:
                candidates = {"life_moment", "recommendation", "greeting"}
            elif label in neutral_moods:
                candidates = {"life_moment", "news", "mood"}
            elif label in negative_moods:
                candidates = {"rant", "mood", "life_moment"}

            if label in dream_moods:
                candidates.add("dream")

            if not candidates:
                return set()

            return candidates & set(fallback_seq)

        except Exception as e:
            logger.debug(f"[DailySharing] 心情驱动选择失败: {e}")
            return set()

    def _parse_targets_config(self, conf_list):
        """核心解析器：支持 群号:Cron时间:类型 这种三段式复杂写法"""
        if isinstance(conf_list, dict): return conf_list
        res = {}
        if isinstance(conf_list, list):
            for item in conf_list:
                s = str(item).strip()
                if not s: continue
                # 支持中英文冒号混用                
                s = s.replace("：", ":")
                parts = [p.strip() for p in s.split(":")]
                
                target_id = parts[0]
                if len(parts) == 1:
                    # 只有群号
                    res[target_id] = {"cron": None, "seq": None}
                elif len(parts) == 2:
                    # 只有群号和类型
                    res[target_id] = {"cron": None, "seq": parts[1]}
                elif len(parts) >= 3:
                    # 群号 : 时间 : 类型 (例如 123456:0 7 * * *:news)
                    cron_str = ":".join(parts[1:-1]).strip()
                    seq_str = parts[-1].strip()
                    res[target_id] = {"cron": cron_str, "seq": seq_str}
        return res

    def get_broadcast_targets(self, exclude_custom_cron=False, receiver_conf=None):
        targets = []
        rc = receiver_conf or self.receiver_conf
        default_adapter_id = self._resolve_adapter_id("get_broadcast_targets", receiver_conf=rc)

        if default_adapter_id:
            r_groups = self._parse_targets_config(rc.get("groups", []))
            r_users = self._parse_targets_config(rc.get("users", []))
            for gid, conf in r_groups.items():
                if gid:
                    if exclude_custom_cron and isinstance(conf, dict) and conf.get("cron"):
                        continue
                    targets.append(f"{default_adapter_id}:GroupMessage:{gid}")
            for uid, conf in r_users.items():
                if uid:
                    if exclude_custom_cron and isinstance(conf, dict) and conf.get("cron"):
                        continue
                    targets.append(f"{default_adapter_id}:FriendMessage:{uid}")

        return targets

    def get_briefing_targets(self):
        """获取早报的独立广播目标，不填则不发"""
        targets = []
        default_adapter_id = self._resolve_adapter_id("get_briefing_targets")

        if default_adapter_id:
            b_groups = self.extra_shares_conf.get("briefing_groups", [])
            b_users = self.extra_shares_conf.get("briefing_users", [])
            for gid in b_groups:
                # 只取纯数字，防止用户误填冒号
                gid_clean = str(gid).split(":")[0].strip()
                if gid_clean:
                    targets.append(f"{default_adapter_id}:GroupMessage:{gid_clean}")
            for uid in b_users:
                uid_clean = str(uid).split(":")[0].strip()
                if uid_clean:
                    targets.append(f"{default_adapter_id}:FriendMessage:{uid_clean}")

        return targets

    async def async_daily_share_task(
        self,
        event: AstrMessageEvent,
        share_type: str,
        source: str,
        get_image: bool,
        need_image: bool,
        need_video: bool,
        need_voice: bool,
        to_qzone: bool
    ):
        """实际执行分享逻辑的后台任务 (LLM 触发)"""
        try:
            persona_name = await self.plugin.resolve_persona_from_event(event)
            
            # 特殊图片类型处理 (60s / AI) 
            st_clean = share_type.lower().replace(" ", "")
            
            # 60s新闻
            if any(k in st_clean for k in ["60s", "六十秒", "读世界"]):
                url = self.news_service.get_60s_image_url()
                if not url:
                    await event.send(event.plain_result("获取 每天60s读世界 失败，请检查API Key配置。"))
                    return 
                    
                if to_qzone:
                    qzone_plugin = self.ctx_service._find_plugin("qzone")
                    if qzone_plugin and hasattr(qzone_plugin, "service"):
                        self.plugin._inject_qzone_client(qzone_plugin)
                        try:
                            await qzone_plugin.service.publish_post(text="【每天60秒读懂世界】", images=[url])
                            await event.send(event.plain_result("每天60s读世界 已成功分享到QQ空间！"))
                            await self.db.add_sent_history("qzone_broadcast", "news", "【每天60秒读懂世界】", True)
                        except Exception as e:
                            await event.send(event.plain_result(f"QQ空间分享失败: {e}"))
                    else:
                        await event.send(event.plain_result("未检测到QQ空间插件！"))
                else:
                    await event.send(event.image_result(url))
                return 

            # AI资讯
            if any(k in st_clean for k in ["ai资讯", "ai新闻", "ai日报"]) or st_clean == "ai":
                ai_data = await self.news_service.get_ai_news_json()
                if not ai_data:
                    await event.send(event.plain_result("获取 AI资讯快报 失败，今日暂无更新。"))
                    return 

                url = self.news_service.get_ai_news_image_url()
                if not url:
                    await event.send(event.plain_result("获取 AI资讯快报 图片失败，请检查API Key配置。"))
                    return 
                    
                if to_qzone:
                    qzone_plugin = self.ctx_service._find_plugin("qzone")
                    if qzone_plugin and hasattr(qzone_plugin, "service"):
                        self.plugin._inject_qzone_client(qzone_plugin)
                        try:
                            await qzone_plugin.service.publish_post(text="【AI资讯快报】", images=[url])
                            await event.send(event.plain_result("AI资讯快报 已成功分享到QQ空间！"))
                            await self.db.add_sent_history("qzone_broadcast", "news", "【AI资讯快报】", True)
                        except Exception as e:
                            await event.send(event.plain_result(f"QQ空间分享失败: {e}"))
                    else:
                        await event.send(event.plain_result("未检测到QQ空间插件！"))
                else:
                    await event.send(event.image_result(url))
                return 

            # === 常规流程 ===
            # 参数清洗与映射
            target_type_enum = None
            
            if share_type == "自动" or share_type == "auto":
                target_type_enum = None  
            else:
                # 映射分享类型 (中文 -> 枚举)
                if share_type in CMD_CN_MAP:
                    target_type_enum = CMD_CN_MAP[share_type]
                else:
                    # 模糊匹配尝试
                    for k, v in CMD_CN_MAP.items():
                        if k in share_type:
                            target_type_enum = v
                            break
                if not target_type_enum:
                    await event.send(event.plain_result(f"不支持的分享类型：{share_type}。支持：自动, 问候, 新闻, 心情, 日常, 吐槽, 梦境, 推荐, 60s新闻, AI资讯。"))
                    return

            # 映射新闻源 (中文 -> key)
            news_src_key = None
            if target_type_enum == SharingType.NEWS and source:
                if source in SOURCE_CN_MAP:
                    news_src_key = SOURCE_CN_MAP[source]
                elif source in NEWS_SOURCE_MAP:
                    news_src_key = source
                else:
                    for name, key in SOURCE_CN_MAP.items():
                        if name in source or source in name:
                            news_src_key = key
                            break
            
            # 逻辑判定：新闻默认发静态图
            is_news = (target_type_enum == SharingType.NEWS)
            
            # 触发静态图发送的条件：
            if is_news and get_image and not need_image and not need_voice and not need_video:
                try:
                    img_url = None
                    src_name = ""
                    # 优先使用指定的源热搜
                    if news_src_key:
                        img_url, src_name = self.news_service.get_hot_news_image_url(news_src_key)
                    else:
                        # 如果没有指定，则随机选择一个已启用的新闻源发送
                        random_src = self.news_service.select_news_source(persona_name=persona_name)
                        img_url, src_name = self.news_service.get_hot_news_image_url(random_src)

                    if img_url:
                        if to_qzone:
                            qzone_plugin = self.ctx_service._find_plugin("qzone")
                            if qzone_plugin and hasattr(qzone_plugin, "service"):
                                self.plugin._inject_qzone_client(qzone_plugin)
                                try:
                                    await qzone_plugin.service.publish_post(text=f"【{src_name}】", images=[img_url])
                                    await event.send(event.plain_result(f"[{src_name}] 图片已成功分享到QQ空间！"))
                                    await self.db.add_sent_history("qzone_broadcast", "news", f"【{src_name}】长图(LLM)", True)
                                except Exception as e:
                                    await event.send(event.plain_result(f"QQ空间分享失败: {e}"))
                            else:
                                await event.send(event.plain_result("未检测到QQ空间插件！"))
                        else:
                            await event.send(event.image_result(img_url))
                    else:
                        await event.send(event.plain_result("获取新闻图片失败。"))
                except Exception as e:
                    logger.error(f"[DailySharing] 获取新闻图片失败: {e}")
                    await event.send(event.plain_result(f"获取新闻图片失败。"))
                
                return

            # 如果用户要求发QQ空间文案说说
            if to_qzone:
                await self.execute_qzone_share(force_type=target_type_enum, news_source=news_src_key, event=event)
                return

            # 场景 B: 标准 LLM 生成流程
            
            # 获取上下文 ID
            uid = event.get_sender_id()
            if not ":" in str(uid):
                target_umo = event.unified_msg_origin
            else:
                target_umo = uid

            # 重新计算时段
            period = self.get_curr_period()
            
            # 准备数据
            life_ctx = await self.ctx_service.get_life_context(persona_name=persona_name)
            news_data = None
            
            # 初始化 img_path (可能用于存放热搜截图)
            img_path = None
            
            if target_type_enum == SharingType.NEWS:
                # 这里的 news_src_key 如果是 None 会自动选择
                if not news_src_key:
                    news_src_key = self.news_service.select_news_source(persona_name=persona_name)
                news_data = await self.news_service.get_hot_news(news_src_key)
                
                # 如果在主流程中且配置允许带上新闻图
                if get_image and not need_image and self.image_conf.get("attach_hot_news_image", True):
                    try:
                        img_path, _ = self.news_service.get_hot_news_image_url(news_src_key)
                    except Exception as e:
                        logger.warning(f"[DailySharing] 主流程获取热搜图片失败: {e}")

            # 获取历史
            is_group = self.ctx_service._is_group_chat(target_umo)
            hist_data = await self.ctx_service.get_history_data(target_umo, is_group)
            hist_prompt = self.ctx_service.format_history_prompt(hist_data, target_type_enum)
            group_info = hist_data.get("group_info")
            life_prompt = self.ctx_service.format_life_context(life_ctx, target_type_enum, is_group, group_info, persona_name=persona_name)
            
            # 获取近期动态记忆
            recent_dynamics_str = ""
            ref_count = self.context_conf.get("reference_history_count", 3)
            if ref_count > 0:
                recent_hist = await self.db.get_recent_history_by_target(uid, limit=ref_count, persona_name=persona_name or "")
                if recent_hist:
                    lines = []
                    for h in reversed(recent_hist):
                        clean_content = re.sub(r'\$\$(?:EMO:)?(?:happy|sad|angry|neutral|surprise)\$\$', '', h.get('content', ''), flags=re.IGNORECASE).strip()
                        lines.append(f"- [{h.get('type')}] {clean_content}")
                    recent_dynamics_str = "\n".join(lines)

            # 获取昵称
            nickname = ""
            if not is_group:
                nickname = event.get_sender_name()

            # 生成内容
            content = await self.content_service.generate(
                target_type_enum, period, target_umo, is_group, life_prompt, hist_prompt, news_data, nickname=nickname, recent_dynamics=recent_dynamics_str, persona_name=persona_name
            )
            
            if not content:
                await event.send(event.plain_result("内容生成失败，请稍后再试。"))
                return
            
            # ================= 视觉生成逻辑 =================
            video_url = None
            should_gen_visual = False
            
            if self.image_conf.get("enable_ai_image", False):
                if need_image or need_video:
                    should_gen_visual = True

            if should_gen_visual:
                # 生成图片 (注意：如果生成了AI图片，会覆盖上面的热搜截图 img_path)
                ai_img_path = await self.image_service.generate_image(content, target_type_enum, life_ctx)
                if ai_img_path:
                    img_path = ai_img_path
                
                # 生成视频 (如果明确要求视频，且图片是本地文件而非URL)
                if img_path and self.image_conf.get("enable_ai_video", False):
                    if need_video and not img_path.startswith("http"):
                        video_url = await self.image_service.generate_video_from_image(img_path, content, persona_name=persona_name)

            # ================= 语音生成逻辑 =================
            audio_path = None
            if self.tts_conf.get("enable_tts", False):
                should_gen_voice = False
                if need_voice:
                    should_gen_voice = True
                        
                if should_gen_voice:
                    audio_path = await self.ctx_service.text_to_speech(content, target_umo, target_type_enum, period, persona_name=persona_name)

            # 发送 (img_path 可能是热搜截图，也可能是AI画的图)
            await self.send(target_umo, content, img_path, audio_path, video_url)
            
            # 记录上下文
            img_desc = self.image_service.get_last_description()
            await self.ctx_service.record_bot_reply_to_history(target_umo, content, image_desc=img_desc)
            await self.ctx_service.record_to_memos(target_umo, content, img_desc)
                
        except Exception as e:
            logger.error(f"[DailySharing] 异步任务错误: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await event.send(event.plain_result(f"执行出错: {str(e)}"))

    async def execute_briefing_share(self, specific_target: str = None):
        """执行早报分享：依次发送开启的 60s 和 AI 资讯"""
        if self.plugin._is_terminated: return
        
        logger.info("[DailySharing] 开始执行早报分享任务")
        
        # 1. 收集需要分享的图片 URL
        images_to_send = [] 
        
        check_60s = self.extra_shares_conf.get("enable_60s_news", False)
        if specific_target: check_60s = True 
        
        if self.extra_shares_conf.get("enable_60s_news", False):
            url = self.news_service.get_60s_image_url()
            if url: images_to_send.append(("每天60s读世界", url))

        if self.extra_shares_conf.get("enable_ai_news", False):
            ai_data = await self.news_service.get_ai_news_json()
            if ai_data:
                url = self.news_service.get_ai_news_image_url()
                if url: images_to_send.append(("AI资讯快报", url))
            else:
                logger.info("[DailySharing] 获取 AI资讯快报 失败，今日暂无更新，跳过分享图片")

        if not images_to_send:
            logger.warning("[DailySharing] 早报任务触发，发现没有开启的早报发送或获取图片失败")
            return

        # 定时早报自动同步到QQ空间
        if specific_target is None and self.extra_shares_conf.get("sync_briefing_to_qzone", False):
            qzone_plugin = self.ctx_service._find_plugin("qzone")
            if qzone_plugin and hasattr(qzone_plugin, "service"):
                self.plugin._inject_qzone_client(qzone_plugin)
                logger.info("[DailySharing] 分享早报到QQ空间已开启...")
                for name, url in images_to_send:
                    try:
                        title = "【每天60秒读懂世界】" if "60s" in name else "【AI资讯快报】"
                        await qzone_plugin.service.publish_post(text=title, images=[url])
                        await self.db.add_sent_history("qzone_broadcast", "news", f"{title}(定时自动)", True)
                        await asyncio.sleep(3) 
                        logger.info(f"[DailySharing] 分享早报 {name} 到QQ空间成功！")
                    except Exception as e:
                        logger.error(f"[DailySharing] 分享早报 {name} 到QQ空间失败: {e}")
            else:
                logger.warning("[DailySharing] 分享早报到QQ空间开启，但未检测到 astrbot_plugin_qzone 插件")

        # 2. 确定目标 (使用全新的独立列表)
        targets = []
        if specific_target:
            targets.append(specific_target)
        else:
            targets = self.get_briefing_targets()
            logger.info(f"[DailySharing] 早报将分享到 {len(targets)} 个目标会话")

        if not targets:
            logger.info("[DailySharing] 未配置任何早报接收目标，已跳过分享。")
            return

        # 3. 分享循环
        for uid in targets:
            if self.plugin._is_terminated: break
            try:
                for name, url in images_to_send:
                    # 构建消息链
                    msg = MessageChain().url_image(url)
                    logger.info(f"[DailySharing] 正在分享 {name} 到 {uid}")
                    await self.plugin.context.send_message(uid, msg)
                    # 每张图之间间隔 1 秒
                    await asyncio.sleep(1)
                
                # 每个群之间间隔 2 秒
                await asyncio.sleep(2) 
            except Exception as e:
                logger.error(f"[DailySharing] 分享早报到 {uid} 失败: {e}")

    async def execute_share(self, force_type: SharingType = None, news_source: str = None, specific_target: str = None, persona_name: str = None):
        if self.plugin._is_terminated: return

        period = self.get_curr_period()
        life_ctx = await self.ctx_service.get_life_context(persona_name=persona_name)

        targets = []
        
        if specific_target:
            targets.append(specific_target)
        else:
            receiver_conf = self.plugin.get_persona_receiver(persona_name) if persona_name else self.receiver_conf
            targets = self.get_broadcast_targets(exclude_custom_cron=True, receiver_conf=receiver_conf)

        if not targets:
            logger.warning(f"[DailySharing]{' ['+persona_name+']' if persona_name else ''} 未配置接收对象，且未指定目标，请在配置页填写群号或QQ号")
            return

        receiver_conf = self.plugin.get_persona_receiver(persona_name) if persona_name else self.receiver_conf
        r_groups = self._parse_targets_config(receiver_conf.get("groups", []))
        r_users = self._parse_targets_config(receiver_conf.get("users", []))

        for uid in targets:
            if self.plugin._is_terminated: break
            try:
                is_group = "group" in uid.lower() or "room" in uid.lower() or "guild" in uid.lower()
                
                adapter_id, real_id = self.ctx_service._parse_umo(uid)
                
                target_specific_type = self.plugin.get_persona_config_value(persona_name, "persona_basic_conf", "sharing_type", None) or self.basic_conf.get("sharing_type", "auto")
                if is_group and real_id in r_groups:
                    conf = r_groups[real_id]
                    st = conf.get("seq") if isinstance(conf, dict) else conf
                    if st is not None: target_specific_type = st
                elif not is_group and real_id in r_users:
                    conf = r_users[real_id]
                    st = conf.get("seq") if isinstance(conf, dict) else conf
                    if st is not None: target_specific_type = st

                if force_type:
                    stype = force_type
                else:
                    stype = await self.decide_type_with_state(period, is_qzone=False, target_id=uid, specific_type=target_specific_type, persona_name=persona_name)

                logger.info(f"[DailySharing]{' ['+persona_name+']' if persona_name else ''} 正在为 {uid} 生成内容... 时段: {period.value}, 类型: {stype.value}")
                
                news_data = None
                if stype == SharingType.NEWS:
                    state = await self.db.get_state(f"target_{uid}", {})
                    last_news_source = state.get("last_news_source")
                    
                    current_news_source = news_source
                    if not current_news_source:
                        current_news_source = self.news_service.select_news_source(excluded_source=last_news_source, persona_name=persona_name)
                        
                    news_data = await self.news_service.get_hot_news(current_news_source)
                    if news_data:
                        await self.db.update_state_dict(f"target_{uid}", {"last_news_source": news_data[1]})

                nickname = ""
                if not is_group:
                    try:
                        if adapter_id and real_id:
                            bot = self.ctx_service._get_bot_instance(adapter_id)
                            if bot:
                                ret = await bot.api.call_action("get_stranger_info", user_id=int(real_id))
                                if ret and isinstance(ret, dict):
                                    nickname = ret.get("nickname", "")
                                    logger.info(f"[DailySharing] 获取到用户昵称: {nickname}")
                    except Exception as e:
                         logger.warning(f"[DailySharing] 获取昵称失败: {e}")

                hist_data = await self.ctx_service.get_history_data(uid, is_group)
                if is_group and "group_info" in hist_data:
                    if not specific_target and not self.ctx_service.check_group_strategy(hist_data["group_info"]):
                        logger.info(f"[DailySharing] 因策略跳过群组 {uid}")
                        continue

                hist_prompt = self.ctx_service.format_history_prompt(hist_data, stype)
                group_info = hist_data.get("group_info")
                life_prompt = self.ctx_service.format_life_context(life_ctx, stype, is_group, group_info, persona_name=persona_name)

                recent_dynamics_str = ""
                ref_count = self.plugin.get_persona_config_value(persona_name, "persona_context_conf", "reference_history_count", None) or self.context_conf.get("reference_history_count", 3)
                if ref_count > 0:
                    recent_hist = await self.db.get_recent_history_by_target(uid, limit=ref_count, persona_name=persona_name or "")
                    if recent_hist:
                        lines = []
                        for h in reversed(recent_hist):  
                            clean_content = re.sub(r'\$\$(?:EMO:)?(?:happy|sad|angry|neutral|surprise)\$\$', '', h.get('content', ''), flags=re.IGNORECASE).strip()
                            lines.append(f"- [{h.get('type')}] {clean_content}")
                        recent_dynamics_str = "\n".join(lines)

                content = await self.content_service.generate(
                    stype, period, uid, is_group, life_prompt, hist_prompt, news_data, nickname=nickname, recent_dynamics=recent_dynamics_str, persona_name=persona_name
                )
                
                if not content:
                    logger.warning(f"[DailySharing] 内容生成失败 {uid}")
                    await self.db.add_sent_history(
                        target_id=uid,
                        sharing_type=stype.value,
                        content="生成失败 (LLM无响应)",
                        success=False,
                        persona_name=persona_name or ""
                    )
                    continue
                
                img_path = None
                video_url = None
                enable_img_global = self.plugin.get_persona_config_value(persona_name, "persona_image_conf", "enable_ai_image", None)
                if enable_img_global is None:
                    enable_img_global = self.image_conf.get("enable_ai_image", False)
                img_allowed_types = self.plugin.get_persona_config_value(persona_name, "persona_image_conf", "image_enabled_types", None)
                if not img_allowed_types:
                    img_allowed_types = self.image_conf.get("image_enabled_types", ["greeting", "mood", "life_moment", "recommendation"])
                
                if stype == SharingType.NEWS and self.image_conf.get("attach_hot_news_image", True):
                    try:
                        state = await self.db.get_state(f"target_{uid}", {})
                        last_source = state.get("last_news_source")
                        if last_source:
                            img_path, _ = self.news_service.get_hot_news_image_url(last_source)
                    except Exception as e:
                        logger.warning(f"[DailySharing] 自动任务获取新闻图片失败: {e}")

                if enable_img_global:
                    if stype.value in img_allowed_types:
                        ai_img_path = await self.image_service.generate_image(content, stype, life_ctx, persona_name=persona_name)
                        if ai_img_path:
                            img_path = ai_img_path
                            
                        enable_ai_video = self.plugin.get_persona_config_value(persona_name, "persona_image_conf", "enable_ai_video", None)
                        if enable_ai_video is None:
                            enable_ai_video = self.image_conf.get("enable_ai_video", False)
                        if img_path and enable_ai_video and not img_path.startswith("http"):
                            video_allowed = self.image_conf.get("video_enabled_types", ["greeting", "mood"])
                            if stype.value in video_allowed:
                                video_url = await self.image_service.generate_video_from_image(img_path, content, persona_name=persona_name)
                    else:
                         logger.info(f"[DailySharing] 当前类型 {stype.value} 不在配图允许列表，跳过配图。")

                audio_path = None
                enable_tts_global = self.plugin.get_persona_config_value(persona_name, "persona_tts_conf", "enable_tts", None)
                if enable_tts_global is None:
                    enable_tts_global = self.tts_conf.get("enable_tts", False)
                tts_allowed_types = self.plugin.get_persona_config_value(persona_name, "persona_tts_conf", "tts_enabled_types", None)
                if not tts_allowed_types:
                    tts_allowed_types = self.tts_conf.get("tts_enabled_types", ["greeting", "mood"])
                
                if enable_tts_global:
                    if stype.value in tts_allowed_types:
                        audio_path = await self.ctx_service.text_to_speech(content, uid, stype, period, persona_name=persona_name)
                    else:
                        logger.info(f"[DailySharing] 当前类型 {stype.value} 不在语音允许列表，跳过语音。")

                # 分享内容
                await self.send(uid, content, img_path, audio_path, video_url)
                
                # 获取图片描述并写入 AstrBot 聊天上下文
                img_desc = self.image_service.get_last_description()
                await self.ctx_service.record_bot_reply_to_history(uid, content, image_desc=img_desc)

                # 记录与历史
                await self.ctx_service.record_to_memos(uid, content, img_desc)

                # 清洗历史记录内容中的情感标签
                clean_content_for_log = re.sub(r'\$\$(?:EMO:)?(?:happy|sad|angry|neutral|surprise)\$\$', '', content, flags=re.IGNORECASE).strip()

                await self.db.add_sent_history(
                    target_id=uid,
                    sharing_type=stype.value,
                    content=clean_content_for_log[:100] + "...",
                    success=True,
                    persona_name=persona_name or ""
                )
                
                await asyncio.sleep(2) 

            except Exception as e:
                logger.error(f"[DailySharing] 处理 {uid} 时出错: {e}")
                import traceback
                logger.error(traceback.format_exc())               

    async def execute_qzone_share(self, force_type: SharingType = None, news_source: str = None, event: AstrMessageEvent = None, persona_name: str = None):
        if self.plugin._is_terminated: return
        
        try:
            qzone_plugin = self.ctx_service._find_plugin("qzone")
            if not qzone_plugin or not hasattr(qzone_plugin, "service"):
                logger.warning("[DailySharing] QQ空间任务触发，但未检测到 astrbot_plugin_qzone 插件")
                if event:
                    await event.send(event.plain_result("未检测到 astrbot_plugin_qzone 插件"))
                return

            self.plugin._inject_qzone_client(qzone_plugin)
            period = self.get_curr_period()
            stype = force_type if force_type else await self.decide_type_with_state(period, is_qzone=True, persona_name=persona_name) 
            logger.info(f"[DailySharing]{' ['+persona_name+']' if persona_name else ''} QQ空间时段: {period.value}, 类型: {stype.value}")

            life_ctx = await self.ctx_service.get_life_context(persona_name=persona_name)
            news_data = None
            
            # 如果是发新闻，单独获取热搜（支持手动指定源）
            if stype == SharingType.NEWS:
                actual_source = news_source if news_source else self.news_service.select_news_source(persona_name=persona_name)
                news_data = await self.news_service.get_hot_news(actual_source)

            # 屏蔽历史记录，使用纯净的提示词让LLM写说说
            qzone_life_prompt = self.ctx_service.format_life_context(life_ctx, stype, False, None, persona_name=persona_name)
            qzone_life_prompt += (
                "\n\n【最高优先级覆盖指令】\n"
                "这是一条个人QQ空间社交平台的动态说说\n"
                "当前任务是以纯粹的【个人日记或心情独白】的口吻来写。\n"
                "1. 请以你的人设性格说话，真实自然\n"
                "2. 只能专注描绘自己的状态，就像自己在自言自语一样。"
            )
            
            # 获取近期动态记忆 (QQ空间)
            qzone_recent_dynamics_str = ""
            ref_count = self.context_conf.get("reference_history_count", 3)
            if ref_count > 0:
                q_recent_hist = await self.db.get_recent_history_by_target("qzone_broadcast", limit=ref_count, persona_name=persona_name or "")
                if q_recent_hist:
                    lines = []
                    for h in reversed(q_recent_hist):
                        clean_content = re.sub(r'\$\$(?:EMO:)?(?:happy|sad|angry|neutral|surprise)\$\$', '', h.get('content', ''), flags=re.IGNORECASE).strip()
                        lines.append(f"- [{h.get('type')}] {clean_content}")
                    qzone_recent_dynamics_str = "\n".join(lines)

            logger.info("[DailySharing] 正在为QQ空间生成文案...")
            qzone_content = await self.content_service.generate(
                stype, period, "qzone_broadcast", False, qzone_life_prompt, "", news_data, nickname="", recent_dynamics=qzone_recent_dynamics_str, persona_name=persona_name
            )
            
            if not qzone_content:
                logger.error("[DailySharing] QQ空间文案生成失败")
                if event:
                    await event.send(event.plain_result("QQ空间文案生成失败"))
                return

            # 清洗情感标签
            clean_qzone_content = re.sub(r'\$\$(?:EMO:)?(?:happy|sad|angry|neutral|surprise)\$\$', '', qzone_content, flags=re.IGNORECASE).strip()

            # 处理配图逻辑
            qzone_images = []
            target_local_img = None
            
            enable_img_qzone = self.plugin.get_persona_config_value(persona_name, "persona_qzone_conf", "qzone_enable_image", None)
            if enable_img_qzone is None:
                enable_img_qzone = self.qzone_conf.get("qzone_enable_image", False)
            enable_img_global = self.plugin.get_persona_config_value(persona_name, "persona_image_conf", "enable_ai_image", None)
            if enable_img_global is None:
                enable_img_global = self.image_conf.get("enable_ai_image", False)
            
            # 获取QQ空间配图允许类型，如果没配置，默认复用群聊分享的配置
            qzone_img_allowed_types = self.qzone_conf.get(
                "qzone_image_enabled_types", 
                self.image_conf.get("image_enabled_types", ["greeting", "mood", "life_moment", "recommendation"])
            )

            if enable_img_qzone and enable_img_global:
                if stype.value in qzone_img_allowed_types:
                    logger.info("[DailySharing] 正在为QQ空间生成配图...")
                    try:
                        new_img_path = await self.image_service.generate_image(clean_qzone_content, stype, life_ctx, persona_name=persona_name)
                        if new_img_path:
                            target_local_img = new_img_path
                    except Exception as e:
                        logger.error(f"[DailySharing] QQ空间配图生成失败: {e}")
                else:
                    logger.info(f"[DailySharing] 当前类型 {stype.value} 不在QQ空间配图允许列表，跳过配图。")
            
            # 如果是新闻类型，且没有开启画图，且配置允许附带热搜图，尝试贴热搜图
            if stype == SharingType.NEWS and not target_local_img and self.qzone_conf.get("qzone_attach_hot_news_image", True):
                try:
                    if news_data:
                        img_url, _ = self.news_service.get_hot_news_image_url(news_data[1])
                        target_local_img = img_url
                except Exception as e:
                    pass

            if target_local_img:
                if target_local_img.startswith("http"):
                    qzone_images.append(target_local_img)
                else:
                    qzone_images.append(f"local_path::{target_local_img}")
                            
            import sys
            import aiofiles
            qzone_utils_mod = None
            for mod_name, mod in sys.modules.items():
                if "qzone" in mod_name and "utils" in mod_name and hasattr(mod, "download_file"):
                    qzone_utils_mod = mod
                    break
                    
            if qzone_utils_mod:
                orig_download_file = qzone_utils_mod.download_file
                async def patched_download_file(url: str):
                    if isinstance(url, str) and url.startswith("local_path::"):
                        real_path = url.split("::", 1)[1]
                        try:
                            async with aiofiles.open(real_path, "rb") as f:
                                return await f.read()  
                        except Exception:
                            return None
                    return await orig_download_file(url)
                qzone_utils_mod.download_file = patched_download_file
                
            try:
                await qzone_plugin.service.publish_post(
                    text=clean_qzone_content,
                    images=qzone_images
                )
                logger.info("[DailySharing] 成功分享内容到QQ空间！")
                
                await self.db.add_sent_history(
                    target_id="qzone_broadcast",
                    sharing_type=stype.value,
                    content=clean_qzone_content[:100] + "...",
                    success=True,
                    persona_name=persona_name or ""
                )
                
                if event:
                    try:
                        text_chain = MessageChain().message(clean_qzone_content)
                        await event.send(text_chain)
                        
                        if target_local_img:
                            await asyncio.sleep(1.0) 
                            img_chain = MessageChain()
                            if target_local_img.startswith("http"):
                                img_chain.url_image(target_local_img)
                            else:
                                img_chain.file_image(target_local_img)
                            await event.send(img_chain)
                    except Exception as e:
                        logger.error(f"[DailySharing] 同步发送内容到会话失败: {e}")
                
            finally:
                if qzone_utils_mod:
                    qzone_utils_mod.download_file = orig_download_file

        except Exception as e:
            logger.error(f"[DailySharing] 生成并分享到QQ空间失败: {e}")
            if event:
                try:
                    await event.send(event.plain_result(f"生成并分享到QQ空间失败: {e}"))
                except:
                    pass

    async def send(self, uid, text, img_path, audio_path=None, video_url=None):
        """分享内容（支持分开分享，支持语音和视频）"""
        if self.plugin._is_terminated: return

        separate_img = self.image_conf.get("separate_text_and_image", True)
        prefer_audio_only = self.tts_conf.get("prefer_audio_only", False)
        
        clean_text = re.sub(r'\$\$(?:EMO:)?(?:happy|sad|angry|neutral|surprise)\$\$', '', text, flags=re.IGNORECASE).strip()
        
        should_send_text = True
        if audio_path and prefer_audio_only:
            should_send_text = False

        # 1. 分享文字
        if should_send_text and clean_text: 
            try:
                text_chain = MessageChain().message(clean_text) 
                if img_path and not video_url and not separate_img and not audio_path:
                    if img_path.startswith("http"): text_chain.url_image(img_path)
                    else: text_chain.file_image(img_path)
                await self.plugin.context.send_message(uid, text_chain)
            except Exception as e:
                logger.error(f"[DailySharing] 发送文字给 {uid} 失败: {e}")
            
            if audio_path or ((img_path or video_url) and separate_img):
                await self.random_sleep()

        # 2. 分享语音
        if audio_path:
            try:
                audio_chain = MessageChain()
                audio_chain.chain.append(Record(file=audio_path))
                await self.plugin.context.send_message(uid, audio_chain)
            except Exception as e:
                logger.error(f"[DailySharing] 发送语音给 {uid} 失败: {e}")
            
            if (img_path or video_url) and separate_img:
                await self.random_sleep()
        
        # 3. 分享视觉媒体（视频优先，其次图片）
        if video_url:
            max_video_retries = 3
            for video_attempt in range(max_video_retries):
                try:
                    video_chain = MessageChain()
                    if video_url.startswith("http"):
                        video_chain.chain.append(Video.fromURL(video_url))
                    else:
                        video_chain.chain.append(Video.fromFileSystem(video_url))
                    await self.plugin.context.send_message(uid, video_chain)
                    break
                except Exception as e:
                    if video_attempt < max_video_retries - 1:
                        wait_sec = 3 * (video_attempt + 1)
                        logger.warning(f"[DailySharing] 发送视频给 {uid} 失败 (尝试 {video_attempt+1}/{max_video_retries}): {e}，{wait_sec}s 后重试...")
                        await asyncio.sleep(wait_sec)
                    else:
                        logger.error(f"[DailySharing] 发送视频给 {uid} 失败 (已重试{max_video_retries}次): {e}")
        elif img_path:
            img_not_sent_yet = separate_img or audio_path or not should_send_text or not clean_text
            if img_not_sent_yet:
                try:
                    img_chain = MessageChain()
                    if img_path.startswith("http"): img_chain.url_image(img_path)
                    else: img_chain.file_image(img_path)
                    await self.plugin.context.send_message(uid, img_chain)
                except Exception as e:
                    logger.error(f"[DailySharing] 发送图片给 {uid} 失败: {e}")

    async def random_sleep(self):
        """随机延迟"""
        if self.plugin._is_terminated: return

        delay_str = self.image_conf.get("separate_send_delay", "1.0-2.0")
        try:
            if "-" in str(delay_str):
                d_min, d_max = map(float, str(delay_str).split("-"))
                await asyncio.sleep(random.uniform(d_min, d_max))
            else:
                await asyncio.sleep(float(delay_str))
        except:
            await asyncio.sleep(1.5)
