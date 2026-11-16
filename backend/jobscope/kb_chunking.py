"""Knowledge-base chunking strategies; semantics ported from kb-kit (packages/core/src/chunk.ts).

- recursive（默认）：中文感知分隔符递归切分；markdown 走标题层级切分（块保留所属标题）
- hard：固定窗口 + 滑动重叠强切（Dify「通用」分段同款）
- punct：分隔符切段，顺序合并到 size 上限，超限滑窗兜底（FastGPT/Dify 分隔符分段同款）
- semantic：逐句嵌入，相邻余弦距离 z-score 断点切分（Kamradt L4 同款）；需嵌入服务
- llm：句子窗口送对话模型判主题边界；需对话模型
依赖缺失时由调用方显式回退 recursive，并区分 requested 与 effective。
"""

import json
import re
import statistics

STRATEGIES = ('recursive', 'hard', 'punct', 'semantic', 'llm')
PUNCT_SEPARATORS = ['\n\n', '\n', '。', '！', '？', '；']
# 中文感知的递归分隔符优先级：空行 → 换行 → 句末标点 → 从句标点 → 空格 → 硬切
PLAIN_SEPARATORS = ['\n\n', '\n', '。', '！', '？', '；', '，', ' ', '']
MARKDOWN_EXTENSIONS = {'.md', '.markdown', '.mdx'}
# LLM 分块的句子窗口（kb-kit 同值）：越小边界越全，调用越多。
LLM_WINDOW = 32
SIZE_RANGE = (64, 2000)


