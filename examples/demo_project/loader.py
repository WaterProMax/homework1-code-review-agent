"""演示缺陷：文件没有明确关闭，异常被忽略。"""


def load_config(path):
    handle = open(path, encoding='utf-8')
    try:
        return handle.read()
    except Exception:
        pass
