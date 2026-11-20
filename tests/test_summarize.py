import os
import json
import io
import urllib.error
import xml.etree.ElementTree as ET
from unittest.mock import patch, MagicMock
import pytest

import summarize
from summarize import (
    normalize_url,
    generate_content_id,
    DedupTracker,
    clean_html,
    source_name,
    fetch_article_html,
    fetch_rss_items,
    log_token_usage,
    print_token_usage_summary,
    get_ai_summary,
    generate_rss_xml,
    git_commit_push,
    process_source,
    main,
    RUN_TOKEN_USAGE,
    DC_NS,
)


# ==========================================
# 1. URL 标准化与 Content ID 测试
# ==========================================
class TestURLAndContentID:
    def test_normalize_url_basic(self):
        assert normalize_url("https://example.com/path/") == "https://example.com/path"
        assert normalize_url("https://example.com/path") == "https://example.com/path"

    def test_normalize_url_strip_fragment(self):
        assert normalize_url("https://example.com/path/#section") == "https://example.com/path"
        assert normalize_url("https://example.com/path?foo=bar#section") == "https://example.com/path?foo=bar"

    def test_normalize_url_keep_query(self):
        assert normalize_url("https://example.com/item/?id=123&sort=asc") == "https://example.com/item?id=123&sort=asc"

    def test_normalize_url_empty_and_whitespace(self):
        assert normalize_url("") == ""
        assert normalize_url(None) == ""
        assert normalize_url("   ") == ""

    def test_normalize_url_no_netloc(self):
        assert normalize_url("/local/path/#heading") == "/local/path"
        assert normalize_url("relative/path/") == "relative/path"

    def test_generate_content_id_consistency(self):
        cid1 = generate_content_id("https://example.com/news/1")
        cid2 = generate_content_id("https://example.com/news/1")
        assert cid1 == cid2
        assert len(cid1) == 32

    def test_generate_content_id_equivalence(self):
        # 带有末尾斜杠和 fragment 的相同 URL 应该产生相同的 content_id
        cid1 = generate_content_id("https://example.com/news/1")
        cid2 = generate_content_id("https://example.com/news/1/")
        cid3 = generate_content_id("https://example.com/news/1/#comments")
        assert cid1 == cid2 == cid3

        # query 参数不同应该生成不同的 content_id
        cid4 = generate_content_id("https://example.com/news/1?ref=rss")
        assert cid1 != cid4


