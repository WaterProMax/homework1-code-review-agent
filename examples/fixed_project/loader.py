"""通过上下文管理关闭文件，让调用方处理读取异常。"""


def load_config(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()
