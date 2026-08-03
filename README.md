[![Yousa Ling](https://count.getloli.com/get/@Inoryu7z.DailySharing?theme=yousa-ling)](https://github.com/Inoryu7z/astrbot_plugin_daily_sharing)
# 📅 astrbot_plugin_daily_sharing (定时主动分享所见所闻)

> 🍃 **Inoryu7z 维护的 fork 版本**，在原版基础上接入 [DayFlow](https://github.com/Inoryu7z/astrbot_plugin_dayflow_life_scheduler) / [DayMind](https://github.com/Inoryu7z/astrbot_plugin_daymind) 的生活与心情上下文。
>
> 原版：[siciyuanweilai/astrbot_plugin_daily_sharing](https://github.com/siciyuanweilai/astrbot_plugin_daily_sharing)，感谢原作者四次元未来。

让 Bot 不再只是"被动应答"，而是会按时间、天气、群聊气氛、心情，自动向群聊 / 私聊 / QQ 空间推送图文、语音、视频分享——像真人一样拥有自己的"生活节奏"。

更新日志：[CHANGELOG.md](https://github.com/Inoryu7z/astrbot_plugin_daily_sharing/blob/main/CHANGELOG.md)

---

## ✨ 核心特性

### 🧠 智能分享调度
跟着穿搭节奏发图，而不是机械地随机发：

- 每天由 LLM 读取 DayFlow 当日日程，识别两套穿搭各自的穿着时段，在每套时段内各选一个最佳分享时间点（偏向前半段，避开忙碌/午休/换装）。
- 解决两个常见痛点：① 某套穿搭连续分享两次、另一套被遗漏；② 分享太晚角色已换睡衣导致穿搭被跳过。
- 多人格场景下会自动协调时间点，避免几个人格在同一时段扎堆发图。
- DayFlow 日程未就绪时会主动触发生成，等待超时或解析失败时自动回退到随机分享。

### 🎭 多人格全面支持
每个人格都是独立的人，而不是共用一套分享逻辑：

- 每个人格独立配置分享目标、LLM、配图、TTS、QQ 空间、新闻源、智能分享等十余项参数，未填字段自动回退到全局默认。
- 思考流、话题历史、定时任务、发送目标、衣橱资产全部按人格隔离，互不干扰。
- 每个人格独立的并发锁，多人格分享任务可同时运行。

### 📰 八种分享类型

分享类型由"心情驱动 + 加权随机"策略自动选择，也支持手动指定：

| 类型 | 说明 |
| :--- | :--- |
| `greeting` | 早安 / 晚安 / 节气问候 |
| `news` | 全网热搜聚合（17 个源，可指定源与图片版） |
| `mood` | 心情随想，基于 DayMind 心情状态生成 |
| `life_moment` | 日常碎片，基于 DayFlow 当前时段日程 + DayMind 心情 |
| `rant` | 吐槽碎碎念，基于 DayMind 负面心情触发 |
| `dream` | 梦境分享，基于 DayMind 梦境历史；无数据时降级为 `mood` |
| `recommendation` | 书籍 / 电影 / 音乐 / 美食等十大类推荐 |
| `topic` | 话题发起（群聊专用），基于 grok 联网搜索热议话题发起讨论 |

> 原版的 `knowledge`(知识) 类型已下线。

### 🛡️ 防幻觉与防打扰

- **Tavily 联网校验**：分享新闻时检索事件最新进展，进行推荐时并发调用"百度百科 + Tavily 搜索"，降低 LLM 胡编乱造。
- **群聊态势感知**：群聊热度过高时自动跳过本次分享，不打断讨论。
- **对话记忆回写**：发送的配图写入对话历史，追问"刚才发的图在哪拍的"可对答如流。
- **第二人称防呆**：私聊场景下修正"第三人称称呼"语病。

### 🎨 视觉与影像

- **形象一致性**：联动 aiimg 插件，通过自拍参考图保持人物形象稳定。
- **图文转视频**：静态配图转 5 秒动态视频；环境光影根据物理时间自动调整。
- **智能视频提示词**：调用多模态 LLM 识图，针对每张图片内容生成专属的"5 秒微小自然动态"提示词，避免千篇一律动效；识图失败自动降级到默认提示词。
- **构图顾问**：内置 Agent 智能判断——分享心情时"画人"，分享新闻时"画景"。
- **自动入衣橱**：与 `astrbot_plugin_wardrobe` 联动，所有自拍图与视频自动入库（带人格名与源配图路径），形成可追溯的视觉资产。

### ⏰ 工业级调度

- 时段序列、Cron 预设（`morning`/`twice`）、自定义 Cron、多时段随机触发。
- 所有"定时随机延迟"写入 SQLite，Bot 重启 / 崩溃后启动时精准恢复未执行任务，杜绝漏发与连发。
- LLM 失败自动降级到系统可用模型，新闻源失败自动切备选池，全网瘫痪时动用 LLM 知识库兜底。

---

## 🧩 推荐插件生态

可独立运行（纯文字模式），但建议安装以下插件以获得完整体验：

| 插件 | 作用 | 缺失影响 |
| :--- | :--- | :--- |
| [astrbot_plugin_dayflow_life_scheduler](https://github.com/Inoryu7z/astrbot_plugin_dayflow_life_scheduler) | 生活轨迹与穿搭日程 | 失去天气/日程感知；智能分享调度无法启用 |
| [astrbot_plugin_daymind](https://github.com/Inoryu7z/astrbot_plugin_daymind) | 心情轨迹与梦境历史 | `life_moment`/`rant`/`dream` 三类退化为无数据支撑的纯 LLM 生成 |
| [astrbot_plugin_qzone_Inoryu7z](https://github.com/Inoryu7z/astrbot_plugin_qzone_Inoryu7z) | QQ 空间基建 | 无法自动发布空间说说 |
| [astrbot_plugin_aiimg](https://github.com/Inoryu7z/astrbot_plugin_aiimg) | 图生图/文生视频 | 无生活配图、无视频；自拍链路与形象一致性失效 |
| [astrbot_plugin_tts_plus](https://github.com/Inoryu7z/astrbot_plugin_tts_plus)（巴巴啵一）| 情感语音合成 | 无语音条；本 fork 已从 `tts_emotion_router` 迁移至此，原生支持多人格语音路由 |
| [astrbot_plugin_wardrobe](https://github.com/Inoryu7z/astrbot_plugin_wardrobe) | 衣橱资产库 | 自拍图与视频不会自动入库 |
| [astrbot_plugin_grok_web_search_Inoryu7z](https://github.com/Inoryu7z/astrbot_plugin_grok_web_search_Inoryu7z) | 联网检索增强 | Tavily 仍可用，部分高级场景效果略减 |

> DayMind 已不是硬依赖，未安装可正常加载；但若想获得 `life_moment`/`rant`/`dream` 三类内容的真实数据支撑，仍是强烈推荐项。

---

## 💬 自然语言交互

直接和 Bot 聊天即可触发分享，无需记忆指令。

| 意图 | 示例 | Bot 动作 |
| :--- | :--- | :--- |
| 发空间说说 | "今天天气不错，**发个空间说说吧**" | 生成第一人称心情独白 + 生活照发布到 QQ 空间 |
| 看动图/视频 | "早上好，给我**发个自拍视频**看看" | 触发画图 + 视频生成，发送动态问候视频 |
| 看独立早报 | "给我来一份 **60s 新闻**" | 获取《60s 读懂世界》长图并发送 |
| 吃瓜看热搜 | "看看**微博热搜**的图片版" | 获取微博实时热搜长图 |
| 听碎碎念 | "你现在心情怎么样？**发条语音**" | 结合虚拟日程生成音频发送 |

---

## 🎮 控制台指令（Admin Only）

| 指令 | 参数 | 功能 |
| :--- | :--- | :--- |
| `/分享` | `[类型]` | 手动触发分享（默认只发给当前窗口） |
| `/分享` | `[类型] 广播` | 向配置的所有群聊和私聊发送 |
| `/分享` | `[类型] 空间` | 单独生成文案并分享到 QQ 空间 |
| `/分享` | `新闻 [源]` | 获取指定源的热搜（文字版） |
| `/分享` | `新闻 [源] 图片` | 获取指定源的热搜长图 |
| `/分享` | `60s` / `ai` | 手动发送【每天 60s 读懂世界】或【AI 资讯快报】 |
| `/分享` | `早报空间 开启/关闭` | 切换是否将定时早报同步到 QQ 空间 |
| `/分享` | `开启` / `关闭` | 启停所有定时自动分享任务 |
| `/分享` | `状态` | 查看运行状态、历史记录、序列索引 |
| `/分享` | `查看序列` | 查看当前时间段会轮换发什么内容 |
| `/分享` | `指定序列 [序号]` | 手动跳到序列中的某一步（支持加后缀 `空间`） |
| `/分享` | `重置序列` | 重置轮换顺序 |

`[类型]` 不区分大小写，可填中文或英文标识（`问候`/`greeting`、`新闻`/`news`、`心情`/`mood`、`日常`/`life_moment`、`吐槽`/`rant`、`梦境`/`dream`、`推荐`/`recommendation`、`自动`/`auto`）。

---

## ⚙️ 进阶配置

在 AstrBot WebUI 的 **插件配置** 面板调整。采用「全局配置 + 人格级覆盖」双层结构，每个人格在 `personas` 列表中独立配置，未填字段自动回退到全局默认值。

<details>
<summary><b>点击展开：核心配置项说明</b></summary>

1. **🎯 千群千面推送**：推送目标支持三段式语法 `群号:独立定时:分享类型`（如 `123456:0 7 * * *:news,mood`）。
2. **📚 内容库自定义**：自定义【推荐】库话题池，格式 `大类: 标签1, 标签2`（如 `游戏: 独立神作, 治愈解谜`）。
3. **📅 早报设置**：独立配置每天 08:00 长图早报，支持一键同步 QQ 空间。
4. **🌟 QQ 空间专属**：独立于群聊/私聊的 Cron 定时器与发布序列；自动屏蔽"大家好"等社交词汇，强制第一人称"日记体"。
5. **🎨 配图与视频**：建议开启配合 aiimg 插件；视频生成仅建议为 `greeting` 和 `mood` 开启；智能视频提示词（`enable_smart_video_prompt`）默认开启，可指定独立识图 LLM（`video_llm_provider_id`）。
6. **🔍 联网检索**：默认开启，结合 Tavily 让热搜点评、推荐更具真实感。
7. **🤖 模型与人设**：可单独指定 LLM 模型，崩溃时自动降级到系统第一个可用模型；每个人格可独立配置 LLM（`persona_llm_conf.llm_provider_id`）与超时。
8. **🧠 智能分享调度（人格级 `persona_smart_share_conf`）**：
   - `enable_smart_share`：是否启用，默认关闭。
   - `smart_share_trigger_time`：每日触发 LLM 分析日程时间，默认 `06:00`，建议设为 DayFlow 日程生成之后。
   - `smart_share_llm_provider_id`：分析日程的专用 LLM，留空用人格默认 LLM。
   - `smart_share_wait_dayflow_timeout`：DayFlow 未就绪时主动触发生成后等待的最长时间（分钟），默认 30。
   - `smart_share_fallback_to_random`：失败时是否回退到随机分享，默认开启。
9. **🎭 多人格配置（`personas` template_list）**：每个人格独立覆盖 11 个配置块，新建人格默认值与全局相同，未填字段自动回退全局。

</details>

---

<div align="center">
  <sub>Made with ❤️ by 四次元未来 · Inoryu7z fork 维护</sub>
</div>
