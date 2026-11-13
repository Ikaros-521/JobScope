"""Channel defaults are suggestions; model availability comes from the account."""

CHANNELS = [
    dict(id='openai', name='OpenAI', url='https://api.openai.com/v1', protocol='openai', models=['gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-4.1', 'gpt-4.1-mini'], embeddings=['text-embedding-3-small', 'text-embedding-3-large']),
    dict(id='anthropic', name='Anthropic', url='https://api.anthropic.com/v1', protocol='anthropic', models=['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-4-6', 'claude-sonnet-4-6', 'claude-sonnet-4-5', 'claude-haiku-4-5'], embeddings=None),
    dict(id='deepseek', name='DeepSeek', url='https://api.deepseek.com/v1', protocol='openai', models=['deepseek-flash', 'deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-chat', 'deepseek-reasoner'], embeddings=None),
    dict(id='aliyun', name='阿里百炼', url='https://dashscope.aliyuncs.com/compatible-mode/v1', protocol='openai', models=['qwen3.7-plus', 'qwen3.6-plus', 'qwen3.6-flash', 'qwen-plus', 'qwen-turbo', 'qwen-max'], embeddings=['text-embedding-v3', 'text-embedding-v4']),
    dict(id='volcengine', name='火山方舟', url='https://ark.cn-beijing.volces.com/api/v3', protocol='openai', models=[], embeddings=[], note='部分服务需要填写控制台的接入点 ID（ep-…），请以账户实际开通情况为准。'),
    dict(id='zhipu', name='智谱', url='https://open.bigmodel.cn/api/paas/v4', protocol='openai', models=['glm-5.3', 'glm-5.1', 'glm-5', 'glm-4-plus', 'glm-4-flash'], embeddings=['embedding-3']),
    dict(id='moonshot', name='月之暗面', url='https://api.moonshot.cn/v1', protocol='openai', models=['kimi-k2', 'moonshot-v1-32k'], embeddings=None),
    dict(id='siliconflow', name='硅基流动', url='https://api.siliconflow.cn/v1', protocol='openai', models=['deepseek-ai/DeepSeek-V3'], embeddings=['BAAI/bge-m3', 'BAAI/bge-large-zh-v1.5']),
    dict(id='gemini', name='Google Gemini', url='https://generativelanguage.googleapis.com/v1beta/openai', protocol='openai', models=['gemini-4-argon'], embeddings=[]),
    dict(id='newapi', name='New API / One API', url='', protocol='openai', models=[], embeddings=[], note='填写你的中转站地址（通常以 /v1 结尾），再获取账户可用模型。'),
    dict(id='custom', name='自定义渠道', url='', protocol='openai', models=[], embeddings=[]),
]


MODEL_METADATA = {}
for model_ids, context_tokens, source in [
    (['gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.5', 'gpt-5.4'], 1_050_000, 'https://developers.openai.com/api/docs/models'),
    (['gpt-4.1'], 1_047_576, 'https://developers.openai.com/api/docs/models/gpt-4.1'),
    (['claude-sonnet-4-5'], 200_000, 'https://platform.claude.com/docs/en/models/overview'),
    (['gpt-5.4-mini'], 400_000, 'https://developers.openai.com/api/docs/models/gpt-5.4-mini'),
    (['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'], 1_000_000, 'https://platform.claude.com/docs/en/models/overview'),
    (['deepseek-flash', 'deepseek-v4-pro', 'deepseek-v4-flash'], 1_000_000, 'https://api-docs.deepseek.com/quick_start/pricing/'),
    (['qwen3.7-plus'], 1_000_000, 'https://help.aliyun.com/zh/model-studio/qwen3-7-plus'),
    (['glm-5.3'], 1_000_000, 'https://docs.bigmodel.cn/cn/guide/start/migrate-to-glm-new'),
    (['glm-5.1'], 200_000, 'https://docs.bigmodel.cn/cn/guide/models/text/glm-5.1'),
    (['gemini-4-argon'], 1_048_576, 'https://ai.google.dev/gemini-api/docs/models/gemini-4-argon'),
]:
    for model_id in model_ids:
        MODEL_METADATA[model_id] = dict(context_tokens=context_tokens, source=source, checked_at='2026-11-12')


def channel_by_id(name):
    for channel in CHANNELS:
        if channel['id'] == name:
            return channel
    raise ValueError('请选择有效渠道，私有网关请使用自定义渠道')
