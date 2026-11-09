"""Channel defaults are suggestions; model availability comes from the account."""

CHANNELS = [
    dict(id='openai', name='OpenAI', url='https://api.openai.com/v1', protocol='openai', models=['gpt-4.1', 'gpt-4.1-mini'], embeddings=['text-embedding-3-small', 'text-embedding-3-large']),
    dict(id='anthropic', name='Anthropic', url='https://api.anthropic.com/v1', protocol='anthropic', models=['claude-sonnet-4-5', 'claude-haiku-4-5'], embeddings=None),
    dict(id='deepseek', name='DeepSeek', url='https://api.deepseek.com/v1', protocol='openai', models=['deepseek-chat', 'deepseek-reasoner'], embeddings=None),
    dict(id='aliyun', name='阿里百炼', url='https://dashscope.aliyuncs.com/compatible-mode/v1', protocol='openai', models=['qwen-plus', 'qwen-turbo', 'qwen-max'], embeddings=['text-embedding-v3', 'text-embedding-v4']),
    dict(id='volcengine', name='火山方舟', url='https://ark.cn-beijing.volces.com/api/v3', protocol='openai', models=[], embeddings=[], note='部分服务需要填写控制台的接入点 ID（ep-…），请以账户实际开通情况为准。'),
    dict(id='zhipu', name='智谱', url='https://open.bigmodel.cn/api/paas/v4', protocol='openai', models=['glm-4-plus', 'glm-4-flash'], embeddings=['embedding-3']),
    dict(id='moonshot', name='月之暗面', url='https://api.moonshot.cn/v1', protocol='openai', models=['kimi-k2', 'moonshot-v1-32k'], embeddings=None),
    dict(id='siliconflow', name='硅基流动', url='https://api.siliconflow.cn/v1', protocol='openai', models=['deepseek-ai/DeepSeek-V3'], embeddings=['BAAI/bge-m3', 'BAAI/bge-large-zh-v1.5']),
    dict(id='custom', name='自定义渠道', url='', protocol='openai', models=[], embeddings=[]),
]


def channel_by_id(name):
    for channel in CHANNELS:
        if channel['id'] == name:
            return channel
    raise ValueError('请选择有效渠道，私有网关请使用自定义渠道')
