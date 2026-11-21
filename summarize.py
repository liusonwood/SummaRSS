from typing import Dict, Iterable, List, Optional, Set
import os
import sys
import json
import html
import logging
import xml.etree.ElementTree as ET
from xml.dom import minidom
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
import http.client
import subprocess
import re
import hashlib
import tempfile
import trafilatura
from trafilatura.settings import use_config
import time
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

try:
    from zoneinfo import ZoneInfo
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # 缺少 tzdata 时退回固定偏移（中国没有夏令时，结果一致）
    BEIJING_TZ = timezone(timedelta(hours=8))

logger = logging.getLogger("summarize")

PROCESSED_FILE = "processed.txt"
OUTPUT_FEED = "summary_feed.xml"
REPO_NAME = os.getenv("GITHUB_REPOSITORY", "iusonwood/summarss")  # Actions 自动提供 owner/name
OPENROUTER_API_ENDPOINT = os.getenv("OPENROUTER_API_ENDPOINT", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MAX_ITEMS = 50
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.0-flash-001")
MAX_HISTORY_ITEMS = int(os.getenv("MAX_HISTORY_ITEMS", "25"))  # RSS 输出文件保留的最大条目数
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))  # 并发处理的 RSS 源数量上限
ARTICLE_WORKERS = int(os.getenv("ARTICLE_WORKERS", "6"))  # 单个源内并发抓取文章的线程数
ARTICLE_TIMEOUT = int(os.getenv("ARTICLE_TIMEOUT", "15"))  # 单篇文章抓取超时（秒）

USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

DC_NS = "http://purl.org/dc/elements/1.1/"  # Dublin Core，用于 dc:creator（文章作者）
ATOM_NS = "http://www.w3.org/2005/Atom"

# trafilatura.fetch_url 兜底用的配置：限制下载超时，避免再次卡住
TRAF_CONFIG = use_config()
TRAF_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(ARTICLE_TIMEOUT))

# 线程锁：保护共享的 token 统计
USAGE_LOCK = threading.Lock()

# 单次运行的 token 用量统计（跨所有源累加，结束时统一输出）
RUN_TOKEN_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0, "cost": 0.0, "calls": 0}

# 配置 - 支持多个RSS源，用逗号分隔；去重并保持顺序
RSS_SOURCES = list(dict.fromkeys(
    url.strip()
    for url in os.getenv("RSS_SOURCE", "https://9to5mac.com/feed/").split(",")
    if url.strip()
))


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def normalize_url(url: str) -> str:
    """标准化 URL
    保留 Query 参数，仅剥离末尾斜杠和 fragment (#anchor)
    与 OughtGather 中的 URL 去重逻辑保持一致
    """
    if not url:
        return ""
    url = url.strip()
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        return url.split('#')[0].rstrip('/')
    path = parsed.path.rstrip('/')
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{parsed.netloc}{path}{query}"

def generate_content_id(url: str) -> str:
    """生成内容唯一标识符 (MD5 哈希)，基于标准化后的 URL"""
    content = normalize_url(url)
    return hashlib.md5(content.encode('utf-8')).hexdigest()

