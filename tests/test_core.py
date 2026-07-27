"""
核心逻辑测试 — 验证去重、状态机、评级等模块
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from processor.dedup import (
    extract_keywords,
    compute_fingerprint,
    jaccard_similarity,
    match_event,
)
from processor.state_machine import EventStateMachine
from processor.rater import EventRater
from storage.event_store import EventStore


def test_keyword_extraction():
    """测试关键词提取"""
    text = "柳螺香全国加盟政策调整，加盟费从5万降至3万"
    keywords = extract_keywords(text, top_k=5)
    assert len(keywords) > 0, "应提取到关键词"
    assert "柳螺香" in keywords or "加盟" in keywords, "应包含核心词"
    print(f"✅ 关键词提取: {keywords}")


def test_fingerprint():
    """测试事件指纹"""
    fp1 = compute_fingerprint("柳螺香", "渠道/扩张动作", ["加盟", "全国", "政策"])
    fp2 = compute_fingerprint("柳螺香", "渠道/扩张动作", ["加盟", "全国", "政策"])
    fp3 = compute_fingerprint("柳螺香", "渠道/扩张动作", ["新店", "开业", "南宁"])

    assert fp1 == fp2, "相同输入应生成相同指纹"
    assert fp1 != fp3, "不同输入应生成不同指纹"
    print(f"✅ 事件指纹: {fp1} (相同={fp1==fp2}, 不同={fp1!=fp3})")


def test_jaccard():
    """测试Jaccard相似度"""
    set_a = {"柳螺香", "加盟", "全国", "政策", "渠道/扩张动作"}
    set_b = {"柳螺香", "加盟", "全国", "费用", "渠道/扩张动作"}
    set_c = {"三品王", "新品", "牛肉粉", "上市", "新品牌/新产品出现"}

    sim_ab = jaccard_similarity(set_a, set_b)
    sim_ac = jaccard_similarity(set_a, set_c)

    assert sim_ab > 0.5, "相似集合应有较高相似度"
    assert sim_ac < 0.3, "不相似集合应有较低相似度"
    print(f"✅ Jaccard: 相似={sim_ab:.2f}, 不相似={sim_ac:.2f}")


def test_state_machine():
    """测试状态机"""
    sm = EventStateMachine()
    today = "2026-07-08"

    # 测试 NEW → ACTIVE
    event = {"status": "NEW", "last_seen": today}
    new_status = sm.on_new_hit(event, today)
    assert new_status == "ACTIVE", f"NEW再次命中应为ACTIVE，实际为{new_status}"

    # 测试评分升级
    escalate = sm.on_score_change({"status": "ACTIVE"}, 1, 4)
    assert escalate == "ESCALATED", "评分变动≥±2应升级"

    print(f"✅ 状态机: NEW→{new_status}, 升级={escalate}")


def test_rater():
    """测试评级打分器"""
    rater = EventRater()

    # Part 1 自身舆情
    event1 = {
        "base_score": 4,
        "circle": "自身品牌",
        "brand_id": "BR001",
        "published_at": "2026-07-08",
        "source": "官方",
    }
    score, reason = rater.rate_self_sentiment(event1)
    print(f"✅ 正向评级: score={score:+d}, reason={reason}")
    assert score >= 3, "正向事件应有较高评分"

    # Part 2 竞品威胁
    event2 = {
        "base_score": 3,
        "circle": "核心竞品",
        "brand_id": "BR003",
        "published_at": "2026-07-08",
        "source": "行业媒体",
    }
    score, reason = rater.rate_competitor_threat(event2)
    print(f"✅ 威胁评级: score={score:+d}, reason={reason}")
    assert score >= 2, "核心竞品威胁应有较高评分"


def test_event_store():
    """测试事件库CRUD"""
    import tempfile
    import os

    tmp_path = Path(tempfile.mktemp(suffix=".json"))
    store = EventStore(tmp_path)

    # 新增事件
    event = {
        "event_id": "EVT-TEST-001",
        "fingerprint": "test_fp_001",
        "brand_name": "测试品牌",
        "brand_id": "BR999",
        "circle": "核心竞品",
        "category": "渠道/扩张动作",
        "title": "测试事件",
        "summary": "这是一个测试事件",
        "keywords": ["测试", "事件"],
        "status": "NEW",
        "score": 0,
        "first_seen": "2026-07-08",
        "last_seen": "2026-07-08",
        "snapshots": [],
    }

    eid = store.add(event)
    assert store.total() == 1
    assert store.find_by_id("EVT-TEST-001") is not None

    # 更新
    store.update("EVT-TEST-001", {"status": "ACTIVE"})
    assert store.find_by_id("EVT-TEST-001")["status"] == "ACTIVE"

    # 快照
    store.add_snapshot("EVT-TEST-001", {"date": "2026-07-09", "type": "UPDATE", "title": "更新", "source": "测试"})
    evt = store.find_by_id("EVT-TEST-001")
    assert len(evt["snapshots"]) == 1

    # 删除
    store.delete("EVT-TEST-001")
    assert store.total() == 0

    # 清理
    os.remove(tmp_path)
    print("✅ 事件库CRUD: 增删改查均正常")


if __name__ == "__main__":
    print("=" * 50)
    print("三品王采集系统 — 核心逻辑测试")
    print("=" * 50)

    test_keyword_extraction()
    test_fingerprint()
    test_jaccard()
    test_state_machine()
    test_rater()
    test_event_store()

    print("=" * 50)
    print("✅ 全部测试通过!")
    print("=" * 50)
