# config.py
from enum import Enum

class TimePeriod(Enum):
    """时间段"""
    DAWN = "dawn"          # 凌晨 0-6
    MORNING = "morning"    # 早晨 6-9
    FORENOON = "forenoon"  # 上午 9-12
    AFTERNOON = "afternoon"  # 下午 12-16  
    EVENING = "evening"    # 傍晚 16-19  
    NIGHT = "night"        # 晚上 19-22
    LATE_NIGHT = "late_night" # 深夜 22-24

class SharingType(Enum):
    """分享类型"""
    GREETING = "greeting"        # 问候
    NEWS = "news"               # 新闻见闻
    MOOD = "mood"               # 心情随想
    LIFE_MOMENT = "life_moment"     # 日常碎片
    RANT = "rant"               # 吐槽碎碎念
    DREAM = "dream"             # 梦境分享
    RECOMMENDATION = "recommendation"  # 随机推荐（书籍/电影/音乐/动漫/美食）
    TOPIC = "topic"               # 话题发起（群聊专用，grok搜索+LLM选题）


# 话题策略默认提示词（可通过顶层配置 topic_search_prompt / topic_content_prompt 自定义）
DEFAULT_TOPIC_SEARCH_PROMPT = """今天是 {date}。请搜索过去 48 小时内，中文互联网上正在热议的、普通网民基于常识和生活经验就能发表观点的话题 {candidate_count} 条。

话题要求：
- 知识门槛低：不需要游戏/番剧/数码/学术等专业背景就能聊
- 有观点空间：存在"你怎么看"的争议面，不是单纯事实报道
- 大众领域：社会现象、消费争议、生活方式、职场教育、公共道德、人情世故等

避开：
- 需要专业背景的圈内话题（游戏配队、番剧细节、数码参数、行业黑话）
- 纯灾情/事故/敏感政治
- 纯娱乐八卦（除非有大众讨论价值）

返回 JSON：
{{
  "topics": [
    {{ "name": "话题名", "background": "2-3句话详细说清来龙去脉", "controversy": "大众在争论什么，有哪些不同观点" }}
  ]
}}"""


DEFAULT_TOPIC_CONTENT_PROMPT = """【当前时间】{date_str} {time_str} ({period_label})
你刚刷到几条今天网上正在热议的话题，想挑一条和群聊分享一下你的看法。

【候选话题】
{candidates_formatted}

【选题要求】
1. 选一条你能基于常识和生活经验说出自己看法的
2. 优先选和你人设性格/价值观有共鸣的——这样你的评价会更自然
3. 如果所有候选都需要你不了解的专业领域知识 → 仅输出 [NO_FIT]

【文案要求】
- 用你自己的话简述一下事情经过，让群友知道你在聊什么
  （背景融入你的表达里，不要像播报新闻一样干巴巴陈述事实）
- 然后给出你的评价：一个态度、一点感慨或一个小立场
- 整体一段话，用你的说话方式，真实自然
- 评价本身能让想附和或反驳的人有话可接
- 字数控制：够说清楚事情 + 表达态度即可，不要长篇大论

【严禁】
- 严禁假装是某领域专家，严禁深度分析
- 严禁下绝对定论，留有讨论空间
- 严禁编造话题里没有的细节
- 严禁像新闻稿一样罗列背景事实
- 严禁使用"看大家聊得这么开心"等评价群氛围的话
- 严禁@任何人

直接输出分享文案。"""

# Cron 模板
CRON_TEMPLATES = {
    "morning": "0 8 * * *",       # 早上8点
    "noon": "0 12 * * *",         # 中午12点
    "afternoon": "0 15 * * *",    # 下午3点
    "evening": "0 19 * * *",      # 晚上7点
    "night": "0 22 * * *",        # 晚上10点
    "twice": "0 8,20 * * *",      # 早晚各一次
    "three_times": "0 8,12,20 * * *",  # 早中晚
}