class DedupTracker:
    """按内容源维护抓取 URL 快照的去重追踪器。

    机制完整迁移自 OughtGather：
    - 按源独立维护快照，每行格式为: {source_key}\t{content_id}
    - 抓取阶段暂存非空源快照（stage_source_snapshot）
    - 运行结束时原子替换写入文件（save），避免全局历史记录无限膨胀
    """

    def __init__(self, data_file: str = PROCESSED_FILE):
        self.data_file = data_file
        self.source_ids: Dict[str, Set[str]] = {}
        self._pending_snapshots: Dict[str, Set[str]] = {}
        self._pending_snapshot_stats: Dict[str, dict] = {}
        self.fetched_ids: Set[str] = set()
        self.new_ids: Set[str] = set()
        self._session_new_ids: Set[str] = set()
        self._session_snapshot_added = 0
        self._session_snapshot_removed = 0
        self.legacy_ids: Set[str] = set()
        self._lock = threading.Lock()
        self._load()

    @staticmethod
    def make_source_key(source) -> str:
        if isinstance(source, str):
            return source if source.startswith("rss:") else f"rss:{source}"
        return f"{getattr(source, 'type', 'rss')}:{getattr(source, 'src', str(source))}"

    def _refresh_fetched_ids(self):
        self.fetched_ids = set().union(*self.source_ids.values()) if self.source_ids else set()

    def _load(self):
        if not os.path.exists(self.data_file):
            logger.info("[DedupTracker] 未找到去重文件: %s", self.data_file)
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line.strip():
                        continue
                    if "\t" in line:
                        source_key, content_id = line.split("\t", 1)
                        if source_key and content_id:
                            self.source_ids.setdefault(source_key, set()).add(content_id)
                    else:
                        # 平滑兼容旧版纯 URL 记录，计算 hash 并放入 legacy_ids，防止初次迁移时全量重抓
                        legacy_url = normalize_url(line)
                        if legacy_url:
                            self.legacy_ids.add(generate_content_id(legacy_url))
            with self._lock:
                self._refresh_fetched_ids()
            legacy_count = len(self.legacy_ids)
            legacy_msg = f" (含 {legacy_count} 条旧版历史记录)" if legacy_count else ""
            logger.info(
                "[DedupTracker] 去重记录加载完成: 运行开始时=%d 条快照%s",
                sum(len(ids) for ids in self.source_ids.values()), legacy_msg,
            )
        except Exception as e:
            logger.error("[DedupTracker] 去重记录加载失败: %s", e)

    def is_fetched(self, url: str, source_key: str) -> bool:
        if not url:
            return False
        content_id = generate_content_id(url)
        with self._lock:
            return (
                content_id in self.source_ids.get(source_key, set())
                or content_id in self.legacy_ids
            )

    def mark_as_fetched(self, url: str, source_key: str):
        if not url:
            return
        content_id = generate_content_id(url)
        with self._lock:
            ids = self.source_ids.setdefault(source_key, set())
            if content_id not in ids:
                ids.add(content_id)
                self.fetched_ids.add(content_id)
                self.new_ids.add(content_id)
                self._session_new_ids.add(content_id)

    def stage_source_snapshot(self, source_key: str, urls: Iterable[str]):
        """暂存非空源快照，实际替换在 save() 时完成。
        空列表或 None 不会覆盖旧快照。
        """
        ids = {generate_content_id(url) for url in urls if url}
        if ids:
            with self._lock:
                previous = self.source_ids.get(source_key, set())
                self._pending_snapshots[source_key] = ids
                self._pending_snapshot_stats[source_key] = {
                    "previous": len(previous),
                    "current": len(ids),
                    "added": len(ids - previous),
                    "removed": len(previous - ids),
                    "retained": len(previous & ids),
                }
                stats = self._pending_snapshot_stats[source_key]
                self._session_snapshot_added += stats["added"]
                self._session_snapshot_removed += stats["removed"]
                logger.info(
                    "[%s] 快照变化: 上次=%d 条, 本次=%d 条, 新增=%d 条, 移除=%d 条, 保留=%d 条",
                    source_key, stats["previous"], stats["current"],
                    stats["added"], stats["removed"], stats["retained"],
                )

    def save(self):
        """原子保存快照到文件，完全替换旧文件。"""
        with self._lock:
            if not self._pending_snapshots and not self.new_ids:
                logger.info("[DedupTracker] 没有新快照或记录需要保存")
                return
            for source_key, ids in self._pending_snapshots.items():
                self._session_new_ids.update(ids - self.source_ids.get(source_key, set()))
                self.source_ids[source_key] = set(ids)
            self._pending_snapshots.clear()
            self._pending_snapshot_stats.clear()
            self._refresh_fetched_ids()
            snapshot = {key: set(ids) for key, ids in self.source_ids.items() if ids}

        try:
            directory = os.path.dirname(os.path.abspath(self.data_file)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix=".processed.", dir=directory, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for source_key in sorted(snapshot.keys()):
                        for content_id in sorted(snapshot[source_key]):
                            f.write(f"{source_key}\t{content_id}\n")
                os.replace(temp_path, self.data_file)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            with self._lock:
                self.new_ids.clear()
                self.legacy_ids.clear()
            stats = self.get_stats()
            logger.info(
                "[DedupTracker] 去重数据库保存完成: 新增=%d 条, 移除=%d 条, 当前=%d 条",
                stats["snapshot_added"], stats["snapshot_removed"], stats["total_fetched"],
            )
        except Exception as e:
            logger.error("[DedupTracker] 去重数据库保存失败: %s", e)

    def clear(self):
        with self._lock:
            self.source_ids.clear()
            self._pending_snapshots.clear()
            self._pending_snapshot_stats.clear()
            self.fetched_ids.clear()
            self.new_ids.clear()
            self._session_new_ids.clear()
            self._session_snapshot_added = 0
            self._session_snapshot_removed = 0
            self.legacy_ids.clear()
            if os.path.exists(self.data_file):
                try:
                    with open(self.data_file, "w", encoding="utf-8") as f:
                        f.write("")
                    logger.info("[DedupTracker] 已清空去重文件: %s", self.data_file)
                except Exception as e:
                    logger.error("[DedupTracker] 清空去重文件失败: %s", e)

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "total_fetched": sum(len(ids) for ids in self.source_ids.values()),
                "new_fetched": len(self._session_new_ids),
                "snapshot_added": self._session_snapshot_added,
                "snapshot_removed": self._session_snapshot_removed,
            }

