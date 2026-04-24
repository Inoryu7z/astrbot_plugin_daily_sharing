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
from ..config import SharingType, TimePeriod, DEFAULT_REC_CATS, NEWS_SOURCE_MAP

class ContentService:
    def __init__(self, config: Dict, llm_func, context, db_manager, news_service=None):
        """
        初始化内容生成服务
        """
        self.config = config
        self.call_llm = llm_func
        self.context = context 
        self.db = db_manager 
        self.news_service = news_service
        
        self.content_lib_conf = self.config.get("content_library", {})
        raw_rec = self.content_lib_conf.get("rec_cats", DEFAULT_REC_CATS)
        if not raw_rec: raw_rec = DEFAULT_REC_CATS
        self.rec_cats = self._parse_str_list_to_dict(raw_rec)
        
        self.basic_conf = self.config.get("basic_conf", {})
        self.dedup_days = int(self.basic_conf.get("dedup_days_limit", 60))
        
        self.news_conf = self.config.get("news_conf", {})
        self.llm_conf = self.config.get("llm_conf", {})
        self.context_conf = self.config.get("context_conf", {})
        
        self._daymind_plugin = None
        self._daymind_not_found = False
        self._dayflow_plugin = None
        self._dayflow_not_found = False

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
        used_topics = await self.db.get_used_topics(target_id, db_category, days_limit=self.dedup_days)
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

        res = await self.call_llm(prompt=user_prompt, system_prompt=system_prompt, timeout=15)
        if not res: return None
        
        # 清洗结果 (去除标点和多余空格)
        topic = res.strip().split("\n")[0].replace("。", "").replace("《", "").replace("》", "")
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
            persona_id = ""
            if persona_name:
                persona_id = self.plugin.get_persona_config_value(persona_name, "persona_llm_conf", "persona_id", "")
            if not persona_id:
                persona_id = self.llm_conf.get("persona_id", "")

            if persona_name and not persona_id:
                try:
                    persona_mgr = getattr(self.context, "persona_manager", None)
                    if persona_mgr:
                        persona_obj = await persona_mgr.get_persona(persona_name)
                        if persona_obj:
                            info["prompt"] = getattr(persona_obj, "system_prompt", "")
                            info["bot_name"] = getattr(persona_obj, "bot_name", "")
                            info["user_name"] = getattr(persona_obj, "user_name", "")
                            return info
                except Exception:
                    pass

            if persona_id:
                persona = await self.context.persona_manager.get_persona(persona_id)
                if persona:
                    info["prompt"] = getattr(persona, "system_prompt", "")
                    info["bot_name"] = getattr(persona, "bot_name", "")
                    info["user_name"] = getattr(persona, "user_name", "")
                    return info

            personality = await self.context.persona_manager.get_default_persona_v3()
            if personality:
                info["prompt"] = personality.get("prompt", "")
                info["bot_name"] = personality.get("bot_name", "")
                info["user_name"] = personality.get("user_name", "")
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
        allow_detail = self.context_conf.get("group_share_schedule", False)

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

