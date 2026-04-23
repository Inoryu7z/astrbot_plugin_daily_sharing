import os
import re
import json
from datetime import datetime
from typing import Optional, Dict, List
from astrbot.api import logger
from ..config import SharingType, TimePeriod

class ImageService:
    def __init__(self, context, config, llm_func):
        self.context = context
        self.config = config
        self.call_llm = llm_func
        self._aiimg_plugin = None
        self._aiimg_plugin_not_found = False
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

    # ==================== 1. 核心逻辑：Agent 提取 ====================

    async def _agent_extract_visuals(self, content: str, life_context: str) -> Dict[str, str]:
        if not content and not life_context: return {}

        now = datetime.now()
        curr_hour = now.hour
        period = self._get_current_period()
        is_night = period in [TimePeriod.LATE_NIGHT, TimePeriod.DAWN]

        if period == TimePeriod.DAWN:
            if curr_hour < 4:
                time_hint = "凌晨深夜的寂静，漆黑的夜空，漆黑的夜色，路灯或城市灯光"
            else:
                time_hint = "黎明前的微光，天空是非常深的暗蓝色，微弱的冷光，清冷寂静，朦胧感"
        elif period == TimePeriod.MORNING:
            time_hint = "早晨的日出晨光, 柔和的朝阳, 清晨柔和的漫射光，丁达尔效应, 梦幻光影"
        elif period == TimePeriod.FORENOON:
            time_hint = "上午的明亮日光，通透，晴朗的天空, 充满活力的光线"
        elif period == TimePeriod.AFTERNOON:
            time_hint = "下午的充足阳光，光影对比清晰，慵懒或明亮的氛围, 清晰的照明"
        elif period == TimePeriod.EVENING:
            time_hint = "傍晚的暖色调，温暖的金色夕阳, 晚霞或暮色，柔和的长阴影，逆光轮廓"
        elif period == TimePeriod.NIGHT:
            time_hint = "夜晚的漆黑天空, 深沉的夜景，城市霓虹灯光, 室内温馨的人造暖光"
        else:
            time_hint = "深夜的幽暗氛围，漆黑的环境，城市夜景，昏暗的室内人造光，宁静的氛围"

        outfit_hint = "当前是休息时间，忽略白天外出服装，仅提取睡衣或家居服。" if is_night else "当前是活动时间，提取完整的外出日常穿搭。"

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
                    curr_hour=curr_hour,
                    time_hint=time_hint,
                    outfit_hint=outfit_hint
                )
            except KeyError as e:
                logger.warning(f"[DailySharing] 自定义视觉导演提示词缺少变量: {e}，使用默认模板")
                system_prompt = self._get_default_visual_director_prompt(logic_prompt, curr_hour, time_hint, outfit_hint)
        else:
            system_prompt = self._get_default_visual_director_prompt(logic_prompt, curr_hour, time_hint, outfit_hint)

        user_prompt = f"【分享文案】：{content}\n【生活日程】：{life_context}\n\n请提取视觉元素："

        if self.debug_mode:
            logger.info("-" * 60)
            logger.info(f"[DailySharing] 【DEBUG】发送给 Agent 的请求详情 (时间: {curr_hour}:00, is_night: {is_night})")
            logger.info(f"[DailySharing] 【DEBUG】Outfit Hint: {outfit_hint}")
            logger.info(f"[DailySharing] 【DEBUG】System Prompt (前300字): {system_prompt[:300]}...")
            logger.info(f"[DailySharing] 【DEBUG】User Prompt: {user_prompt}")

        try:
            res = await self.call_llm(user_prompt, system_prompt, timeout=45)

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

    def _get_default_visual_director_prompt(self, logic_prompt: str, curr_hour: int, time_hint: str, outfit_hint: str) -> str:
        return f"""你是一个专业的 AI 绘画视觉导演。
任务：根据用户的【分享文案】和【生活日程】，提取画面关键词。

【提取逻辑】
1. **分析主体 (Subject)**：首先判断文案是否在描述或推荐一个**具体物品**（如美食、书籍、电子产品、电影海报）。
   - 如果是：该物品就是【subject】。
   - 如果否（文案是纯风景描绘）：【subject】填"无"。
2. **分析背景 (Environment)**：
{logic_prompt}
3. **负向过滤（未来禁区）**：**严禁**提取 {curr_hour}:00 之后的未来日程作为背景。
   - 错误示例：现在8点，日程显示11点去公园。-> **绝对不能**画公园。
   - 正确操作：现在8点，日程显示9点才醒。-> **必须**画卧室/床/室内。

【提取要求】
1. **主体 (subject)**：【最重要】画面的核心物体描述（例如：精致的荷花酥，一杯牛奶或者一本封皮复古的书）。如果是纯风景或画人，此项填"无"。
2. **环境 (environment)**：根据逻辑确定的具体地点。
3. **光影 (lighting)**：参考时间段[{time_hint}]。如果是室内，强调人造光；如果是室外，强调自然天气氛围。
4. **穿搭 (outfit)**：{outfit_hint} 请明确区分"内搭"和"外穿"层次。
5. **动作 (action)**：人物动作。

请严格输出 JSON 格式：
{{
    "subject": "...",      // 主体 (例如: 粉色荷花酥)
    "environment": "...",  // 环境 (例如: 苏州河畔的野餐垫上)
    "lighting": "...",     // 光影 (例如：昏黄的室内灯光)
    "outfit": "...",       // 穿搭 (例如：白色棒球服外套，内搭黑色高领毛衣)
    "action": "...",       // 动作 (例如：双手捧着热咖啡)
    "weather_vibe": "..."  // 例如：玻璃上有水雾，朦胧感
}}
"""

    # ==================== 2. 主入口 ====================

    async def generate_image(self, content: str, sharing_type: SharingType, life_context: str = None) -> Optional[str]:
        if not self.img_conf.get("enable_ai_image", False): return None

        is_text_priority = self.img_conf.get("priority_text_over_schedule", True)
        logic_str = "文案主导" if is_text_priority else "日程主导"

        logger.info(f"[DailySharing] 配图决策: 自拍模式 ({logic_str}) | 类型: {sharing_type.value}")

        visuals = {}
        if content or life_context:
            visuals = await self._agent_extract_visuals(content, life_context)

            if not visuals:
                logger.warning("[DailySharing] Agent 提取失败，已取消配图，仅发送文案")
                return None

            env = visuals.get('environment', '无')
            subj = visuals.get('subject', '无')
            outfit = visuals.get('outfit', '无')
            weather = visuals.get('weather_vibe', '无')
            logger.info(f"[DailySharing] Agent 提取 -> 主体: {subj} | 环境: {env} | 天气: {weather} | 穿搭: {outfit[:15] if outfit else '无'}...")

        prompt = self._assemble_selfie_prompt(content, sharing_type, visuals)

        if not prompt:
            logger.warning("[DailySharing] Prompt 组装失败，取消配图")
            return None
        logger.info(f"[DailySharing] 最终配图 Prompt: {prompt[:100]}...")
        self._last_image_description = prompt

        return await self._call_aiimg_selfie(prompt)

    def _assemble_selfie_prompt(self, content: str, sharing_type: SharingType, visuals: Dict) -> str:
        parts = []

        if self.debug_mode:
            logger.info("+" * 60)
            logger.info(f"[DailySharing] 【DEBUG】开始组装自拍 Prompt")
            logger.info(f"[DailySharing] 【DEBUG】Visuals 字典内容: {visuals}")

        subject_str = visuals.get("subject", "")
        has_subj = subject_str and subject_str not in ["无", "N/A", "None", ""]

        if has_subj:
            parts.append(f"手持或展示{subject_str}")

        outfit = visuals.get("outfit", "")
        if outfit:
            parts.append(f"穿着{outfit}")

        action = visuals.get("action", "")
        if action:
            parts.append(action)

        if sharing_type == SharingType.GREETING:
            parts.append("半身自拍，面对镜头，微笑，背景虚化")
        elif sharing_type == SharingType.MOOD:
            parts.append("特写自拍，情绪表达，景深效果")
        elif sharing_type == SharingType.NEWS:
            if not action and not has_subj:
                parts.append("中景生活快照，看手机或屏幕")
            else:
                parts.append("中景生活快照")
        elif sharing_type == SharingType.RECOMMENDATION:
            if not action and not has_subj:
                parts.append("中景，展示物品，手部特写")
            else:
                parts.append("中景，聚焦物体")
        else:
            parts.append("中景自然姿态自拍")

        env = visuals.get("environment", "")
        if env:
            parts.append(f"位于{env}")

        lighting = visuals.get("lighting", "")
        if lighting:
            parts.append(lighting)
        else:
            period = self._get_current_period()
            if period in [TimePeriod.NIGHT, TimePeriod.LATE_NIGHT]:
                parts.append("夜晚城市灯光")
            else:
                parts.append("白天自然光")

        weather_vibe = visuals.get("weather_vibe", "")
        if weather_vibe:
            parts.append(weather_vibe)

        result = "，".join(filter(None, parts))

        if self.debug_mode:
            logger.info(f"[DailySharing] 【DEBUG】组装完成，最终 Prompt 长度: {len(result)}")
            logger.info("+" * 60)

        return result

    # ==================== 3. 工具函数 ====================

    async def generate_video_from_image(self, image_path: str, content: str) -> Optional[str]:
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

            video_prompt = f"{self._last_image_description}, 生活片段, 电影感运镜, 缓慢平移, 高质量"

            if hasattr(self._aiimg_plugin, "_get_video_chain"):
                chain = self._aiimg_plugin._get_video_chain()
            else:
                logger.warning("[DailySharing] 无法获取视频服务配置链")
                return None

            if not chain:
                logger.warning("[DailySharing] 未配置视频服务提供商")
                return None

            provider_id = chain[0]
            try:
                backend = self._aiimg_plugin.registry.get_video_backend(provider_id)
                return await backend.generate_video_url(prompt=video_prompt, image_bytes=image_bytes)
            except Exception as e:
                logger.error(f"[DailySharing] 获取视频后端或生成失败: {e}")
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

    async def _call_aiimg_selfie(self, prompt: str) -> Optional[str]:
        self._ensure_plugin()
        if not self._aiimg_plugin:
            logger.error("[DailySharing] 未找到AI图像插件，请确保已安装 astrbot_plugin_aiimg")
            return None

        aiimg = self._aiimg_plugin

        try:
            persona_name = await self._get_default_persona_name()
            if not persona_name:
                logger.error("[DailySharing] 未获取到默认人格名称，无法使用自拍功能。请在 AstrBot 中配置默认人格。")
                return None

            logger.info(f"[DailySharing] 使用自拍模式，人格: {persona_name}")

            ref_paths = []
            if hasattr(aiimg, "_get_persona_config_selfie_reference_paths"):
                ref_paths = aiimg._get_persona_config_selfie_reference_paths(persona_name)

            if not ref_paths:
                logger.error(f"[DailySharing] 人格「{persona_name}」未配置自拍参考照。请在 aiimg 插件的 WebUI 中为该人格配置参考图。")
                return None

            if not hasattr(aiimg, "_read_paths_bytes") or not hasattr(aiimg, "edit"):
                logger.error("[DailySharing] aiimg 插件版本不支持自拍功能所需接口")
                return None

            ref_images = await aiimg._read_paths_bytes(ref_paths)
            if not ref_images:
                logger.error(f"[DailySharing] 人格「{persona_name}」的参考图文件读取失败")
                return None

            logger.info(f"[DailySharing] 获取到 {len(ref_images)} 张参考图")

            chain_override = None
            if hasattr(aiimg, "_get_persona_selfie_chain"):
                chain_override = aiimg._get_persona_selfie_chain(persona_name)

            if not chain_override:
                logger.error(f"[DailySharing] 人格「{persona_name}」未配置自拍服务商链路。请在 aiimg 插件的 WebUI 中为该人格配置 chain。")
                return None

            size = None
            prompt_prefix = ""
            if hasattr(aiimg, "_get_persona_selfie_config"):
                persona_conf = aiimg._get_persona_selfie_config(persona_name)
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
                    final_prompt = f"{prompt_prefix}\n\n用户要求：{prompt}"
                else:
                    final_prompt = f"请根据参考图生成一张新的自拍照：\n1) 以第1张参考图的人脸身份为准（仅人脸身份特征），保持五官/气质一致。\n2) 如果还有其它参考图，请将它们仅作为服装/姿势/构图/场景的参考。\n3) 输出一张高质量照片风格自拍，不要拼图，不要水印。\n\n用户要求：{prompt}"

            if self.debug_mode:
                logger.info("=" * 60)
                logger.info("[DailySharing] 【DEBUG】自拍模式参数：")
                logger.info(f"[DailySharing] 【DEBUG】人格: {persona_name}")
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
            return str(path_obj)

        except Exception as e:
            logger.error(f"[DailySharing] 自拍生成出错: {e}")
            return None
