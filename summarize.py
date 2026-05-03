import os
import json
import xml.etree.ElementTree as ET
from xml.dom import minidom
import urllib.request
import urllib.parse
from datetime import datetime, timezone
import subprocess
import re
import trafilatura

# 配置
RSS_SOURCE = os.getenv("RSS_SOURCE", "https://9to5mac.com/feed/")
PROCESSED_FILE = "processed.txt"
OUTPUT_FEED = "summary_feed.xml"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MAX_ITEMS = 28
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.0-flash-001")
MAX_HISTORY_ITEMS = 400  # 保留的历史条目数量

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

def fetch_rss_items(source, processed_links):
    """抓取 RSS：先判断是否已读，只有新文章才抓取全文"""
    try:
        print(f"正在读取 RSS 源: {source}")
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
            
            items.append({"title": title, "link": link, "body": body})
            
            # 达到单次最大处理量提前停止抓取
            if len(items) >= MAX_ITEMS:
                break
                
        return items
    except Exception as e:
        print(f"Error fetching RSS: {e}")
        return []

def get_ai_summary(items):
    """调用 AI 生成摘要"""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not set!")
        
    prompt = (
        "你是一位专业的科技新闻编辑。请根据提供的 RSS 文章内容，整理出一份精炼、客观的中文每日简报。\n\n"
        "### 要求：\n"
        "1. **分类汇总**：按主题对内容进行分类。如果主题跨度不大，则按重要程度排序。\n"
        "2. **内容质量**：每条摘要应直击核心事实，剔除营销废话，保持中立专业的语气。\n"
        "3. **格式规范**：\n"
        "   - 使用 Markdown 格式：分类标题加粗，如 **[分类名称]**。\n"
        "   - 条目使用 `- ` 开头的无序列表。\n"
        "   - 核心关键词或结论使用 **双星号加粗**。\n"
        "   - **严禁** 开场白、问候语或结束语（直接输出正文内容）。\n"
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
    
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(data).encode('utf-8'),
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/liusonwood/rss_ai_summarise",
            "X-Title": "RSS AI Summary Agent"
        }
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            return res_data['choices'][0]['message']['content']
    except Exception as e:
        print(f"Error calling AI API: {e}")
        return "摘要生成失败。"

def generate_rss_xml(summary_text):
    """生成或更新 RSS XML 文件 (与天气项目相同的 minidom 格式)"""
    
    # 注册 Atom 命名空间
    ET.register_namespace('atom', "http://www.w3.org/2005/Atom")
    
    now_utc = datetime.now(timezone.utc)
    rfc822_date = now_utc.strftime("%a, %d %b %Y %H:%M:%S GMT")
    
    # 将 AI 生成的 Markdown 转为基础 HTML，确保阅读器兼容
    # 1. 处理标题 (将 # 替换为加粗，避免 RSS 中出现 #)
    html_content = re.sub(r'^#+\s*(.*)', r'<strong>\1</strong>', summary_text, flags=re.MULTILINE)
    # 2. 处理列表符号
    html_content = re.sub(r'^- (.*)', r'• \1', html_content, flags=re.MULTILINE)
    # 3. 换行符转为 <br/>
    html_content = html_content.replace('\n', '<br/>')
    # 4. 处理 Markdown 加粗
    html_content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', html_content)
    
    # 唯一标识符和时间戳
    timestamp = now_utc.strftime('%Y%m%d%H%M%S')
    guid_text = f"ai-summary-{timestamp}"
    item_link = f"https://github.com/liusonwood/rss_ai_summarise#{timestamp}"

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
            ET.SubElement(channel, "link").text = "https://github.com/liusonwood/rss_ai_summarise"
            ET.SubElement(channel, "description").text = "由 AI 自动生成的文章全文摘要"
    else:
        rss = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = "AI RSS 简报"
        ET.SubElement(channel, "link").text = "https://github.com/liusonwood/rss_ai_summarise"
        ET.SubElement(channel, "description").text = "由 AI 自动生成的文章全文摘要"

    # 处理 atom:link (解决验证报错)
    atom_ns = "http://www.w3.org/2005/Atom"
    # 此处假设你 GitHub Pages 暴露的最终订阅地址是这个
    atom_link_url = "https://liusonwood.github.io/rss_ai_summarise/summary_feed.xml" 
    
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

    # 去重处理：以防短时间内重复运行
    existing_item = None
    for item in channel.findall("item"):
        guid = item.find("guid")
        if guid is not None and guid.text == guid_text:
            existing_item = item
            break
            
    if existing_item:
        channel.remove(existing_item)

    # 创建新的 Item 条目
    item = ET.Element("item")
    ET.SubElement(item, "title").text = f"AI 简报 - {now_utc.strftime('%Y-%m-%d %H:%M')} (UTC)"
    ET.SubElement(item, "link").text = item_link
    ET.SubElement(item, "description").text = html_content  # 写入转换后的 HTML
    ET.SubElement(item, "guid", isPermaLink="false").text = guid_text
    ET.SubElement(item, "pubDate").text = rfc822_date
    
    # 查找第一个 item 的位置并插入到最前面
    first_item_index = -1
    for i, child in enumerate(channel):
        if child.tag == 'item':
            first_item_index = i
            break
            
    if first_item_index != -1:
        channel.insert(first_item_index, item)
    else:
        channel.append(item)

    # 限制历史条目数量
    items = channel.findall("item")
    if len(items) > MAX_HISTORY_ITEMS:
        for old_item in items[MAX_HISTORY_ITEMS:]:
            channel.remove(old_item)

    # 使用 minidom 格式化 XML (与天气项目完全相同的逻辑)
    xml_str = minidom.parseString(ET.tostring(rss)).toprettyxml(indent="  ")
    # 去除 minidom 产生的多余空行
    xml_str = "\n".join([line for line in xml_str.split('\n') if line.strip()])
    
    with open(OUTPUT_FEED, "w", encoding="utf-8") as f:
        f.write(xml_str)
        
    print(f"Successfully generated {OUTPUT_FEED}")

def git_commit_push():
    """推送更改到 GitHub"""
    if os.getenv("GITHUB_ACTIONS") != "true":
        print("Not running in GitHub Actions. Skipping git push.")
        return

    commands = [
        ["git", "config", "user.name", "github-actions[bot]"],
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        ["git", "add", PROCESSED_FILE, OUTPUT_FEED],
        ["git", "commit", "-m", f"Auto-update: {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
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
    
    # 获取并处理文章（内部已做查重，未读的才抓取）
    new_items = fetch_rss_items(RSS_SOURCE, processed_links)
    
    print(f"Found {len(new_items)} new items to summarize.")
    
    if not new_items:
        print("No new items to process. Exiting.")
        return
        
    # 全部标记已读
    update_storage([item['link'] for item in new_items])
    
    print(f"Processing AI summary...")
    summary = get_ai_summary(new_items)
    generate_rss_xml(summary)
    
    print("Summary generated successfully!")
    git_commit_push()

if __name__ == "__main__":
    main()