def clamp_size(size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return 500
    return min(SIZE_RANGE[1], max(SIZE_RANGE[0], size))


def clamp_overlap(overlap, size):
    try:
        overlap = int(overlap)
    except (TypeError, ValueError):
        return 50
    return min(max(0, size // 4), max(0, overlap))


def format_for(filename):
    from pathlib import PurePosixPath
    return 'markdown' if PurePosixPath(str(filename or '').replace('\\', '/')).suffix.lower() in MARKDOWN_EXTENSIONS else 'plain'


def split_sentences(text):
    return [s for s in (part.strip() for part in re.split(r'(?<=[。！？!?；;\n])', text)) if s]


def hard_slice(text, size, overlap):
    """固定窗口按 overlap 滑动直切；重叠步进覆盖到尾。"""
    if len(text) <= size:
        return [text]
    if overlap <= 0:
        return [text[start:start + size] for start in range(0, len(text), size)]
    step = size - overlap
    out = []
    for start in range(0, len(text), step):
        out.append(text[start:start + size])
        if start + size >= len(text):
            break
    return out


def _merge_pieces(pieces, size, overlap):
    """把小块顺序合并到 size 上限；相邻块间按 overlap 字符重叠（前块尾部）。"""
    merged = []
    buffer = ''
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) > size:
            if buffer:
                merged.append(buffer)
                buffer = ''
            merged.extend(hard_slice(piece, size, overlap))
            continue
        if buffer and len(buffer) + 1 + len(piece) > size:
            merged.append(buffer)
            tail = buffer[-overlap:] if overlap > 0 else ''
            buffer = (tail + '\n' + piece) if tail else piece
            # 重叠前缀也可能直接超限：保证块仍不超过 size。
            buffer = buffer[:size] if len(buffer) > size else buffer
        else:
            buffer = f'{buffer}\n{piece}' if buffer else piece
    if buffer.strip():
        merged.append(buffer)
    return [block.strip() for block in merged if block.strip()]


def recursive_chunks(content, size, overlap, format='plain'):
    if format == 'markdown':
        # markdown 按标题层级独立切节（LangChain MarkdownTextSplitter 同款），节间不合并；
        # 块保留所属标题面包屑，检索命中自带上下文。
        pieces = []
        for heading, section in markdown_sections(content):
            for block in _merge_pieces(_recursive_split(section, list(PLAIN_SEPARATORS), size), size, 0):
                pieces.append(f'{heading}\n{block}' if heading and not block.startswith(heading) else block)
        return [piece.strip() for piece in pieces if piece.strip()]
    return _merge_pieces(_recursive_split(content, list(PLAIN_SEPARATORS), size), size, overlap)


def _recursive_split(text, separators, size):
    """LangChain 递归切分语义：用第一个真实存在的分隔符切，超长片段继续降级，直至硬切单字。"""
    if len(text) <= size:
        return [text]
    if not separators:
        return hard_slice(text, size, 0)
    separator, rest = separators[0], separators[1:]
    if separator == '':
        return hard_slice(text, size, 0)
    if separator not in text:
        return _recursive_split(text, rest, size)
    parts = text.split(separator)
    # 分隔符保留在段尾：句子边界对检索与展示更完整。
    pieces = [part + separator for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    out = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) <= size:
            out.append(piece)
        else:
            out.extend(_recursive_split(piece, rest, size))
    return out


def markdown_sections(content):
    """按标题层级切节；返回 (标题面包屑, 节文本)。无标题的散文整体返回。"""
    lines = content.split('\n')
    sections, stack, buffer = [], [], []
    heading_re = re.compile(r'^(#{1,6})\s+(.*)$')

    def flush():
        if any(part.strip() for part in buffer):
            sections.append((' > '.join(title for _, title in stack), '\n'.join(buffer).strip()))

    for line in lines:
        match = heading_re.match(line.strip())
        if match:
            flush()
            level, title = len(match.group(1)), match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            buffer = [line]
        else:
            buffer.append(line)
    flush()
    if not sections:
        return [('', content)]
    return sections


def punct_chunks(content, size, overlap, separators=None):
    seps = [s for s in (separators or PUNCT_SEPARATORS) if s]
    segments = [content]
    for sep in seps:
        if sep in content:
            parts = content.split(sep)
            segments = [part.strip() + sep for part in parts[:-1]] + ([parts[-1]] if parts[-1].strip() else [])
            segments = [part.strip() for part in segments if part.strip()]
            break
    return _merge_pieces(segments, size, overlap)


def cosine_distance(a, b):
    numerator = sum(x * y for x, y in zip(a, b))
    left = sum(x * x for x in a) ** .5
    right = sum(x * x for x in b) ** .5
    if not left or not right:
        return 1.0
    return 1.0 - numerator / (left * right)


def semantic_chunks(content, size, overlap, format, embedder):
    """逐句嵌入 + 相邻余弦距离 z-score 断点（Kamradt L4）；embedder 由调用方保证已配置。"""
    sentences = split_sentences(content)
    if len(sentences) <= 1:
        return [content] if content.strip() else []
    vectors = embedder.embed(sentences)
    distances = [cosine_distance(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)]
    threshold = statistics.mean(distances) + statistics.pstdev(distances) if len(distances) > 1 else float('inf')
    groups, current = [], [sentences[0]]
    for index, distance in enumerate(distances):
        if distance > threshold:
            groups.append(current)
            current = []
        current.append(sentences[index + 1])
    if current:
        groups.append(current)
    minimum = max(1, size // 4)
    merged, carry = [], ''
    for group in groups:
        text = (carry + ''.join(group)).strip()
        carry = ''
        if len(text) < minimum and merged:
            merged[-1] += text
            continue
        if len(text) < minimum:
            carry = text
            continue
        merged.append(text)
    if carry:
        if merged:
            merged[-1] += carry
        else:
            merged = [carry]
    out = []
    for text in merged:
        out.extend(hard_slice(text, size, overlap) if len(text) > size else [text])
    return [block.strip() for block in out if block.strip()]


LLM_SYSTEM = '你是文档切分助手。只在主题真正改变处标记边界；同一主题的展开、举例、补充细节不算新主题。只返回一个 JSON 数组，元素是句子编号（数字），例如 [3,7]；没有边界返回 []。不要输出任何解释。'


def llm_chunks(content, size, format, chat):
    """句子按窗口送对话模型标边界；窗口失败该窗不切（kb-kit 同款折衷）。"""
    sentences = split_sentences(content)
    if len(sentences) <= 1:
        return [content] if content.strip() else []
    breaks = set()
    for start in range(0, len(sentences), LLM_WINDOW):
        window = sentences[start:start + LLM_WINDOW]
        lines = '\n'.join(f'{index + 1}. {sentence}' for index, sentence in enumerate(window))
        try:
            raw = chat(LLM_SYSTEM + '\n\n' + lines)
            match_start, match_end = raw.find('['), raw.rfind(']')
            if match_start < 0 or match_end <= match_start:
                raise ValueError('响应不含 JSON 数组')
            parsed = json.loads(raw[match_start:match_end + 1])
            for value in parsed:
                number = int(value)
                if 1 <= number <= len(window) - 1:
                    breaks.add(start + number)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue  # 该窗口边界不可用：保持整窗一组，不中断导入
    groups, current = [], []
    for index, sentence in enumerate(sentences):
        if index in breaks and current:
            groups.append(current)
            current = []
        current.append(sentence)
    if current:
        groups.append(current)
    minimum = max(1, size // 4)
    merged = []
    for group in groups:
        text = ''.join(group).strip()
        if merged and len(text) < minimum:
            merged[-1] += text
            continue
        merged.append(text)
    out = []
    for text in merged:
        out.extend(recursive_chunks(text, size, 0, format) if len(text) > size else [text])
    return [block.strip() for block in out if block.strip()]


def chunk_text(content, *, strategy='recursive', size=500, overlap=50, separators=None,
               format='plain', embedder=None, chat=None):
    """统一入口；返回 (chunks, effective_strategy)。semantic/llm 依赖缺失时回退 recursive。"""
    size = clamp_size(size)
    overlap = clamp_overlap(overlap, size)
    if strategy == 'semantic' and embedder is not None:
        return semantic_chunks(content, size, overlap, format, embedder), 'semantic'
    if strategy == 'llm' and chat is not None:
        return llm_chunks(content, size, format, chat), 'llm'
    if strategy == 'hard':
        return [block.strip() for block in hard_slice(content, size, overlap) if block.strip()], 'hard'
    if strategy == 'punct':
        return punct_chunks(content, size, overlap, separators), 'punct'
    return recursive_chunks(content, size, overlap, format), 'recursive'
