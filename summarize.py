import os
import json
import xml.etree.ElementTree as ET
from xml.dom import minidom
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
import subprocess
import re
import trafilatura
import time

PROCESSED_FILE = "processed.txt"
OUTPUT_FEED = "summary_feed.xml"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MAX_ITEMS = 50
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.0-flash-001")
MAX_HISTORY_ITEMS = int(os.getenv("MAX_HISTORY_ITEMS", "400"))  # RSS 输出文件保留的最大条目数
MAX_PROCESSED_LINKS = int(os.getenv("MAX_PROCESSED_LINKS", "5000"))  # processed.txt 保留的最大链接数

# 配置 - 支持多个RSS源，用逗号分隔
RSS_SOURCES = [
    url.strip()
    for url in os.getenv("RSS_SOURCE", "https://9to5mac.com/feed/").split(",")
    if url.strip()
]


def clean_html(raw_html):
    """清理 HTML 标签 (兜底时使用)"""
    if not raw_html: return ""
    clean_re = re.compile('<.*?>')
    return re.sub(clean_re, '', raw_html).strip()

def load_processed_links():
    """加载已处理过的链接"""
    if not os.path.exists(PROCESSED_FILE):
        return set()
    with open(PROCESSED_FILE, 'r') as f:
        return set(line.strip() for line in f if line.strip())

def update_storage(links):
    """记录已处理的链接"""
    with open(PROCESSED_FILE, 'a') as f:
        for link in links:
            if link:
                f.write(f"{link}\n")

def trim_processed_file():
    """裁剪 processed.txt，只保留最近的 MAX_PROCESSED_LINKS 条记录
    文件按行追加，越靠后的行越新，所以保留最后 N 行
    """
    if not os.path.exists(PROCESSED_FILE):
        return
    with open(PROCESSED_FILE, 'r') as f:
        lines = [line for line in f if line.strip()]
    if len(lines) > MAX_PROCESSED_LINKS:
        kept = lines[-MAX_PROCESSED_LINKS:]
        print(f"Trimming {PROCESSED_FILE}: {len(lines)} -> {len(kept)} links")
        with open(PROCESSED_FILE, 'w') as f:
            f.writelines(kept)

def source_name(url):
    """从URL提取可读的来源名称"""
    hostname = urllib.parse.urlparse(url).hostname or url
    name = hostname.replace("www.", "").split(".")[0]
    return name.title()