def clean_html(raw_html):
    """清理 HTML 标签并还原实体 (兜底时使用)"""
    if not raw_html:
        return ""
    return html.unescape(re.sub(r'<.*?>', '', raw_html)).strip()

# 常见的二级公共后缀（bbc.co.uk、foo.com.cn 之类），避免把 "co" 当成站点名
_SECOND_LEVEL_SUFFIXES = {"co", "com", "org", "net", "gov", "edu", "ac"}

def _clean_hostname(url):
    hostname = (urllib.parse.urlparse(url).hostname or url).lower()
    return hostname[4:] if hostname.startswith("www.") else hostname

def source_name(url):
    """从URL提取可读的来源名称（取可注册域名主体，如 blog.example.com -> Example）"""
    parts = _clean_hostname(url).split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL_SUFFIXES:
        name = parts[-3]
    elif len(parts) >= 2:
        name = parts[-2]
    else:
        name = parts[0]
    return name.title()

def build_source_labels(sources: Iterable[str]) -> Dict[str, str]:
    """为每个源生成互不相同的标签：重名时依次退化为完整主机名、主机名+路径。"""
    sources = list(sources)
    labels = {s: source_name(s) for s in sources}

    def resolve(current, fallback):
        counts = Counter(current.values())
        return {s: (fallback(s) if counts[label] > 1 else label) for s, label in current.items()}

    labels = resolve(labels, _clean_hostname)
    labels = resolve(
        labels,
        lambda s: _clean_hostname(s) + urllib.parse.urlparse(s).path.rstrip("/"),
    )
    return labels

def fetch_article_html(url):
    """带超时地下载文章 HTML（返回 bytes，交给 trafilatura 自行判断编码）"""
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=ARTICLE_TIMEOUT) as response:
        return response.read()


# ---------- Feed 解析（RSS 2.0 / RSS 1.0 / Atom） ----------

def _local(tag) -> str:
    """去掉命名空间后的标签名；注释等非字符串 tag 返回空串"""
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ""

def _children(elem, name):
    return [c for c in elem if _local(c.tag) == name]

def _child_text(elem, name) -> str:
    for c in _children(elem, name):
        text = "".join(c.itertext()).strip()  # itertext 兼容 Atom 的 xhtml 内容
        if text:
            return text
    return ""

def _entry_link(entry) -> str:
    for c in _children(entry, "link"):
        rel = c.get("rel")
        href = (c.get("href") or c.text or "").strip()
        if href and rel in (None, "alternate"):
            return href
    return _child_text(entry, "guid") or _child_text(entry, "id")

def parse_feed_entries(root) -> List[dict]:
    """把 RSS item / Atom entry 统一解析为 {title, link, fallback} 列表"""
    entries = []
    for elem in root.iter():
        if _local(elem.tag) not in ("item", "entry"):
            continue
        fallback = (
            _child_text(elem, "encoded")      # content:encoded
            or _child_text(elem, "content")   # Atom
            or _child_text(elem, "description")
            or _child_text(elem, "summary")   # Atom
        )
        entries.append({
            "title": _child_text(elem, "title") or "No Title",
            "link": _entry_link(elem),
            "fallback": clean_html(fallback),
        })
    return entries

