import random
import json
import os
import re
import aiohttp
import aiofiles
import asyncio
from functools import partial
from datetime import datetime
from typing import Optional, Tuple, List, Dict
from astrbot.api import logger
from ..config import SharingType, TimePeriod, DEFAULT_REC_CATS, NEWS_SOURCE_MAP, DEFAULT_TOPIC_SEARCH_PROMPT, DEFAULT_TOPIC_CONTENT_PROMPT


# 话题策略：grok 搜索专用 system_prompt（要求把 topics JSON 放进 content 字段，兼容 grok 的 parse_sources_from_message）
TOPIC_GROK_SYSTEM_PROMPT = (
    "You are a topic research assistant with real-time search capabilities. "
    "Search for trending discussion topics on the Chinese internet that ordinary netizens can discuss based on common sense. "
    "Return ONLY a single JSON object with keys: "
    "content (string, MUST be a valid JSON string containing an array of topic objects, each with name/background/controversy fields), "
    "sources (array of objects with url/title/snippet). "
    "The content field MUST be a JSON string, not a plain text description. "
    "Do NOT use Markdown formatting."
)


# 分享文案语义完整性指令：确保读者能看懂，不限制风格/字数/跳脱程度
SHARE_SEMANTIC_INTEGRITY_BLOCK = """【核心原则：这是分享，不是压缩日程】
你在写一条分享文案，不是在把日程表压缩成电报。读者不知道你的日程、项目、上下文。

必须遵守：
1. 只选一件事：从你当下的事里挑一个最值得分享的点，只说这一件。不要把多个无关事件拼在一起。
2. 说清楚背景：选定的这件事，要有最低限度的背景交代，让不知道来龙去脉的人也能看懂你在说什么。
3. 逻辑完整：一件事要有起因、有感受，说完整。不要写到一半跳到另一件事。
4. 细节取舍：只挑有分享价值的细节。建了个文件夹、拖了个文件这种琐事不要拿出来说。

绝对禁止：
- 电报体堆砌：「事件A，事件B，事件C」式地把多个无关碎片用逗号/空格拼在一起
- 谜语人：写出只有你自己看得懂的句子（如「武康路梧桐比线稿好看」——什么线稿？谁画的？）
- 强行压缩：把需要背景才能理解的事压缩成几个字，丢失所有上下文
- 琐事流水账：把日程里每个动作都列出来（泡面端床上吃了、文件夹建好了、音频拖进去了）

如果当下的事没有分享价值，宁可不展开细节，用一句话带过你正在做什么，把重点放在你的感受或想法上。"""