def fetch_rss_items(source, processed_links):
    """抓取 RSS：先判断是否已读，只有新文章才抓取全文"""
    try:
        source_label = source_name(source)
        print(f"正在读取 RSS 源: {source_label}")
        req = urllib.request.Request(source, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read()
                
        root = ET.fromstring(content)
        items = []
        for item in root.findall('.//item'):
            title = item.find('title').text if item.find('title') is not None else "No Title"
            link = item.find('link').text if item.find('link') is not None else ""
            
            if not link:
                guid = item.find('guid')
                if guid is not None: link = guid.text
            
            link = link.strip()

            # --- 先做判断，跳过已读文章，极大提升速度 ---
            if link in processed_links:
                continue
                
            print(f"发现新文章，正在抓取全文: {title}")
            body = ""
            try:
                downloaded = trafilatura.fetch_url(link)
                if downloaded:
                    body = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            except Exception as e:
                print(f"  [!] 全文提取失败: {e}")

            if not body or len(body) < 100:
                print("  [!] 内容过少，使用原生摘要兜底")
                content_encoded = item.find('{http://purl.org/rss/1.0/modules/content/}encoded')
                description = item.find('description').text if item.find('description') is not None else ""
                fallback = content_encoded.text if content_encoded is not None else description
                body = clean_html(fallback)
            
            items.append({"title": title, "link": link, "body": body, "source": source})
            
            # 达到单次最大处理量提前停止抓取
            if len(items) >= MAX_ITEMS:
                break
                
        return items
    except Exception as e:
        print(f"Error fetching RSS: {e}")
        return []

def get_ai_summary(items, source_label=None):
    """调用 AI 生成摘要，带重试机制"""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not set!")

    # 构建来源上下文
    source_context = ""
    if source_label:
        source_context = f"以下文章均来自 **{source_label}** 这个 RSS 源，请在不要简报中标注来源。\n\n"

    prompt = (
        "你是一位专业的科技新闻编辑。请根据提供的 RSS 文章内容，整理出一份精炼、客观的中文简报。\n\n"
        f"{source_context}"
        "### 要求：\n"
        "1. **分类汇总**：按主题对内容进行分类。如果主题跨度不大，则按重要程度排序。\n"
        "2. **内容质量**：每条摘要应直击核心事实，剔除营销废话，保持中立专业的语气。\n"
        "3. **格式规范**：\n"
        "   - 使用 Markdown 格式：分类标题加粗，如 **[分类名称]**。\n"
        "   - 条目使用 `- ` 开头的无序列表。\n"
        "   - 核心关键词或结论使用 **双星号加粗**。\n"
        "   - **严禁** 开场白、问候语 or 结束语（直接输出正文内容）。\n"
        "   - **禁止** 使用 Emoji 表情或任何非标准特殊符号。\n"
        "4. **语言要求**：简洁地道的中文。\n\n"
        "### 待处理文章列表：\n\n"
    )
    
    for idx, item in enumerate(items, 1):
        prompt += f"文章 {idx}: {item['title']}\n内容: {item['body'][:4000]}\n---\n"
        
    data = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}]
    }
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps(data).encode('utf-8'),
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/liusonwood/summarss",
                    "X-Title": "RSS AI Summary Agent"
                }
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                return res_data['choices'][0]['message']['content']
        except Exception as e:
            wait_time = (attempt + 1) * 10
            print(f"Error calling AI API (Attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                return "摘要生成失败：API 调用多次重试均告失败。"
    
    return "摘要生成失败。"

def generate_rss_xml(summaries):
    """生成或更新 RSS XML 文件
    summaries: list of (source_label, summary_text) tuples
    每个源生成一个独立的RSS item
    """

    # 注册 Atom 命名空间
    ET.register_namespace('atom', "http://www.w3.org/2005/Atom")

    now_utc = datetime.now(timezone.utc)
    now_beijing = now_utc + timedelta(hours=8)
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
            print("Warning: Corrupt or invalid RSS file. Creating new.")
            rss = ET.Element("rss", version="2.0")
            channel = ET.SubElement(rss, "channel")
            ET.SubElement(channel, "title").text = "AI RSS 简报"
            ET.SubElement(channel, "link").text = "https://github.com/liusonwood/summarss"
            ET.SubElement(channel, "description").text = "由 AI 自动生成的文章全文摘要"
    else:
        rss = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = "AI RSS 简报"
        ET.SubElement(channel, "link").text = "https://github.com/liusonwood/summarss"
        ET.SubElement(channel, "description").text = "由 AI 自动生成的文章全文摘要"

    # 处理 atom:link (解决验证报错)
    atom_ns = "http://www.w3.org/2005/Atom"
    atom_link_url = "https://raw.githubusercontent.com/liusonwood/summarss/main/summary_feed.xml"

    atom_link = None
    for child in channel.findall(f"{{{atom_ns}}}link"):
        if child.get("rel") == "self":
            atom_link = child
            break

    if atom_link is None:
        atom_link = ET.SubElement(channel, f"{{{atom_ns}}}link")
        atom_link.set("rel", "self")
        atom_link.set("type", "application/rss+xml")

    atom_link.set("href", atom_link_url)

    # 更新 lastBuildDate
    last_build_date = channel.find("lastBuildDate")
    if last_build_date is None:
        last_build_date = ET.SubElement(channel, "lastBuildDate")
    last_build_date.text = rfc822_date

    # 查找第一个 item 的位置
    first_item_index = -1
    for i, child in enumerate(channel):
        if child.tag == 'item':
            first_item_index = i
            break

    # 为每个源创建一个独立的RSS item
    # 使用 reversed 使最终顺序与 summaries 列表顺序一致
    for label, summary_text in reversed(summaries):
        # 将 AI 生成的 Markdown 转为基础 HTML
        html_content = re.sub(r'^#+\s*(.*)', r'<strong>\1</strong>', summary_text, flags=re.MULTILINE)
        html_content = re.sub(r'^- (.*)', r'• \1', html_content, flags=re.MULTILINE)
        html_content = html_content.replace('\n', '<br/>')
        html_content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', html_content)

        # 每个源独立的唯一标识符
        item_guid = f"ai-summary-{label}-{timestamp}"
        item_link = f"https://github.com/liusonwood/summarss#{label}-{timestamp}"

        # 去重：移除已存在的相同GUID的item
        for existing in channel.findall("item"):
            guid = existing.find("guid")
            if guid is not None and guid.text == item_guid:
                channel.remove(existing)
                break

        # 创建新的 Item 条目
        item = ET.Element("item")
        ET.SubElement(item, "title").text = f"AI 简报 - {label} - {now_beijing.strftime('%Y-%m-%d')}"
        ET.SubElement(item, "link").text = item_link
        ET.SubElement(item, "description").text = html_content
        ET.SubElement(item, "guid", isPermaLink="false").text = item_guid
        ET.SubElement(item, "pubDate").text = rfc822_date

        # 插入到最前面
        if first_item_index != -1:
            channel.insert(first_item_index, item)
        else:
            channel.append(item)

    # 限制历史条目数量
    items = channel.findall("item")
    if len(items) > MAX_HISTORY_ITEMS:
        print(f"Trimming {OUTPUT_FEED}: {len(items)} -> {MAX_HISTORY_ITEMS} items")
        for old_item in items[MAX_HISTORY_ITEMS:]:
            channel.remove(old_item)

    # 使用 minidom 格式化 XML
    xml_str = minidom.parseString(ET.tostring(rss)).toprettyxml(indent="  ")
    # 去除 minidom 产生的多余空行
    xml_str = "\n".join([line for line in xml_str.split('\n') if line.strip()])

    with open(OUTPUT_FEED, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"Successfully generated {OUTPUT_FEED} with {len(summaries)} summaries")

def git_commit_push():
    """推送更改到 GitHub"""
    if os.getenv("GITHUB_ACTIONS") != "true":
        print("Not running in GitHub Actions. Skipping git push.")
        return

    now_beijing = datetime.now(timezone.utc) + timedelta(hours=8)
    commands = [
        ["git", "config", "user.name", "github-actions[bot]"],
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        ["git", "add", PROCESSED_FILE, OUTPUT_FEED],
        ["git", "commit", "-m", f"Auto-update: {now_beijing.strftime('%Y-%m-%d')} (UTC+8)"],
        ["git", "push"]
    ]

    for cmd in commands:
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"Git command skipped/failed: {e.stdout.decode()}")

