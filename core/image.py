import os
import re
from datetime import datetime
from typing import Optional
from astrbot.api import logger
from ..config import SharingType, TimePeriod

class ImageService:
    def __init__(self, context, config, llm_func, plugin=None):
        self.context = context
        self.config = config
        self.call_llm = llm_func
        self.plugin = plugin
        self._aiimg_plugin = None
        self._aiimg_plugin_not_found = False
        self._wardrobe_plugin = None
        self._wardrobe_plugin_not_found = False
        self._last_image_description = None

        self.debug_mode = self.config.get("debug_mode", False)

    def _get_current_period(self) -> TimePeriod:
        hour = datetime.now().hour
        if 0 <= hour < 6: return TimePeriod.DAWN
        elif 6 <= hour < 9: return TimePeriod.MORNING
        elif 9 <= hour < 12: return TimePeriod.FORENOON
        elif 12 <= hour < 16: return TimePeriod.AFTERNOON
        elif 16 <= hour < 19: return TimePeriod.EVENING
        elif 19 <= hour < 22: return TimePeriod.NIGHT
        else: return TimePeriod.LATE_NIGHT

    def _ensure_plugin(self):
        supported_names = ["astrbot_plugin_aiimg", "astrbot_plugin_gitee_aiimg"]

        # 每次重新获取当前插件实例，防止 aiimg 重载后引用过期
        # 旧实例的 ProviderRegistry 只在 __init__ 加载一次，不会随配置更新而刷新
        current = None
        matched_name = ""
        for p in self.context.get_all_stars():
            if p.name in supported_names:
                matched_name = p.name
                if hasattr(p, "star_instance") and p.star_instance:
                    current = p.star_instance
                elif hasattr(p, "instance") and p.instance:
                    current = p.instance
                else:
                    current = getattr(p, "star_cls", None)
                    if current:
                        logger.debug(f"[DailySharing] 获取到 {p.name} 类引用 (非实例)")
                break

        if current is not self._aiimg_plugin:
            if current:
                logger.info(f"[DailySharing] 已绑定AI图像插件: {matched_name}")
            else:
                logger.debug("[DailySharing] 未找到AI图像插件")
            self._aiimg_plugin = current
            self._aiimg_plugin_not_found = current is None

    def _get_wardrobe_instance(self):
        if not self._wardrobe_plugin and not self._wardrobe_plugin_not_found:
            for p in self.context.get_all_stars():
                if p.name == "astrbot_plugin_wardrobe":
                    if hasattr(p, "star_instance") and p.star_instance:
                        self._wardrobe_plugin = p.star_instance
                    elif hasattr(p, "instance") and p.instance:
                        self._wardrobe_plugin = p.instance
                    else:
                        self._wardrobe_plugin = getattr(p, "star_cls", None)
                    if self._wardrobe_plugin:
                        logger.info("[DailySharing] 已找到衣橱插件: astrbot_plugin_wardrobe")
                    break
            if not self._wardrobe_plugin:
                self._wardrobe_plugin_not_found = True
        return self._wardrobe_plugin

    # ==================== 1. 核心逻辑：Agent 提取 ====================

    async def _agent_extract_visuals(self, content: str, life_context: str, persona_name: str = None) -> Optional[str]:
        if not content and not life_context: return None

        now = datetime.now()
        curr_hour = now.hour
        curr_time = f"{now.hour:02d}:{now.minute:02d}"

        prioritize_text = False

        if prioritize_text:
            logic_prompt = f"""
1. **第一优先级（文案主导）**：首先检查【分享文案】。如果文案中明确提及了地点（例如："我在海边"、"刚到酒店"、"去公园玩"），**必须无条件直接绘制文案描述的地点**，即使它与日程表冲突。
2. **第二优先级（日程补缺）**：只有当【分享文案】**完全未提及**地点时，才提取日程中 **{curr_time} 正在进行** 的状态来设定背景场景。
"""
        else:
            logic_prompt = f"""
1. **第一优先级（日程主导）**：首先检查【生活日程】。如果 **{curr_time}** 有明确的活动地点（例如："在办公室"、"在健身房"），**必须无条件优先绘制日程地点**。忽略文案中的地点（视为比喻或回忆）。
2. **第二优先级（文案补缺）**：只有当【生活日程】为空或未明确指定地点时，才参考【分享文案】中的地点描述。
"""

        custom_prompt = self.config.get("visual_director_prompt", "").strip()
        if custom_prompt:
            try:
                safe_format = dict(logic_prompt=logic_prompt, curr_hour=curr_hour, curr_time=curr_time)
                system_prompt = custom_prompt.replace("{time_hint}", "").replace("{outfit_hint}", "").format(**safe_format)
                if self.debug_mode:
                    logger.info(f"[DailySharing] 视觉导演使用自定义提示词 (长度: {len(custom_prompt)} 字符)")
            except (KeyError, ValueError) as e:
                logger.warning(f"[DailySharing] 自定义视觉导演提示词格式化失败: {e}，使用默认模板")
                system_prompt = self._get_default_visual_director_prompt(logic_prompt, curr_time)
        else:
            if self.debug_mode:
                logger.info("[DailySharing] 视觉导演使用默认提示词模板")
            system_prompt = self._get_default_visual_director_prompt(logic_prompt, curr_time)

        user_prompt = f"【分享文案】：{content}\n【生活日程】：{life_context}\n\n请生成绘画提示词："

        if self.debug_mode:
            logger.info("-" * 60)
            logger.info(f"[DailySharing] 【DEBUG】发送给 Agent 的请求详情 (时间: {curr_time})")
            logger.info(f"[DailySharing] 【DEBUG】System Prompt (前300字): {system_prompt[:300]}...")
            logger.info(f"[DailySharing] 【DEBUG】User Prompt: {user_prompt}")

        try:
            res = await self.call_llm(user_prompt, system_prompt, timeout=45, persona_name=persona_name)

            if self.debug_mode:
                logger.info(f"[DailySharing] 【DEBUG】Agent 原始回复: {res}")

            if not res: return None
            clean = res.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```\w*\n?", "", clean)
                clean = re.sub(r"\n?```$", "", clean)
            return clean.strip() or None
        except Exception as e:
            logger.warning(f"[DailySharing] Agent 提取失败: {e}")
            return None

    def _get_default_visual_director_prompt(self, logic_prompt: str, curr_time: str) -> str:
        return f"""你是 AI 绘画视觉导演。从用户的【分享文案】和【生活日程】中提取画面信息，生成一段完整的 AI 绘画提示词。

当前时间：{curr_time}。确保场景的光线和活动类型与当前时间吻合。

{logic_prompt}
严禁提取 {curr_time} 之后的日程作为当前场景。

【输出格式】
输出一段连贯的自然语言段落，直接作为 AI 绘画提示词。禁止输出 JSON、禁止使用分类标签（如"内搭：""外穿：""风格："等元标记）。

段落必须以这句话开头：
"以前三张参考图中同一少女为基准，完整保留少女五官、身材等全部人体身份特征，绝对禁止任何拼图，为少女生成一张新的写真：她有着白皙细腻的皮肤，纤细的身姿与格外饱满的曲线形成鲜明对比，"

段落必须以这句话结尾：
"完全保留少女的面部特征与丰满的身材。"

【必须包含的视觉要素】
段落中必须依次覆盖以下要素，用自然语言句子衔接，不要用词组堆砌：
1. 穿搭：逐件描述她穿着的服装，每件给出款式、颜色、材质、版型，用"她穿着……"等自然句式
2. 动作：描述一个能被快门定格的瞬时静态姿势，写出具体身体部位在做什么
3. 构图：景别、视角、镜头效果
4. 环境：具体地点和场景内的可见物体
5. 光影：光源类型、方向、颜色、明暗分布

如果文案推荐了具体物品，在动作或穿搭描述中自然融入。

【禁止】
- 禁止元概念：不写情绪词、氛围渲染、抽象状态、嗅觉听觉
- 禁止词组堆砌：不用"棉质材质，浅粉色配色"这种离散词组，改用"一件浅粉色的棉质……"等自然句子
- 禁止分类标签：不用"内搭：""外穿：""风格："等元标记
- 禁止输出 JSON 或任何格式标记"""

    # ==================== 2. 主入口 ====================

    async def generate_image(self, content: str, sharing_type: SharingType, life_context: str = None, persona_name: str = None) -> Optional[str]:
        if not self.plugin.get_persona_config_value(persona_name, "persona_image_conf", "enable_ai_image", False): return None

        is_text_priority = False
        logic_str = "日程主导"

        logger.info(f"[DailySharing] 配图决策: 自拍模式 ({logic_str}) | 类型: {sharing_type.value}")

        prompt = None
        if content or life_context:
            prompt = await self._agent_extract_visuals(content, life_context, persona_name=persona_name)

            if not prompt:
                logger.warning("[DailySharing] 视觉导演生成失败，已取消配图，仅发送文案")
                return None

            logger.info(f"[DailySharing] 视觉导演生成 Prompt: {prompt[:80]}...")

        if not prompt:
            logger.warning("[DailySharing] Prompt 为空，取消配图")
            return None

        max_sensitive_retries = 2
        current_prompt = prompt

        for attempt in range(1 + max_sensitive_retries):
            result, is_sensitive = await self._call_aiimg_selfie(current_prompt, persona_name=persona_name)

            if result:
                self._last_image_description = current_prompt
                return result

            if not is_sensitive:
                self._last_image_description = None
                return None

            if attempt < max_sensitive_retries:
                logger.warning(f"[DailySharing] 配图因敏感内容被拦截 (第{attempt+1}次)，正在重新生成提示词...")
                current_prompt = await self._regenerate_prompt_avoiding_sensitive(
                    content, life_context, current_prompt, persona_name
                )
                if not current_prompt:
                    logger.warning("[DailySharing] 重新生成提示词失败，放弃配图")
                    self._last_image_description = None
                    return None
                logger.info(f"[DailySharing] 重试配图 Prompt: {current_prompt[:80]}...")
            else:
                logger.warning(f"[DailySharing] 配图因敏感内容被拦截，已重试{max_sensitive_retries}次，放弃配图仅发送文案")
                self._last_image_description = None
                return None

        self._last_image_description = None
        return None

    # ==================== 3. 工具函数 ====================

    async def _analyze_image_for_video(self, image_path: str, content: str, persona_name: str = None) -> str:
        """使用多模态LLM分析图片内容，生成针对性的视频提示词"""
        try:
            custom_prompt = self.config.get("video_director_prompt", "").strip()
            if custom_prompt:
                system_prompt = custom_prompt
            else:
                system_prompt = self._get_default_video_director_prompt()

            user_prompt = "请描述这张照片变成视频后的动态效果。"

            logger.info("[DailySharing] 正在使用多模态模型分析图片内容...")

            # 优先级：人格级 video_llm_provider_id > 全局 video_llm_provider_id
            video_provider_id = None
            if persona_name:
                persona_vid_provider = self.plugin.get_persona_config_value(persona_name, "persona_image_conf", "video_llm_provider_id", "")
                if persona_vid_provider:
                    video_provider_id = persona_vid_provider
            if not video_provider_id:
                video_provider_id = self.plugin.get_persona_config_value(persona_name, "persona_image_conf", "video_llm_provider_id", "") or None

            # 调用多模态LLM，直接传本地文件路径，AstrBot会自动处理编码
            resp = await self.call_llm(
                prompt=user_prompt,
                system_prompt=system_prompt,
                timeout=30,
                image_urls=[image_path],
                provider_id=video_provider_id
            )

            if resp and len(resp) > 10:
                # 清理可能的引号或多余格式
                video_prompt = resp.strip().strip('"').strip("'")
                logger.info(f"[DailySharing] AI生成的视频提示词: {video_prompt}")
                return video_prompt
            else:
                logger.warning("[DailySharing] 多模态模型返回空或无效内容，使用默认提示词")
                return self._get_default_video_prompt()

        except Exception as e:
            logger.warning(f"[DailySharing] 图片分析失败: {e}，使用默认提示词")
            return self._get_default_video_prompt()

    def _get_default_video_prompt(self) -> str:
        """获取默认视频提示词（兼容旧逻辑）"""
        base = self._last_image_description or "生活片段"
        return f"{base}, 生活片段, 电影感运镜, 缓慢平移, 高质量"

    def _get_default_video_director_prompt(self) -> str:
        return """你是一个视频动效设计师。用户会给你一张美少女的生活照，你需要描述这张照片变成5秒视频后的动态效果。

只输出动态描述，不要任何解释或格式标记。5秒视频，只写微小自然的动态，不要大幅度动作或场景切换。

输出必须以"参考图片中的少女形象，她"开头，然后依次描述以下内容，用逗号连接：
1. 她在画面中的姿态和正在做的事
2. 她身上最打动人的细节动起来——必须基于图片实际内容，突出她的可爱、魅力或者性感
3. 背景环境中自然发生的动态变化（风吹、水动、物品晃动等）
4. 适合当前构图的运镜方式
5. 光线或色彩的微妙变化
以"电影感，高质量，流畅"结尾。"""

    async def generate_video_from_image(self, image_path: str, content: str, persona_name: str = None) -> Optional[str]:
        if not self.plugin.get_persona_config_value(persona_name, "persona_image_conf", "enable_ai_video", False): return None

        self._ensure_plugin()
        if not self._aiimg_plugin: return None

        if not hasattr(self._aiimg_plugin, "registry"):
            logger.warning("[DailySharing] 检测到AI图像插件不支持 registry ，跳过视频生成")
            return None

        try:
            if not os.path.exists(image_path): return None
            with open(image_path, "rb") as f: image_bytes = f.read()
            logger.info(f"[DailySharing] 正在将配图转换为视频...")

            if self.plugin.get_persona_config_value(persona_name, "persona_image_conf", "enable_smart_video_prompt", True):
                video_prompt = await self._analyze_image_for_video(image_path, content, persona_name=persona_name)
            else:
                video_prompt = self._get_default_video_prompt()
                logger.info(f"[DailySharing] 使用默认视频提示词: {video_prompt}")

            chain = None
            if persona_name and hasattr(self._aiimg_plugin, "_get_persona_video_chain"):
                chain = self._aiimg_plugin._get_persona_video_chain(persona_name)
            if not chain and hasattr(self._aiimg_plugin, "_get_video_chain"):
                chain = self._aiimg_plugin._get_video_chain()
            if not chain:
                logger.warning("[DailySharing] 未配置视频服务提供商")
                return None

            video_url = None
            for provider_id in chain:
                try:
                    backend = self._aiimg_plugin.registry.get_video_backend(provider_id)
                    video_url = await backend.generate_video_url(prompt=video_prompt, image_bytes=image_bytes)
                    if video_url:
                        break
                except Exception as e:
                    logger.warning(f"[DailySharing] 视频后端 {provider_id} 生成失败: {e}")
                    continue

            if not video_url:
                logger.error("[DailySharing] 所有视频后端均失败")
                return None

            await self._auto_save_video_to_wardrobe(video_url, image_path, persona_name)

            return video_url

        except Exception as e:
            logger.error(f"[DailySharing] 视频生成流程异常: {e}")
            return None

    async def _auto_save_video_to_wardrobe(self, video_url: str, source_image_path: str, persona_name: str = ""):
        wardrobe = self._get_wardrobe_instance()
        if not wardrobe or not hasattr(wardrobe, "_save_video_from_bytes"):
            return
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status != 200:
                        logger.debug("[DailySharing] 下载视频失败: HTTP %d", resp.status)
                        return
                    video_bytes = await resp.read()

            if not video_bytes:
                return

            logger.info("[DailySharing] 自动存视频到衣橱，视频大小=%.2fKB 人格=%s", len(video_bytes) / 1024, persona_name or "无")
            await wardrobe._save_video_from_bytes(
                video_bytes,
                persona=persona_name,
                source_image_path=source_image_path,
                created_by="dailysharing",
            )
        except Exception as e:
            logger.debug("[DailySharing] 自动存视频到衣橱失败: %s", e)

    def get_last_description(self) -> Optional[str]:
        return self._last_image_description

    async def _get_default_persona_name(self) -> Optional[str]:
        try:
            persona_mgr = getattr(self.context, "persona_manager", None)
            if persona_mgr and hasattr(persona_mgr, "get_default_persona_v3"):
                persona_obj = await persona_mgr.get_default_persona_v3()
                if persona_obj:
                    if isinstance(persona_obj, dict):
                        for key in ("name", "persona_id", "id"):
                            val = persona_obj.get(key)
                            if val and str(val).strip():
                                return str(val).strip()
                    else:
                        for attr in ("name", "persona_id", "id"):
                            val = getattr(persona_obj, attr, None)
                            if val and str(val).strip():
                                return str(val).strip()
        except Exception as e:
            logger.debug(f"[DailySharing] 获取默认人格名失败: {e}")
        return None

    async def _regenerate_prompt_avoiding_sensitive(
        self, content: str, life_context: str,
        old_prompt: str, persona_name: str = None
    ) -> Optional[str]:
        retry_prompt = (
            "你之前为以下内容生成的配图提示词被图片生成服务的安全策略拦截了，"
            "可能是因为描述中包含了过于暴露的服装、暗示性姿势、或其他敏感元素。\n\n"
            f"原提示词：{old_prompt}\n\n"
            "请重新为这段分享内容设计配图描述，务必遵守以下规则：\n"
            "1. 穿搭描述必须保守、日常，禁止低胸、超短、透视、紧身等暗示性服装\n"
            "2. 动作必须自然大方，禁止任何暗示性姿势\n"
            "3. 构图以中景、远景为主，避免特写敏感部位\n"
            "4. 整体氛围健康积极\n\n"
            f"分享内容：{content}\n"
        )
        if life_context:
            retry_prompt += f"\n生活上下文：{life_context}\n"

        retry_prompt += (
            "\n输出要求与视觉导演提示词相同：一段连贯的自然语言段落，"
            "以\"以前三张参考图中同一少女为基准，完整保留少女五官、身材等全部人体身份特征，绝对禁止任何拼图，为少女生成一张新的写真：她有着白皙细腻的皮肤，纤细的身姿与格外饱满的曲线形成鲜明对比，\"开头，"
            "以\"完全保留少女的面部特征与丰满的身材。\"结尾。"
            "禁止输出 JSON 或任何格式标记。"
        )

        result = await self.call_llm(
            retry_prompt,
            system_prompt="你是一个专业的配图导演。你的任务是为分享内容设计安全、健康的配图描述。之前的设计因敏感内容被拦截，你必须大幅降低敏感度。输出一段连贯的自然语言段落，不要输出 JSON。",
            persona_name=persona_name,
        )

        if not result:
            return None

        clean = result.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```\w*\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean)
        return clean.strip() or None

    async def _call_aiimg_selfie(self, prompt: str, persona_name: str = None):
        self._ensure_plugin()
        if not self._aiimg_plugin:
            logger.error("[DailySharing] 未找到AI图像插件，请确保已安装 astrbot_plugin_aiimg")
            return None, False

        aiimg = self._aiimg_plugin

        try:
            resolved_persona = persona_name or await self._get_default_persona_name()
            if not resolved_persona:
                logger.error("[DailySharing] 未获取到默认人格名称，无法使用自拍功能。请在 AstrBot 中配置默认人格。")
                return None, False

            logger.info(f"[DailySharing] 使用自拍模式，人格: {resolved_persona}")

            ref_paths = []
            if hasattr(aiimg, "_get_selfie_reference_paths"):
                ref_paths, source = await aiimg._get_selfie_reference_paths(None, persona_name=resolved_persona)
                logger.info(f"[DailySharing] 参考照来源: {source}, 数量: {len(ref_paths)}")
            elif hasattr(aiimg, "_get_persona_config_selfie_reference_paths"):
                ref_paths = aiimg._get_persona_config_selfie_reference_paths(resolved_persona)

            if not ref_paths:
                logger.error(f"[DailySharing] 人格「{resolved_persona}」未配置自拍参考照。请在 aiimg 插件的 WebUI 中为该人格配置参考图。")
                return None, False

            if not hasattr(aiimg, "_read_paths_bytes") or not hasattr(aiimg, "edit"):
                logger.error("[DailySharing] aiimg 插件版本不支持自拍功能所需接口")
                return None, False

            ref_images = await aiimg._read_paths_bytes(ref_paths)
            if not ref_images:
                logger.error(f"[DailySharing] 人格「{resolved_persona}」的参考图文件读取失败")
                return None, False

            logger.info(f"[DailySharing] 获取到 {len(ref_images)} 张参考图")

            chain_override = None
            if hasattr(aiimg, "_get_persona_selfie_chain"):
                chain_override = aiimg._get_persona_selfie_chain(resolved_persona)

            if not chain_override:
                logger.error(f"[DailySharing] 人格「{resolved_persona}」未配置自拍服务商链路。请在 aiimg 插件的 WebUI 中为该人格配置 chain。")
                return None, False

            persona_default_output = ""
            if hasattr(aiimg, "_get_persona_selfie_config"):
                persona_conf = aiimg._get_persona_selfie_config(resolved_persona)
                if persona_conf:
                    persona_default_output = str(persona_conf.get("default_output", "") or "").strip()

            final_prompt = prompt

            if self.debug_mode:
                logger.info("=" * 60)
                logger.info("[DailySharing] 【DEBUG】自拍模式参数：")
                logger.info(f"[DailySharing] 【DEBUG】人格: {resolved_persona}")
                logger.info(f"[DailySharing] 【DEBUG】参考图数量: {len(ref_images)}")
                logger.info(f"[DailySharing] 【DEBUG】服务商链路: {[x.get('provider_id') for x in chain_override if isinstance(x, dict)]}")
                logger.info(f"[DailySharing] 【DEBUG】输出尺寸: {persona_default_output or 'default'}")
                logger.info(f"[DailySharing] 【DEBUG】完整 Prompt:\n{final_prompt}")
                logger.info("=" * 60)

            path_obj = await aiimg.edit.edit(
                prompt=final_prompt,
                images=ref_images,
                backend=None,
                size=None,
                default_output=persona_default_output,
                chain_override=chain_override,
            )

            await self._auto_save_to_wardrobe(path_obj, resolved_persona)

            if path_obj is None:
                logger.error("[DailySharing] aiimg.edit.edit() 返回 None，自拍生成失败")
                return None, False
            return str(path_obj), False

        except Exception as e:
            err_str = str(e).lower()
            is_sensitive = any(kw in err_str for kw in [
                "sensitivecontent", "sensitive_content", "sensitive information",
                "contentpolicy", "content_policy", "safety", "prohibited",
                "nsfw", "inappropriate",
            ])
            if is_sensitive:
                logger.warning(f"[DailySharing] 自拍生成因敏感内容被拦截: {e}")
                return None, True
            else:
                logger.error(f"[DailySharing] 自拍生成出错: {e}")
                return None, False

    async def _auto_save_to_wardrobe(self, image_path, persona_name: str = ""):
        wardrobe = self._get_wardrobe_instance()
        if not wardrobe or not hasattr(wardrobe, "_save_image_from_bytes"):
            return
        try:
            from pathlib import Path as _Path
            p = _Path(image_path)
            if not p.exists():
                logger.debug("[DailySharing] 自动存图跳过：图片文件不存在 %s", image_path)
                return
            import aiofiles
            async with aiofiles.open(p, "rb") as f:
                image_bytes = await f.read()
            if not image_bytes:
                return
            logger.info("[DailySharing] 自动存图到衣橱，图片大小=%.2fKB 人格=%s", len(image_bytes) / 1024, persona_name or "无")
            image_id, attrs, duplicate = await wardrobe._save_image_from_bytes(
                image_bytes,
                persona=persona_name,
                created_by="dailysharing",
            )
            if duplicate:
                logger.debug("[DailySharing] 自动存图跳过：图片重复 (hash已存在)")
            elif image_id:
                logger.info("[DailySharing] 自动存图成功，ID=%s", image_id)
        except Exception as e:
            logger.debug("[DailySharing] 自动存图到衣橱失败: %s", e)
