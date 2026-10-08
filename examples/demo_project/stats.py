"""演示缺陷：未使用的导入与字面量除零。"""
import math


def ratio(total):
    return total / 0
