"""进程级控制台日志：采集、对话、后台任务统一打到 stderr。

此前业务代码零日志：后台线程与 except 吞掉的异常只剩任务状态里一句中文提示，
控制台只有 uvicorn 的访问行，出错只能盲猜。导入 jobscope.main 时装配一次；
JOBSCOPE_LOG_LEVEL=DEBUG 可调到调试级别。幂等：重复导入/多测试实例不会叠加 handler。

颜色沿用 colorlog，配色参考 loguru 默认方案：时间戳绿、模块名青、等级分色
（TRACE 暗青 / DEBUG 蓝 / INFO 加粗白 / WARNING 黄 / ERROR 红 / CRITICAL 粗红）。
TTY 才上色，管道/重定向到文件自动退回纯文本；未安装 colorlog 时退回纯格式。
"""
import logging
import os
import sys

_PLAIN_FORMAT = '%(asctime)s %(levelname)-7s %(name)s: %(message)s'
_COLOR_FORMAT = ('%(time_log_color)s%(asctime)s %(log_color)s%(levelname)-7s%(reset)s '
                 '%(name_log_color)s%(name)s%(reset)s: %(message)s')

# loguru 默认等级配色（SUCCESS 绿不适用，本库没有该等级）。
_LEVEL_COLORS = {'TRACE': 'thin_cyan', 'DEBUG': 'blue', 'INFO': 'bold_white',
                 'WARNING': 'yellow', 'ERROR': 'red', 'CRITICAL': 'bold_red'}
_LEVELS = ('CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG', 'TRACE')


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
    root.setLevel(getattr(logging, os.getenv('JOBSCOPE_LOG_LEVEL', 'INFO').upper(), logging.INFO))
    return root