def fetch_article_body(link, fallback, title, source_label):
    """抓取单篇文章正文；失败时依次回退 trafilatura.fetch_url、RSS 自带摘要"""
    logger.info("[%s] 发现新文章，正在抓取全文: %s", source_label, title)
    body = ""
    downloaded = None
    try:
        downloaded = fetch_article_html(link)
    except Exception as e:
        logger.warning("[%s] urllib 抓取失败: %s，改用 trafilatura.fetch_url", source_label, e)
        try:
            downloaded = trafilatura.fetch_url(link, config=TRAF_CONFIG)
        except Exception as e2:
            logger.warning("[%s] trafilatura 抓取也失败: %s", source_label, e2)

    if downloaded:
        try:
            body = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        except Exception as e:
            logger.warning("[%s] 全文提取失败: %s", source_label, e)

    if not body or len(body) < 10:
        logger.warning("[%s] 内容过少，使用原生摘要兜底: %s", source_label, title)
        body = fallback
    return body

def fetch_rss_items(source, tracker: DedupTracker, label: Optional[str] = None):
    """抓取 feed：收集候选、暂存源快照，过滤出未抓取的新文章后并发提取正文"""
    try:
        source_key = DedupTracker.make_source_key(source)
        source_label = label or source_name(source)
        logger.info("[%s] 正在读取 RSS 源: %s", source_label, source)
        req = urllib.request.Request(source, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read()

        root = ET.fromstring(content)
        raw_candidates = []
        seen_in_batch = set()
        for entry in parse_feed_entries(root):
            norm_link = normalize_url(entry["link"])

            # 过滤空链接与批次内重复条目
            if not norm_link or norm_link in seen_in_batch:
                continue
            seen_in_batch.add(norm_link)
            raw_candidates.append({**entry, "link": norm_link})

        if not raw_candidates:
            logger.info("[%s] 源返回空条目列表，不更新去重快照", source_label)
            return []

        # 暂存当前源的完整候选 URL 快照（遵循 OughtGather 快照机制：成功返回非空列表即暂存）
        tracker.stage_source_snapshot(source_key, [c["link"] for c in raw_candidates])

        # 使用上一轮快照过滤出未读新文章
        new_candidates = [
            c for c in raw_candidates
            if not tracker.is_fetched(c["link"], source_key)
        ]

        skipped_count = len(raw_candidates) - len(new_candidates)
        logger.info(
            "[%s] 候选共 %d 篇，跳过已读 %d 篇，待抓取 %d 篇",
            source_label, len(raw_candidates), skipped_count, len(new_candidates),
        )

        if not new_candidates:
            return []

        # 限制单次抓取数量
        to_fetch = new_candidates[:MAX_ITEMS]

        # 文章之间互相独立，并发抓取；ex.map 保持与 feed 一致的顺序
        workers = max(1, min(ARTICLE_WORKERS, len(to_fetch)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            bodies = list(ex.map(
                lambda c: fetch_article_body(c["link"], c["fallback"], c["title"], source_label),
                to_fetch,
            ))

        return [
            {"title": c["title"], "link": c["link"], "body": body, "source": source}
            for c, body in zip(to_fetch, bodies)
        ]
    except Exception as e:
        logger.error("Error fetching RSS: %s", e)
        return []

def log_token_usage(source_label, usage):
    """打印单次 AI 调用的 token 用量日志，并累加到本次运行的总计（加锁）"""
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)  # 已包含思考 token
    reasoning = usage.get("reasoning_tokens", 0)
    answer = max(completion - reasoning, 0)  # 实际正文输出

    with USAGE_LOCK:
        RUN_TOKEN_USAGE["prompt_tokens"] += prompt
        RUN_TOKEN_USAGE["completion_tokens"] += completion
        RUN_TOKEN_USAGE["reasoning_tokens"] += reasoning
        RUN_TOKEN_USAGE["total_tokens"] += usage.get("total_tokens", 0)
        RUN_TOKEN_USAGE["cost"] += usage.get("cost", 0.0)
        RUN_TOKEN_USAGE["calls"] += 1

    cost = usage.get("cost")
    cost_str = f" | 费用: ${cost:.6f}" if cost else ""
    logger.info(
        "[Token 用量] %s | 模型: %s | 输入: %d | 思考: %d | 正文: %d | 总计: %d%s",
        source_label or 'default', AI_MODEL, prompt, reasoning, answer,
        usage.get('total_tokens', 0), cost_str,
    )

def print_token_usage_summary():
    """输出本次运行所有 AI 调用的 token 用量总计"""
    if RUN_TOKEN_USAGE["calls"] == 0:
        return
    cost = RUN_TOKEN_USAGE["cost"]
    cost_str = f" | 总费用: ${cost:.6f}" if cost else ""
    reasoning = RUN_TOKEN_USAGE["reasoning_tokens"]
    answer = max(RUN_TOKEN_USAGE["completion_tokens"] - reasoning, 0)
    logger.info(
        "[Token 用量总计] 调用 %d 次 | 输入: %d | 思考: %d | 正文: %d | 总计: %d%s",
        RUN_TOKEN_USAGE['calls'], RUN_TOKEN_USAGE['prompt_tokens'], reasoning, answer,
        RUN_TOKEN_USAGE['total_tokens'], cost_str,
    )


class EmptyResponseError(Exception):
    """流式响应结束但没有任何正文，可重试"""

def is_retryable_error(e: Exception) -> bool:
    """只重试可能自行恢复的错误：429 / 408 / 5xx、网络错误、超时、流中断、空响应。
    401、400、404 等客户端错误重试也不会好转，直接放弃。"""
    if isinstance(e, urllib.error.HTTPError):  # 必须先于 URLError / OSError 判断
        return e.code in (408, 425, 429) or e.code >= 500
    return isinstance(e, (EmptyResponseError, OSError, http.client.HTTPException))

def retry_delay(e: Exception, attempt: int) -> int:
    """默认线性退避；429 带 Retry-After（秒）时尊重它，上限 60 秒"""
    delay = (attempt + 1) * 10
    if isinstance(e, urllib.error.HTTPError) and e.headers is not None:
        retry_after = e.headers.get("Retry-After")
        if retry_after and retry_after.strip().isdigit():
            delay = max(delay, min(int(retry_after), 60))
    return delay

def get_ai_summary(items, source_label=None):
    """调用 AI 生成摘要，使用流式请求避免长耗时被超时截断，带重试机制"""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not set!")

    token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0, "cost": 0.0}

    # 构建来源上下文
    source_context = ""
    if source_label:
        source_context = f"以下文章均来自 **{source_label}** 这个 RSS 源，请不要在简报中标注来源。\n\n"

    prompt = (
        "你是一位资深科技新闻编辑，正在为专业读者撰写每日简报。\n\n"
        f"{source_context}"
        "### 要求：\n"
        "阅读下面的文章，提炼出真正有信息量的内容，输出一份结构清晰的中文简报。\n\n"
        "1. **分类汇总**：按主题对内容进行分类，并按重要程度排序。\n\n"
        "2. **内容标准**:\n"
        "- 每条摘要保留事实。例如：发生了什么、谁做的、关键数据/时间点、影响是什么。\n"
        "- 删除以下内容：营销话术、与主题无关的背景铺垫、重复表述。\n"
        "- 如果多篇文章报道同一事件，合并为一条，不要重复罗列。\n"
        "- 如果某篇文章内容空洞或纯属八卦猜测，可以简化为一句话甚至跳过，不必强行展开。\n"
        "- 涉及数字、价格、日期等具体信息时必须准确引用原文，不得编造或模糊化。\n\n"
        "3. **格式规范**：\n"
        "- 使用 Markdown 格式：分类标题加粗，如 **[分类名称]**。\n"
        "- 条目使用 `- ` 开头的无序列表。\n"
        "- 核心关键词或结论使用 **双星号加粗**。\n"
        "- 禁止：开场白、总结句、结束语、Emoji、多余的标点符号（如波浪号、感叹号连用）。\n"
        "- 直接输出正文，不要任何前后缀说明。\n"
        "4. **语言要求**：简洁地道的中文。\n\n"
        "### 示例输出格式\n"
        "**[分类名称]**\n"
        "- 具体事实描述，包含关键数据和核心结论。\n"
        "- 另一条事实，**加粗关键词**。\n\n"
        "**[另一分类]**\n"
        "- 事实描述。\n\n"
        "### 待处理文章列表：\n\n"
    )

    for idx, item in enumerate(items, 1):
        prompt += f"文章 {idx}: {item['title']}\n内容: {item['body'][:4000]}\n---\n"

    data = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,  # 开启流式响应，避免总耗时超过单次超时限制
        "stream_options": {"include_usage": True},  # 让流式响应在最后一个 chunk 附带 token 用量
    }

    # 每个数据块之间最长等待秒数（不是总耗时上限）
    CHUNK_TIMEOUT = 60
    max_retries = 3

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                OPENROUTER_API_ENDPOINT,
                data=json.dumps(data).encode('utf-8'),
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                    "HTTP-Referer": f"https://github.com/{REPO_NAME}",
                    "X-Title": "RSS AI Summary Agent"
                }
            )

            content_parts = []
            with urllib.request.urlopen(req, timeout=CHUNK_TIMEOUT) as response:
                for raw_line in response:
                    line = raw_line.decode('utf-8', errors='ignore').strip()
                    if not line or not line.startswith("data:"):
                        continue

                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break

                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        # 有些实现会把多个 JSON 对象粘连在一行，忽略解析失败的行
                        continue

                    # 用量 chunk 可能没有 choices，必须在 choices 判断之前捕获
                    usage = chunk.get("usage")
                    if usage:
                        details = usage.get("completion_tokens_details") or {}
                        token_usage.update({
                            "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
                            "completion_tokens": usage.get("completion_tokens", 0) or 0,
                            "reasoning_tokens": details.get("reasoning_tokens", 0) or 0,
                            "total_tokens": usage.get("total_tokens", 0) or 0,
                            "cost": usage.get("cost") or 0.0,
                        })

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {}) or {}
                    piece = delta.get("content")
                    if piece:
                        content_parts.append(piece)

            full_content = "".join(content_parts).strip()
            if full_content:
                log_token_usage(source_label, token_usage)
                return full_content

            raise EmptyResponseError("Streamed response was empty")

        except Exception as e:
            # HTTP 错误时读取响应体，OpenRouter 会在 body 里说明 400 的具体原因
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass
            logger.warning("Error calling AI API (Attempt %d/%d): %s %s", attempt + 1, max_retries, e, detail)

            if not is_retryable_error(e):
                logger.error("Non-retryable error, giving up without further attempts.")
                return "摘要生成失败：API 返回了不可重试的错误。"

            if attempt < max_retries - 1:
                wait_time = retry_delay(e, attempt)
                logger.info("Retrying in %d seconds...", wait_time)
                time.sleep(wait_time)
            else:
                return "摘要生成失败：API 调用多次重试均告失败。"

    return "摘要生成失败。"

