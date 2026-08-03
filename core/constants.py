from ..config import SharingType, NEWS_SOURCE_MAP

# 类型中文映射表
TYPE_CN_MAP = {
    "greeting": "问候",
    "news": "新闻",
    "mood": "心情",
    "life_moment": "日常",
    "rant": "吐槽",
    "dream": "梦境",
    "recommendation": "推荐",
    "topic": "话题"
}

# 输入指令映射表
CMD_CN_MAP = {
    "问候": SharingType.GREETING,
    "新闻": SharingType.NEWS,
    "心情": SharingType.MOOD,
    "日常": SharingType.LIFE_MOMENT,
    "吐槽": SharingType.RANT,
    "梦境": SharingType.DREAM,
    "推荐": SharingType.RECOMMENDATION,
    "话题": SharingType.TOPIC
}

# 新闻源中文映射表
SOURCE_CN_MAP = {v['name']: k for k, v in NEWS_SOURCE_MAP.items()}
SOURCE_CN_MAP.update({
    "知乎": "zhihu", 
    "微博": "weibo", 
    "B站": "bili", 
    "小红书": "xiaohongshu", 
    "抖音": "douyin", 
    "头条": "toutiao", 
    "百度": "baidu", 
    "腾讯": "tencent",
    "夸克": "quark",
    "36氪": "36kr",
    "51CTO": "51cto",
    "A站": "acfun",     
    "爱范儿": "ifanr",
    "网易": "netease",
    "新浪": "sina",
    "澎湃": "thepaper",
    "第一财经": "yicai"       
})