{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{dynamics_prompt}
{context_instruction}
{address_rule}

【重要】关于场景状态：
- 如果提供了生活状态（如天气、忙碌/空闲）：
  - 群聊：可以简单带过状态和活动来让问候更真实。
  - 私聊：请结合你当前具体的状态和活动来让问候更真实。

【开头方式】（自然直接）
- 早安/晚安问候："{'大家' if is_group else ''}早安/晚安 "
- 心情切入："今天心情不错呢"
- 状态切入："刚忙完..." / "今天有点..."
- 天气切入：（仅在天气特殊时使用）

要求：
1. 以你的人设性格说话，真实自然
2. 基于当前真实时间问候
3. 忽略群聊历史，直接开启新问候
{greeting_constraint} 
5. {'简短（80-100字）' if is_group else '可适当长一些（100-120字）'}
6. 直接输出内容，不要解释

请生成{p_label}问候："""

        res = await self.call_llm(prompt=prompt, system_prompt=ctx['persona'])
        if res:
            return f"{res}"
        return None  

    async def _gen_mood(self, period, ctx):
        is_group = ctx['is_group']
        is_qzone = ctx.get('target_id') == 'qzone_broadcast'
        call_name = ctx.get('nickname', '')
        detect_name = ctx.get('detect_name', '')

        # 0. 获取配置
        allow_detail = self.context_conf.get("group_share_schedule", False)
        
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
            resonance_guide = "【QQ空间日记策略】无需顾及听众，无需互动提问，只专注描绘你周遭的光影、细微的动作和个人的思绪沉淀。"
        elif is_group:
            resonance_guide = f"""
【群聊共鸣策略 - 日程中的"治愈微光"】
请拒绝机械的时间报时（如"早上了"、"晚上了"），而是捕捉你当前生活状态中那些微小但能抚慰人心的瞬间。
请根据你的【生活状态】选择对应策略：

1. 若你当前【忙碌/工作/学习/攻坚】：
   - 寻找"缝隙中的安宁"：不要单纯宣泄压力，而是分享你在忙乱中如何自我安抚。
   - 示例：忙得焦头烂额时偷喝的一口冰美式、解决难题后那一秒的长舒一口气、或是告诉大家“虽然很累，但我们在一点点变好”。
   - 治愈目标：给同样在奋斗的群友一种“并肩作战的陪伴感”，让他们觉得焦虑是被接纳的。

2. 若你当前【休闲/摸鱼/饮食/宅家】：
   - 传递"允许暂停的松弛感"：描述感官上的舒适细节，传递慢下来的权利。
   - 示例：窗帘透进来的光影、食物冒出的热气、被窝里安全的包裹感、或者是“就在此刻，世界与我无关”的窃喜。
   - 治愈目标：成为群里的“精神充电站”，让紧绷的人看到你的文字能感到一丝放松。

3. 若你当前【运动/外出/通勤/散步】：
   - 捕捉"世界的生命力"：跳出赶路的焦躁，分享你眼中的风景和生机。
   - 示例：耳机里的BGM和步伐踩点的瞬间、路边顽强开出的小花、晚霞落在建筑上的温柔、甚至是风吹过脸颊的真实触感。
   - 治愈目标：为群聊打开一扇窗，带去一点“户外的氧气”和对生活的热爱。

核心要求：
情绪必须源于你正在做的事，但视角要温柔且有力量。不要说教，而是通过分享你的“小确幸”，治愈屏幕对面的人。
"""
        else:
            resonance_guide = "【私聊策略】像对亲密好友一样，分享一点私人的、细腻的小情绪，或者一个小秘密。"

        dynamics_prompt = ""
        if ctx.get('recent_dynamics'):
            dynamics_prompt = f"\n【你最近发过的动态回顾】\n{ctx['recent_dynamics']}\n【注】请保持人设连贯，可以偶尔自然呼应之前的心情，但绝对不要重复发过的内容"

        target_str = "QQ空间" if is_qzone else ('群聊' if is_group else '私聊')

        prompt = f"""
【当前时间】{ctx['date_str']} {ctx['time_str']} ({ctx['period_label']})
你想和{target_str}分享一下现在的心情或想法。

{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{dynamics_prompt}
{vibe_check}
{address_rule}
{resonance_guide}

【重要：如何结合当下状态】
- 群聊（寻找话题点）：
  不要干巴巴地汇报你在干什么。
  请把你【正在做的事】作为引子，转化为一种社交话题或情绪宣泄。
- 私聊（分享沉浸感）：
  请深入描述你【正在做的事】中的某个具体细节，展现你此时此刻的内心独白。

要求：
1. 以你的人设性格说话，真实自然
2. 分享此刻的感受、想法或小感悟
3. 忽略群聊历史，直接开启新话题
4. 基于当前真实时间感悟
5. 字数：{'80-100字' if is_group else '100-120字'}
6. 直接输出内容

你的随想："""
        
        return await self.call_llm(prompt=prompt, system_prompt=ctx['persona'])

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
            async with aiohttp.ClientSession() as session:
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
        allow_detail = self.context_conf.get("group_share_schedule", False)
        enable_tavily = self.news_conf.get("enable_tavily_search", True)

        news_list, source_key = news_data
        source_config = NEWS_SOURCE_MAP.get(source_key, {"name": "热搜", "icon": "📰"})
        source_name = source_config["name"]
        
        items_limit = self.news_conf.get("news_items_count", 5)
        selected_to_search = news_list[:items_limit]

        # 并发调用内置的 Tavily 搜索来获取新闻真相
        if enable_tavily:
            logger.info(f"[内容服务] 正在为 {source_name} 自动检索新闻背景...")
            tasks = [self._fetch_search_tavily(item.get("title", ""), "news") for item in selected_to_search]
            search_results = await asyncio.gather(*tasks)
        else:
            logger.info(f"[内容服务] Tavily 搜索功能已关闭，跳过检索。")
            search_results = [(item.get("title", ""), "") for item in selected_to_search]
        
        raw_share_count = self.news_conf.get("news_share_count", "1-2")
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

【事实核查指令】
下面提供的新闻列表可能已经由系统预先完成了联网检索，包含了事件的真实细节。
如果新闻下方附带有 `[真实事件细节]`，你**绝对不能只读标题自由脑补**，必须把其中的真相融入到你的文案中！

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

【开头方式】（必须自然提到平台"{source_name}"）
- "忙里偷闲刷了下{source_name}..."
- "刚在{source_name}看到..."
- "休息的时候看了眼{source_name}..."
- "{source_name}今天这个..."
- 其他自然的方式
{'【组织方式】' if share_count > 1 else ''}
{f'''- 可以逐条分享：每条新闻+你的看法
- 也可以串联：找出多条新闻的共同点''' if share_count > 1 else ''}

要求：
1. 以你的人设性格说话，真实自然
2. 选择{share_count}条你最感兴趣的热搜
3. {'对每条' if share_count > 1 else '对这条'}热搜要有自己的真实观点，如果有事实细节，必须结合细节进行锐评，不能像没营养的复读机
4. 观点真诚，避免过度情绪化或标题党式表达
5. {'群聊中简洁有重点' if is_group else '私聊可以详细展开想法，并结合你当下的状态'}
6. 用【】标注热搜标题
7. {'字数：120-150字' if is_group else '字数：150-200字'}
8. 直接输出分享内容

直接输出："""

        res = await self.call_llm(prompt=prompt, system_prompt=ctx['persona'], timeout=60)
        
        if res:
            return f"{res}"
        return None 


    # ==================== DayMind / DayFlow 集成 ====================

    def _find_daymind_plugin(self):
        if self._daymind_not_found:
            return None
        if self._daymind_plugin:
            return self._daymind_plugin
        try:
            for p in self.context.get_all_stars():
                p_name = getattr(p, "name", "")
                if "daymind" in p_name:
                    for attr in ("star_instance", "instance", "star_cls"):
                        candidate = getattr(p, attr, None)
                        if candidate and hasattr(candidate, "scheduler"):
                            self._daymind_plugin = candidate
                            logger.info("[内容服务] 已找到 DayMind 插件")
                            return candidate
            self._daymind_not_found = True
        except Exception as e:
            logger.debug(f"[内容服务] 查找 DayMind 插件失败: {e}")
            self._daymind_not_found = True
        return None

    def _find_dayflow_plugin(self):
        if self._dayflow_not_found:
            return None
        if self._dayflow_plugin:
            return self._dayflow_plugin
        try:
            for p in self.context.get_all_stars():
                p_name = getattr(p, "name", "")
                if "dayflow" in p_name or "life_scheduler" in p_name:
                    for attr in ("star_instance", "instance", "star_cls"):
                        candidate = getattr(p, attr, None)
                        if candidate and hasattr(candidate, "get_life_context"):
                            self._dayflow_plugin = candidate
                            logger.info("[内容服务] 已找到 DayFlow 插件")
                            return candidate
            self._dayflow_not_found = True
        except Exception as e:
            logger.debug(f"[内容服务] 查找 DayFlow 插件失败: {e}")
            self._dayflow_not_found = True
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

{user_info_prompt}
{dynamics_prompt}
{aftereffect_hint}
{address_rule}

【你昨晚的梦境】
{dreams_str}

【核心要求】
这是一条"梦境分享"，不是复述梦的内容，而是用你自己的话把梦的感觉说出来。
就像跟朋友说"我昨晚做了个超奇怪的梦"那样自然。

【内容方向】
- 用自己的话重新描述梦的片段，不要照搬上面的原文
- 可以只提最印象深刻的那个画面或感觉
- 可以加上醒来后的感受（"醒来后还觉得..."）
- 如果做了多个梦，可以挑一个最有趣的说，也可以串联

【严禁】
- 严禁使用"看大家"、"既然"等评价群氛围的话
- 严禁像写日记一样正式
- 严禁编造梦里没有的内容
- 严禁过度解读梦的含义（"这个梦意味着..."）

要求：
1. 以你的人设性格说话，真实自然
2. 基于你的真实梦境来写，但用自己的话重新组织
3. 像随口说出来的感觉，简短随意
4. 字数：60-100字
5. 直接输出内容

你的梦境分享："""

        return await self.call_llm(prompt=prompt, system_prompt=ctx['persona'])

    # ==================== 日常碎片 & 吐槽 ====================

    async def _gen_life_moment(self, period: TimePeriod, ctx: dict):
        is_group = ctx['is_group']
        is_qzone = ctx.get('target_id') == 'qzone_broadcast'
        call_name = ctx.get('nickname', '')
        detect_name = ctx.get('detect_name', '')

        allow_detail = self.context_conf.get("group_share_schedule", False)

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

{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{dynamics_prompt}
{mood_hint}
{activity_hint}
{address_rule}

【核心要求】
这是一条"日常碎片"，不是正式分享，不是科普，不是推荐。
就像你随手拿起手机打了一行字发出去的那种感觉。

【内容方向】（从以下中选择一个最自然的）
- 刚做完/正在做的一件小事（做饭、拆快递、泡咖啡、遛狗...）
- 看到的一个小细节（窗外的云、路边的猫、食物的热气...）
- 一个即兴的小想法（"如果XX就好了"、"突然觉得XX"...）
- 一个小确幸（刚好赶上了、意外的好吃、被夸了一句...）

【严禁】
- 严禁使用"看大家"、"既然"等评价群氛围的话
- 严禁像写日记一样正式，这不是日记
- 严禁使用"脑子里突然蹦出"等描述思维过程的语句
- 严禁编造不在日程中的活动

要求：
1. 以你的人设性格说话，真实自然
2. 必须基于你的【真实日程】和【真实心情】来写
3. 像随手打字一样简短随意
4. 字数：60-90字
5. 直接输出内容

你的日常碎片："""

        return await self.call_llm(prompt=prompt, system_prompt=ctx['persona'])

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

{user_info_prompt}
{ctx['chat_hint']}
{mood_hint}
{activity_hint}
{dynamics_prompt}
{address_rule}

【核心要求】
这是一条"吐槽碎碎念"，语气要轻松、带点自嘲或幽默。
不是真的生气，是那种"唉又来了"的无奈感。

【吐槽方向】（从以下中选择一个最贴合的）
- 工作学习中的小挫折（改不完的bug、写不出的方案...）
- 生活中的小不便（外卖送错、闹钟没响、排队太久...）
- 天气/环境的小抱怨（太热/太冷/太吵...）
- 社交中的小尴尬（说错话、忘记回消息...）

【严禁】
- 严禁真的愤怒或攻击性言论
- 严禁使用"看大家"、"既然"等评价群氛围的话
- 严禁编造不在日程中的场景
- 严禁过度负能量，要有"吐槽完就好了"的轻松感

要求：
1. 以你的人设性格说话，真实自然
2. 必须基于你的【真实日程】和【真实心情】来写
3. 带点幽默或自嘲，不要太严肃
4. 字数：60-90字
5. 直接输出内容

你的吐槽："""

        return await self.call_llm(prompt=prompt, system_prompt=ctx['persona'])

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
        allow_detail = self.context_conf.get("group_share_schedule", False)
        enable_tavily = self.news_conf.get("enable_tavily_search", True)
        
        # 随机选择大类和子类
        rec_type = random.choice(list(self.rec_cats.keys()))
        sub_style = random.choice(self.rec_cats[rec_type])
        
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
                 context_instruction = "- 场景参考：可以提及你当下的活动（如刚看完书、听完歌、吃完饭），作为推荐的引子。"
             else:
                 context_instruction = "- 忽略天气，除非它能极大烘托氛围（如下雨推爵士）。重点关注内容本身。如果状态忙碌，可以说“忙里偷闲推荐个”，状态休闲可以说“打发时间”。"
        else:
             context_instruction = """
- 场景筛选（重要）：
  1. 关于天气：只有当天气能完美烘托作品氛围时才提，否则请完全忽略天气。
  2. 关于状态：请尝试将推荐理由与你【当前正在做的事】联系起来。
     - 刚忙完工作 -> 推荐轻松的剧/音乐来回血
     - 正在深夜网抑云 -> 推荐致郁/治愈电影
     - 正在吃饭 -> 推荐下饭综/美食番/好吃的
     让推荐看起来像是你此刻真实需求的延伸。
  3. 如果联系不上，就直接说“最近在重温/看到了这个”即可，不要强行编造理由，也不要说“突然想到”。
"""

        dynamics_prompt = ""
        if ctx.get('recent_dynamics'):
            dynamics_prompt = f"\n【你最近发过的动态回顾】\n{ctx['recent_dynamics']}\n【注】请保持人设连贯，可以偶尔自然呼应之前的心情，但绝对不要重复发过的内容"

        target_str = "QQ空间" if is_qzone else ('群聊' if is_group else '私聊')

        prompt = f"""
【当前时间】{ctx['date_str']} {ctx['time_str']} ({ctx['period_label']})
你现在的任务是：向{target_str}推荐【{target_work}】。

【核心指令】
1. 必须基于下面的资料进行推荐，不要更换目标。

{baike_context}
{user_info_prompt}
{ctx['life_hint']}
{ctx['chat_hint']}
{dynamics_prompt}

【拒绝神怪/脑补开头】
- 严禁使用“脑子里突然蹦出”、“突然灵光一闪”、“不知怎么的脑海中浮现”等描述思维跳跃的语句。
- 严禁描述你大脑内部的运作过程。
- 必须像个正常人类一样，自然地开启话题。

【严重警告 - 拒绝尴尬开头】
- 严禁使用：“看大家推了那么多”、“看你们都在聊窝被窝”。
- 直接说“最近发现了一个...”或者“推荐一部/一个...”
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
6. {'字数：100-120字' if is_group else '字数：120-150字'}。
7. 直接输出推荐内容。
"""

        res = await self.call_llm(prompt=prompt, system_prompt=ctx['persona'])
        
        if res:
            try:
                matches = re.findall(r"【(.*?)】", res)
                keyword = matches[0] if matches else target_work or res[:10]
                await self.db.record_topic(target_id, "rec", keyword)
            except: pass
            return f"推荐类型: {rec_type} - {sub_style}\n\n{res}"
        return None
