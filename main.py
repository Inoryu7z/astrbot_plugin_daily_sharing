import asyncio
import json
import random
import os
import re 
from typing import Optional
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api import AstrBotConfig

from .config import TimePeriod, SharingType, NEWS_SOURCE_MAP
from .core.constants import CMD_CN_MAP, SOURCE_CN_MAP, TYPE_CN_MAP
from .core.news import NewsService
from .core.image import ImageService
from .core.content import ContentService
from .core.context import ContextService
from .core.db import DatabaseManager 
from .core.tasks import TaskManager
from .core.commands import CommandHandler
from .core.persona_utils import PersonaConfigMixin
from .core.smart_share import SmartShareScheduler

class DailySharingPlugin(Star, PersonaConfigMixin):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config 
        self.receiver_conf = self.config.get("receiver", {})
        self.scheduler = AsyncIOScheduler()
        
        # 分享内容记录条数 
        self.history_limit = 100
        
        # 锁与防抖
        self._locks = {}
        self._last_share_time = {}
        
        # 生命周期标志位 
        self._is_terminated = False
        
        # 缓存 Adapter ID 
        self._cached_adapter_id = None 

        # 任务追踪 (用于生命周期清理)
        self._bg_tasks = set()
        
        # 数据路径
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_daily_sharing_inoryu7z")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 配置文件路径
        config_dir = self.data_dir.parent.parent / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = config_dir / "astrbot_plugin_daily_sharing_inoryu7z_config.json"
        
        # 数据库初始化
        self.db = DatabaseManager(self.data_dir)
        
        # 初始化服务层
        self.ctx_service = ContextService(context, config, self)
        self.news_service = NewsService(config, self)
        self.image_service = ImageService(context, config, self._call_llm_wrapper, self)
        
        # 初始化内容服务
        self.content_service = ContentService(
            config, 
            self._call_llm_wrapper, 
            context,
            self.db, 
            self.news_service,
            self
        )
        
        # 核心逻辑解耦器
        self.task_manager = TaskManager(self)
        self.command_handler = CommandHandler(self)
        self.smart_scheduler = SmartShareScheduler(self)
        
        # 启动延迟初始化 Bot 缓存的任务
        bot_init_task = asyncio.create_task(self._delayed_init_bots())
        self._bg_tasks.add(bot_init_task)
        bot_init_task.add_done_callback(self._bg_tasks.discard)

    def _get_lock(self, persona_name: str = None) -> asyncio.Lock:
        key = persona_name or "_default"
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def get_enabled_personas(self) -> list:
        entries = self._persona_entries()
        return entries

    def get_persona_config_value(self, persona_name: str, conf_key: str, sub_key: str, default=None):
        item = self._find_persona_config(persona_name)
        if item is not None:
            persona_conf = item.get(conf_key, {})
            if isinstance(persona_conf, dict) and sub_key in persona_conf:
                val = persona_conf[sub_key]
                if val is not None and val != "" and val != []:
                    return val
        return default

    def get_persona_receiver(self, persona_name: str) -> dict:
        item = self._find_persona_config(persona_name)
        if item is not None:
            pr = item.get("persona_receiver", {})
            if isinstance(pr, dict) and (pr.get("groups") or pr.get("users") or pr.get("adapter_id")):
                return pr
        return self.receiver_conf if self.receiver_conf else {"groups": [], "users": [], "adapter_id": ""}

    def _persona_name_of_entry(self, entry: dict) -> str:
        """条目的人格标识；与 _find_persona_config 的别名口径一致，避免只填 persona_id/alias 的条目被漏掉"""
        aliases = self._persona_aliases(entry) if isinstance(entry, dict) else []
        return aliases[0] if aliases else ""

    async def _resolve_persona_from_framework(self, event) -> str:
        """按 AstrBot 口径解析当前会话实际生效的人格名

        顺序：umo 级 session_service_config 强制人格 > 会话绑定 persona > 全局默认人格。
        这是唯一能正确处理「用户 d 绑 f 人格」这类会话规则的路径 —— 只读 conversation.persona_id
        会漏掉 umo 级会话规则。
        """
        umo = getattr(event, "unified_msg_origin", None)
        persona_mgr = getattr(self.context, "persona_manager", None)
        if not umo or not persona_mgr:
            return ""

        conv_pid = None
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr:
                conv_id = await conv_mgr.get_curr_conversation_id(umo)
                if conv_id:
                    conv = await conv_mgr.get_conversation(conv_id)
                    conv_pid = getattr(conv, "persona_id", None) if conv else None
        except Exception:
            conv_pid = None

        try:
            platform_name = event.get_platform_name() or ""
        except Exception:
            platform_name = ""

        try:
            _pid, persona, _force, _webchat = await persona_mgr.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conv_pid,
                platform_name=platform_name,
            )
        except Exception as e:
            logger.debug(f"[DailySharing] 框架人格解析失败（降级到其它方式）: {e}")
            return ""

        if isinstance(persona, dict):
            name = persona.get("name") or ""
        elif persona is not None:
            name = getattr(persona, "name", "") or ""
        else:
            name = ""
        if not name:
            return ""
        # 框架给的人格不一定归本插件管（例如 AstrBot 的默认人格）。直接透传会让下游
        # get_persona_receiver / get_persona_config_value 全部退化到全局配置、锁 key 也对不上，
        # 所以这里只认插件 personas 里真实存在的人格。
        if not self._find_persona_config(name):
            return ""
        return self._canonical_persona_name(name) or name

    def _match_personas_by_receiver(self, event, names: list = None) -> list:
        """按 persona_receiver 配置反查：这个会话（umo）挂在哪个人格下"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        parts = umo.split(":")
        if len(parts) < 3:
            return []
        adapter_id, msg_type = parts[0], parts[1]
        target_id = ":".join(parts[2:])
        is_group = "Group" in msg_type

        if names is None:
            names = [self._persona_name_of_entry(e) for e in self.get_enabled_personas()]
        names = [n for n in names if n]

        hits = []
        for name in names:
            # 只看人格自己配的 persona_receiver：get_persona_receiver 在人格未配置时会回退全局，
            # 那样会让所有人格都命中同一个目标，反查就失去意义。
            item = self._find_persona_config(name) or {}
            receiver = item.get("persona_receiver") or {}
            conf_ids = self.task_manager._parse_targets_config(
                receiver.get("groups" if is_group else "users", [])
            )
            if target_id not in {str(k) for k in conf_ids.keys()}:
                continue
            conf_adapter = (receiver.get("adapter_id") or "").strip()
            if conf_adapter and conf_adapter != adapter_id:
                continue
            hits.append(name)
        return hits

    async def _resolve_event_persona(self, event) -> str:
        """当前会话对应的人格名；解析不出来返回空串（绝不兜底成第一个人格）"""
        try:
            name = await self._resolve_persona_from_framework(event)
            if name:
                return name
        except Exception:
            pass
        try:
            names = [self._persona_name_of_entry(e) for e in self.get_enabled_personas()]
            hits = self._match_personas_by_receiver(event, [n for n in names if n])
            if len(hits) == 1:
                return hits[0]
        except Exception:
            pass
        return ""

    async def resolve_persona_from_event(self, event) -> str:
        """当前会话的人格：框架（仅限插件已配置的人格）→ 投递目标反查 → 第一个人格

        末级 entries[0] 是为兼容 /分享、LLM 工具的历史行为保留的；/重新调度 走的是
        _resolve_event_persona，不会静默落到第一个人格。
        """
        try:
            name = await self._resolve_persona_from_framework(event)
            if name:
                return name
        except Exception:
            pass
        try:
            names = [n for n in (self._persona_name_of_entry(e) for e in self.get_enabled_personas()) if n]
            hits = self._match_personas_by_receiver(event, names)
            if len(hits) == 1:
                return hits[0]
        except Exception:
            pass
        entries = self.get_enabled_personas()
        if entries:
            first_name = self._persona_name_of_entry(entries[0])
            if first_name:
                return self._canonical_persona_name(first_name) or first_name
        return ""

    def _inject_qzone_client(self, qzone_plugin):
        """尝试为 QQ空间 插件注入 CQHttp 客户端，解决自动任务时没有 client 的报错"""
        try:
            if qzone_plugin and hasattr(qzone_plugin, "cfg") and not qzone_plugin.cfg.client:
                if self.ctx_service.bot_map:
                    # 优先寻找 aiocqhttp 适配器
                    aiocqhttp_bot = None
                    for pid, bot in self.ctx_service.bot_map.items():
                        if "aiocqhttp" in pid.lower():
                            aiocqhttp_bot = bot
                            break
                    bot_client = aiocqhttp_bot or list(self.ctx_service.bot_map.values())[0]
                    if bot_client:
                        qzone_plugin.cfg.client = bot_client
                        logger.debug(f"[DailySharing] QQ空间插件注入客户端成功！")
        except Exception as e:
            logger.warning(f"[DailySharing] QQ空间插件注入客户端失败: {e}")        

    async def initialize(self):
        """初始化插件"""
        task = asyncio.create_task(self._delayed_init())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def terminate(self):
        """插件卸载/重载时的清理逻辑"""
        self._is_terminated = True 
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
            
            for task in self._bg_tasks:
                if not task.done():
                    task.cancel()

            await self.news_service.close()
            await self.content_service.close()
            
            logger.info("[DailySharing] 插件已停止，清理资源完成")
        except Exception as e:
            logger.error(f"[DailySharing] 停止插件出错: {e}")        

    async def _delayed_init(self):
        """延迟初始化逻辑 (调度器)"""
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            return 
        
        # 再次检查终止状态，防止僵尸实例启动调度器
        if self._is_terminated:
            return

        # 启动时清理一次过期数据
        try:
            days_limit = int(self.config.get("data_retention_days", 60))
            await self.db.clean_expired_data(days_limit)
        except Exception:
            pass

        has_targets = False
        for entry in self.get_enabled_personas():
            pname = entry.get("persona_name") or entry.get("name") or entry.get("select_persona", "")
            if pname:
                canonical = self._canonical_persona_name(pname) or pname
                receiver = self.get_persona_receiver(canonical)
                if receiver.get("groups") or receiver.get("users"):
                    has_targets = True
                    break
        
        if not has_targets:
            logger.warning("[DailySharing] 未配置接收对象 (receiver)")

        # 通过 TaskManager 挂载所有定时任务
        self.task_manager.setup_tasks()
        
        # 启动调度器 
        if not self._is_terminated and not self.scheduler.running:
            self.scheduler.start()

    async def _delayed_init_bots(self):
        """延迟初始化 Bot 缓存"""
        try:
            # 等待 30 秒，确保 AstrBot 核心和适配器完全加载
            await asyncio.sleep(30)
            if self._is_terminated: return
            
            # 调用 ContextService 进行 Bot 扫描
            await self.ctx_service.init_bots()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[DailySharing] Bot 初始化任务出错: {e}")

    @staticmethod
    def _write_json_sync(path, data):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def _save_config_file(self):
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_json_sync, self.config_file, self.config)
        except Exception as e:
            logger.error(f"[DailySharing] 保存配置失败: {e}")

    async def _call_llm_wrapper(self, prompt: str, system_prompt: str = None, timeout: int = 60, max_retries: int = 2, tools: list = None, persona_name: str = None, image_urls: list = None, provider_id: str = None) -> Optional[str]:
        """LLM 调用包装器（支持失败重试与自动降级，支持图片输入）"""
        if self._is_terminated: return None
        
        def _get_system_default_provider() -> str:
            try:
                cfg = self.context.get_config()
                if cfg:
                    pid = cfg.get("provider_settings", {}).get("default_provider_id", "")
                    if pid: return pid
                    for p in cfg.get("provider", []):
                        if p.get("enable", False) and "chat" in p.get("provider_type", "chat"):
                            return p.get("id")
            except Exception:
                pass
            return ""

        if provider_id:
            user_provider_id = provider_id
            config_timeout = 300
        elif persona_name:
            user_provider_id = self.get_persona_config_value(persona_name, "persona_llm_conf", "llm_provider_id", "")
            config_timeout = 300
        else:
            user_provider_id = ""
            config_timeout = 300

        current_provider_id = user_provider_id if user_provider_id else _get_system_default_provider()

        actual_timeout = max(timeout, config_timeout)

        for attempt in range(max_retries + 1):
            if self._is_terminated: return None
            
            is_last_attempt = (attempt == max_retries)
            if is_last_attempt and attempt > 0 and user_provider_id and current_provider_id == user_provider_id:
                default_pid = _get_system_default_provider()
                if default_pid and default_pid != current_provider_id:
                    logger.info(f"[DailySharing] 指定 LLM 已达到重试次数，降级使用默认的第一个模型({default_pid})...")
                    current_provider_id = default_pid

            try:
                kwargs = {"prompt": prompt}
                if system_prompt is not None and system_prompt != "":
                    kwargs["system_prompt"] = system_prompt
                if current_provider_id:
                    kwargs["chat_provider_id"] = current_provider_id
                if tools:
                    kwargs["func_tool_names"] = tools
                if image_urls:
                    kwargs["image_urls"] = image_urls

                resp = await asyncio.wait_for(
                    self.context.llm_generate(**kwargs),
                    timeout=actual_timeout
                )
                
                if resp and hasattr(resp, 'completion_text'):
                    result = resp.completion_text.strip()
                    if result:
                        return result
                    
            except asyncio.TimeoutError:
                logger.warning(f"[DailySharing] LLM 超时 ({actual_timeout}s) (尝试 {attempt+1}/{max_retries+1})")
                if attempt < max_retries:
                    await asyncio.sleep(2)
                    continue
            except Exception as e:
                err_str = str(e)
                if "PROHIBITED_CONTENT" in err_str or "blocked" in err_str:
                    logger.error(f"[DailySharing] 内容被模型安全策略拦截 (敏感词): {prompt[:50]}...")
                    return None 

                if "401" in err_str:
                    logger.error(f"[DailySharing] LLM 认证失败(401)，请检查 API Key。provider: {current_provider_id}")
                    if provider_id:
                        return None
                    if attempt < max_retries and user_provider_id and current_provider_id == user_provider_id:
                        default_pid = _get_system_default_provider()
                        if default_pid and default_pid != current_provider_id:
                            logger.info(f"[DailySharing] 遇到 401 错误，降级使用默认的第一个模型({default_pid})...")
                            current_provider_id = default_pid
                            await asyncio.sleep(2)
                            continue
                        else:
                            return None
                    else:
                        return None
                
                logger.error(f"[DailySharing] LLM异常 (尝试 {attempt+1}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2)
                    continue

        logger.error(f"[DailySharing] LLM调用失败（已重试{max_retries}次）")
        return None

    @filter.llm_tool(name="daily_share")
    async def daily_share_tool(
        self, 
        event: AstrMessageEvent, 
        share_type: str, 
        source: str = None, 
        get_image: bool = True,
        need_image: bool = False,
        need_video: bool = False,
        need_voice: bool = False,
        to_qzone: bool = False
    ):
        """
        主动分享日常内容、新闻热搜、获取热搜图片等。
        当用户想要看新闻、热搜、早安晚安、心情、日常碎片、吐槽、梦境或推荐时调用此工具。
        也支持获取"每天60s读世界"或"AI资讯快报"图片。

        Args:
            share_type(string): 分享类型。支持：'自动', '问候', '新闻', '心情', '日常', '吐槽', '梦境', '推荐', '话题', '60s新闻', 'AI资讯'。当用户没有明确指出发什么类型的内容（比如只说"发个说说"、"分享一下"）时，请务必将其设为 '自动'。'话题'表示发起一个网上热议话题的讨论，仅适用于群聊。
            source(string): 仅当 share_type 为'新闻'时有效。指定新闻平台。支持：微博, 知乎, B站, 抖音, 头条, 百度, 腾讯, 小红书, 夸克, 36氪, 51CTO, A站, 爱范儿, 网易, 新浪, 澎湃, 第一财经。如果不指定则留空。
            get_image(boolean): 仅当 share_type 为'新闻'时有效。默认为 True (优先分享热搜长图)。只有当用户明确要求“文字版”、“文本”、“不要图片”或“写一段新闻”时，才将其设为 False。
            need_image(boolean): 是否需要AI为这段文案配图。默认为 False。仅当用户明确说“配图”、“带图”、“发张图”时，才将其设为 True。
            need_video(boolean): 是否需要AI为这段文案生成视频。默认为 False。仅当用户明确说“视频”、“动态图”、“动起来”时，才将其设为 True。
            need_voice(boolean): 是否需要将文案转为语音(TTS)分享。默认为 False。仅当用户明确提到“语音”、“朗读”、“念给我听”时，设为 True。
            to_qzone(boolean): 是否需要将内容作为说说分享到QQ空间。默认为 False。仅当用户明确要求“发说说”、“发空间”、“分享到空间”时，必须设为 True。
        """
        if self._is_terminated: return ""

        # 1. 防抖检查
        persona_name = await self.resolve_persona_from_event(event)
        lock = self._get_lock(persona_name)
        if lock.locked():
            await event.send(event.plain_result("正如火如荼地准备中，请稍后..."))
            return None

        # 2. 启动后台异步任务
        task = asyncio.create_task(
            self.task_manager.async_daily_share_task(
                event, share_type, source, get_image, need_image, need_video, need_voice, to_qzone
            )
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

        # 3. 直接返回空字符串，让 LLM 闭嘴，不再生成回复
        return None

    @filter.command("重新调度")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def handle_reschedule(self, event: AstrMessageEvent):
        """用智能调度重新安排当日分享时间

        只重选时间点，不重新生成穿搭内容；当日日程缺失时 `ensure_dayflow_ready` 会顺带触发生成
        （可能等较久，取决于 dayflow）。当天已经分享过的那套会保留原状态跳过。
        不带参数时按当前会话对应的分享人格生效，也可以 `/重新调度 人格名` 指定。
        """
        if self._is_terminated:
            return

        parts = event.message_str.strip().split()
        target = parts[1] if len(parts) > 1 else ""

        names = []
        for entry in self.get_enabled_personas():
            raw = self._persona_name_of_entry(entry)
            if raw:
                names.append(self._canonical_persona_name(raw) or raw)

        if target:
            matched = [n for n in names if n == target]
            if not matched:
                matched = [n for n in names if target in n]
            if not matched:
                yield event.plain_result(
                    f"未找到人格：{target}\n可调度：{'、'.join(names) if names else '（未配置人格）'}"
                )
                return
            if len(matched) > 1:
                yield event.plain_result(
                    f"「{target}」匹配到多个人格：{'、'.join(matched)}\n请写完整的人格名"
                )
                return
            persona_name = matched[0]
        else:
            # 投递映射优先：本插件是按 persona_receiver 把不同会话分给不同人格的，
            # 这一步才是"这个群的分享由谁发"的权威来源；框架人格只用来裁决歧义与兜底。
            hits = self._match_personas_by_receiver(event, names)
            framework_name = await self._resolve_event_persona(event)
            if len(hits) == 1:
                persona_name = hits[0]
            elif len(hits) > 1:
                if framework_name in hits:
                    persona_name = framework_name
                else:
                    yield event.plain_result(
                        f"这个会话同时挂在多个人格下：{'、'.join(hits)}\n请指定：/重新调度 人格名"
                    )
                    return
            elif framework_name in names:
                persona_name = framework_name
            elif len(names) == 1:
                persona_name = names[0]
            else:
                yield event.plain_result(
                    f"无法确定当前会话属于哪个人格，请指定：/重新调度 人格名\n"
                    f"可调度：{'、'.join(names) if names else '（未配置人格）'}"
                )
                return

        if not self.smart_scheduler._is_smart_share_enabled(persona_name):
            yield event.plain_result(f"[{persona_name}] 未启用智能调度，无法重新调度。")
            return

        if persona_name in self.smart_scheduler._running_personas:
            yield event.plain_result(f"[{persona_name}] 智能调度正在进行中，请稍后再试。")
            return

        yield event.plain_result(f"[{persona_name}] 正在按今日日程重新调度分享时间...")
        task = asyncio.create_task(self._reschedule_today(event, persona_name))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _reschedule_today(self, event: AstrMessageEvent, persona_name: str):
        """后台执行重新调度，完成后把新时间回报到原会话"""
        try:
            state_key = self.smart_scheduler._get_state_key(persona_name)
            before = await self.db.get_state(state_key, {})
            ok = await self.smart_scheduler.run_smart_schedule(persona_name)
            state = await self.db.get_state(state_key, {})
            # register_smart_jobs 每次成功都会刷新 registered_at。
            # 没刷新说明 run_smart_schedule 只是在"该人格正在调度中"分支返回了 True，
            # 此时报"完成"是假成功，必须单独说清楚。
            did_register = bool(state.get("registered_at")) and (
                state.get("registered_at") != before.get("registered_at")
            )
            lines = []
            for key in ("look_1", "look_2", "look_3"):
                item = state.get(key)
                if not isinstance(item, dict):
                    continue
                if item.get("skipped"):
                    status = "已跳过（时间已过）"
                elif item.get("executed"):
                    status = "已分享过"
                else:
                    status = "待分享"
                lines.append(
                    f"{key}：{item.get('share_time', '?')}（{item.get('wear_window', '?')}，{status}）"
                )
            if ok and did_register and lines:
                await event.send(
                    event.plain_result(f"[{persona_name}] 重新调度完成\n" + "\n".join(lines))
                )
            elif ok:
                await event.send(
                    event.plain_result(
                        f"[{persona_name}] 该人格的智能调度正在进行中，本次没有重排，请稍后再试。"
                    )
                )
            else:
                await event.send(
                    event.plain_result(
                        f"[{persona_name}] 重新调度失败：dayflow 日程未就绪或 LLM 未给出有效时间，原计划未改动。"
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[DailySharing] 重新调度失败 [{persona_name}]: {e}")
            try:
                await event.send(event.plain_result(f"[{persona_name}] 重新调度出错：{e}"))
            except Exception:
                pass

    @filter.command("分享")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def handle_share_main(self, event: AstrMessageEvent):
        """
        每日分享统一命令入口
        """
        msg = event.message_str.strip()
        parts = msg.split()

        # 指令触发时缓存 Adapter ID (若未强制指定)
        try:
            persona_name = await self.resolve_persona_from_event(event)
            receiver = self.get_persona_receiver(persona_name) if persona_name else {}
            configured_id = receiver.get("adapter_id", "").strip()
            if not configured_id and event.unified_msg_origin:
                adapter_id = event.unified_msg_origin.split(":")[0]
                if adapter_id:
                    self._cached_adapter_id = adapter_id
        except Exception:
            pass
        
        if len(parts) == 1:
            yield event.plain_result("指令格式错误，请指定参数。\n示例：/分享 新闻\n可加后缀：广播、空间")
            return
            
        arg = parts[1].lower()
        
        # 判断后缀模式
        is_broadcast = "广播" in parts
        is_qzone_target = "空间" in parts  # 判断是否指向QQ空间
        
        current_uid = event.unified_msg_origin
        specific_target = None if is_broadcast else current_uid

        # =============== 手动触发 60s 新闻 ===============
        if arg == "60s":
            url = self.news_service.get_60s_image_url(persona_name=persona_name)
            if not url:
                yield event.plain_result("获取60s新闻失败，请检查API Key配置。")
                return
                
            if is_qzone_target:
                yield event.plain_result("正在分享每天60s读世界到QQ空间...")
                qzone_plugin = self.ctx_service._find_plugin("qzone")
                if qzone_plugin and hasattr(qzone_plugin, "controller") and qzone_plugin.controller is not None:
                    try:
                        await qzone_plugin.controller.publish_post(content="【每天60秒读懂世界】", media=[url], content_sanitized=True)
                        yield event.plain_result("每天60s读世界已成功分享到QQ空间！")
                        await self.db.add_sent_history("qzone_broadcast", "news", "【每天60秒读懂世界】(手动)", True)
                    except Exception as e:
                        yield event.plain_result(f"QQ空间分享失败: {e}")
                else:
                    yield event.plain_result("未检测到QQ空间插件！")
                return

            target_desc = "配置的所有群聊和私聊" if is_broadcast else "当前会话"
            yield event.plain_result(f"正在向{target_desc}分享60s新闻...")
            targets = [specific_target] if specific_target else self.task_manager.get_broadcast_targets(persona_name=persona_name)
            for target in targets:
                await self.context.send_message(target, MessageChain().url_image(url))
                await asyncio.sleep(1)
            return

        # =============== 手动触发AI资讯 ===============
        if arg == "ai":
            # 先拦截检测
            ai_data = await self.news_service.get_ai_news_json(persona_name=persona_name)
            if not ai_data:
                yield event.plain_result("获取AI资讯失败或今日暂无更新。")
                return

            url = self.news_service.get_ai_news_image_url(persona_name=persona_name)
            if not url:
                yield event.plain_result("获取AI资讯图片失败，请检查API Key配置。")
                return

            if is_qzone_target:
                yield event.plain_result("正在分享AI资讯快报到QQ空间...")
                qzone_plugin = self.ctx_service._find_plugin("qzone")
                if qzone_plugin and hasattr(qzone_plugin, "controller") and qzone_plugin.controller is not None:
                    try:
                        await qzone_plugin.controller.publish_post(content="【AI资讯快报】", media=[url], content_sanitized=True)
                        yield event.plain_result("AI资讯快报已成功分享到QQ空间！")
                        await self.db.add_sent_history("qzone_broadcast", "news", "【AI资讯快报】(手动)", True)
                    except Exception as e:
                        yield event.plain_result(f"QQ空间分享失败: {e}")
                else:
                    yield event.plain_result("未检测到QQ空间插件！")
                return

            target_desc = "配置的所有群聊和私聊" if is_broadcast else "当前会话"
            yield event.plain_result(f"正在向{target_desc}分享AI资讯...")
            targets = [specific_target] if specific_target else self.task_manager.get_broadcast_targets(persona_name=persona_name)
            for target in targets:
                await self.context.send_message(target, MessageChain().url_image(url))
                await asyncio.sleep(1)
            return
        
        # =============== 配置命令 ===============
        if arg == "早报空间":
            async for res in self.command_handler.cmd_briefing_qzone_sync(event, parts): yield res
            return
        elif arg == "状态":
            async for res in self.command_handler.cmd_status(event): yield res
            return
        elif arg == "开启":
            async for res in self.command_handler.cmd_enable(event): yield res
            return
        elif arg == "关闭":
            async for res in self.command_handler.cmd_disable(event): yield res
            return
        elif arg == "重置序列":
            async for res in self.command_handler.cmd_reset_seq(event): yield res
            return
        elif arg == "查看序列":
            async for res in self.command_handler.cmd_view_seq(event): yield res
            return
        elif arg == "帮助":
            async for res in self.command_handler.cmd_help(event): yield res
            return
        elif arg == "指定序列":
            async for res in self.command_handler.cmd_set_seq(event, parts): yield res
            return

        # =============== 自动或具体类型生成 ===============
        if arg in ["自动", "auto"]:
            if is_qzone_target:
                yield event.plain_result("正在向QQ空间生成并分享内容(自动类型)...")
                await self.task_manager.execute_qzone_share(None, event=event)
            else:
                target_desc = "配置的所有群聊和私聊" if is_broadcast else "当前会话"
                yield event.plain_result(f"正在向{target_desc}生成并分享内容(自动类型)...")
                await self.task_manager.execute_share(None, specific_target=specific_target)
            return

        else:
            force_type = None
            if arg in CMD_CN_MAP:
                force_type = CMD_CN_MAP[arg]
            else:
                try:
                    force_type = SharingType(arg)
                except ValueError:
                    yield event.plain_result(f"未知指令或无效类型: {arg}\n可用: 问候, 新闻, 心情, 日常, 吐槽, 梦境, 推荐, 话题, 60s, ai")
                    return

            type_cn = TYPE_CN_MAP.get(force_type.value, arg)
            
            if force_type == SharingType.NEWS:
                news_src = None
                is_image_mode = "图片" in parts
                
                for p in parts[2:]:
                    if p in ["图片", "广播", "空间"]: continue 
                    if p in SOURCE_CN_MAP:
                        news_src = SOURCE_CN_MAP[p]
                        break
                    elif p in NEWS_SOURCE_MAP:
                        news_src = p
                        break
                        
                if is_image_mode:
                    if not news_src: news_src = self.news_service.select_news_source(persona_name=persona_name)
                    img_url, src_name = self.news_service.get_hot_news_image_url(news_src, persona_name=persona_name)
                    
                    if is_qzone_target:
                        yield event.plain_result(f"正在获取[{src_name}]图片并分享到QQ空间...")
                        qzone_plugin = self.ctx_service._find_plugin("qzone")
                        if qzone_plugin and hasattr(qzone_plugin, "controller") and qzone_plugin.controller is not None:
                            try:
                                await qzone_plugin.controller.publish_post(content=f"【{src_name}】", media=[img_url], content_sanitized=True)
                                yield event.plain_result("QQ空间分享成功！")
                                await self.db.add_sent_history("qzone_broadcast", "news", f"【{src_name}】长图(手动)", True)
                            except Exception as e:
                                yield event.plain_result(f"QQ空间分享失败: {e}")
                        else:
                            yield event.plain_result("未检测到QQ空间插件！")
                        return

                    yield event.plain_result(f"正在获取 [{src_name}] 图片...")
                    yield event.image_result(img_url)
                    return
                    
                src_info = f" ({NEWS_SOURCE_MAP[news_src]['name']})" if news_src else ""
                
                if is_qzone_target:
                    yield event.plain_result(f"正在向QQ空间生成并分享{type_cn}{src_info} ...")
                    await self.task_manager.execute_qzone_share(force_type, news_source=news_src, event=event)
                else:
                    target_desc = "配置的所有群聊和私聊" if is_broadcast else "当前会话"
                    yield event.plain_result(f"正在向{target_desc}生成并分享{type_cn}{src_info} ...")
                    await self.task_manager.execute_share(force_type, news_source=news_src, specific_target=specific_target)
                return

            if force_type == SharingType.TOPIC:
                if is_qzone_target:
                    yield event.plain_result("话题策略仅适用于群聊，不支持QQ空间。")
                    return
                target_desc = "配置的话题目标群" if is_broadcast else "当前会话"
                yield event.plain_result(f"正在搜索热议话题并向{target_desc}发起讨论...")
                await self.task_manager.execute_topic_share(persona_name=persona_name, specific_target=specific_target)
                return
                
            if is_qzone_target:
                yield event.plain_result(f"正在向QQ空间生成并分享{type_cn} ...")
                await self.task_manager.execute_qzone_share(force_type, event=event)
            else:
                target_desc = "配置的所有群聊和私聊" if is_broadcast else "当前会话"
                yield event.plain_result(f"正在向{target_desc}生成并分享{type_cn} ...")
                await self.task_manager.execute_share(force_type, specific_target=specific_target)