# 新闻源配置
NEWS_SOURCE_MAP = {
    "zhihu": {
        "url": "https://api.nycnm.cn/API/zhihu.php",
        "name": "知乎热搜",
        "icon": "📚"
    },
    "weibo": {
        "url": "https://api.nycnm.cn/API/wb.php",
        "name": "微博热搜",
        "icon": "🔥"
    },
    "quark": {
        "url": "https://api.nycnm.cn/API/quark.php",
        "name": "夸克热搜",
        "icon": "⚛️"
    },
    "bili": {
        "url": "https://api.nycnm.cn/API/bilibilirs.php",
        "name": "B站热搜",
        "icon": "📺"
    },
    "xiaohongshu": {
        "url": "https://api.nycnm.cn/API/xhsrs.php",
        "name": "小红书热搜",
        "icon": "📕"
    },
    "douyin": {
        "url": "https://api.nycnm.cn/API/douyinrs.php",
        "name": "抖音热搜",
        "icon": "🎵"
    },
    "toutiao": {
        "url": "https://api.nycnm.cn/API/toutiao.php",
        "name": "头条热搜",
        "icon": "🗞️"
    },
    "baidu": {
        "url": "https://api.nycnm.cn/API/baidu.php",
        "name": "百度热搜",
        "icon": "🔍"
    },
    "tencent": {
        "url": "https://api.nycnm.cn/API/txxw.php",
        "name": "腾讯热搜",
        "icon": "🐧"
    },
    "36kr": {
        "url": "https://api.nycnm.cn/API/36kr.php",
        "name": "36氪热搜",
        "icon": "📈",
        "extra_params": "&type=comment"
    },
    "51cto": {
        "url": "https://api.nycnm.cn/API/51cto.php",
        "name": "51CTO热搜",
        "icon": "💻"
    },
    "acfun": {
        "url": "https://api.nycnm.cn/API/acfun.php",
        "name": "A站热搜",
        "icon": "📺"
    },
    "ifanr": {
        "url": "https://api.nycnm.cn/API/ifanr.php",
        "name": "爱范儿热搜",
        "icon": "📱"
    },
    "netease": {
        "url": "https://api.nycnm.cn/API/netease.php",
        "name": "网易热搜",
        "icon": "📰"
    },
    "sina": {
        "url": "https://api.nycnm.cn/API/sina.php",
        "name": "新浪热搜",
        "icon": "👀"
    },
    "thepaper": {
        "url": "https://api.nycnm.cn/API/thepaper.php",
        "name": "澎湃热搜",
        "icon": "🌊"
    },
    "yicai": {
        "url": "https://api.nycnm.cn/API/yicai.php",
        "name": "第一财经热搜",
        "icon": "💴"
    }    
}

# 时间段新闻源偏好 (已包含所有 17 个新闻源)
NEWS_TIME_PREFERENCES = {
    # 凌晨
    TimePeriod.DAWN: {
        "netease": 0.20, "weibo": 0.15, "douyin": 0.15, "zhihu": 0.10, "sina": 0.10,
        "quark": 0.05, "thepaper": 0.05, "toutiao": 0.03, "baidu": 0.03, "tencent": 0.02,
        "36kr": 0.02, "51cto": 0.02, "ifanr": 0.02, "yicai": 0.02, "acfun": 0.02, 
        "bili": 0.01, "xiaohongshu": 0.01
    },    
    # 早晨
    TimePeriod.MORNING: {
        "weibo": 0.15, "quark": 0.10, "zhihu": 0.10, "36kr": 0.10, "thepaper": 0.08, 
        "netease": 0.08, "sina": 0.08, "toutiao": 0.05, "baidu": 0.05, "tencent": 0.05, 
        "51cto": 0.05, "yicai": 0.05, "xiaohongshu": 0.02, "ifanr": 0.02, "bili": 0.01, 
        "douyin": 0.01, "acfun": 0.01
    },
    # 上午
    TimePeriod.FORENOON: {
        "douyin": 0.15, "tencent": 0.12, "weibo": 0.10, "quark": 0.10, "yicai": 0.10,
        "toutiao": 0.08, "zhihu": 0.08, "baidu": 0.05, "51cto": 0.05, "thepaper": 0.05, 
        "sina": 0.05, "netease": 0.05, "36kr": 0.02, "ifanr": 0.02, "bili": 0.01, 
        "xiaohongshu": 0.01, "acfun": 0.01
    },    
    # 下午
    TimePeriod.AFTERNOON: {
        "netease": 0.15, "zhihu": 0.15, "quark": 0.10, "weibo": 0.10, "douyin": 0.10, 
        "sina": 0.08, "baidu": 0.05, "toutiao": 0.05, "thepaper": 0.05, "tencent": 0.05, 
        "acfun": 0.05, "36kr": 0.02, "51cto": 0.02, "yicai": 0.02, "ifanr": 0.01, 
        "xiaohongshu": 0.01, "bili": 0.01
    },
    # 傍晚
    TimePeriod.EVENING: {
        "weibo": 0.15, "douyin": 0.15, "quark": 0.10, "thepaper": 0.10, "netease": 0.10,
        "tencent": 0.08, "sina": 0.08, "zhihu": 0.08, "baidu": 0.05, "toutiao": 0.05, 
        "36kr": 0.01, "51cto": 0.01, "ifanr": 0.01, "acfun": 0.01, "xiaohongshu": 0.01, 
        "bili": 0.01, "yicai": 0.01
    },
    # 晚上
    TimePeriod.NIGHT: {
        "douyin": 0.20, "zhihu": 0.15, "weibo": 0.15, "netease": 0.10, "tencent": 0.10, 
        "quark": 0.08, "baidu": 0.05, "sina": 0.05, "yicai": 0.05, "toutiao": 0.02, 
        "36kr": 0.01, "51cto": 0.01, "ifanr": 0.01, "acfun": 0.01, "bili": 0.01, 
        "thepaper": 0.01, "xiaohongshu": 0.01
    },
    # 深夜
    TimePeriod.LATE_NIGHT: {
        "weibo": 0.20, "zhihu": 0.20, "yicai": 0.15, "douyin": 0.15, "sina": 0.10, 
        "netease": 0.05, "quark": 0.05, "baidu": 0.02, "toutiao": 0.02, "tencent": 0.02,
        "36kr": 0.01, "51cto": 0.01, "ifanr": 0.01, "acfun": 0.01, "xiaohongshu": 0.01, 
        "thepaper": 0.01, "bili": 0.01
    },
}