def main():
    print("Starting RSS AI Summarizer...")
    processed_links = load_processed_links()

    all_summaries = []  # 收集所有源的摘要 (source_label, summary_text) 元组列表

    # 循环处理每个RSS源
    for source in RSS_SOURCES:
        print(f"\n{'='*50}")
        print(f"Processing source: {source_name(source)}")
        print(f"{'='*50}")

        try:
            # 获取并处理文章（内部已做查重，未读的才抓取）
            new_items = fetch_rss_items(source, processed_links)
            label = source_name(source)
            print(f"Found {len(new_items)} new items from {label}.")

            if not new_items:
                print(f"No new items from {label}. Skipping.")
                continue

            # 标记已读 - 在AI调用前就标记，避免AI失败时重复处理
            update_storage([item['link'] for item in new_items])

            # 为当前源生成AI摘要
            print(f"Generating AI summary for {label}...")
            summary = get_ai_summary(new_items, source_label=label)
            all_summaries.append((label, summary))

        except Exception as e:
            # 单个源失败不影响其他源的处理
            print(f"[ERROR] Failed to process {source_name(source)}: {e}")
            continue

    if not all_summaries:
        print("No new items from any source. Exiting.")
        return

    # 生成包含所有源摘要的RSS XML
    generate_rss_xml(all_summaries)

    # 裁剪 processed.txt 防止无限增长
    trim_processed_file()

    print("Summary generated successfully!")
    git_commit_push()

if __name__ == "__main__":
    main()