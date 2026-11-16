"""Chunking strategy behavior; semantics ported from kb-kit, fixtures without model services."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobscope.kb_chunking import (chunk_text, clamp_overlap, clamp_size, format_for,
                                  hard_slice, llm_chunks, markdown_sections, punct_chunks,
                                  recursive_chunks, semantic_chunks, split_sentences)


class ThemeEmbedder:
    """确定性嵌入：苹果句与手机句分属两个正交主题，主题内部距离 0。"""

    def embed(self, texts):
        return [[1.0, 0.0] if ('苹果' in text or '水果' in text) else [0.0, 1.0] for text in texts]


def test_param_clamping():
    assert clamp_size(1) == 64 and clamp_size(99999) == 2000 and clamp_size('bad') == 500
    assert clamp_overlap(9999, 500) == 125 and clamp_overlap(-5, 500) == 0 and clamp_overlap('bad', 500) == 50
    assert format_for('笔记.MD') == 'markdown' and format_for('doc.docx') == 'plain' and format_for(None) == 'plain'


def test_hard_slice_windows_and_overlap():
    assert hard_slice('短文本', 10, 3) == ['短文本']
    pieces = hard_slice('x' * 25, 10, 0)
    assert pieces == ['x' * 10, 'x' * 10, 'x' * 5]
    overlapped = hard_slice('x' * 25, 10, 4)
    # 滑窗步长 size-overlap：相邻块共享 4 个字符，覆盖到尾。
    assert overlapped[0] == 'x' * 10 and overlapped[1].startswith('x' * 6) and ''.join(overlapped).count('x') >= 25


def test_punct_chunks_merge_and_fallback():
    content = '第一句。第二句。第三句。第四句。'
    # 片段合并时以换行连接（kb-kit push 同款）；顺序合并到 size 上限。
    assert punct_chunks(content, 10, 0) == ['第一句。\n第二句。', '第三句。\n第四句。']
    # 超限片段滑窗兜底
    long_segment = '超' * 30 + '。'
    assert all(len(piece) <= 20 for piece in punct_chunks(long_segment, 20, 0))
    # 自定义分隔符
    custom = punct_chunks('a\n\nb', 10, 0, separators=['\n\n'])
    assert custom == ['a', 'b']


def test_recursive_respects_structure_and_size_cap():
    content = '段落一的内容。\n\n段落二的内容。\n\n段落三的内容。'
    chunks = recursive_chunks(content, 20, 0)
    assert len(chunks) >= 2 and all(len(chunk) <= 20 for chunk in chunks)
    assert '段落一' in chunks[0]


def test_markdown_sections_keep_heading_breadcrumb():
    content = '''# 指南
总述内容。

## 安装
安装说明，步骤较多足够长的一段正文，用来验证分块带标题。

## 配置
配置说明。'''
    sections = markdown_sections(content)
    assert [heading for heading, _ in sections] == ['指南', '指南 > 安装', '指南 > 配置']
    chunks = recursive_chunks(content, 500, 0, format='markdown')
    assert any(chunk.startswith('指南 > 安装') for chunk in chunks)


def test_semantic_chunks_split_at_theme_boundary():
    # 每个主题块都要长过 minimum=size/4，否则整篇合成一块才是正确行为。
    apple = ['苹果是一种常见水果，品种繁多，产地遍布温带地区。', '苹果产量集中在秋季，贮藏后可以供应全年市场。',
             '水果富含维生素，苹果也不例外，日常食用有益健康。', '苹果种植需要修剪、疏果和病虫害防治等农艺管理。',
             '果汁、果酱和苹果干是苹果深加工的主要产品形态。', '苹果出口量在水果贸易中长期占据前列位置。']
    phone = ['手机是更新最快的数码产品，换代周期通常不到一年。', '手机芯片制程逐年推进，性能与功耗持续再平衡。',
             '影像系统是手机竞争的核心卖点，计算摄影成为主流。', '手机操作系统生态决定应用分发与开发者收益格局。',
             '折叠屏、卫星通信等新形态正在改写手机的产品定义。', '手机换机周期拉长，保值率成为购机的重要考量。']
    content = ''.join(apple) + ''.join(phone)
    chunks = semantic_chunks(content, 500, 0, 'plain', ThemeEmbedder())
    assert len(chunks) == 2
    assert '苹果' in chunks[0] and '手机' in chunks[1] and '苹果' not in chunks[1]


def test_chunk_text_fallbacks_are_explicit():
    content = '句子一。句子二。句子三。句子四。句子五。'
    # semantic/llm 依赖缺失：显式回退 recursive，不假装语义切分。
    chunks, effective = chunk_text(content, strategy='semantic', embedder=None)
    assert effective == 'recursive'
    chunks, effective = chunk_text(content, strategy='llm', chat=None)
    assert effective == 'recursive'
    # hard / punct 生效
    _, effective = chunk_text(content, strategy='hard', size=20, overlap=0)
    assert effective == 'hard'
    _, effective = chunk_text(content, strategy='punct', size=20, overlap=0)
    assert effective == 'punct'


def test_llm_chunks_use_reported_boundaries():
    # 每句约 45 字，两组各 3 句都超过 minimum=size/4，边界切分才能保留为两块。
    sentence = lambda index: f'这是第{index}段长句，主题{"甲" if index <= 3 else "乙"}的内容，包含足够的细节信息量用于测试。'
    content = ''.join(sentence(index) for index in range(1, 7))
    chat = lambda prompt: '响应前缀 [3] 响应后缀'  # 第 3 句之后开新主题
    chunks = llm_chunks(content, 500, 'plain', chat)
    assert len(chunks) == 2 and '第3段' in chunks[0] and '第4段' in chunks[1]
    # 模型输出异常时该窗口不切分：整篇一组
    broken = lambda prompt: '完全不是 JSON'
    assert len(llm_chunks(content, 500, 'plain', broken)) == 1


def test_split_sentences_keeps_punctuation():
    # 在句末标点/换行之后切分（kb-kit 同款 lookbehind），strip 后换行归入前句句尾。
    assert split_sentences('你好。世界！\n下一行；结束') == ['你好。', '世界！', '下一行；', '结束']