# 除 \t \n \r 外的控制字符不能出现在 XML 1.0 中，会让 minidom 解析失败
_XML_INVALID_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

def markdown_to_html(text: str) -> str:
    """把 AI 输出的简单 Markdown（加粗、无序列表、标题）转成安全的 HTML。
    先转义再套标签，模型输出里的 <script> 之类内容不会被当作标签。"""
    def inline(s: str) -> str:
        s = html.escape(s, quote=False)
        return re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)

    blocks: List[str] = []
    in_list = False
    for raw in _XML_INVALID_CHARS.sub('', text or '').splitlines():
        line = raw.strip()
        item = re.match(r'^[-*+]\s+(.*)', line)  # "**x**" 的 * 后面不是空白，不会误判
        if item:
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{inline(item.group(1))}</li>")
            continue
        if in_list:
            blocks.append("</ul>")
            in_list = False
        if not line:
            continue
        heading = re.match(r'^#+\s*(.*)', line)
        if heading:
            blocks.append(f"<p><strong>{inline(heading.group(1))}</strong></p>")
        else:
            blocks.append(f"<p>{inline(line)}</p>")
    if in_list:
        blocks.append("</ul>")
    return "".join(blocks)

def _new_feed():
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "AI RSS 简报"
    ET.SubElement(channel, "link").text = f"https://github.com/{REPO_NAME}"
    ET.SubElement(channel, "description").text = "由 AI 自动生成的文章全文摘要"
    return rss, channel

