import os
import io
import logging
import subprocess
import time
import urllib.error
import xml.etree.ElementTree as ET
from email.message import Message
from unittest.mock import patch, MagicMock, ANY
import pytest

import summarize
from summarize import (
    normalize_url,
    generate_content_id,
    DedupTracker,
    clean_html,
    source_name,
    build_source_labels,
    fetch_article_html,
    fetch_rss_items,
    log_token_usage,
    print_token_usage_summary,
    get_ai_summary,
    is_retryable_error,
    retry_delay,
    markdown_to_html,
    generate_rss_xml,
    git_commit_push,
    process_source,
    main,
    RUN_TOKEN_USAGE,
    DC_NS,
)


def make_http_error(code, headers=None):
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("https://api.test", code, "err", hdrs, io.BytesIO(b'{"error": "x"}'))


def stream_response(lines):
    """模拟 urlopen 返回的上下文管理器，迭代时产出 SSE 行"""
    resp = MagicMock()
    resp.__iter__.return_value = lines
    cm = MagicMock()
    cm.__enter__.return_value = resp
    return cm


SSE_OK = [
    b"data: {\"choices\": [{\"delta\": {\"content\": \"**[AI]**\\n\"}}]}\n",
    b"data: {\"choices\": [{\"delta\": {\"content\": \"- ok\\n\"}}]}\n",
    b"data: {\"usage\": {\"prompt_tokens\": 5, \"completion_tokens\": 3, \"total_tokens\": 8}}\n",
    b"data: [DONE]\n",
]


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
        cid1 = generate_content_id("https://example.com/news/1")
        cid2 = generate_content_id("https://example.com/news/1/")
        cid3 = generate_content_id("https://example.com/news/1/#comments")
        assert cid1 == cid2 == cid3

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
            "\n",
            f"{url_legacy}\n",
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
        assert not tracker.is_fetched(url, "rss:other_feed")
        assert generate_content_id(url) in tracker.fetched_ids

    def test_stage_source_snapshot_and_save(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        source = "rss:test_source"

        tracker.stage_source_snapshot(source, ["https://example.com/post-1", "https://example.com/post-2"])
        tracker.save()

        with open(data_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert tracker.is_fetched("https://example.com/post-1", source)
        assert tracker.is_fetched("https://example.com/post-2", source)

        tracker.stage_source_snapshot(source, ["https://example.com/post-1", "https://example.com/post-3"])
        tracker.save()

        stats = tracker.get_stats()
        assert stats["total_fetched"] == 2
        assert tracker.is_fetched("https://example.com/post-3", source)
        assert not tracker.is_fetched("https://example.com/post-2", source)

    def test_stage_source_snapshot_empty_ignored(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
        source = "rss:test"

        tracker.stage_source_snapshot(source, ["https://example.com/post-1"])
        tracker.save()

        tracker.stage_source_snapshot(source, [])
        tracker.save()
        assert tracker.is_fetched("https://example.com/post-1", source)

    def test_save_noop_when_nothing_pending(self, tmp_path):
        data_file = str(tmp_path / "processed.txt")
        tracker = DedupTracker(data_file)
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


# ==========================================
# 3. 辅助函数测试
# ==========================================
class TestHelpers:
    def test_clean_html(self):
        assert clean_html("<p>Hello <b>World</b></p>") == "Hello World"
        assert clean_html("<a href='https://link'>Click</a>") == "Click"
        assert clean_html("Tom &amp; Jerry") == "Tom & Jerry"
        assert clean_html("") == ""
        assert clean_html(None) == ""

    def test_source_name(self):
        assert source_name("https://9to5mac.com/feed/") == "9To5Mac"
        assert source_name("https://www.techcrunch.com/feed") == "Techcrunch"
        # 取可注册域名主体，而不是第一段子域名
        assert source_name("https://sub.domain.org/rss.xml") == "Domain"
        assert source_name("https://feeds.arstechnica.com/arstechnica/index") == "Arstechnica"
        assert source_name("https://www.bbc.co.uk/news/rss.xml") == "Bbc"
        assert source_name("http://localhost/feed") == "Localhost"

    def test_build_source_labels_distinct_domains(self):
        labels = build_source_labels(["https://blog.a.com/feed", "https://blog.b.com/feed"])
        assert labels == {"https://blog.a.com/feed": "A", "https://blog.b.com/feed": "B"}

    def test_build_source_labels_same_base_name_uses_hostname(self):
        labels = build_source_labels(["https://a.blog.com/feed", "https://b.blog.com/feed"])
        assert labels["https://a.blog.com/feed"] == "a.blog.com"
        assert labels["https://b.blog.com/feed"] == "b.blog.com"

    def test_build_source_labels_same_host_uses_path(self):
        labels = build_source_labels(["https://x.com/tech/feed", "https://x.com/biz/feed"])
        assert labels["https://x.com/tech/feed"] == "x.com/tech/feed"
        assert labels["https://x.com/biz/feed"] == "x.com/biz/feed"
        assert len(set(labels.values())) == 2

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

    def test_token_usage_tracking(self, caplog):
        for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens", "calls"):
            RUN_TOKEN_USAGE[k] = 0
        RUN_TOKEN_USAGE["cost"] = 0.0

        log_token_usage("TestSource", {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "reasoning_tokens": 10,
            "total_tokens": 150,
            "cost": 0.002,
        })

        assert RUN_TOKEN_USAGE["prompt_tokens"] == 100
        assert RUN_TOKEN_USAGE["completion_tokens"] == 50
        assert RUN_TOKEN_USAGE["reasoning_tokens"] == 10
        assert RUN_TOKEN_USAGE["calls"] == 1

        with caplog.at_level(logging.INFO, logger="summarize"):
            print_token_usage_summary()
        assert "Token 用量总计" in caplog.text
        assert "调用 1 次" in caplog.text


# ==========================================
# 4. RSS / Atom 抓取与内容提取测试
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

    SAMPLE_ATOM_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Atom Feed</title>
      <link rel="self" href="https://example.com/atom.xml"/>
      <entry>
        <title>Atom One</title>
        <id>tag:example.com,2026:1</id>
        <link rel="replies" href="https://example.com/atom-1/comments"/>
        <link rel="alternate" href="https://example.com/atom-1/"/>
        <summary>Atom summary 1</summary>
      </entry>
      <entry>
        <title>Atom Two</title>
        <link href="https://example.com/atom-2"/>
        <content type="html">&lt;p&gt;Atom content 2&lt;/p&gt;</content>
      </entry>
    </feed>
    """

    @staticmethod
    def _serve(mock_urlopen, xml):
        mock_resp = MagicMock()
        mock_resp.read.return_value = xml
        mock_urlopen.return_value.__enter__.return_value = mock_resp

    def test_fetch_rss_items_success(self, tmp_path):
        tracker = DedupTracker(str(tmp_path / "processed.txt"))

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("summarize.fetch_article_html", return_value=b"<html><body>Downloaded HTML</body></html>"), \
             patch("trafilatura.extract", return_value="Extracted body text from trafilatura"):
            self._serve(mock_urlopen, self.SAMPLE_RSS_XML)
            items = fetch_rss_items("https://example.com/feed.xml", tracker)

            assert len(items) == 2
            assert items[0]["title"] == "Article One"
            assert items[0]["link"] == "https://example.com/article-1"
            assert items[0]["body"] == "Extracted body text from trafilatura"
            assert items[1]["title"] == "Article Two"

    def test_fetch_rss_items_fallback_to_description(self, tmp_path):
        tracker = DedupTracker(str(tmp_path / "processed.txt"))

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("summarize.fetch_article_html", side_effect=Exception("Connection timed out")), \
             patch("trafilatura.fetch_url", return_value=None):
            self._serve(mock_urlopen, self.SAMPLE_RSS_XML)
            items = fetch_rss_items("https://example.com/feed.xml", tracker)

            assert len(items) == 2
            assert items[0]["body"] == "Summary of article 1"
            assert items[1]["body"] == "Full content of article 2"

    def test_fetch_rss_items_skips_already_fetched(self, tmp_path):
        tracker = DedupTracker(str(tmp_path / "processed.txt"))
        source_url = "https://example.com/feed.xml"
        tracker.mark_as_fetched("https://example.com/article-1", DedupTracker.make_source_key(source_url))

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("summarize.fetch_article_html", return_value=b"<html><body>Test</body></html>"), \
             patch("trafilatura.extract", return_value="Extracted text"):
            self._serve(mock_urlopen, self.SAMPLE_RSS_XML)
            items = fetch_rss_items(source_url, tracker)
            assert len(items) == 1
            assert items[0]["title"] == "Article Two"

    def test_fetch_rss_items_atom(self, tmp_path):
        tracker = DedupTracker(str(tmp_path / "processed.txt"))

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("summarize.fetch_article_html", side_effect=Exception("boom")), \
             patch("trafilatura.fetch_url", return_value=None):
            self._serve(mock_urlopen, self.SAMPLE_ATOM_XML)
            items = fetch_rss_items("https://example.com/atom.xml", tracker)

        assert [i["title"] for i in items] == ["Atom One", "Atom Two"]
        # 选 rel=alternate 的链接，而不是 replies / self
        assert items[0]["link"] == "https://example.com/atom-1"
        assert items[1]["link"] == "https://example.com/atom-2"
        assert items[0]["body"] == "Atom summary 1"
        assert items[1]["body"] == "Atom content 2"

    def test_fetch_rss_items_concurrent_preserves_order(self, tmp_path):
        tracker = DedupTracker(str(tmp_path / "processed.txt"))

        def slow_first(url):
            if url.endswith("article-1"):
                time.sleep(0.2)  # 第一篇最慢，结果顺序仍应与 feed 一致
            return f"<html>{url}</html>".encode()

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("summarize.fetch_article_html", side_effect=slow_first), \
             patch("trafilatura.extract", side_effect=lambda data, **kw: data.decode()):
            self._serve(mock_urlopen, self.SAMPLE_RSS_XML)
            items = fetch_rss_items("https://example.com/feed.xml", tracker)

        assert [i["title"] for i in items] == ["Article One", "Article Two"]
        assert "article-1" in items[0]["body"]
        assert "article-2" in items[1]["body"]


# ==========================================
# 5. AI 摘要生成测试
# ==========================================
class TestGetAISummary:
    ITEMS = [{"title": "AI 新突破", "body": "这是正文"}]

    def test_missing_api_key_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "")
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY is not set"):
            get_ai_summary([{"title": "Test", "body": "Body"}])

    def test_get_ai_summary_streaming_success(self, monkeypatch):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "test-key")
        sse_lines = [
            b"data: {\"choices\": [{\"delta\": {\"content\": \"**[\xe4\xba\xba\xe5\xb7\xa5\xe6\x99\xba\xe8\x83\xbd]**\\n\"}}]}\n",
            b"data: {\"choices\": [{\"delta\": {\"content\": \"- \xe6\x96\xb0\xe5\x9e\x8b\xe6\xa8\xa1\xe5\x9e\x8b\xe5\x8f\x91\xe5\xb8\x83\\n\"}}]}\n",
            b"data: {\"usage\": {\"prompt_tokens\": 50, \"completion_tokens\": 30, \"total_tokens\": 80}}\n",
            b"data: [DONE]\n"
        ]
        with patch("urllib.request.urlopen", return_value=stream_response(sse_lines)):
            result = get_ai_summary(self.ITEMS, source_label="TechNews")
            assert "**[人工智能]**" in result
            assert "- 新型模型发布" in result

    def test_get_ai_summary_retry_and_exhaustion(self, monkeypatch):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "test-key")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Network Error")) as m, \
             patch("time.sleep"):
            result = get_ai_summary(self.ITEMS)
            assert "摘要生成失败" in result
            assert m.call_count == 3

    @pytest.mark.parametrize("code", [400, 401, 403, 404])
    def test_non_retryable_http_error_fails_fast(self, monkeypatch, code):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "test-key")
        with patch("urllib.request.urlopen", side_effect=make_http_error(code)) as m, \
             patch("time.sleep") as mock_sleep:
            result = get_ai_summary(self.ITEMS)
            assert "摘要生成失败" in result
            assert m.call_count == 1
            mock_sleep.assert_not_called()

    def test_retry_on_429_then_success(self, monkeypatch):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "test-key")
        with patch("urllib.request.urlopen",
                   side_effect=[make_http_error(429), stream_response(SSE_OK)]) as m, \
             patch("time.sleep") as mock_sleep:
            result = get_ai_summary(self.ITEMS)
            assert "**[AI]**" in result
            assert m.call_count == 2
            mock_sleep.assert_called_once_with(10)

    def test_retry_on_500_then_success(self, monkeypatch):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "test-key")
        with patch("urllib.request.urlopen",
                   side_effect=[make_http_error(503), stream_response(SSE_OK)]), \
             patch("time.sleep"):
            assert "- ok" in get_ai_summary(self.ITEMS)

    def test_retry_on_empty_stream(self, monkeypatch):
        monkeypatch.setattr(summarize, "OPENROUTER_API_KEY", "test-key")
        with patch("urllib.request.urlopen",
                   side_effect=[stream_response([b"data: [DONE]\n"]), stream_response(SSE_OK)]) as m, \
             patch("time.sleep"):
            assert "- ok" in get_ai_summary(self.ITEMS)
            assert m.call_count == 2

    def test_is_retryable_error(self):
        assert is_retryable_error(make_http_error(429))
        assert is_retryable_error(make_http_error(408))
        assert is_retryable_error(make_http_error(502))
        assert not is_retryable_error(make_http_error(401))
        assert not is_retryable_error(make_http_error(400))
        assert is_retryable_error(TimeoutError())
        assert is_retryable_error(urllib.error.URLError("x"))
        assert is_retryable_error(ConnectionResetError())
        assert not is_retryable_error(ValueError("bug"))

    def test_retry_delay_honors_retry_after(self):
        assert retry_delay(make_http_error(429, {"Retry-After": "30"}), 0) == 30
        assert retry_delay(make_http_error(429, {"Retry-After": "5"}), 0) == 10
        assert retry_delay(make_http_error(429, {"Retry-After": "9999"}), 0) == 60
        assert retry_delay(make_http_error(429, {"Retry-After": "soon"}), 1) == 20
        assert retry_delay(urllib.error.URLError("x"), 2) == 30


# ==========================================
# 6. Markdown 转换与 RSS 输出生成测试
# ==========================================
class TestMarkdownToHtml:
    def test_basic_structure(self):
        out = markdown_to_html("**[分类]**\n- 事实 **重点**\n- 第二条\n\n# 标题\n正文")
        assert "<p><strong>[分类]</strong></p>" in out
        assert "<ul><li>事实 <strong>重点</strong></li><li>第二条</li></ul>" in out
        assert "<p><strong>标题</strong></p>" in out
        assert "<p>正文</p>" in out

    def test_escapes_html(self):
        out = markdown_to_html("<script>alert(1)</script> & **x**")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "&amp;" in out
        assert "<strong>x</strong>" in out

    def test_strips_xml_invalid_control_chars(self):
        assert "\x0b" not in markdown_to_html("a\x0bb\x00c")

    def test_empty(self):
        assert markdown_to_html("") == ""
        assert markdown_to_html(None) == ""


class TestGenerateRSSXML:
    @pytest.fixture(autouse=True)
    def _paths(self, tmp_path, monkeypatch):
        self.out_file = tmp_path / "summary_feed.xml"
        monkeypatch.setattr(summarize, "OUTPUT_FEED", str(self.out_file))
        monkeypatch.setattr(summarize, "REPO_NAME", "user/repo")
        monkeypatch.setattr(summarize, "AI_MODEL", "test-model")

    def _items(self):
        return ET.parse(str(self.out_file)).getroot().find("channel").findall("item")

    def test_generate_new_rss_xml(self):
        generate_rss_xml([("TechSource", "**[AI 硬件]**\n- 新芯片发布，性能提升 **50%**\n# 总结标题\n另一行内容")])

        root = ET.parse(str(self.out_file)).getroot()
        assert root.tag == "rss"
        channel = root.find("channel")
        assert channel.find("title").text == "AI RSS 简报"

        items = channel.findall("item")
        assert len(items) == 1
        item = items[0]
        assert "TechSource" in item.find("title").text
        desc = item.find("description").text
        assert "<li>新芯片发布，性能提升 <strong>50%</strong></li>" in desc
        assert "<strong>[AI 硬件]</strong>" in desc
        assert "<strong>总结标题</strong>" in desc
        creator = item.find(f"{{{DC_NS}}}creator")
        assert creator is not None and creator.text == "test-model"

    def test_new_feed_keeps_summaries_order(self):
        # 回归：新建 feed 时（没有旧 item）顺序不能被反转
        generate_rss_xml([("Src1", "a"), ("Src2", "b"), ("Src3", "c")])
        titles = [i.find("title").text for i in self._items()]
        assert len(titles) == 3
        assert "Src1" in titles[0] and "Src2" in titles[1] and "Src3" in titles[2]

    def test_existing_feed_new_items_first_in_order(self):
        generate_rss_xml([("S1", "x")])
        generate_rss_xml([("S3", "x"), ("S4", "x")])
        titles = [i.find("title").text for i in self._items()]
        assert len(titles) == 3
        assert "S3" in titles[0] and "S4" in titles[1] and "S1" in titles[2]

    def test_rss_xml_history_trimming(self, monkeypatch):
        monkeypatch.setattr(summarize, "MAX_HISTORY_ITEMS", 3)
        generate_rss_xml([("SourceA", "A"), ("SourceB", "B"), ("SourceC", "C"), ("SourceD", "D")])
        assert len(self._items()) == 3

    def test_rss_xml_update_existing_feed(self):
        generate_rss_xml([("Source1", "Initial summary")])
        generate_rss_xml([("Source2", "Updated summary")])
        items = self._items()
        assert len(items) == 2
        assert "Source2" in items[0].find("title").text
        assert "Source1" in items[1].find("title").text

    def test_rss_xml_corrupt_file_handling(self):
        self.out_file.write_text("Not valid XML <broken>", encoding="utf-8")
        generate_rss_xml([("Source1", "New summary after corrupt")])
        assert len(self._items()) == 1

    def test_item_link_quotes_label(self):
        generate_rss_xml([("blog.a.com/tech feed", "x")])
        link = self._items()[0].find("link").text
        assert " " not in link
        assert "blog.a.com%2Ftech%20feed" in link

    def test_control_chars_do_not_break_feed(self):
        generate_rss_xml([("S", "ok\x0b\x00 text")])
        assert len(self._items()) == 1

    def test_description_is_escaped(self):
        generate_rss_xml([("S", "<img src=x onerror=alert(1)>")])
        desc = self._items()[0].find("description").text
        assert "<img" not in desc
        assert "&lt;img" in desc


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
            assert res == ("Example", "Mocked AI Summary")

    def test_process_source_uses_given_label(self):
        tracker = MagicMock(spec=DedupTracker)
        with patch("summarize.fetch_rss_items", return_value=[{"title": "P", "body": "B"}]) as mock_fetch, \
             patch("summarize.get_ai_summary", return_value="S") as mock_ai:
            res = process_source("https://a.blog.com/feed", tracker, "a.blog.com")
            assert res == ("a.blog.com", "S")
            assert mock_fetch.call_args[0][2] == "a.blog.com"
            assert mock_ai.call_args.kwargs["source_label"] == "a.blog.com"

    def test_process_source_no_items(self):
        tracker = MagicMock(spec=DedupTracker)
        with patch("summarize.fetch_rss_items", return_value=[]):
            assert process_source("https://example.com/feed", tracker) is None

    def test_process_source_error_isolation(self):
        tracker = MagicMock(spec=DedupTracker)
        with patch("summarize.fetch_rss_items", side_effect=Exception("Feed broken")):
            assert process_source("https://example.com/feed", tracker) is None

    # ---- git ----
    @staticmethod
    def _fake_git(rc_by_subcommand=None):
        calls = []
        rcs = rc_by_subcommand or {}

        def run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, rcs.get(cmd[1], 0), "", "boom")

        return run, calls

    def test_git_commit_push_skipped_outside_actions(self, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with patch("subprocess.run") as mock_subproc:
            git_commit_push()
            mock_subproc.assert_not_called()

    def test_git_commit_push_full_flow(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        run, calls = self._fake_git({"diff": 1})
        with patch("subprocess.run", side_effect=run):
            git_commit_push()
        assert [c[1] for c in calls] == ["config", "config", "add", "diff", "commit", "pull", "push"]
        assert calls[5] == ["git", "pull", "--rebase"]

    def test_git_commit_push_no_changes(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        run, calls = self._fake_git({"diff": 0})
        with patch("subprocess.run", side_effect=run):
            git_commit_push()  # 不应抛异常
        subs = [c[1] for c in calls]
        assert "commit" not in subs and "push" not in subs

    def test_git_commit_push_push_failure_raises(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        run, _ = self._fake_git({"diff": 1, "push": 1})
        with patch("subprocess.run", side_effect=run):
            with pytest.raises(RuntimeError, match="push"):
                git_commit_push()

    def test_git_commit_push_rebase_failure_stops_before_push(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        run, calls = self._fake_git({"diff": 1, "pull": 1})
        with patch("subprocess.run", side_effect=run):
            with pytest.raises(RuntimeError, match="pull"):
                git_commit_push()
        assert "push" not in [c[1] for c in calls]

    def test_git_commit_push_diff_error_raises(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        run, _ = self._fake_git({"diff": 128})
        with patch("subprocess.run", side_effect=run):
            with pytest.raises(RuntimeError, match="diff"):
                git_commit_push()

    # ---- main ----
    def test_main_workflow(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summarize, "PROCESSED_FILE", str(tmp_path / "processed.txt"))
        monkeypatch.setattr(summarize, "OUTPUT_FEED", str(tmp_path / "feed.xml"))
        monkeypatch.setattr(summarize, "RSS_SOURCES", ["https://example.com/feed"])

        with patch("summarize.process_source", return_value=("Example", "Summary text")) as mock_proc, \
             patch("summarize.generate_rss_xml") as mock_gen_xml, \
             patch("summarize.git_commit_push"):
            main()
            mock_gen_xml.assert_called_once_with([("Example", "Summary text")])
            mock_proc.assert_called_once_with("https://example.com/feed", ANY, "Example")

    def test_main_passes_unique_labels(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summarize, "PROCESSED_FILE", str(tmp_path / "processed.txt"))
        monkeypatch.setattr(summarize, "OUTPUT_FEED", str(tmp_path / "feed.xml"))
        monkeypatch.setattr(summarize, "RSS_SOURCES", ["https://a.blog.com/f", "https://b.blog.com/f"])

        with patch("summarize.process_source", return_value=None) as mock_proc:
            main()
        labels = {c[0][0]: c[0][2] for c in mock_proc.call_args_list}
        assert labels == {"https://a.blog.com/f": "a.blog.com", "https://b.blog.com/f": "b.blog.com"}

    def test_main_git_failure_propagates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summarize, "PROCESSED_FILE", str(tmp_path / "processed.txt"))
        monkeypatch.setattr(summarize, "RSS_SOURCES", ["https://example.com/feed"])

        with patch("summarize.process_source", return_value=("Example", "S")), \
             patch("summarize.generate_rss_xml"), \
             patch("summarize.git_commit_push", side_effect=RuntimeError("push failed")):
            with pytest.raises(RuntimeError, match="push failed"):
                main()

    def test_main_no_rss_sources(self, monkeypatch):
        monkeypatch.setattr(summarize, "RSS_SOURCES", [])
        with patch("summarize.process_source") as mock_proc:
            main()
            mock_proc.assert_not_called()

    def test_main_no_new_summaries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summarize, "PROCESSED_FILE", str(tmp_path / "processed.txt"))
        monkeypatch.setattr(summarize, "RSS_SOURCES", ["https://example.com/feed"])

        with patch("summarize.process_source", return_value=None), \
             patch("summarize.generate_rss_xml") as mock_gen_xml:
            main()
            mock_gen_xml.assert_not_called()