# ==========================================
# 2. DedupTracker 去重追踪器测试
# ==========================================
class TestDedupTracker:
    def test_make_source_key(self):
        assert DedupTracker.make_source_key("https://a.com/rss") == "rss:https://a.com/rss"
        assert DedupTracker.make_source_key("rss:https://a.com/rss") == "rss:https://a.com/rss"

        class DummySource:
            type = "atom"
            src = "https://a.com/feed"

        assert DedupTracker.make_source_key(DummySource()) == "atom:https://a.com/feed"

    def test_load_nonexistent_file(self, tmp_path):
        data_file = str(tmp_path / "nonexistent.txt")
        tracker = DedupTracker(data_file)
        assert tracker.source_ids == {}
        assert tracker.fetched_ids == set()

    def test_load_snapshots_and_legacy(self, tmp_path):
        data_file = tmp_path / "processed.txt"
        url_legacy = "https://example.com/legacy-article"
        content_lines = [
            "rss:https://a.com\thash123\n",
            "rss:https://a.com\thash456\n",
            "rss:https://b.com\thash789\n",
            "\n",  # 空行
            f"{url_legacy}\n",  # 旧版纯 URL
        ]
        data_file.write_text("".join(content_lines), encoding="utf-8")

        tracker = DedupTracker(str(data_file))
        assert "hash123" in tracker.source_ids["rss:https://a.com"]
        assert "hash456" in tracker.source_ids["rss:https://a.com"]
        assert "hash789" in tracker.source_ids["rss:https://b.com"]
        assert len(tracker.legacy_ids) == 1
        assert generate_content_id(url_legacy) in tracker.legacy_ids
        assert tracker.is_fetched(url_legacy, "rss:https://any.com")

    def test_is_fetched_and_mark_as_fetched(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        source = "rss:test_feed"
        url = "https://example.com/article-1"

        assert not tracker.is_fetched(url, source)
        assert not tracker.is_fetched("", source)

        tracker.mark_as_fetched(url, source)
        assert tracker.is_fetched(url, source)
        assert not tracker.is_fetched(url, "rss:other_feed")  # 隔离源检查
        assert generate_content_id(url) in tracker.fetched_ids

    def test_stage_source_snapshot_and_save(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        source = "rss:test_source"

        urls_round_1 = [
            "https://example.com/post-1",
            "https://example.com/post-2",
        ]
        tracker.stage_source_snapshot(source, urls_round_1)
        tracker.save()

        # 检查持久化文件
        with open(data_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert tracker.is_fetched("https://example.com/post-1", source)
        assert tracker.is_fetched("https://example.com/post-2", source)

        # 第 2 轮：post-1 保留，post-2 消失，post-3 新增
        urls_round_2 = [
            "https://example.com/post-1",
            "https://example.com/post-3",
        ]
        tracker.stage_source_snapshot(source, urls_round_2)
        tracker.save()

        stats = tracker.get_stats()
        assert stats["total_fetched"] == 2
        assert tracker.is_fetched("https://example.com/post-3", source)
        # post-2 在当前快照中已被移除
        assert not tracker.is_fetched("https://example.com/post-2", source)

    def test_stage_source_snapshot_empty_ignored(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        source = "rss:test"

        tracker.stage_source_snapshot(source, ["https://example.com/post-1"])
        tracker.save()

        # 传入空列表，不应覆盖已有快照
        tracker.stage_source_snapshot(source, [])
        tracker.save()
        assert tracker.is_fetched("https://example.com/post-1", source)

    def test_save_noop_when_nothing_pending(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        # 未做任何 stage 或 mark，save 应该安全跳过
        tracker.save()
        assert not os.path.exists(data_file)

    def test_clear(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        tracker.mark_as_fetched("https://example.com/1", "rss:src")
        tracker.save()
        assert os.path.exists(data_file)

        tracker.clear()
        assert len(tracker.source_ids) == 0
        assert len(tracker.fetched_ids) == 0
        with open(data_file, "r", encoding="utf-8") as f:
            assert f.read() == ""

    def test_legacy_compatibility_functions(self, tmp_path, monkeypatch):
        data_file = tmp_path / "processed.txt"
        monkeypatch.setattr(summarize, "PROCESSED_FILE", str(data_file))

        # 测试 update_storage
        summarize.update_storage(["https://example.com/item1", "https://example.com/item2"])
        assert data_file.exists()

        # 测试 load_processed_links
        links = summarize.load_processed_links()
        assert generate_content_id("https://example.com/item1") in links
        assert generate_content_id("https://example.com/item2") in links

        # 测试 trim_processed_file (应安全通过)
        summarize.trim_processed_file()


# ==========================================
# 3. 辅助函数测试
# ==========================================
class TestHelpers:
    def test_clean_html(self):
        assert clean_html("<p>Hello <b>World</b></p>") == "Hello World"
        assert clean_html("<a href='https://link'>Click</a>") == "Click"
        assert clean_html("") == ""
        assert clean_html(None) == ""

    def test_source_name(self):
        assert source_name("https://9to5mac.com/feed/") == "9To5Mac"
        assert source_name("https://www.techcrunch.com/feed") == "Techcrunch"
        assert source_name("https://sub.domain.org/rss.xml") == "Sub"

    def test_fetch_article_html(self):
        dummy_html = b"<html><body>Sample content</body></html>"
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = dummy_html
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            result = fetch_article_html("https://example.com/article")
            assert result == dummy_html
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://example.com/article"
            assert any(k.lower() == "user-agent" for k in req.headers)

    def test_token_usage_tracking(self, capsys):
        # 重置全局用量
        RUN_TOKEN_USAGE["prompt_tokens"] = 0
        RUN_TOKEN_USAGE["completion_tokens"] = 0
        RUN_TOKEN_USAGE["reasoning_tokens"] = 0
        RUN_TOKEN_USAGE["total_tokens"] = 0
        RUN_TOKEN_USAGE["cost"] = 0.0
        RUN_TOKEN_USAGE["calls"] = 0

        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "reasoning_tokens": 10,
            "total_tokens": 150,
            "cost": 0.002,
        }
        log_token_usage("TestSource", usage)

        assert RUN_TOKEN_USAGE["prompt_tokens"] == 100
        assert RUN_TOKEN_USAGE["completion_tokens"] == 50
        assert RUN_TOKEN_USAGE["reasoning_tokens"] == 10
        assert RUN_TOKEN_USAGE["calls"] == 1

        print_token_usage_summary()
        captured = capsys.readouterr()
        assert "Token 用量总计" in captured.out
        assert "调用 1 次" in captured.out


# ==========================================
# 4. RSS 抓取与内容提取测试
# ==========================================
class TestFetchRSSItems:
    SAMPLE_RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel>
        <title>Tech Feed</title>
        <link>https://example.com</link>
        <item>
          <title>Article One</title>
          <link>https://example.com/article-1/</link>
          <description>Summary of article 1</description>
        </item>
        <item>
          <title>Article Two</title>
          <guid>https://example.com/article-2</guid>
          <content:encoded><![CDATA[<p>Full content of article 2</p>]]></content:encoded>
        </item>
        <item>
          <title>Article One Duplicate</title>
          <link>https://example.com/article-1/#duplicate</link>
          <description>Duplicate</description>
        </item>
      </channel>
    </rss>
    """

    def test_fetch_rss_items_success(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        source_url = "https://example.com/feed.xml"

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("summarize.fetch_article_html", return_value=b"<html><body>Downloaded HTML</body></html>"), \
             patch("trafilatura.extract", return_value="Extracted body text from trafilatura"):

            mock_resp = MagicMock()
            mock_resp.read.return_value = self.SAMPLE_RSS_XML
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            items = fetch_rss_items(source_url, tracker)

            # Article 1 和 Article 2 会被提取，Article One Duplicate 会在 batch 去重时被跳过
            assert len(items) == 2
            assert items[0]["title"] == "Article One"
            assert items[0]["link"] == "https://example.com/article-1"
            assert items[0]["body"] == "Extracted body text from trafilatura"
            assert items[1]["title"] == "Article Two"

    def test_fetch_rss_items_fallback_to_description(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        source_url = "https://example.com/feed.xml"

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("summarize.fetch_article_html", side_effect=Exception("Connection timed out")), \
             patch("trafilatura.fetch_url", return_value=None):

            mock_resp = MagicMock()
            mock_resp.read.return_value = self.SAMPLE_RSS_XML
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            items = fetch_rss_items(source_url, tracker)
            assert len(items) == 2
            # 抓取正文失败后应回退到 RSS description 或 content:encoded
            assert items[0]["body"] == "Summary of article 1"
            assert items[1]["body"] == "Full content of article 2"

    def test_fetch_rss_items_skips_already_fetched(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        source_url = "https://example.com/feed.xml"
        source_key = DedupTracker.make_source_key(source_url)

        # 标记 article-1 为已抓取
        tracker.mark_as_fetched("https://example.com/article-1", source_key)

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("summarize.fetch_article_html", return_value=b"<html><body>Test</body></html>"), \
             patch("trafilatura.extract", return_value="Extracted text"):

            mock_resp = MagicMock()
            mock_resp.read.return_value = self.SAMPLE_RSS_XML
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            items = fetch_rss_items(source_url, tracker)
            assert len(items) == 1
            assert items[0]["title"] == "Article Two"


# ==========================================
# 5. AI 摘要生成测试
# ==========================================
class TestGetAISummary:
    def test_missing_api_key_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "")
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY is not set"):
            get_ai_summary([{"title": "Test", "body": "Body"}])

    def test_get_ai_summary_streaming_success(self, monkeypatch):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "test-key")

        # 构造 SSE 流式响应 chunks
        sse_lines = [
            b"data: {\"choices\": [{\"delta\": {\"content\": \"**[\xe4\xba\xba\xe5\xb7\xa5\xe6\x99\xba\xe8\x83\xbd]**\\n\"}}]}\n",
            b"data: {\"choices\": [{\"delta\": {\"content\": \"- \xe6\x96\xb0\xe5\x9e\x8b\xe6\xa8\xa1\xe5\x9e\x8b\xe5\x8f\x91\xe5\xb8\x83\\n\"}}]}\n",
            b"data: {\"usage\": {\"prompt_tokens\": 50, \"completion_tokens\": 30, \"total_tokens\": 80}}\n",
            b"data: [DONE]\n"
        ]

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__iter__.return_value = sse_lines
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            items = [{"title": "AI 新突破", "body": "这是正文"}]
            result = get_ai_summary(items, source_label="TechNews")
            assert "**[人工智能]**" in result
            assert "- 新型模型发布" in result

    def test_get_ai_summary_retry_and_exhaustion(self, monkeypatch):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "test-key")

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Network Error")), \
             patch("time.sleep"):  # 避免测试中真实睡眠
            items = [{"title": "AI 新突破", "body": "这是正文"}]
            result = get_ai_summary(items)
            assert "摘要生成失败" in result


# ==========================================
# 6. RSS 输出生成测试
# ==========================================
class TestGenerateRSSXML:
    def test_generate_new_rss_xml(self, tmp_path, monkeypatch):
        out_file = tmp_path / "summary_feed.xml"
        monkeypatch.setattr(summarize, "OUTPUT_FEED", str(out_file))
        monkeypatch.setattr(summarize, "REPO_NAME", "user/repo")
        monkeypatch.setattr(summarize, "AI_MODEL", "test-model")

        summaries = [
            ("TechSource", "**[AI 硬件]**\n- 新芯片发布，性能提升 **50%**\n# 总结标题\n另一行内容")
        ]
        generate_rss_xml(summaries)

        assert out_file.exists()
        tree = ET.parse(str(out_file))
        root = tree.getroot()
        assert root.tag == "rss"
        channel = root.find("channel")
        assert channel is not None
        assert channel.find("title").text == "AI RSS 简报"

        items = channel.findall("item")
        assert len(items) == 1
        item = items[0]
        assert "TechSource" in item.find("title").text
        # 验证 Markdown 转换后的 HTML 标记
        desc = item.find("description").text
        assert "• 新芯片发布" in desc
        assert "<strong>[AI 硬件]</strong>" in desc
        assert "<strong>50%</strong>" in desc
        assert "<strong>总结标题</strong>" in desc
        # 验证 dc:creator
        creator = item.find(f"{{{DC_NS}}}creator")
        assert creator is not None and creator.text == "test-model"

    def test_rss_xml_history_trimming(self, tmp_path, monkeypatch):
        out_file = tmp_path / "summary_feed.xml"
        monkeypatch.setattr(summarize, "OUTPUT_FEED", str(out_file))
        monkeypatch.setattr(summarize, "MAX_HISTORY_ITEMS", 3)

        # 写入 4 条摘要
        summaries = [
            ("SourceA", "Summary A"),
            ("SourceB", "Summary B"),
            ("SourceC", "Summary C"),
            ("SourceD", "Summary D"),
        ]
        generate_rss_xml(summaries)

        tree = ET.parse(str(out_file))
        items = tree.getroot().find("channel").findall("item")
        assert len(items) == 3

    def test_rss_xml_update_existing_feed(self, tmp_path, monkeypatch):
        out_file = tmp_path / "summary_feed.xml"
        monkeypatch.setattr(summarize, "OUTPUT_FEED", str(out_file))
        monkeypatch.setattr(summarize, "REPO_NAME", "user/repo")

        # 第一次写入
        generate_rss_xml([("Source1", "Initial summary")])
        assert out_file.exists()

        # 第二次追加更新
        generate_rss_xml([("Source2", "Updated summary")])
        tree = ET.parse(str(out_file))
        items = tree.getroot().find("channel").findall("item")
        assert len(items) == 2
        # 最新的应该在最前面
        assert "Source2" in items[0].find("title").text
        assert "Source1" in items[1].find("title").text

    def test_rss_xml_corrupt_file_handling(self, tmp_path, monkeypatch):
        out_file = tmp_path / "summary_feed.xml"
        out_file.write_text("Not valid XML <broken>", encoding="utf-8")
        monkeypatch.setattr(summarize, "OUTPUT_FEED", str(out_file))

        # 损坏的 XML 会被捕获并重新生成崭新的 RSS
        generate_rss_xml([("Source1", "New summary after corrupt")])
        tree = ET.parse(str(out_file))
        items = tree.getroot().find("channel").findall("item")
        assert len(items) == 1


# ==========================================
# 7. 流程调度与 Git 测试
# ==========================================
class TestProcessSourceAndGit:
    def test_process_source_success(self):
        tracker = MagicMock(spec=DedupTracker)
        with patch("summarize.fetch_rss_items") as mock_fetch, \
             patch("summarize.get_ai_summary") as mock_ai:

            mock_fetch.return_value = [{"title": "Post 1", "body": "Body 1"}]
            mock_ai.return_value = "Mocked AI Summary"

            res = process_source("https://example.com/feed", tracker)
            assert res is not None
            assert res[0] == "Example"
            assert res[1] == "Mocked AI Summary"

    def test_process_source_no_items(self):
        tracker = MagicMock(spec=DedupTracker)
        with patch("summarize.fetch_rss_items", return_value=[]):
            res = process_source("https://example.com/feed", tracker)
            assert res is None

    def test_process_source_error_isolation(self):
        tracker = MagicMock(spec=DedupTracker)
        with patch("summarize.fetch_rss_items", side_effect=Exception("Feed broken")):
            # 错误被捕获，返回 None，不向外抛出异常
            res = process_source("https://example.com/feed", tracker)
            assert res is None

    def test_git_commit_push_skipped_outside_actions(self, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with patch("subprocess.run") as mock_subproc:
            git_commit_push()
            mock_subproc.assert_not_called()

    def test_git_commit_push_runs_in_actions(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with patch("subprocess.run") as mock_subproc:
            git_commit_push()
            assert mock_subproc.call_count == 5

    def test_main_workflow(self, tmp_path, monkeypatch):
        test_processed = tmp_path / "processed.txt"
        test_feed = tmp_path / "feed.xml"
        monkeypatch.setattr(summarize, "PROCESSED_FILE", str(test_processed))
        monkeypatch.setattr(summarize, "OUTPUT_FEED", str(test_feed))
        monkeypatch.setattr(summarize, "RSS_SOURCES", ["https://example.com/feed"])

        with patch("summarize.process_source", return_value=("Example", "Summary text")), \
             patch("summarize.generate_rss_xml") as mock_gen_xml, \
             patch("summarize.git_commit_push"):

            main()
            mock_gen_xml.assert_called_once_with([("Example", "Summary text")])

    def test_main_no_rss_sources(self, monkeypatch):
        monkeypatch.setattr(summarize, "RSS_SOURCES", [])
        with patch("summarize.process_source") as mock_proc:
            main()
            mock_proc.assert_not_called()

    def test_main_no_new_summaries(self, tmp_path, monkeypatch):
        test_processed = tmp_path / "processed.txt"
        monkeypatch.setattr(summarize, "PROCESSED_FILE", str(test_processed))
        monkeypatch.setattr(summarize, "RSS_SOURCES", ["https://example.com/feed"])

        with patch("summarize.process_source", return_value=None), \
             patch("summarize.generate_rss_xml") as mock_gen_xml:

            main()
            mock_gen_xml.assert_not_called()
