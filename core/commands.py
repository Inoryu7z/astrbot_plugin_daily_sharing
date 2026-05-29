import asyncio
from datetime import datetime
from astrbot.api.event import AstrMessageEvent
from ..config import SharingType, TimePeriod, SHARING_TYPE_SEQUENCES, QZONE_SHARING_TYPE_SEQUENCES
from .constants import TYPE_CN_MAP

class CommandHandler:
    def __init__(self, plugin):
        self.plugin = plugin
        self.db = plugin.db
        self.config = plugin.config

    def _get_first_enabled_persona_conf(self):
        entries = self.plugin.get_enabled_personas()
        if not entries:
            return {}, None
        entry = entries[0]
        pname = entry.get("persona_name") or entry.get("name") or entry.get("select_persona", "")
        canonical = self.plugin._canonical_persona_name(pname) or (pname if pname else "")
        return entry, canonical

    def _get_persona_val(self, conf_key, sub_key, default=None):
        _, pname = self._get_first_enabled_persona_conf()
        if pname:
            return self.plugin.get_persona_config_value(pname, conf_key, sub_key, default)
        return default

    async def cmd_enable(self, event: AstrMessageEvent):
        personas = self.config.get("personas", [])
        for p in personas:
            if isinstance(p, dict):
                p["enabled"] = True
        await self.plugin._save_config_file()
        self.plugin.task_manager.setup_tasks()
        if not self.plugin.scheduler.running:
            self.plugin.scheduler.start()
        yield event.plain_result("自动分享已启用")

    async def cmd_disable(self, event: AstrMessageEvent):
        personas = self.config.get("personas", [])
        for p in personas:
            if isinstance(p, dict):
                p["enabled"] = False
        await self.plugin._save_config_file()
        for job in self.plugin.scheduler.get_jobs():
            self.plugin.scheduler.remove_job(job.id)
        yield event.plain_result("自动分享已禁用")

    async def cmd_status(self, event: AstrMessageEvent):
        target_uid = event.unified_msg_origin
        state_key = f"target_{target_uid}"
        state = await self.db.get_state(state_key, {})

        enabled_personas = self.plugin.get_enabled_personas()
        enabled = bool(enabled_personas)

        last_type_raw = state.get('last_type', '无')
        last_type_cn = TYPE_CN_MAP.get(last_type_raw, last_type_raw)

        period = self.plugin.task_manager.get_curr_period()
        time_range = self.plugin.task_manager.get_period_range_str(period)

        recent_history = await self.db.get_recent_history_by_target(target_uid, limit=5)
        hist_txt = "无记录"
        if recent_history:
            lines = []
            for h in recent_history:
                ts = str(h.get("timestamp", ""))
                content_preview = h.get('content', '') or ""
                t_raw = h.get('type')
                t_cn = TYPE_CN_MAP.get(t_raw, t_raw)
                lines.append(f"• {ts} [{t_cn}] {content_preview}")
            hist_txt = "\n".join(lines)

        adapter_id, real_id = self.plugin.ctx_service._parse_umo(target_uid)
        is_group = self.plugin.ctx_service._is_group_chat(target_uid)

        _, first_pname = self._get_first_enabled_persona_conf()
        receiver = self.plugin.get_persona_receiver(first_pname) if first_pname else {"groups": [], "users": []}
        r_groups = self.plugin.task_manager._parse_targets_config(receiver.get("groups", []))
        r_users = self.plugin.task_manager._parse_targets_config(receiver.get("users", []))

        custom_cron = "无"
        target_specific_type = "auto"
        if is_group and real_id in r_groups:
            conf = r_groups[real_id]
            if isinstance(conf, dict):
                custom_cron = conf.get("cron") or "无"
                target_specific_type = conf.get("seq", "auto")
        elif not is_group and real_id in r_users:
            conf = r_users[real_id]
            if isinstance(conf, dict):
                custom_cron = conf.get("cron") or "无"
                target_specific_type = conf.get("seq", "auto")

        is_custom_seq = target_specific_type != "auto"
        idx_display = state.get('custom_sequence_index', 0) if is_custom_seq else state.get('sequence_index', 0)

        persona_info = ""
        if enabled_personas:
            persona_names = [p.get("persona_name") or p.get("name") or p.get("select_persona", "?") for p in enabled_personas]
            persona_info = f"\n已配置人格: {', '.join(persona_names)}"

        msg = f"""每日分享状态
================
运行状态: {'启用' if enabled else '禁用'}
全局触发: 随机时段{persona_info}

【当前会话独立配置】
独立定时: {custom_cron}
分享类型: {target_specific_type}

【当前会话执行状态】
当前时段: {period.value} ({time_range})
上次类型: {last_type_cn}
上次时间: {state.get('last_timestamp', '无')[5:16].replace('T', ' ')}
当前指针: {idx_display}

【最近记录】
{hist_txt}
"""
        yield event.plain_result(msg)

    async def cmd_reset_seq(self, event: AstrMessageEvent):
        is_qzone = "空间" in event.message_str

        if is_qzone:
            qzone_updates = {"sequence_index": 0, "custom_sequence_index": 0, "last_period": None}
            for p in TimePeriod:
                qzone_updates[f"index_{p.value}"] = 0
            await self.db.update_state_dict("qzone", qzone_updates)
            yield event.plain_result("QQ空间的序列指针已重置")

        else:
            target_uid = event.unified_msg_origin
            state_key = f"target_{target_uid}"

            updates = {"sequence_index": 0, "custom_sequence_index": 0, "last_period": None}
            for p in TimePeriod:
                updates[f"index_{p.value}"] = 0
            await self.db.update_state_dict(state_key, updates)
            yield event.plain_result("当前会话的序列指针已重置")

    async def cmd_view_seq(self, event: AstrMessageEvent):
        target_uid = event.unified_msg_origin
        is_qzone = "空间" in event.message_str

        period = self.plugin.task_manager.get_curr_period()
        time_range = self.plugin.task_manager.get_period_range_str(period)

        adapter_id, real_id = self.plugin.ctx_service._parse_umo(target_uid)
        is_group = self.plugin.ctx_service._is_group_chat(target_uid)

        _, first_pname = self._get_first_enabled_persona_conf()
        receiver = self.plugin.get_persona_receiver(first_pname) if first_pname else {"groups": [], "users": []}
        r_groups = self.plugin.task_manager._parse_targets_config(receiver.get("groups", []))
        r_users = self.plugin.task_manager._parse_targets_config(receiver.get("users", []))

        target_specific_type = "auto"
        if not is_qzone:
            if is_group and real_id in r_groups:
                conf = r_groups[real_id]
                target_specific_type = conf.get("seq", "auto") if isinstance(conf, dict) else conf
            elif not is_group and real_id in r_users:
                conf = r_users[real_id]
                target_specific_type = conf.get("seq", "auto") if isinstance(conf, dict) else conf
        else:
            target_specific_type = "auto"

        state_key = "qzone" if is_qzone else f"target_{target_uid}"
        state = await self.db.get_state(state_key, {})

        if target_specific_type and target_specific_type.lower() != "auto":
            seq_str = target_specific_type.replace("，", ",")
            custom_seq = [s.strip().lower() for s in seq_str.split(",") if s.strip()]

            if custom_seq and custom_seq != ["auto"]:
                idx = state.get("custom_sequence_index", 0)
                if idx >= len(custom_seq): idx = 0

                target_desc = "QQ空间" if is_qzone else "当前会话"

                txt = f"当前时段: {period.value} ({time_range})\n"
                txt += f"{target_desc}: 独立时段序列\n"
                for i, t_raw in enumerate(custom_seq):
                    mark = "👉 " if i == idx else "   "
                    t_cn = TYPE_CN_MAP.get(t_raw, t_raw)
                    txt += f"{mark}{i}. {t_cn}\n"
                yield event.plain_result(txt)
                return

        prefix = "qzone_" if is_qzone else ""
        conf_key = "persona_qzone_conf" if is_qzone else "persona_basic_conf"
        config_key_map = {
            TimePeriod.MORNING: f"{prefix}morning_sequence",
            TimePeriod.FORENOON: f"{prefix}forenoon_sequence",
            TimePeriod.AFTERNOON: f"{prefix}afternoon_sequence",
            TimePeriod.EVENING: f"{prefix}evening_sequence",
            TimePeriod.NIGHT: f"{prefix}night_sequence",
            TimePeriod.LATE_NIGHT: f"{prefix}late_night_sequence",
            TimePeriod.DAWN: f"{prefix}dawn_sequence"
        }
        config_key = config_key_map.get(period)
        seq = self._get_persona_val(conf_key, config_key, [])
        if not seq:
            fallback = QZONE_SHARING_TYPE_SEQUENCES if is_qzone else SHARING_TYPE_SEQUENCES
            seq = fallback.get(period, [])

        idx_key = f"index_{period.value}"
        idx = state.get(idx_key, 0)
        if idx >= len(seq): idx = 0

        txt = f"当前时段: {period.value} ({time_range})\n"
        txt += f"当前会话: 全局时段序列\n"
        for i, t_raw in enumerate(seq):
            mark = "👉 " if i == idx else "   "
            t_cn = TYPE_CN_MAP.get(t_raw, t_raw)
            txt += f"{mark}{i}. {t_cn}\n"
        yield event.plain_result(txt)

    async def cmd_set_seq(self, event, parts):
        if len(parts) > 2 and parts[2].isdigit():
            target_idx = int(parts[2])
            is_qzone = "空间" in parts
            target_uid = event.unified_msg_origin

            adapter_id, real_id = self.plugin.ctx_service._parse_umo(target_uid)
            is_group = self.plugin.ctx_service._is_group_chat(target_uid)

            _, first_pname = self._get_first_enabled_persona_conf()
            receiver = self.plugin.get_persona_receiver(first_pname) if first_pname else {"groups": [], "users": []}
            r_groups = self.plugin.task_manager._parse_targets_config(receiver.get("groups", []))
            r_users = self.plugin.task_manager._parse_targets_config(receiver.get("users", []))

            target_specific_type = "auto"
            if not is_qzone:
                if is_group and real_id in r_groups:
                    conf = r_groups[real_id]
                    target_specific_type = conf.get("seq", "auto") if isinstance(conf, dict) else conf
                elif not is_group and real_id in r_users:
                    conf = r_users[real_id]
                    target_specific_type = conf.get("seq", "auto") if isinstance(conf, dict) else conf
            else:
                target_specific_type = "auto"

            state_key = "qzone" if is_qzone else f"target_{target_uid}"

            if target_specific_type and target_specific_type.lower() != "auto":
                seq_str = target_specific_type.replace("，", ",")
                custom_seq = [s.strip().lower() for s in seq_str.split(",") if s.strip()]
                if custom_seq and custom_seq != ["auto"]:
                    if 0 <= target_idx < len(custom_seq):
                        await self.db.update_state_dict(state_key, {
                            "custom_sequence_index": target_idx,
                            "sequence_index": target_idx
                        })
                        t_raw = custom_seq[target_idx]
                        t_cn = TYPE_CN_MAP.get(t_raw, t_raw)
                        target_desc = "QQ空间" if is_qzone else "当前独立序列"
                        yield event.plain_result(f"已切换[{target_desc}]下一次自动分享：{target_idx}. {t_cn}")
                    else:
                        yield event.plain_result(f"序号无效，独立序列范围: 0 ~ {len(custom_seq)-1}")
                    return

            period = self.plugin.task_manager.get_curr_period()
            conf_key = "persona_qzone_conf" if is_qzone else "persona_basic_conf"
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
            config_key = config_key_map.get(period)
            seq = self._get_persona_val(conf_key, config_key, [])
            if not seq:
                fallback = QZONE_SHARING_TYPE_SEQUENCES if is_qzone else SHARING_TYPE_SEQUENCES
                seq = fallback.get(period, [])

            if 0 <= target_idx < len(seq):
                idx_key = f"index_{period.value}"
                await self.db.update_state_dict(state_key, {
                    idx_key: target_idx,
                    "sequence_index": target_idx,
                    "last_period": period.value
                })
                t_raw = seq[target_idx]
                t_cn = TYPE_CN_MAP.get(t_raw, t_raw)
                target_desc = "QQ空间" if is_qzone else "当前时段"
                yield event.plain_result(f"已切换[{target_desc}]下一次自动分享：{target_idx}. {t_cn}")
            else:
                yield event.plain_result(f"序号无效，当前时段[{period.value}] 范围: 0 ~ {len(seq)-1}")
        else:
            yield event.plain_result("格式错误。例如：/分享 指定序列 1\n可加后缀：空间")

    async def cmd_briefing_qzone_sync(self, event: AstrMessageEvent, parts: list):
        _, first_pname = self._get_first_enabled_persona_conf()
        if len(parts) > 2 and parts[2] in ["开启", "关闭"]:
            enable = (parts[2] == "开启")
            if first_pname:
                item = self.plugin._find_persona_config(first_pname)
                if item is not None:
                    extra = item.setdefault("persona_extra_shares", {})
                    extra["sync_briefing_to_qzone"] = enable
            await self.plugin._save_config_file()
            yield event.plain_result(f"✅ 定时早报自动同步QQ空间功能已【{parts[2]}】。")
        else:
            status_val = self._get_persona_val("persona_extra_shares", "sync_briefing_to_qzone", False)
            status = "开启" if status_val else "关闭"
            yield event.plain_result(f"ℹ️ 当前分享早报到QQ空间状态为: 【{status}】\n提示：发送 /分享 早报空间 开启/关闭 来切换。")

    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result("""每日分享插件帮助:
/分享 [类型] - 立即在当前会话生成分享 (默认文字模式)
支持类型: 问候、新闻、心情、日常、吐槽、梦境、推荐、60s、ai

【可用后缀】
 1. 广播：/分享 [类型] 广播 - 向所有配置的群聊、私聊发送
 2. 空间：/分享 [类型] 空间 - 单独生成文案并分享到QQ空间
 3. 图片：/分享 新闻 [源] 图片 -直接分享热搜图片

【配置指令】
/分享 开启/关闭 - 启停自动分享
/分享 早报空间 开启/关闭 - 启停自动分享早报到QQ空间
/分享 状态 - 查看本会话的运行状态
/分享 查看序列 - 查看本会话当前时段序列及指针
/分享 指定序列 [序号] - 调整本会话分享内容指针位置 (支持加后缀 空间)
/分享 重置序列 - 重置本会话分享内容序列到开头""")