def generate_rss_xml(summaries):
    """生成或更新 RSS XML 文件
    summaries: list of (source_label, summary_text) tuples
    每个源生成一个独立的RSS item
    """

    # 注册 Atom 命名空间
    ET.register_namespace('atom', ATOM_NS)
    ET.register_namespace('dc', DC_NS)

    now_utc = datetime.now(timezone.utc)
    now_beijing = now_utc.astimezone(BEIJING_TZ)
    # 使用 email.utils.format_datetime 生成符合 RFC 2822 的标准日期
    # 不受系统 locale 影响，始终输出英文星期/月份缩写，RSS 验证器可识别
    rfc822_date = format_datetime(now_utc)
    timestamp = now_utc.strftime('%Y%m%d%H%M%S')

    # 加载现有 RSS 或创建新 RSS
    if os.path.exists(OUTPUT_FEED):
        try:
            tree = ET.parse(OUTPUT_FEED)
            rss = tree.getroot()
            channel = rss.find("channel")
            if channel is None:
                raise ValueError("Invalid RSS: Missing channel")
        except (ET.ParseError, ValueError):
            logger.warning("Corrupt or invalid RSS file. Creating new.")
            rss, channel = _new_feed()
    else:
        rss, channel = _new_feed()

    # 处理 atom:link (解决验证报错)
    atom_link_url = f"https://raw.githubusercontent.com/{REPO_NAME}/main/summary_feed.xml"

    atom_link = None
    for child in channel.findall(f"{{{ATOM_NS}}}link"):
        if child.get("rel") == "self":
            atom_link = child
            break

    if atom_link is None:
        atom_link = ET.SubElement(channel, f"{{{ATOM_NS}}}link")
        atom_link.set("rel", "self")
        atom_link.set("type", "application/rss+xml")

    atom_link.set("href", atom_link_url)

    # 更新 lastBuildDate
    last_build_date = channel.find("lastBuildDate")
    if last_build_date is None:
        last_build_date = ET.SubElement(channel, "lastBuildDate")
    last_build_date.text = rfc822_date

    # 先构造所有新条目，并移除已存在的相同 GUID 条目
    new_items = []
    for label, summary_text in summaries:
        # 每个源独立的唯一标识符
        # 使用 query 而非 fragment：多数去重逻辑（如 OughtGather normalize_url）会剥离 #
        # 导致所有条目归一成同一 URL；?item= 会被保留，保证每条 hash 唯一
        item_guid = f"ai-summary-{label}-{timestamp}"
        item_link = f"https://github.com/{REPO_NAME}?item={urllib.parse.quote(label, safe='')}-{timestamp}"

        for existing in channel.findall("item"):
            guid = existing.find("guid")
            if guid is not None and guid.text == item_guid:
                channel.remove(existing)
                break

        item = ET.Element("item")
        ET.SubElement(item, "title").text = f"AI 简报 - {label} - {now_beijing.strftime('%Y-%m-%d')}"
        ET.SubElement(item, "link").text = item_link
        ET.SubElement(item, "description").text = f"<div>{markdown_to_html(summary_text)}</div>"
        ET.SubElement(item, "guid", isPermaLink="false").text = item_guid
        ET.SubElement(item, "pubDate").text = rfc822_date
        ET.SubElement(item, f"{{{DC_NS}}}creator").text = AI_MODEL  # 作者 = 当前配置的完整模型名称
        new_items.append(item)

    # 在第一个旧 item 之前（没有旧 item 时在末尾）按 summaries 顺序依次插入
    insert_at = len(channel)
    for i, child in enumerate(channel):
        if child.tag == 'item':
            insert_at = i
            break
    for offset, item in enumerate(new_items):
        channel.insert(insert_at + offset, item)

    # 限制历史条目数量
    items = channel.findall("item")
    if len(items) > MAX_HISTORY_ITEMS:
        logger.info("Trimming %s: %d -> %d items", OUTPUT_FEED, len(items), MAX_HISTORY_ITEMS)
        for old_item in items[MAX_HISTORY_ITEMS:]:
            channel.remove(old_item)

    # 使用 minidom 格式化 XML
    xml_str = minidom.parseString(ET.tostring(rss)).toprettyxml(indent="  ")
    # 去除 minidom 产生的多余空行
    xml_str = "\n".join([line for line in xml_str.split('\n') if line.strip()])

    with open(OUTPUT_FEED, "w", encoding="utf-8") as f:
        f.write(xml_str)

    logger.info("Successfully generated %s with %d summaries", OUTPUT_FEED, len(summaries))

