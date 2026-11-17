"""进程级控制台日志：采集、对话、后台任务统一打到 stderr。

此前业务代码零日志：后台线程与 except 吞掉的异常只剩任务状态里一句中文提示，
控制台只有 uvicorn 的访问行，出错只能盲猜。导入 jobscope.main 时装配一次；
JOBSCOPE_LOG_LEVEL=DEBUG 可调到调试级别。幂等：重复导入/多测试实例不会叠加 handler。

颜色沿用 colorlog，配色参考 loguru 默认方案：时间戳绿、模块名青、等级分色
（TRACE 暗青 / DEBUG 蓝 / INFO 加粗白 / WARNING 黄 / ERROR 红 / CRITICAL 粗红）。
TTY 才上色，管道/重定向到文件自动退回纯文本；未安装 colorlog 时退回纯格式。

同一份记录同时进进程内环形缓冲（_RING）：前端「运行日志」页经 main 的 /api/logs
轮询这份缓冲。只保留最近 RING_SIZE 条、服务重启即清空、不落盘——它是「看现场」
工具，历史问题的排查仍以任务记录与控制台为准。
"""
import collections
import itertools
import logging
import os
import sys
import time

_FORMAT = '%(asctime)s %(levelname)-7s %(name)s: %(message)s'
_COLOR_FORMAT = ('%(time_log_color)s%(asctime)s %(log_color)s%(levelname)-7s%(reset)s '
                 '%(name_log_color)s%(name)s%(reset)s: %(message)s')

# loguru 默认等级配色（SUCCESS 绿不适用，本库没有该等级）。
_LEVEL_COLORS = {'TRACE': 'thin_cyan', 'DEBUG': 'blue', 'INFO': 'bold_white',
                 'WARNING': 'yellow', 'ERROR': 'red', 'CRITICAL': 'bold_red'}
_LEVELS = ('CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG', 'TRACE')

RING_SIZE = 2000   # 环形缓冲条数上限：够回看一次采集/对话全程，又不至于占用过多内存。
MESSAGE_CAP = 4000  # 单条上限：异常堆栈完整保留，超长消息截断（正常远达不到）。
_RING = collections.deque(maxlen=RING_SIZE)
_SEQ = itertools.count(1)


class _RingHandler(logging.Handler):
    """把每条到达挂载点的记录按序存进环形缓冲，供 /api/logs 轮询。"""

    def __init__(self):
        super().__init__()
        # %(message)s：exc_info 的堆栈由 Formatter 自动附加在消息尾部，一条存完整文本。
        self.setFormatter(logging.Formatter('%(message)s'))

    def emit(self, record):
        try:
            message = self.format(record)
            # 日志页每 2 秒轮询一次 /api/logs；uvicorn.access 行若照收，页面会一直滚出
            # 自己的请求噪声。只对访问行做这一个例外，其余访问行照常入缓冲。
            if record.name == 'uvicorn.access' and '/api/logs' in message:
                return
            _RING.append(dict(seq=next(_SEQ), ts=time.strftime('%Y-%m-%d %H:%M:%S'),
                              level=record.levelname, logger=record.name,
                              message=message[:MESSAGE_CAP]))
        except Exception:
            self.handleError(record)


def recent_logs(after=0, level='', q='', limit=500):
    """环形缓冲快照：seq 大于 after 的增量；等级（精确）、关键词、条数在快照上过滤。

    latest 恒取整份缓冲的最大 seq（与过滤无关），调用方拿它当下一轮的 after，
    过滤条件变化也不会漏掉增量。
    """
    entries = list(_RING)
    result = [entry for entry in entries if entry['seq'] > after]
    if level:
        result = [entry for entry in result if entry['level'] == level]
    if q:
        needle = q.lower()
        result = [entry for entry in result
                  if needle in entry['message'].lower() or needle in entry['logger'].lower()]
    return dict(entries=result[-int(limit):], latest=entries[-1]['seq'] if entries else 0)


def _handler():
    try:
        import colorlog
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_PLAIN_FORMAT))
        return handler
    handler = colorlog.StreamHandler(sys.stderr)  # Windows 下用 colorama 转义到终端。
    handler.setFormatter(colorlog.ColoredFormatter(
        _COLOR_FORMAT, stream=sys.stderr,  # 非 TTY 自动去色；FORCE_COLOR/NO_COLOR 环境变量可强制。
        log_colors=_LEVEL_COLORS,
        secondary_log_colors={'time': {level: 'green' for level in _LEVELS},
                              'name': {level: 'cyan' for level in _LEVELS}}))
    return handler


def setup():
    """装配根 Logger。Windows 管道/重定向下流编码常是 GBK，中文日志放宽为替换而非编码报错。"""
    root = logging.getLogger()
    if any(getattr(handler, '_jobscope_log', False) for handler in root.handlers):
        return root
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, 'encoding', '') or '').lower().replace('-', '')
        if encoding and encoding != 'utf8' and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(errors='replace')
            except (OSError, ValueError):
                pass  # pytest 捕获等非文本流：保持原样，编码交给消费方。
    handler = _handler()
    handler._jobscope_log = True
    root.addHandler(handler)
    ring = _RingHandler()
    root.addHandler(ring)
    # uvicorn 的自有 Logger propagate=False（自带 handler、独立配置），访问行与生命周期行
    # 要直接挂到这两个 Logger 上才进得了缓冲；uvicorn 未运行时 getLogger 只是占位，无害。
    for name in ('uvicorn', 'uvicorn.access'):
        logging.getLogger(name).addHandler(ring)
    root.setLevel(getattr(logging, os.getenv('JOBSCOPE_LOG_LEVEL', 'INFO').upper(), logging.INFO))
    return root