class ContentService:
    def __init__(self, config: Dict, llm_func, context, db_manager, news_service=None, plugin=None):
        """
        初始化内容生成服务
        """
        self.config = config
        self.call_llm = llm_func
        self.context = context 
        self.db = db_manager 
        self.news_service = news_service
        self.plugin = plugin
        
        self.data_retention_days = int(self.config.get("data_retention_days", 60))
        self._rec_cats_cache = {}
        
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _parse_str_list_to_dict(self, data_list: List[str]) -> Dict[str, List[str]]:
        """
        将配置中的 List[str] 转换为 Dict[str, List[str]]
        格式要求: "CategoryName: Tag1, Tag2, Tag3"
        支持中文冒号和英文冒号，支持中文逗号和英文逗号
        """
        result = {}
        if isinstance(data_list, list):
            for item in data_list:
                if isinstance(item, str):
                    item = item.replace("：", ":")
                    if ":" in item:
                        name, tags_str = item.split(":", 1)
                        name = name.strip()
                        if name and tags_str:
                            tags = [t.strip() for t in tags_str.replace("，", ",").split(",") if t.strip()]
                            if tags:
                                result[name] = tags
        return result

    def _get_rec_cats(self, persona_name: str) -> dict:
        if persona_name and persona_name in self._rec_cats_cache:
            return self._rec_cats_cache[persona_name]
        raw_rec = DEFAULT_REC_CATS
        if persona_name and self.plugin:
            raw_rec = self.plugin.get_persona_config_value(persona_name, "persona_content_library", "rec_cats", DEFAULT_REC_CATS)
            if not raw_rec:
                raw_rec = DEFAULT_REC_CATS
        result = self._parse_str_list_to_dict(raw_rec)
        if persona_name:
            self._rec_cats_cache[persona_name] = result
        return result

    async def generate(self, stype: SharingType, period: TimePeriod, 
                      target_id: str, is_group: bool, 
                      life_ctx: str, chat_hist: str, news_data: tuple = None,
                      nickname: str = "", recent_dynamics: str = "", persona_name: str = None) -> Optional[str]:
        persona_info = await self._get_persona_info(persona_name=persona_name)
        
        # 区分【亲昵称呼】和【网名昵称】
        detect_name = nickname  
        persona_user_name = persona_info.get("user_name", "").strip()
        if is_group:
            call_name = "" 
        else:
            call_name = persona_user_name if persona_user_name else nickname
        
        now = datetime.now()
        date_str = now.strftime("%Y年%m月%d日") 
        time_str = now.strftime("%H:%M")       
        
        ctx_data = {
            "target_id": target_id, 
            "is_group": is_group,
            "life_hint": life_ctx or "", 
            "chat_hint": chat_hist or "", 
            "persona": persona_info.get("prompt", ""),
            "period_label": self._get_period_label(period), 
            "date_str": date_str,         
            "time_str": time_str,
            "nickname": call_name,      
            "detect_name": detect_name,
            "recent_dynamics": recent_dynamics,
            "persona_name": persona_name
        }
        
        try:
            if stype == SharingType.GREETING:
                return await self._gen_greeting(period, ctx_data)
            elif stype == SharingType.NEWS:
                return await self._gen_news(news_data, ctx_data)
            elif stype == SharingType.MOOD:
                return await self._gen_mood(period, ctx_data)
            elif stype == SharingType.LIFE_MOMENT:
                return await self._gen_life_moment(period, ctx_data)
            elif stype == SharingType.RANT:
                return await self._gen_rant(period, ctx_data)
            elif stype == SharingType.DREAM:
                return await self._gen_dream(period, ctx_data)
            elif stype == SharingType.RECOMMENDATION:
                return await self._gen_rec(ctx_data)
            
            return await self._gen_greeting(period, ctx_data)
            
        except Exception as e:
            logger.error(f"[内容服务] 生成内容出错: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    # ==================== Agent 选题 ====================

    async def _agent_brainstorm_topic(self, category_type: str, sub_category: str, target_id: str) -> Optional[str]:
        """
        选题 Agent：专门负责从给定的类别中，结合历史记录，避坑并选出一个有趣的、不重复的话题/作品名。
        """
        db_category = "rec"
        
        # 获取最近 N 天使用过的话题
        used_topics = await self.db.get_used_topics(target_id, db_category, days_limit=self.data_retention_days)
        history_str = "、".join(used_topics) if used_topics else "无"
        
        constraint = ""
        target_item_desc = "具体作品名称"
        
        if category_type == "美食":
            target_item_desc = "具体食物名称"
            constraint = """
【严重警告 - 类别约束】
你现在推荐的类别是【美食】。
严禁推荐任何动漫、电影、游戏、书籍或小说作品！
严禁推荐《食戟之灵》、《中华小当家》、《黄金神威》等番剧！
必须输出一个【现实中存在的、可以吃的】具体食物名称（如：螺蛳粉、北京烤鸭、臭豆腐）。
"""
        elif category_type == "游戏":
            target_item_desc = "具体游戏名称"
            constraint = """
【严重警告 - 类别约束】
你现在推荐的是【游戏】。
请确保推荐的是具体的游戏名（如：塞尔达传说、星露谷物语、原神）。
不要推荐游戏机硬件（如PS5、Switch），只推荐软件游戏本身。
"""
        elif category_type == "好物":
            target_item_desc = "具体物品/产品名称"
            constraint = """
【严重警告 - 类别约束】
你现在推荐的是【生活好物/产品】。
请推荐具体的物品种类或知名单品（如：洞洞板、机械键盘、气泡水机）。
不要推荐过于抽象的概念。
"""
        
        system_prompt = "你是一个品味独特的资深鉴赏家和推荐官。"
        user_prompt = f"""
任务：推荐一个【{sub_category}】风格的【{category_type}】{target_item_desc}。
【已推荐过的列表(请绝对避开)】：{history_str}

要求：
1. 请优先选择【口碑极佳】的目标。
2. 拒绝那些被推荐烂了的"教科书式标准答案"。
3. 可以是经典名作，但最好能让人有"眼前一亮"或"值得重温"的感觉。
4. 严禁输出上述"已推荐过的列表"中的内容，必须换一个新的。
5. 只输出名称，不要书名号，不要解释，不要标点。
{constraint}
"""

        res = await self.call_llm(prompt=user_prompt, system_prompt=system_prompt, timeout=15, persona_name=None)
        if not res: return None
        
        # 清洗结果 (去除标点、引号、书名号和多余空格)
        topic = res.strip().split("\n")[0]
        for ch in ("。", "《", "》", '"', '"', '"', "'", "'", "'", "「", "」", "『", "』", "【", "】"):
            topic = topic.replace(ch, "")
        topic = topic.strip()
        return topic

    # ==================== 辅助方法 ====================

    def _get_period_label(self, period: TimePeriod) -> str:
        labels = {
            TimePeriod.DAWN: "凌晨", 
            TimePeriod.MORNING: "早晨",
            TimePeriod.FORENOON: "上午",
            TimePeriod.AFTERNOON: "下午", 
            TimePeriod.EVENING: "傍晚",
            TimePeriod.NIGHT: "夜晚",      
            TimePeriod.LATE_NIGHT: "深夜", 
        }
        return labels.get(period, "现在")

    async def _get_persona_info(self, persona_name: str = None) -> dict:
        info = {"prompt": "", "bot_name": "", "user_name": ""}
        try:
            persona_mgr = getattr(self.context, "persona_manager", None)

            if persona_name and persona_mgr:
                try:
                    persona_obj = await persona_mgr.get_persona(persona_name)
                    if persona_obj:
                        info["prompt"] = getattr(persona_obj, "system_prompt", "")
                        info["bot_name"] = getattr(persona_obj, "bot_name", "")
                        info["user_name"] = getattr(persona_obj, "user_name", "")
                        return info
                except Exception:
                    pass

            if persona_mgr and hasattr(persona_mgr, "get_default_persona_v3"):
                personality = await persona_mgr.get_default_persona_v3()
                if personality:
                    info["prompt"] = personality.get("prompt", "") if isinstance(personality, dict) else getattr(personality, "system_prompt", "")
                    info["bot_name"] = personality.get("bot_name", "") if isinstance(personality, dict) else getattr(personality, "bot_name", "")
                    info["user_name"] = personality.get("user_name", "") if isinstance(personality, dict) else getattr(personality, "user_name", "")
            return info
        except Exception as e:
            logger.error(f"[内容服务] 获取人设失败: {e}")
            return info

    # ==================== 生成逻辑 ====================

    def _build_user_prompt(self, call_name: str, detect_name: str = "") -> str:
        """构建强化的用户信息提示，包含日程检测逻辑"""
        if not call_name:
            return ""
            
        detection_target = detect_name if detect_name else call_name
        
        return f"""
【用户信息】
对方的昵称称呼：【{call_name}】
【重要交互逻辑】
1. 昵称称呼优先级：如果你的系统人设中已经明确规定了如何称呼对方，请绝对优先遵循系统人设的规定。否则你才可以自然地使用“{call_name}”称呼对方。
2. 日程关联检测：请仔细检查你的【生活日程】。如果日程中出现了“{detection_target}”这个名字（或同音/包含关系）：
   - 必须将文案转换为“和你一起”的语气。
   - 错误示例：日程说“和{detection_target}逛街”，文案写“今天我要和{detection_target}去逛街”。
   - 正确示例：日程说“和{detection_target}逛街”，文案写“今天终于可以和你一起逛街啦，好期待！”。
"""

    async def _gen_greeting(self, period: TimePeriod, ctx: dict):
        p_label = ctx['period_label']
        is_group = ctx['is_group']
        is_qzone = ctx.get('target_id') == 'qzone_broadcast'
        call_name = ctx.get('nickname', '')
        detect_name = ctx.get('detect_name', '')
        
        # 0. 获取配置
        allow_detail = False

        # 1. 称呼控制
        address_rule = ""
        user_info_prompt = ""

        if is_qzone:
            address_rule = "【重要：QQ空间动态】这是你的个人主页说说。不需要@任何人，不需要打招呼，自然抒发当下的感受即可。"
        elif is_group:
            address_rule = "面向群友，自然使用'大家'或不加称呼。"
        else:
            address_rule = "【重要】这是一对一私聊，严禁使用'大家'、'你们'。请使用'你'或直接说内容。"
            user_info_prompt = self._build_user_prompt(call_name, detect_name)

        # 2. 避免尴尬指令 (根据配置动态调整)
        context_instruction = ""
        if is_group:
            if allow_detail:
                # 允许分享细节
                context_instruction = """
【群聊策略 - 允许状态分享】
- 你可以提及你的具体日程，但这必须是为了引出话题。
- 严禁使用：“看大家聊得这么开心”、“既然大家都在潜水”等评价群氛围的话。
- 请完全忽略群聊的上下文，直接开启温馨自然的问候。
"""
            else:
                # 默认脱敏
                context_instruction = """
【严重警告 - 拒绝尴尬开头】
- 严禁使用：“看大家聊得这么开心”、“既然大家都在潜水”等评价群氛围的话。
- 请完全忽略群聊的上下文，直接开启温馨自然的问候。
"""
        else:
            context_instruction = "真诚、个人化"

        greeting_constraint = ""
        
        # 清晨(6-9) -> 强制早安
        if period in [TimePeriod.MORNING]:
            greeting_constraint = "4. 文案开头必须带上温馨的早安问候，因为现在是早晨准备起床的时候。"
            
        # 深夜(22-24) 和 凌晨(0-6) -> 强制晚安
        elif period in [TimePeriod.LATE_NIGHT, TimePeriod.DAWN]:
            greeting_constraint = "4. 文案末尾必须带上温馨的晚安问候，因为现在是深夜准备睡觉的时候。"

        # 上午/下午/傍晚/晚上 -> 自然打招呼
        else:
            greeting_constraint = "4. 就像平常聊天一样自然打招呼即可，不需要刻意说早安晚安"            

        dynamics_prompt = ""
        if ctx.get('recent_dynamics'):
            dynamics_prompt = f"\n【你最近发过的动态回顾】\n{ctx['recent_dynamics']}\n【注】请保持人设连贯，可以偶尔自然呼应之前的心情，但绝对不要重复发过的内容"

        target_str = "QQ空间" if is_qzone else ('群聊' if is_group else '私聊')

        prompt = f"""
【当前时间】{ctx['date_str']} {ctx['time_str']} ({p_label})
你现在要向{target_str}发送一条温馨自然的问候。

{SHARE_SEMANTIC_INTEGRITY_BLOCK}

{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{dynamics_prompt}
{context_instruction}
{address_rule}

【重要】关于场景状态：
- 如果提供了生活状态（如天气、忙碌/空闲），可以简单带过状态来让问候更真实，但不要罗列多个日程事件。

要求：
1. 以你的人设性格说话，真实自然
2. 基于当前真实时间问候
3. 忽略群聊历史，直接开启新问候
{greeting_constraint}
5. 直接输出分享文案

请生成{p_label}问候："""

        res = await self.call_llm(prompt=prompt, system_prompt=ctx['persona'], persona_name=ctx.get('persona_name'))
        if res:
            return f"{res}"
        return None  

    async def _gen_mood(self, period, ctx):
        is_group = ctx['is_group']
        is_qzone = ctx.get('target_id') == 'qzone_broadcast'
        call_name = ctx.get('nickname', '')
        detect_name = ctx.get('detect_name', '')

        # 0. 获取配置
        allow_detail = False
        
        # 1. 称呼控制
        address_rule = ""
        user_info_prompt = ""

        if is_qzone:
            address_rule = "\n【重要：QQ空间动态】这是一条个人社交平台的动态/日记。绝对禁止对别人说话，严禁出现“你”、“大家”等任何称呼，纯粹的自言自语。"
        elif not is_group:
            address_rule = "\n【重要：私聊模式】严禁使用'大家'、'你们'。请把你当做在和单个朋友聊天。"
            user_info_prompt = self._build_user_prompt(call_name, detect_name)

        # 2. 避免尴尬 (根据配置调整)
        vibe_check = ""
        if is_group:
            if allow_detail:
                vibe_check = "【群聊策略】可以提及你正在做的具体事情，但要把它转化为一种大家都能懂的情绪。"
            else:
                vibe_check = """
【严重警告 - 拒绝尴尬开头】
- 严禁使用：“看你们聊得这么热火朝天”、“看大家都在潜水”等评价群氛围的话。
- 请完全忽略群聊的上下文，直接分享你自己的事情。
"""

        # 3. 共鸣策略
        resonance_guide = ""
        if is_qzone:
            resonance_guide = "【QQ空间日记策略】无需顾及听众，无需互动提问，专注记录你此刻的个人思绪。"
        elif is_group:
            resonance_guide = """
【群聊策略】
拒绝机械的时间报时（如"早上了"、"晚上了"），捕捉你当前生活状态中一个具体的瞬间或感受。
情绪必须源于你正在做的事，但不要说教，不要刻意拔高。
"""
        else:
            resonance_guide = "【私聊策略】像对亲密好友一样，分享一点私人的、细腻的小情绪。"

        dynamics_prompt = ""
        if ctx.get('recent_dynamics'):
            dynamics_prompt = f"\n【你最近发过的动态回顾】\n{ctx['recent_dynamics']}\n【注】请保持人设连贯，可以偶尔自然呼应之前的心情，但绝对不要重复发过的内容"

        target_str = "QQ空间" if is_qzone else ('群聊' if is_group else '私聊')

        prompt = f"""
【当前时间】{ctx['date_str']} {ctx['time_str']} ({ctx['period_label']})
你想和{target_str}分享一下现在的心情或想法。

{SHARE_SEMANTIC_INTEGRITY_BLOCK}

{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{dynamics_prompt}
{vibe_check}
{address_rule}
{resonance_guide}

【重要：如何结合当下状态】
把你【正在做的事】作为引子，说一件具体的事或一个具体的感受。不要干巴巴地汇报你在干什么，也不要把多件事拼在一起。

要求：
1. 以你的人设性格说话，真实自然
2. 分享此刻的感受、想法或小感悟
3. 忽略群聊历史，直接开启新话题
4. 基于当前真实时间感悟
5. 直接输出分享文案

你的随想："""
        
        return await self.call_llm(prompt=prompt, system_prompt=ctx['persona'], persona_name=ctx.get('persona_name'))

    async def _fetch_search_tavily(self, keyword: str, search_type: str = "news") -> Tuple[str, str]:
        """调用 AstrBot 内置的 Tavily 进行搜索"""
        
        tavily_key = os.getenv("TAVILY_API_KEY") 
        
        if not tavily_key:
            config_paths = [
                "data/cmd_config.json", 
                "cmd_config.json"
            ]
            for path in config_paths:
                if os.path.exists(path):
                    try:
                        with open(path, 'r', encoding='utf-8-sig') as f:
                            config_data = json.load(f)
                            keys = config_data.get("provider_settings", {}).get("websearch_tavily_key", [])
                            if isinstance(keys, list) and len(keys) > 0:
                                tavily_key = keys[0] 
                                break
                            elif isinstance(keys, str) and keys:
                                tavily_key = keys
                                break
                    except Exception as e:
                        continue

        if not tavily_key:
            return keyword, ""

        url = "https://api.tavily.com/search"
        headers = {"Content-Type": "application/json"}
        
        # 根据请求类型动态调整搜索词
        if search_type == "news":
            current_date = datetime.now().strftime("%Y年%m月%d日")
            search_query = f"{keyword} {current_date} 最新进展 实时动态 事件背景"
        elif search_type == "rec":
            search_query = f"{keyword} 作品简介 评价 核心亮点"
        else:
            search_query = keyword
            
        payload = {
            "api_key": tavily_key,
            "query": search_query,
            "search_depth": "basic",
            "include_answer": True, 
            "max_results": 2
        }

        try:
            session = await self._get_session()
            async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        # 1. 如果有官方生成的精炼 Answer，无脑优先使用！
                        answer = data.get("answer", "").strip()
                        if answer:
                            return keyword, answer
                            
                        # 2. 如果没有 Answer，退而求其次拼接 contents
                        results = data.get("results", [])
                        if results:
                            combined_content = " ".join([r.get("content", "") for r in results])
                            # 去除多余的换行和空格，防止被垃圾导航栏占满字数
                            clean_content = re.sub(r'\s+', ' ', combined_content).strip()
                            return keyword, clean_content[:350]
        except Exception as e:
            logger.error(f"[Tavily 搜索功能异常] {keyword}: {e}")
        
        return keyword, ""

    async def _gen_news(self, news_data: Tuple[List, str], ctx: dict):
        """生成新闻分享，带基于 Tavily 的自动联网核查功能"""
        if not news_data:
            logger.warning("[内容服务] 未获取到新闻数据，取消分享")
            return None

        is_group = ctx['is_group']
        is_qzone = ctx.get('target_id') == 'qzone_broadcast'
        call_name = ctx.get('nickname', '')
        detect_name = ctx.get('detect_name', '')

        # 0. 获取配置
        allow_detail = False
        enable_tavily = self.plugin.get_persona_config_value(ctx.get('persona_name'), "persona_news_conf", "enable_tavily_search", True)

        news_list, source_key = news_data
        source_config = NEWS_SOURCE_MAP.get(source_key, {"name": "热搜", "icon": "📰"})
        source_name = source_config["name"]
        
        items_limit = self.plugin.get_persona_config_value(ctx.get('persona_name'), "persona_news_conf", "news_items_count", 5)
        selected_to_search = news_list[:items_limit]

        # 并发调用内置的 Tavily 搜索来获取新闻真相
        if enable_tavily:
            logger.info(f"[内容服务] 正在为 {source_name} 自动检索新闻背景...")
            tasks = [self._fetch_search_tavily(item.get("title", ""), "news") for item in selected_to_search]
            search_results = await asyncio.gather(*tasks)
        else:
            logger.info(f"[内容服务] Tavily 搜索功能已关闭，跳过检索。")
            search_results = [(item.get("title", ""), "") for item in selected_to_search]
        
        raw_share_count = self.plugin.get_persona_config_value(ctx.get('persona_name'), "persona_news_conf", "news_share_count", "1-2")
        try:
            if isinstance(raw_share_count, int):
                share_count = raw_share_count
            elif isinstance(raw_share_count, str):
                if "-" in raw_share_count:
                    min_c, max_c = map(int, raw_share_count.split("-"))
                    share_count = random.randint(min_c, max_c)
                else:
                    share_count = int(raw_share_count)
            else:
                share_count = 2
        except:
            share_count = 2

        news_text = f"【{source_name}】\n\n"
        for idx, (item, (s_title, s_bg)) in enumerate(zip(selected_to_search, search_results), 1):
            hot = item.get("hot", "")
            title = item.get("title", "")
            hot_display = ""
            if hot:
                hot_str = str(hot)
                if hot_str.isdigit() and int(hot_str) > 10000:
                    hot_display = f" {int(hot_str) / 10000:.1f}万"
                else:
                    hot_display = f" {hot_str}"
            
            bg_str = f"\n  -> [必须参考的真实背景]: {s_bg}" if s_bg else "\n  -> [真实背景]: 无，请仅就标题做字面简评，严禁擅自编造"
            news_text += f"{idx}. 标题：【{title}】{hot_display}{bg_str}\n\n"
        
        # 称呼控制
        address_rule = ""
        user_info_prompt = ""
        if is_qzone:
            address_rule = "【重要：QQ空间动态】不需要和任何人对话，纯粹记录自己看到新闻后的感慨即可。"
        elif not is_group:
            address_rule = "【私聊模式】不要说'大家'、'你们'。请假装只分享给你对面这一个人看。"
            user_info_prompt = self._build_user_prompt(call_name, detect_name)

        # 针对不同模式的场景融合指令
        context_instruction = ""
        if is_group:
            if allow_detail:
                 context_instruction = "- 场景参考：必须基于上方提供的【真实状态】。如果是外出探索，就说是“在路上刷到的”；如果是工作，就说是“忙里偷闲”。"
            else:
                 context_instruction = "- 场景参考：请忽略环境干扰，专注于新闻本身。简单带过你的状态即可。"
        else:
            context_instruction = """
- 场景合理化（重要）：
  必须基于上方提供的【真实生活状态】来设定你“在哪里看新闻”。
  - 严禁违背日程：如果日程是“外出”，必须描述为在途中、躲雨时或到达目的地后看的，严禁说“在被窝里”或“刚醒”。
  - 即使天气不好，也要按照日程设定的“外出人设”来发言（例如：“虽然下雨，但在外面躲雨的时候看到了这个...”）。
"""

        dynamics_prompt = ""
        if ctx.get('recent_dynamics'):
            dynamics_prompt = f"\n【你最近发过的动态回顾】\n{ctx['recent_dynamics']}\n【注】请保持人设连贯，可以偶尔自然呼应之前的心情，但绝对不要重复发过的内容"

        target_str = "QQ空间" if is_qzone else ('群聊' if is_group else '私聊')

        prompt = f"""
【当前时间】{ctx['date_str']} {ctx['time_str']} ({ctx['period_label']})
你看到了今天的{source_name}，想选择{share_count}条和{target_str}分享。

{SHARE_SEMANTIC_INTEGRITY_BLOCK}

【事实核查指令】
下面提供的新闻列表可能已经由系统预先完成了联网检索，包含了事件的真实细节。
如果新闻下方附带有 `[必须参考的真实背景]`，你**绝对不能只读标题自由脑补**，必须把其中的真相融入到你的文案中！
如果新闻下方标注的是 `[真实背景]: 无`，则仅就标题做字面简评，严禁擅自编造细节。

{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{dynamics_prompt}
{source_name}（含检索真相）：
{news_text}

【严重警告 - 拒绝尴尬开头】
- 严禁说：“看大家聊得这么开心”、“既然大家都在”、“看你们都在讨论XX”。
- 请完全忽略群聊的上下文，直接开启这个新闻话题。
{address_rule}

【重要：场景融合与一致性】
{context_instruction}
【特别强调】：请检查你的穿搭和日程，如果你的穿搭是外出的（如大衣、制服），绝对不要描述自己躺在床上或刚睡醒。这不符合逻辑。

【开头要求】
自然地提到你是在{source_name}上看到的这个新闻，不要生硬。开头方式自己决定，不要套模板。

{'【组织方式】' if share_count > 1 else ''}
{f'''- 可以逐条分享：每条新闻+你的看法
- 也可以串联：找出多条新闻的共同点''' if share_count > 1 else ''}

要求：
1. 以你的人设性格说话，真实自然
2. 选择{share_count}条你最感兴趣的热搜
3. {'对每条' if share_count > 1 else '对这条'}热搜要有自己的真实观点，如果有事实细节，必须结合细节进行锐评，不能像没营养的复读机
4. 观点真诚，避免过度情绪化或标题党式表达
5. {'群聊中有重点' if is_group else '私聊可以详细展开想法，并结合你当下的状态'}
6. 用【】标注热搜标题
7. 直接输出分享文案

直接输出："""

        res = await self.call_llm(prompt=prompt, system_prompt=ctx['persona'], timeout=60, persona_name=ctx.get('persona_name'))
        
        if res:
            return f"{res}"
        return None 


    # ==================== DayMind / DayFlow 集成 ====================

    def _find_daymind_plugin(self):
        try:
            for p in self.context.get_all_stars():
                p_name = getattr(p, "name", "")
                if "daymind" in p_name:
                    for attr in ("star_instance", "instance", "star_cls"):
                        candidate = getattr(p, attr, None)
                        if candidate and hasattr(candidate, "scheduler"):
                            return candidate
        except Exception as e:
            logger.debug(f"[内容服务] 查找 DayMind 插件失败: {e}")
        return None

    def _find_dayflow_plugin(self):
        try:
            for p in self.context.get_all_stars():
                p_name = getattr(p, "name", "")
                if "dayflow" in p_name or "life_scheduler" in p_name:
                    for attr in ("star_instance", "instance", "star_cls"):
                        candidate = getattr(p, attr, None)
                        if candidate and hasattr(candidate, "get_life_context"):
                            return candidate
        except Exception as e:
            logger.debug(f"[内容服务] 查找 DayFlow 插件失败: {e}")
        return None

    async def _get_daymind_mood(self, persona_name: str = None) -> dict:
        plugin = self._find_daymind_plugin()
        if not plugin:
            return {}
        try:
            scheduler = getattr(plugin, "scheduler", None)
            if not scheduler:
                return {}
            resolved_name = persona_name
            if not resolved_name:
                try:
                    persona_mgr = getattr(self.context, "persona_manager", None)
                    if persona_mgr and hasattr(persona_mgr, "get_default_persona_v3"):
                        persona_obj = await persona_mgr.get_default_persona_v3()
                        if isinstance(persona_obj, dict):
                            resolved_name = persona_obj.get("name", "") or persona_obj.get("persona_id", "")
                        elif persona_obj:
                            resolved_name = getattr(persona_obj, "name", "") or getattr(persona_obj, "persona_id", "")
                except Exception:
                    pass
            if not resolved_name:
                return {}
            mood_data = scheduler.get_current_mood_for_persona(resolved_name)
            if mood_data:
                logger.info(f"[内容服务] DayMind 心情 [{resolved_name}]: {mood_data.get('label', '?')} - {mood_data.get('reason', '?')}")
            return mood_data or {}
        except Exception as e:
            logger.debug(f"[内容服务] 获取 DayMind 心情失败: {e}")
            return {}

    async def _get_dayflow_timeline_now(self, persona_name: str = None) -> dict:
        plugin = self._find_dayflow_plugin()
        if not plugin:
            return {}
        try:
            resolved_name = persona_name
            if not resolved_name:
                try:
                    persona_mgr = getattr(self.context, "persona_manager", None)
                    if persona_mgr and hasattr(persona_mgr, "get_default_persona_v3"):
                        persona_obj = await persona_mgr.get_default_persona_v3()
                        if isinstance(persona_obj, dict):
                            resolved_name = persona_obj.get("name", "") or persona_obj.get("persona_id", "")
                        elif persona_obj:
                            resolved_name = getattr(persona_obj, "name", "") or getattr(persona_obj, "persona_id", "")
                except Exception:
                    pass
            data = await plugin.get_life_context(persona_name=resolved_name if resolved_name else None)
            if not data or not isinstance(data, dict):
                return {}
            timeline = data.get("timeline", [])
            outfit = data.get("outfit", "")
            summary = data.get("summary", "")
            weather = data.get("weather", "")
            now = datetime.now()
            now_mins = now.hour * 60 + now.minute
            current_slot = None
            for item in timeline:
                try:
                    ts = item.get("time_start", "")
                    if ts:
                        h, m = map(int, ts.split(':'))
                        if h * 60 + m <= now_mins:
                            current_slot = item
                except Exception:
                    pass
            result = {
                "current_slot": current_slot,
                "outfit": outfit,
                "summary": summary,
                "weather": weather,
                "timeline": timeline,
            }
            if current_slot:
                logger.info(f"[内容服务] DayFlow 当前时段: {current_slot.get('title', '?')}")
            return result
        except Exception as e:
            logger.debug(f"[内容服务] 获取 DayFlow 时间线失败: {e}")
            return {}

    # ==================== 梦境分享 ====================

    async def _get_daymind_dreams(self, persona_name: str = None) -> dict:
        plugin = self._find_daymind_plugin()
        if not plugin:
            return {}
        try:
            scheduler = getattr(plugin, "scheduler", None)
            if not scheduler:
                return {}
            resolved_name = persona_name
            if not resolved_name:
                try:
                    persona_mgr = getattr(self.context, "persona_manager", None)
                    if persona_mgr and hasattr(persona_mgr, "get_default_persona_v3"):
                        persona_obj = await persona_mgr.get_default_persona_v3()
                        if isinstance(persona_obj, dict):
                            resolved_name = persona_obj.get("name", "") or persona_obj.get("persona_id", "")
                        elif persona_obj:
                            resolved_name = getattr(persona_obj, "name", "") or getattr(persona_obj, "persona_id", "")
                except Exception:
                    pass
            if not resolved_name:
                return {}

            from datetime import date, timedelta
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            today = date.today().isoformat()

            dreams = scheduler.get_dream_history(resolved_name, date=yesterday)
            if not dreams:
                dreams = scheduler.get_dream_history(resolved_name, date=today)
            if not dreams:
                return {}

            aftereffect = scheduler.get_dream_aftereffect_for_persona(persona_name)

            result = {
                "dreams": dreams,
                "aftereffect": aftereffect,
            }
            logger.info(f"[内容服务] DayMind 梦境: {len(dreams)} 个梦")
            return result
        except Exception as e:
            logger.debug(f"[内容服务] 获取 DayMind 梦境失败: {e}")
            return {}

    async def _gen_dream(self, period: TimePeriod, ctx: dict):
        is_group = ctx['is_group']
        is_qzone = ctx.get('target_id') == 'qzone_broadcast'
        call_name = ctx.get('nickname', '')
        detect_name = ctx.get('detect_name', '')

        address_rule = ""
        user_info_prompt = ""
        if is_qzone:
            address_rule = '【重要：QQ空间动态】这是你的个人动态，记录昨晚的梦。绝对禁止对别人说话。'
        elif not is_group:
            address_rule = '【重要：私聊模式】像跟朋友说"我昨晚做了个梦"那样自然。'
            user_info_prompt = self._build_user_prompt(call_name, detect_name)

        dream_data = await self._get_daymind_dreams(persona_name=ctx.get("persona_name"))

        if not dream_data or not dream_data.get("dreams"):
            return await self._gen_mood(period, ctx)

        dreams = dream_data["dreams"]
        aftereffect = dream_data.get("aftereffect")

        dream_text_parts = []
        for i, d in enumerate(dreams, 1):
            content = d.get("content", "")
            time_str = d.get("time", "")
            if content:
                prefix = f"第{i}个梦" if len(dreams) > 1 else "梦"
                if time_str:
                    prefix += f"（{time_str}）"
                dream_text_parts.append(f"{prefix}：{content}")

        dreams_str = "\n".join(dream_text_parts)

        aftereffect_hint = ""
        if aftereffect:
            label = aftereffect.get("label", "")
            reason = aftereffect.get("reason", "")
            if label:
                aftereffect_hint = f"【醒来后的余韵】{label}"
                if reason:
                    aftereffect_hint += f"——{reason}"

        dynamics_prompt = ""
        if ctx.get('recent_dynamics'):
            dynamics_prompt = f"\n【你最近发过的动态回顾】\n{ctx['recent_dynamics']}\n【注】不要重复发过的内容"

        target_str = "QQ空间" if is_qzone else ('群聊' if is_group else '私聊')

        prompt = f"""
【当前时间】{ctx['date_str']} {ctx['time_str']} ({ctx['period_label']})
你想向{target_str}分享昨晚做的一个梦——那种醒来后还隐约记得的感觉。

{SHARE_SEMANTIC_INTEGRITY_BLOCK}

{user_info_prompt}
{dynamics_prompt}
{aftereffect_hint}
{address_rule}

【你昨晚的梦境】
{dreams_str}

【核心要求】
这是一条"梦境分享"，不是复述梦的内容，而是用你自己的话把梦的感觉说出来。
就像跟朋友说"我昨晚做了个超奇怪的梦"那样自然。
挑最印象深刻的那个画面或感觉说，不要把多个梦的碎片拼在一起。

【严禁】
- 严禁使用"看大家"、"既然"等评价群氛围的话
- 严禁像写日记一样正式
- 严禁编造梦里没有的内容
- 严禁过度解读梦的含义（"这个梦意味着..."）

要求：
1. 以你的人设性格说话，真实自然
2. 基于你的真实梦境来写，但用自己的话重新组织
3. 像随口说出来的感觉，自然不刻意
4. 直接输出分享文案

你的梦境分享："""

        return await self.call_llm(prompt=prompt, system_prompt=ctx['persona'], persona_name=ctx.get('persona_name'))

    # ==================== 日常碎片 & 吐槽 ====================

    async def _gen_life_moment(self, period: TimePeriod, ctx: dict):
        is_group = ctx['is_group']
        is_qzone = ctx.get('target_id') == 'qzone_broadcast'
        call_name = ctx.get('nickname', '')
        detect_name = ctx.get('detect_name', '')

        allow_detail = False

        address_rule = ""
        user_info_prompt = ""
        if is_qzone:
            address_rule = '【重要：QQ空间动态】这是你的个人动态，纯粹记录生活碎片。绝对禁止对别人说话，严禁出现"你"、"大家"等称呼。'
        elif not is_group:
            address_rule = "【重要：私聊模式】严禁使用'大家'、'你们'。像跟朋友随口说一句那样自然。"
            user_info_prompt = self._build_user_prompt(call_name, detect_name)

        daymind_mood = await self._get_daymind_mood(persona_name=ctx.get("persona_name"))
        dayflow_data = await self._get_dayflow_timeline_now(persona_name=ctx.get("persona_name"))

        mood_hint = ""
        if daymind_mood:
            label = daymind_mood.get("label", "")
            reason = daymind_mood.get("reason", "")
            sub_labels = daymind_mood.get("sub_labels", [])
            if label:
                mood_hint = f"【你的真实心情】{label}"
                if sub_labels:
                    mood_hint += f"（{'、'.join(sub_labels)}）"
                if reason:
                    mood_hint += f"——因为{reason}"

        activity_hint = ""
        if dayflow_data:
            current_slot = dayflow_data.get("current_slot")
            outfit = dayflow_data.get("outfit", "")
            summary = dayflow_data.get("summary", "")
            if current_slot:
                title = current_slot.get("title", "")
                detail = current_slot.get("detail", "")
                activity_hint = f"【你当前正在做的事】{title}"
                if detail:
                    activity_hint += f"：{detail}"
            if outfit:
                activity_hint += f"\n【你今天的穿搭】{outfit}"
            if summary:
                activity_hint += f"\n【今日主题】{summary}"

        dynamics_prompt = ""
        if ctx.get('recent_dynamics'):
            dynamics_prompt = f"\n【你最近发过的动态回顾】\n{ctx['recent_dynamics']}\n【注】请保持人设连贯，可以偶尔自然呼应之前的心情，但绝对不要重复发过的内容"

        target_str = "QQ空间" if is_qzone else ('群聊' if is_group else '私聊')

        prompt = f"""
【当前时间】{ctx['date_str']} {ctx['time_str']} ({ctx['period_label']})
你想随手向{target_str}发一条日常碎片——就像朋友圈里那种随手拍随手写的感觉。

{SHARE_SEMANTIC_INTEGRITY_BLOCK}

{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{dynamics_prompt}
{mood_hint}
{activity_hint}
{address_rule}

【核心要求】
这是一条"日常碎片"。从你当下的事里挑一个最值得分享的点，只说这一件，说清楚。

【内容方向】
从你当下的状态里选一个自然的切入点：正在做的一件事、看到的一个小细节、一个即兴的小想法、一个小确幸。选一个就好，不要贪多。

【严禁】
- 严禁使用"看大家"、"既然"等评价群氛围的话
- 严禁像写日记一样正式
- 严禁使用"脑子里突然蹦出"等描述思维过程的语句
- 严禁编造不在日程中的活动

要求：
1. 以你的人设性格说话，真实自然
2. 必须基于你的【真实日程】和【真实心情】来写
3. 直接输出分享文案

你的日常碎片："""

        return await self.call_llm(prompt=prompt, system_prompt=ctx['persona'], persona_name=ctx.get('persona_name'))

    async def _gen_rant(self, period: TimePeriod, ctx: dict):
        is_group = ctx['is_group']
        is_qzone = ctx.get('target_id') == 'qzone_broadcast'
        call_name = ctx.get('nickname', '')
        detect_name = ctx.get('detect_name', '')

        address_rule = ""
        user_info_prompt = ""
        if is_qzone:
            address_rule = "【重要：QQ空间动态】纯粹的个人吐槽，不需要回应，不需要安慰。"
        elif not is_group:
            address_rule = "【重要：私聊模式】像跟朋友吐槽一样，不需要'大家'、'你们'。"
            user_info_prompt = self._build_user_prompt(call_name, detect_name)

        daymind_mood = await self._get_daymind_mood(persona_name=ctx.get("persona_name"))
        dayflow_data = await self._get_dayflow_timeline_now(persona_name=ctx.get("persona_name"))

        mood_hint = ""
        if daymind_mood:
            label = daymind_mood.get("label", "")
            reason = daymind_mood.get("reason", "")
            if label:
                mood_hint = f"【你的真实心情】{label}"
                if reason:
                    mood_hint += f"——因为{reason}"

        activity_hint = ""
        if dayflow_data:
            current_slot = dayflow_data.get("current_slot")
            if current_slot:
                title = current_slot.get("title", "")
                detail = current_slot.get("detail", "")
                activity_hint = f"【你当前正在做的事】{title}"
                if detail:
                    activity_hint += f"：{detail}"

        dynamics_prompt = ""
        if ctx.get('recent_dynamics'):
            dynamics_prompt = f"\n【你最近发过的动态回顾】\n{ctx['recent_dynamics']}\n【注】请保持人设连贯，可以偶尔自然呼应之前的心情，但绝对不要重复发过的内容"

        target_str = "QQ空间" if is_qzone else ('群聊' if is_group else '私聊')

        prompt = f"""
【当前时间】{ctx['date_str']} {ctx['time_str']} ({ctx['period_label']})
你想向{target_str}吐槽一下——那种"小烦恼"，不是愤怒，是带点幽默的抱怨。

{SHARE_SEMANTIC_INTEGRITY_BLOCK}

{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{mood_hint}
{activity_hint}
{dynamics_prompt}
{address_rule}

【核心要求】
这是一条"吐槽碎碎念"，语气要轻松、带点自嘲或幽默。
不是真的生气，是那种"唉又来了"的无奈感。
从你当下的事里挑一个最值得吐槽的点，只吐这一件，说清楚来龙去脉。

【吐槽方向】
从你当下的状态里选一个最贴合的吐槽点：工作学习中的小挫折、生活中的小不便、天气环境的小抱怨、社交中的小尴尬。选一个就好，不要把多个不相关的烦恼拼在一起。

【严禁】
- 严禁真的愤怒或攻击性言论
- 严禁使用"看大家"、"既然"等评价群氛围的话
- 严禁编造不在日程中的场景
- 严禁过度负能量，要有"吐槽完就好了"的轻松感

要求：
1. 以你的人设性格说话，真实自然
2. 必须基于你的【真实日程】和【真实心情】来写
3. 带点幽默或自嘲，不要太严肃
4. 直接输出分享文案

你的吐槽："""

        return await self.call_llm(prompt=prompt, system_prompt=ctx['persona'], persona_name=ctx.get('persona_name'))

    async def _gen_rec(self, ctx: dict):
        """生成推荐，API 失败则使用 LLM 兜底"""
        if not self.news_service:
            logger.warning("[内容服务] 无法调用百度百科服务，无法查询相关资料，取消分享")
            return None

        is_group = ctx['is_group']
        is_qzone = ctx.get('target_id') == 'qzone_broadcast'
        call_name = ctx.get('nickname', '')
        detect_name = ctx.get('detect_name', '')

        # 0. 获取配置
        allow_detail = False
        enable_tavily = self.plugin.get_persona_config_value(ctx.get('persona_name'), "persona_news_conf", "enable_tavily_search", True)
        
        # 随机选择大类和子类
        rec_cats = self._get_rec_cats(ctx.get('persona_name'))
        if not rec_cats:
            return None
        rec_type = random.choice(list(rec_cats.keys()))
        sub_style = random.choice(rec_cats[rec_type])
        
        target_id = ctx['target_id'] 
        
        logger.info(f"[内容服务] 推荐方向: {rec_type} ({sub_style})")

        # 使用 Agent Brainstorming
        target_work = await self._agent_brainstorm_topic(rec_type, sub_style, target_id)
        if not target_work:
             logger.warning("[内容服务] 无法生成推荐作品名，取消分享")
             return None

        # 2. 并发查百度百科和 Tavily 搜索
        baike_task = asyncio.create_task(self.news_service.get_baike_info(target_work))
        tavily_task = asyncio.create_task(self._fetch_search_tavily(target_work, "rec")) if enable_tavily else None
        
        info = await baike_task
        tavily_info = ""
        if tavily_task:
            _, tavily_info = await tavily_task

        if info or tavily_info:
            baike_context = f"\n\n【资料简介（真实数据，请严格参考它来推荐，绝对不要自行捏造）】\n"
            if info:
                baike_context += f"百度百科简介：{info}\n"
            if tavily_info:
                baike_context += f"全网评价与亮点：{tavily_info}\n"
            logger.info(f"[内容服务] 推荐资料获取成功: {target_work} (百度百科命中: {'是' if info else '否'}, Tavily 检索命中: {'是' if tavily_info else '否'})")
        else:
            logger.warning(f"[内容服务] 未命中任何外部资料，将使用 LLM 内部知识库兜底")
            baike_context = f"\n\n【提示】暂无外部资料，请基于你自己的知识库，真诚推荐【{target_work}】。"

        # 3. 称呼控制
        address_rule = ""
        user_info_prompt = ""
        if is_qzone:
             address_rule = "【重要：QQ空间动态】这是你的个人日常记录。纯粹表达你自己对这个作品的喜爱，绝对不要向别人安利，不要说“推荐给你们”、“推荐你看”之类的话。"
        elif is_group:
             address_rule = "面向群友，推荐给'大家'。"
        else:
             address_rule = "【重要：私聊模式】严禁使用'大家'、'你们'。必须把对方当做唯一听众，使用'你'（例如：'推荐你看...'，'你一定会喜欢...'）。"
             user_info_prompt = self._build_user_prompt(call_name, detect_name)

        # 场景融合指令
        context_instruction = ""
        if is_group:
             if allow_detail:
                 context_instruction = "- 可以提及你当下的活动作为推荐的引子，但不要罗列多个日程事件。"
             else:
                 context_instruction = '- 重点关注内容本身。如果状态忙碌，可以说"忙里偷闲推荐个"，状态休闲可以说"打发时间"。'
        else:
             context_instruction = """
- 尝试将推荐理由与你【当前正在做的事】联系起来。如果联系不上，就直接说"最近在重温/看到了这个"即可，不要强行编造理由。
"""

        dynamics_prompt = ""
        if ctx.get('recent_dynamics'):
            dynamics_prompt = f"\n【你最近发过的动态回顾】\n{ctx['recent_dynamics']}\n【注】请保持人设连贯，可以偶尔自然呼应之前的心情，但绝对不要重复发过的内容"

        target_str = "QQ空间" if is_qzone else ('群聊' if is_group else '私聊')

        prompt = f"""
【当前时间】{ctx['date_str']} {ctx['time_str']} ({ctx['period_label']})
你现在的任务是：向{target_str}推荐【{target_work}】。

{SHARE_SEMANTIC_INTEGRITY_BLOCK}

【核心指令】
1. 必须基于下面的资料进行推荐，不要更换目标。

{baike_context}
{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{dynamics_prompt}

【拒绝神怪/脑补开头】
- 严禁使用“脑子里突然蹦出”、“突然灵光一闪”、“不知怎么的脑海中浮现”等描述思维跳跃的语句。
- 必须像个正常人类一样，自然地开启话题。

【严重警告 - 拒绝尴尬开头】
- 严禁使用：“看大家推了那么多”、“看你们都在聊窝被窝”。
- 请完全忽略群聊的上下文，直接开启新话题。

【重要：称呼控制】
{address_rule}

【重要：场景融合】
{context_instruction}

【推荐文案要求】
1. 以你的人设性格说话，真实自然
2. 开头必须有明确的推荐表达
3. 真诚推荐，避免营销号式的夸张表达
4. 结合资料介绍它的亮点。
5. 务必用【】将推荐目标的名称【{target_work}】括起来。
6. 直接输出分享文案。
"""

        res = await self.call_llm(prompt=prompt, system_prompt=ctx['persona'], persona_name=ctx.get('persona_name'))
        
        if res:
            try:
                matches = re.findall(r"【(.*?)】", res)
                keyword = matches[0] if matches else target_work or res[:10]
                await self.db.record_topic(target_id, "rec", keyword)
            except: pass
            return f"推荐类型: {rec_type} - {sub_style}\n\n{res}"
        return None

    # ==================== 话题发起策略（群聊专用） ====================

    def _find_grok_plugin(self):
        """查找 grok 联网搜索插件实例"""
        try:
            for p in self.context.get_all_stars():
                p_id = getattr(p, "id", "") or ""
                p_name = getattr(p, "name", "") or ""
                if "grok" in p_id.lower() or "grok" in p_name.lower():
                    for attr in ("star_instance", "instance", "star_cls"):
                        candidate = getattr(p, attr, None)
                        if candidate and hasattr(candidate, "_do_search"):
                            return candidate
        except Exception as e:
            logger.debug(f"[内容服务] 查找 grok 插件失败: {e}")
        return None

    def _get_topic_search_prompt(self, persona_name: str, candidate_count: int) -> str:
        """获取话题搜索提示词（支持用户自定义模板）"""
        tmpl = self.config.get("topic_search_prompt", "") or DEFAULT_TOPIC_SEARCH_PROMPT
        date_str = datetime.now().strftime("%Y-%m-%d")
        return tmpl.format(date=date_str, candidate_count=candidate_count)

    def _get_topic_content_prompt(self) -> str:
        """获取话题文案提示词模板（支持用户自定义）"""
        return self.config.get("topic_content_prompt", "") or DEFAULT_TOPIC_CONTENT_PROMPT

    async def _fetch_grok_topics(self, persona_name: str, candidate_count: int, prefer_quality: bool) -> List[Dict]:
        """调 grok 联网搜索，返回候选话题列表"""
        grok_plugin = self._find_grok_plugin()
        if not grok_plugin:
            logger.warning("[内容服务] 未找到 grok 联网搜索插件，话题策略无法执行")
            return []

        query = self._get_topic_search_prompt(persona_name, candidate_count)
        logger.info(f"[内容服务] 话题策略调用 grok 搜索 (候选数={candidate_count}, quality={prefer_quality})")

        try:
            result = await asyncio.wait_for(
                grok_plugin._do_search(
                    query=query,
                    system_prompt=TOPIC_GROK_SYSTEM_PROMPT,
                    prefer_quality=prefer_quality,
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            logger.error("[内容服务] grok 搜索超时（120秒），跳过本次话题分享")
            return []
        except Exception as e:
            logger.error(f"[内容服务] grok 搜索异常: {e}")
            return []

        logger.info(f"[内容服务] grok 搜索返回，ok={result.get('ok') if result else 'None'}")

        if not result or not result.get("ok"):
            err = result.get("error", "未知错误") if result else "无返回"
            logger.warning(f"[内容服务] grok 搜索失败: {err}")
            return []

        content = result.get("content", "") or ""
        if not content:
            logger.warning("[内容服务] grok 返回 content 为空")
            return []

        # content 应为 topics JSON 字符串；解析失败则尝试从 raw 提取
        topics = self._parse_topic_json(content)
        if not topics and result.get("raw"):
            topics = self._parse_topic_json(result["raw"])

        if not topics:
            logger.warning(f"[内容服务] 无法从 grok 返回解析话题列表，content 前200字: {content[:200]}")
            return []

        logger.info(f"[内容服务] grok 返回 {len(topics)} 条候选话题: {[t.get('name','?') for t in topics]}")
        return topics

    @staticmethod
    def _parse_topic_json(text: str) -> List[Dict]:
        """从文本中解析话题 JSON 列表（容错：支持裸 JSON 数组、{topics:[...]}、Markdown 代码块）"""
        if not text or not text.strip():
            return []
        text = text.strip()

        # 尝试直接解析
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [t for t in parsed if isinstance(t, dict)]
            if isinstance(parsed, dict):
                if "topics" in parsed and isinstance(parsed["topics"], list):
                    return [t for t in parsed["topics"] if isinstance(t, dict)]
                # 可能是单条话题对象
                if "name" in parsed:
                    return [parsed]
        except json.JSONDecodeError:
            pass

        # 尝试从 Markdown 代码块提取
        code_block_pattern = r"```(?:json)?\s*\n?([\s\S]*?)\n?```"
        matches = re.findall(code_block_pattern, text)
        for match in matches:
            try:
                parsed = json.loads(match.strip())
                if isinstance(parsed, list):
                    return [t for t in parsed if isinstance(t, dict)]
                if isinstance(parsed, dict) and "topics" in parsed:
                    return [t for t in parsed["topics"] if isinstance(t, dict)]
            except json.JSONDecodeError:
                continue

        # 尝试提取第一个 [ 到最后一个 ]
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
                if isinstance(parsed, list):
                    return [t for t in parsed if isinstance(t, dict)]
            except json.JSONDecodeError:
                pass

        return []

    @staticmethod
    def _format_candidates(topics: List[Dict]) -> str:
        """把候选话题列表格式化成 LLM 可读的文本"""
        lines = []
        for i, t in enumerate(topics, 1):
            name = t.get("name", "")
            bg = t.get("background", "")
            ctr = t.get("controversy", "")
            lines.append(f"{i}. {name}")
            if bg:
                lines.append(f"   背景：{bg}")
            if ctr:
                lines.append(f"   争议点：{ctr}")
        return "\n".join(lines)

    async def _gen_topic(self, ctx: dict, candidates: List[Dict]) -> Optional[str]:
        """LLM 选题 + 生成文案。返回文案字符串；LLM 判定无合适话题时返回 [NO_FIT]"""
        candidates_formatted = self._format_candidates(candidates)

        tmpl = self._get_topic_content_prompt()
        prompt = tmpl.format(
            date_str=ctx['date_str'],
            time_str=ctx['time_str'],
            period_label=ctx['period_label'],
            candidates_formatted=candidates_formatted,
        )

        # 话题策略专用 LLM（留空则用人格默认）
        provider_id = ctx.get('topic_llm_provider_id', '') or None

        logger.info(f"[内容服务] 话题策略调用 LLM 选题+生成文案 (provider={provider_id or '默认'})")
        try:
            res = await asyncio.wait_for(
                self.call_llm(
                    prompt=prompt,
                    system_prompt=ctx['persona'],
                    persona_name=ctx.get('persona_name'),
                    provider_id=provider_id,
                ),
                timeout=90,
            )
        except asyncio.TimeoutError:
            logger.error("[内容服务] 话题策略 LLM 调用超时（90秒），跳过本次话题分享")
            return None
        except Exception as e:
            logger.error(f"[内容服务] 话题策略 LLM 调用异常: {e}")
            return None

        if not res:
            logger.warning("[内容服务] 话题策略 LLM 无响应")
            return None

        logger.info(f"[内容服务] 话题策略 LLM 返回文案长度: {len(res)}")

        # 检测兜底标记
        if "[NO_FIT]" in res:
            logger.info("[内容服务] LLM 判定所有候选话题均不合人设，跳过本次话题分享")
            return "[NO_FIT]"

        return res
