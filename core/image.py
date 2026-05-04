import os
import re
import json
from datetime import datetime
from typing import Optional, Dict, List
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

        self.img_conf = self.config.get("image_conf", {})
        self.llm_conf = self.config.get("llm_conf", {})
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
        if not self._aiimg_plugin and not self._aiimg_plugin_not_found:
            supported_names = ["astrbot_plugin_aiimg", "astrbot_plugin_gitee_aiimg"]

            for p in self.context.get_all_stars():
                if p.name in supported_names:
                    if hasattr(p, "star_instance") and p.star_instance:
                        self._aiimg_plugin = p.star_instance
                    elif hasattr(p, "instance") and p.instance:
                        self._aiimg_plugin = p.instance
                    else:
                        self._aiimg_plugin = getattr(p, "star_cls", None)
                        if self._aiimg_plugin:
                            logger.debug(f"[DailySharing] 获取到 {p.name} 类引用 (非实例)")

                    if self._aiimg_plugin:
                        logger.info(f"[DailySharing] 已找到AI图像插件: {p.name}")
                    break

            if not self._aiimg_plugin:
                self._aiimg_plugin_not_found = True

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

    async def _agent_extract_visuals(self, content: str, life_context: str, persona_name: str = None) -> Dict[str, str]:
        if not content and not life_context: return {}

        now = datetime.now()
        curr_hour = now.hour

        prioritize_text = self.img_conf.get("priority_text_over_schedule", True)

        if prioritize_text:
            logic_prompt = f"""
1. **第一优先级（文案主导）**：首先检查【分享文案】。如果文案中明确提及了地点（例如："我在海边"、"刚到酒店"、"去公园玩"），**必须无条件直接绘制文案描述的地点**，即使它与日程表冲突。
2. **第二优先级（日程补缺）**：只有当【分享文案】**完全未提及**地点时，才提取日程中 **{curr_hour}:00 正在进行** 的状态来设定背景场景。
"""
        else:
            logic_prompt = f"""
1. **第一优先级（日程主导）**：首先检查【生活日程】。如果 **{curr_hour}:00** 有明确的活动地点（例如："在办公室"、"在健身房"），**必须无条件优先绘制日程地点**。忽略文案中的地点（视为比喻或回忆）。
2. **第二优先级（文案补缺）**：只有当【生活日程】为空或未明确指定地点时，才参考【分享文案】中的地点描述。
"""

        custom_prompt = self.config.get("visual_director_prompt", "").strip()
        if custom_prompt:
            try:
                system_prompt = custom_prompt.format(
                    logic_prompt=logic_prompt,
                    curr_hour=curr_hour
                )
            except KeyError as e:
                logger.warning(f"[DailySharing] 自定义视觉导演提示词缺少变量: {e}，使用默认模板")
                system_prompt = self._get_default_visual_director_prompt(logic_prompt, curr_hour)
        else:
            system_prompt = self._get_default_visual_director_prompt(logic_prompt, curr_hour)

        user_prompt = f"【分享文案】：{content}\n【生活日程】：{life_context}\n\n请提取视觉元素："

        if self.debug_mode:
            logger.info("-" * 60)
            logger.info(f"[DailySharing] 【DEBUG】发送给 Agent 的请求详情 (时间: {curr_hour}:00)")
            logger.info(f"[DailySharing] 【DEBUG】System Prompt (前300字): {system_prompt[:300]}...")
            logger.info(f"[DailySharing] 【DEBUG】User Prompt: {user_prompt}")

        try:
            res = await self.call_llm(user_prompt, system_prompt, timeout=45, persona_name=persona_name)

            if self.debug_mode:
                logger.info(f"[DailySharing] 【DEBUG】Agent 原始回复: {res}")

            if not res: return {}
            clean_json = res.replace("```json", "").replace("```", "").strip()
            match = re.search(r"\{.*\}", clean_json, re.DOTALL)
            if match: clean_json = match.group(0)
            return json.loads(clean_json)
        except Exception as e:
            logger.warning(f"[DailySharing] Agent 提取失败: {e}")
            return {}

    def _get_default_visual_director_prompt(self, logic_prompt: str, curr_hour: int) -> str:
        return f"""你是 AI 绘画视觉导演。从用户的【分享文案】和【生活日程】中提取画面关键词。

当前时间：{curr_hour}:00。确保场景的光线和活动类型与当前时间吻合。

{logic_prompt}
严禁提取 {curr_hour}:00 之后的日程作为当前场景。

【输出铁律】
只输出相机镜头能直接拍到的内容：颜色、形状、材质、光线、空间关系。
禁止：情绪词、氛围渲染、抽象状态、嗅觉听觉。

【字段要求】

穿搭 (outfit) —— 最重要，画面核心
逐件描述服装，区分内搭/外穿，每件给出：款式 + 颜色 + 材质 + 版型特征。
差："一身暗黑系穿搭，神秘优雅" ← 这是总结，不是描述

动作 (action) —— 次重要，决定画面生动度
描述一个能被快门定格的瞬时静态姿势。写出具体的身体部位在做什么。
差："正在卸妆，动作轻柔" ← 太笼统，没有具体姿态

构图 (composition)
景别（特写/半身/全身）、视角（平视/俯拍/仰拍）、镜头效果（景深/虚化/广角）。
差："营造亲密的视觉感受"

环境 (environment)
具体地点 + 场景内的可见物体（家具、摆设、墙色、窗等）。用物体定位空间。
差："温馨的卧室，宁静的氛围"

光影 (lighting)
光源类型（日光/台灯/顶灯/窗光）、方向、颜色、画面的明暗分布。
差："柔和的灯光营造出温馨氛围"

主体 (subject)
若文案推荐具体物品则描述该物品；纯风景或画人时填"无"。

请严格输出 JSON：
{{
    "outfit": "...",       // 穿搭的详细描述——逐件列出，区分内外搭，每件写款式、颜色、材质、版型
    "action": "...",       // 动作描述——具体身体部位在做什么，能被快门定格的一个瞬时静态姿势
    "composition": "...",  // 构图——景别、视角、镜头效果
    "environment": "...",  // 环境——具体地点和可见物体
    "lighting": "...",     // 光影——光源类型、方向、颜色、明暗分布
    "subject": "..."       // 主体物品描述，纯风景或画人时填"无"
}}"""

    # ==================== 2. 主入口 ====================

    async def generate_image(self, content: str, sharing_type: SharingType, life_context: str = None, persona_name: str = None) -> Optional[str]:
        if not self.img_conf.get("enable_ai_image", False): return None

        is_text_priority = self.img_conf.get("priority_text_over_schedule", True)
        logic_str = "文案主导" if is_text_priority else "日程主导"

        logger.info(f"[DailySharing] 配图决策: 自拍模式 ({logic_str}) | 类型: {sharing_type.value}")

        visuals = {}
        if content or life_context:
            visuals = await self._agent_extract_visuals(content, life_context, persona_name=persona_name)

            if not visuals:
                logger.warning("[DailySharing] Agent 提取失败，已取消配图，仅发送文案")
                return None

            env = visuals.get('environment', '无')
            subj = visuals.get('subject', '无')
            outfit = visuals.get('outfit', '无')
            comp = visuals.get('composition', '无')
            logger.info(f"[DailySharing] Agent 提取 -> 主体: {subj} | 环境: {env} | 构图: {comp} | 穿搭: {outfit[:15] if outfit else '无'}...")

        prompt = self._assemble_selfie_prompt(content, sharing_type, visuals)

        if not prompt:
            logger.warning("[DailySharing] Prompt 组装失败，取消配图")
            return None
        logger.info(f"[DailySharing] 最终配图 Prompt: {prompt[:100]}...")

        result = await self._call_aiimg_selfie(prompt, persona_name=persona_name)
        if result:
            self._last_image_description = prompt
        else:
            self._last_image_description = None

        return result

    def _assemble_selfie_prompt(self, content: str, sharing_type: SharingType, visuals: Dict) -> str:
        parts = []

        if self.debug_mode:
            logger.info("+" * 60)
            logger.info(f"[DailySharing] 【DEBUG】开始组装自拍 Prompt")
            logger.info(f"[DailySharing] 【DEBUG】Visuals 字典内容: {visuals}")

        def _clean(s: str) -> str:
            return s.strip().rstrip("。，,").strip()

        subject_str = visuals.get("subject", "")
        has_subj = subject_str and subject_str not in ["无", "N/A", "None", ""]

        if has_subj:
            parts.append(f"手持或展示{_clean(subject_str)}")

        outfit = visuals.get("outfit", "")
        if outfit:
            outfit_clean = _clean(outfit)
            if outfit_clean.startswith("穿着") or outfit_clean.startswith("身穿"):
                parts.append(outfit_clean)
            else:
                parts.append(f"穿着{outfit_clean}")

        action = visuals.get("action", "")
        if action:
            parts.append(_clean(action))

        composition = visuals.get("composition", "")
        if composition:
            parts.append(_clean(composition))

        env = visuals.get("environment", "")
        if env:
            env_clean = _clean(env)
            if env_clean.startswith("在") or env_clean.startswith("位于"):
                env_clean = env_clean.lstrip("在位于")
            parts.append(f"位于{env_clean}")

        lighting = visuals.get("lighting", "")
        if lighting:
            parts.append(_clean(lighting))
        else:
            period = self._get_current_period()
            if period in [TimePeriod.NIGHT, TimePeriod.LATE_NIGHT]:
                parts.append("夜晚城市灯光")
            else:
                parts.append("白天自然光")

        result = "，".join(filter(None, parts))

        if self.debug_mode:
            logger.info(f"[DailySharing] 【DEBUG】组装完成，最终 Prompt 长度: {len(result)}")
            logger.info("+" * 60)

        return result

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
                video_provider_id = self.img_conf.get("video_llm_provider_id", "").strip() or None

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
        if not self.img_conf.get("enable_ai_video", False): return None

        self._ensure_plugin()
        if not self._aiimg_plugin: return None

        if not hasattr(self._aiimg_plugin, "registry"):
            logger.warning("[DailySharing] 检测到AI图像插件不支持 registry ，跳过视频生成")
            return None

        try:
            if not os.path.exists(image_path): return None
            with open(image_path, "rb") as f: image_bytes = f.read()
            logger.info(f"[DailySharing] 正在将配图转换为视频...")

            # 判断是否启用智能视频提示词
            if self.img_conf.get("enable_smart_video_prompt", True):
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

            for provider_id in chain:
                try:
                    backend = self._aiimg_plugin.registry.get_video_backend(provider_id)
                    return await backend.generate_video_url(prompt=video_prompt, image_bytes=image_bytes)
                except Exception as e:
                    logger.warning(f"[DailySharing] 视频后端 {provider_id} 生成失败: {e}")
                    continue

            logger.error("[DailySharing] 所有视频后端均失败")
            return None

        except Exception as e:
            logger.error(f"[DailySharing] 视频生成流程异常: {e}")
            return None

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

    async def _call_aiimg_selfie(self, prompt: str, persona_name: str = None) -> Optional[str]:
        self._ensure_plugin()
        if not self._aiimg_plugin:
            logger.error("[DailySharing] 未找到AI图像插件，请确保已安装 astrbot_plugin_aiimg")
            return None

        aiimg = self._aiimg_plugin

        try:
            resolved_persona = persona_name or await self._get_default_persona_name()
            if not resolved_persona:
                logger.error("[DailySharing] 未获取到默认人格名称，无法使用自拍功能。请在 AstrBot 中配置默认人格。")
                return None

            logger.info(f"[DailySharing] 使用自拍模式，人格: {resolved_persona}")

            ref_paths = []
            if hasattr(aiimg, "_get_selfie_reference_paths"):
                ref_paths, source = await aiimg._get_selfie_reference_paths(None, persona_name=resolved_persona)
                logger.info(f"[DailySharing] 参考照来源: {source}, 数量: {len(ref_paths)}")
            elif hasattr(aiimg, "_get_persona_config_selfie_reference_paths"):
                ref_paths = aiimg._get_persona_config_selfie_reference_paths(resolved_persona)

            if not ref_paths:
                logger.error(f"[DailySharing] 人格「{resolved_persona}」未配置自拍参考照。请在 aiimg 插件的 WebUI 中为该人格配置参考图。")
                return None

            if not hasattr(aiimg, "_read_paths_bytes") or not hasattr(aiimg, "edit"):
                logger.error("[DailySharing] aiimg 插件版本不支持自拍功能所需接口")
                return None

            ref_images = await aiimg._read_paths_bytes(ref_paths)
            if not ref_images:
                logger.error(f"[DailySharing] 人格「{resolved_persona}」的参考图文件读取失败")
                return None

            logger.info(f"[DailySharing] 获取到 {len(ref_images)} 张参考图")

            chain_override = None
            if hasattr(aiimg, "_get_persona_selfie_chain"):
                chain_override = aiimg._get_persona_selfie_chain(resolved_persona)

            if not chain_override:
                logger.error(f"[DailySharing] 人格「{resolved_persona}」未配置自拍服务商链路。请在 aiimg 插件的 WebUI 中为该人格配置 chain。")
                return None

            size = None
            prompt_prefix = ""
            if hasattr(aiimg, "_get_persona_selfie_config"):
                persona_conf = aiimg._get_persona_selfie_config(resolved_persona)
                if persona_conf:
                    default_output = str(persona_conf.get("default_output", "") or "").strip()
                    if default_output:
                        size = default_output
                    prompt_prefix = str(persona_conf.get("prompt_prefix", "") or "").strip()

            final_prompt = prompt
            if hasattr(aiimg, "_build_selfie_prompt"):
                final_prompt = aiimg._build_selfie_prompt(prompt, extra_refs=0, prompt_prefix=prompt_prefix)
            else:
                if prompt_prefix:
                    final_prompt = f"{prompt_prefix}\n\n{prompt}"
                else:
                    final_prompt = f"以参考图中同一少女为基准，完整保留其五官、身材等全部人体特征，绝对禁止任何拼图，参考她的面部特征为其生成一张单人的自然生活照：她有着白皙细腻的皮肤，纤细的身姿与格外饱满的曲线形成鲜明对比，{prompt}"

            if self.debug_mode:
                logger.info("=" * 60)
                logger.info("[DailySharing] 【DEBUG】自拍模式参数：")
                logger.info(f"[DailySharing] 【DEBUG】人格: {resolved_persona}")
                logger.info(f"[DailySharing] 【DEBUG】参考图数量: {len(ref_images)}")
                logger.info(f"[DailySharing] 【DEBUG】服务商链路: {[x.get('provider_id') for x in chain_override if isinstance(x, dict)]}")
                logger.info(f"[DailySharing] 【DEBUG】输出尺寸: {size or 'default'}")
                logger.info(f"[DailySharing] 【DEBUG】提示词前缀: {prompt_prefix or '(无)'}")
                logger.info(f"[DailySharing] 【DEBUG】完整 Prompt:\n{final_prompt}")
                logger.info("=" * 60)

            path_obj = await aiimg.edit.edit(
                prompt=final_prompt,
                images=ref_images,
                backend=None,
                size=size,
                chain_override=chain_override,
            )

            await self._auto_save_to_wardrobe(path_obj, resolved_persona)

            if path_obj is None:
                logger.error("[DailySharing] aiimg.edit.edit() 返回 None，自拍生成失败")
                return None
            return str(path_obj)

        except Exception as e:
            logger.error(f"[DailySharing] 自拍生成出错: {e}")
            return None

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
