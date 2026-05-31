#!/usr/bin/env python3
"""
自动更新热门主题推荐
从 NASA API 和其他科学资源获取最新主题
"""
import json
import os
import random
from datetime import datetime

def generate_fresh_topics():
    """生成新的主题推荐（示例实现）"""

    # 这里可以接入真实的 API，比如：
    # - NASA API: https://api.nasa.gov/
    # - arXiv API: https://arxiv.org/help/api/
    # - 科学新闻 RSS

    # 当前使用预定义的主题池作为示例
    topic_pool = [
        {
            "title": "如果太阳突然消失，地球会怎样？",
            "category": "太阳系",
            "angle": "从引力波速度讲到8分钟后的黑暗和轨道混乱。",
            "source": "NASA Solar System",
            "source_url": "https://science.nasa.gov/solar-system/",
            "evergreen": True
        },
        {
            "title": "为什么月球总是同一面对着地球？",
            "category": "太阳系",
            "angle": "从潮汐锁定讲到月球自转和公转的完美同步。",
            "source": "NASA Moon",
            "source_url": "https://science.nasa.gov/moon/",
            "evergreen": True
        },
        {
            "title": "宇宙的温度是多少？",
            "category": "宇宙",
            "angle": "从宇宙微波背景辐射讲到绝对零度之上2.7K的余温。",
            "source": "NASA Cosmic Background",
            "source_url": "https://science.nasa.gov/universe/",
            "evergreen": True
        },
        {
            "title": "光速真的是宇宙的速度极限吗？",
            "category": "量子",
            "angle": "从相对论讲到量子纠缠和信息传递的边界。",
            "source": "Physics Today",
            "source_url": "https://science.nasa.gov/",
            "evergreen": True
        },
        {
            "title": "如果时间倒流，宇宙会发生什么？",
            "category": "时间",
            "angle": "从热力学第二定律讲到熵增和时间箭头的不可逆。",
            "source": "Thermodynamics",
            "source_url": "https://science.nasa.gov/",
            "evergreen": True
        },
        {
            "title": "中子星有多重？",
            "category": "恒星",
            "angle": "从一勺中子星物质重达10亿吨讲到极端密度的世界。",
            "source": "NASA Neutron Stars",
            "source_url": "https://science.nasa.gov/universe/",
            "evergreen": True
        },
        {
            "title": "宇宙中最冷的地方在哪里？",
            "category": "宇宙",
            "angle": "从回旋镖星云讲到比宇宙背景辐射还冷的地方。",
            "source": "NASA Extreme Universe",
            "source_url": "https://science.nasa.gov/universe/",
            "evergreen": True
        },
        {
            "title": "为什么火星是红色的？",
            "category": "太阳系",
            "angle": "从氧化铁讲到火星表面的铁锈和古老的水痕迹。",
            "source": "NASA Mars",
            "source_url": "https://science.nasa.gov/mars/",
            "evergreen": True
        },
        {
            "title": "黑洞会死亡吗？",
            "category": "黑洞",
            "angle": "从霍金辐射讲到黑洞蒸发和最终的消失。",
            "source": "Hawking Radiation",
            "source_url": "https://science.nasa.gov/universe/black-holes/",
            "evergreen": True
        },
        {
            "title": "宇宙有边界吗？",
            "category": "宇宙",
            "angle": "从可观测宇宙讲到视界之外的未知领域。",
            "source": "NASA Universe",
            "source_url": "https://science.nasa.gov/universe/",
            "evergreen": True
        }
    ]

    # 随机选择一些主题，添加时间戳
    today = datetime.now().strftime('%Y-%m-%d')
    selected = random.sample(topic_pool, min(5, len(topic_pool)))

    for topic in selected:
        topic['updated_at'] = today
        topic['evergreen'] = random.choice([True, False])

    return selected

def update_topic_recommendations(file_path):
    """更新主题推荐文件"""

    # 读取现有主题
    existing_topics = []
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                existing_topics = json.load(f)
        except:
            existing_topics = []

    # 获取新主题
    new_topics = generate_fresh_topics()

    # 合并：新主题放在前面，去重
    existing_titles = {t.get('title') for t in existing_topics}
    fresh_topics = [t for t in new_topics if t.get('title') not in existing_titles]

    # 组合：新主题 + 现有主题，限制总数
    all_topics = fresh_topics + existing_topics
    all_topics = all_topics[:30]  # 保留最多30条

    # 写回文件
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(all_topics, f, ensure_ascii=False, indent=2)

    return len(fresh_topics)

if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(script_dir, '..', 'topic_recommendations.json')

    count = update_topic_recommendations(file_path)
    print(f'✅ 已添加 {count} 条新主题')