def _git(*args) -> subprocess.CompletedProcess:
    """运行 git 命令并返回结果，不自动抛异常（由调用方按需检查 returncode）"""
    return subprocess.run(["git", *args], capture_output=True, text=True)

def _git_checked(*args) -> subprocess.CompletedProcess:
    result = _git(*args)
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} 失败 (exit {result.returncode}): {output}")
    return result

def git_commit_push():
    """推送更改到 GitHub (仅提交输出的 RSS feed，去重快照保存在 Actions Cache 中)
    没有变更时直接跳过；任何真正的 git 失败都会抛出 RuntimeError，让 Actions 任务标红。"""
    if os.getenv("GITHUB_ACTIONS") != "true":
        logger.info("Not running in GitHub Actions. Skipping git push.")
        return

    now_beijing = datetime.now(timezone.utc).astimezone(BEIJING_TZ)

    _git_checked("config", "user.name", "github-actions[bot]")
    _git_checked("config", "user.email", "github-actions[bot]@users.noreply.github.com")
    _git_checked("add", OUTPUT_FEED)

    # diff --quiet：0 = 无变更，1 = 有变更，其他 = 出错
    diff = _git("diff", "--cached", "--quiet", "--", OUTPUT_FEED)
    if diff.returncode == 0:
        logger.info("No changes to commit.")
        return
    if diff.returncode != 1:
        raise RuntimeError(f"git diff 失败 (exit {diff.returncode}): {(diff.stderr or '').strip()}")

    _git_checked("commit", "-m", f"Auto-update: {now_beijing.strftime('%Y-%m-%d')} (UTC+8)")
    _git_checked("pull", "--rebase")  # 远端有新提交时先变基，避免 push 被拒
    _git_checked("push")
    logger.info("Pushed %s to remote.", OUTPUT_FEED)