# 分享类型序列 — 群聊/私聊（社交场景，偏话题性和互动性）
SHARING_TYPE_SEQUENCES = {
    TimePeriod.DAWN: [
        SharingType.DREAM.value,
        SharingType.MOOD.value,
    ],
    TimePeriod.MORNING: [
        SharingType.GREETING.value,
        SharingType.LIFE_MOMENT.value,
        SharingType.RANT.value,
    ],
    TimePeriod.FORENOON: [
        SharingType.NEWS.value,
        SharingType.LIFE_MOMENT.value,
        SharingType.RANT.value,
    ],
    TimePeriod.AFTERNOON: [
        SharingType.LIFE_MOMENT.value,
        SharingType.RANT.value,
    ],
    TimePeriod.EVENING: [
        SharingType.MOOD.value,
        SharingType.RANT.value,
        SharingType.LIFE_MOMENT.value,
    ],
    TimePeriod.NIGHT: [
        SharingType.GREETING.value,
        SharingType.LIFE_MOMENT.value,
        SharingType.RANT.value,
        SharingType.MOOD.value,
    ],
    TimePeriod.LATE_NIGHT: [
        SharingType.MOOD.value,
        SharingType.RANT.value,
    ],
}

# QQ空间默认序列（个人日记场景，偏私密感和生活记录）
QZONE_SHARING_TYPE_SEQUENCES = {
    TimePeriod.DAWN: [
        SharingType.DREAM.value,
        SharingType.MOOD.value,
    ],
    TimePeriod.MORNING: [
        SharingType.MOOD.value,
        SharingType.LIFE_MOMENT.value,
        SharingType.RANT.value,
    ],
    TimePeriod.FORENOON: [
        SharingType.LIFE_MOMENT.value,
        SharingType.RANT.value,
    ],
    TimePeriod.AFTERNOON: [
        SharingType.LIFE_MOMENT.value,
        SharingType.RANT.value,
    ],
    TimePeriod.EVENING: [
        SharingType.LIFE_MOMENT.value,
        SharingType.RANT.value,
    ],
    TimePeriod.NIGHT: [
        SharingType.MOOD.value,
        SharingType.RANT.value,
    ],
    TimePeriod.LATE_NIGHT: [
        SharingType.MOOD.value,
        SharingType.RANT.value,
    ],
}

# 默认推荐库细分
DEFAULT_REC_CATS = {
    "书籍": "悬疑推理, 当代文学, 历史传记, 科普新知, 商业思维, 治愈系绘本, 科幻神作, 哲学入门, 古典诗词, 艺术图鉴",
    "电影": "高分冷门, 烧脑科幻, 经典黑白, 是枝裕和风, 赛博朋克, 奥斯卡遗珠, 纪录片, 励志传记, 暴力美学, 黑色幽默",
    "音乐": "新世纪音乐, 治愈系钢琴, 氛围电子, 华语流行, 梦幻流行, 影视原声, 自然白噪音, 爵士蓝调, 摇滚精神, 民谣故事",
    "动漫": "治愈日常, 硬核科幻, 热血运动, 悬疑智斗, 吉卜力风, 奇幻史诗, 冷门佳作, 机甲浪漫, 异世界冒险, 推理侦探",
    "美食": "地方特色小吃, 创意懒人菜, 季节限定, 深夜治愈美食, 传统糕点, 异国风味, 烘焙甜点, 咖啡茶饮, 海鲜料理, 面食文化",
    "游戏": "独立神作, 治愈解谜, 剧情向, 像素风, 肉鸽Like, 模拟经营, 开放世界, 恐怖游戏, 复古怀旧, 派对游戏",
    "剧集": "英美神剧, 悬疑破案, 高分韩剧, 下饭情景剧, 职场爽剧, 历史正剧, 日式律政, 迷你剧, 真人秀, 讽刺喜剧",
    "播客": "怪诞故事, 商业内幕, 历史闲聊, 科技前沿, 情感治愈, 真实罪案, 文化对谈, 读书分享, 英语听力, 助眠ASMR",
    "好物": "桌面美学, 创意文具, 数码配件, 居家神器, 露营装备, 解压玩具, 咖啡器具, 极简收纳, 黑科技, 手工DIY",
    "旅行": "避世古镇, 赛博城市, 海岛度假, 徒步路线, 博物馆, 自驾公路, 露营圣地, 建筑打卡, 云旅游, 特色民宿"
}
