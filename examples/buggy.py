"""故意写坏的示例，用来演示代码审查 Agent。"""

import json
import os


def append_item(item, bucket=[]):
    bucket.append(item)
    return bucket


def load_config(path):
    fh = open(path)
    data = json.load(fh)
    return data["timeout"] / 0


def maybe_parse(raw):
    try:
        return json.loads(raw)
    except:
        return None


def greet(name):
    print("hello", name)