def process_source(source, tracker: DedupTracker, label: Optional[str] = None):
    """处理单个 RSS 源（在线程池中运行）。
    返回 (label, summary) 或 None（无新文章 / 失败）。
    单个源失败不影响其他源。
    """
    label = label or source_name(source)
    try:
        logger.info("%s\nProcessing source: %s\n%s", "=" * 50, label, "=" * 50)

        # 获取并处理文章（内部做候选收集、快照暂存与过滤，未读的才抓取）
        new_items = fetch_rss_items(source, tracker, label)
        logger.info("[%s] Found %d new items.", label, len(new_items))

        if not new_items:
            logger.info("[%s] No new items. Skipping.", label)
            return None

        # 为当前源生成AI摘要
        logger.info("[%s] Generating AI summary...", label)
        summary = get_ai_summary(new_items, source_label=label)
        return (label, summary)

    except Exception as e:
        logger.error("Failed to process %s: %s", label, e)
        return None

def main():
    logger.info("Starting RSS AI Summarizer...")
    tracker = DedupTracker(PROCESSED_FILE)

    if not RSS_SOURCES:
        logger.info("No RSS sources configured. Exiting.")
        return

    labels = build_source_labels(RSS_SOURCES)

    # 并发处理所有 RSS 源；ex.map 保持结果顺序与 RSS_SOURCES 一致
    workers = max(1, min(MAX_WORKERS, len(RSS_SOURCES)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda s: process_source(s, tracker, labels[s]), RSS_SOURCES))

    print_token_usage_summary()

    # (source_label, summary_text) 元组列表
    all_summaries = [r for r in results if r]

    # 保存去重快照数据库（原子写入，按源更新快照，由 Actions Cache 自动缓存）
    tracker.save()

    if not all_summaries:
        logger.info("No new items from any source. Exiting.")
        return

    # 生成包含所有源摘要的RSS XML
    generate_rss_xml(all_summaries)

    logger.info("Summary generated successfully!")
    git_commit_push()

if __name__ == "__main__":
    setup_logging()
    try:
        main()
    except Exception:
        logger.exception("Run failed")
        sys.exit(1)
