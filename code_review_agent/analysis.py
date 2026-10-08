"""Deterministic, evidence-backed checks; intentionally not a full Python linter."""
from __future__ import annotations

import ast
import hashlib
import re
from typing import Any

SUGGESTIONS = {
    'mutable-default': '将可变默认值改为 None，在函数内创建新的容器。',
    'bare-except': '捕获具体异常；需要重新抛出时使用 raise，不要捕获退出信号。',
    'swallowed-exception': '记录或重新抛出异常，避免忽略真实错误。',
    'div-zero': '修正分母；对于动态分母，明确处理为零的情况。',
    'unused-import': '确认不是公开重导出后，移除未使用的导入。',
    'open-without-with': '使用 with open(...) as file 管理文件生命周期。',
    'syntax-error': '先修复语法错误，再继续审查。',
}


def analyze_python(source: str, filename: str = '<source>') -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    scopes = ['<module>']
    occurrences: dict[str, int] = {}

    def add(code: str, node: ast.AST, message: str, severity: str, evidence: str | None = None) -> None:
        text = evidence if evidence is not None else ast.get_source_segment(source, node) or ''
        scope = '/'.join(scopes)
        normalized = re.sub(r'\s+', '', text)
        anchor = f'{code}|{scope}|{message}|{normalized}'
        occurrences[anchor] = occurrences.get(anchor, 0) + 1
        fingerprint = hashlib.sha256(f'{anchor}|{occurrences[anchor]}'.encode()).hexdigest()[:24]
        issues.append(dict(code=code, severity=severity, line=getattr(node, 'lineno', 1),
                           end_line=getattr(node, 'end_lineno', getattr(node, 'lineno', 1)),
                           message=message, evidence=text, scope=scope, fingerprint=fingerprint,
                           confidence='confirmed' if code in {'syntax-error', 'mutable-default', 'div-zero'} else 'suspected',
                           suggestion=SUGGESTIONS[code]))

    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        node = ast.Pass(lineno=exc.lineno or 1, end_lineno=exc.lineno or 1)
        add('syntax-error', node, f'语法错误: {exc.msg}', '高', (exc.text or '').strip())
        return dict(syntax_ok=False, issues=issues, symbols=[], scopes=['<module>'])

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    known_scopes = ['<module>']

    class Visitor(ast.NodeVisitor):
        def function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            symbols.append(dict(type='function', name=node.name, line=node.lineno))
            scopes.append(node.name)
            known_scopes.append('/'.join(scopes))
            positional = [*node.args.posonlyargs, *node.args.args]
            pairs = list(zip(positional[-len(node.args.defaults):], node.args.defaults)) if node.args.defaults else []
            pairs.extend((arg, default) for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is not None)
            for arg, default in pairs:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    add('mutable-default', default, f'参数 {arg.arg} 使用了可变默认值', '高')
            self.generic_visit(node)
            scopes.pop()

        visit_FunctionDef = function
        visit_AsyncFunctionDef = function

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            symbols.append(dict(type='class', name=node.name, line=node.lineno))
            scopes.append(node.name)
            known_scopes.append('/'.join(scopes))
            self.generic_visit(node)
            scopes.pop()

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is None:
                add('bare-except', node, '裸 except 捕获所有异常，包括退出信号', '中')
            elif isinstance(node.type, ast.Name) and node.type.id == 'Exception' and all(isinstance(stmt, ast.Pass) for stmt in node.body):
                add('swallowed-exception', node, '捕获 Exception 后 pass，可能掩盖真实错误', '中')
            self.generic_visit(node)

        def visit_BinOp(self, node: ast.BinOp) -> None:
            if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and isinstance(node.right, ast.Constant) and node.right.value == 0:
                add('div-zero', node, '除法或取余的分母为字面量 0', '高')
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == 'open':
                parent = parents.get(node)
                # Only the actual context expression gets credit, not another open in its body.
                in_with = isinstance(parent, ast.withitem) and parent.context_expr is node
                managed = in_with
                owner = node
                while parents.get(owner) is not None and not isinstance(parents[owner], (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                    owner = parents[owner]
                region = parents.get(owner, tree)
                assignment = parents.get(node)
                names: set[str] = set()
                if isinstance(assignment, ast.Assign):
                    names = {target.id for target in assignment.targets if isinstance(target, ast.Name)}
                for other in ast.walk(region):
                    if isinstance(other, ast.Call) and isinstance(other.func, ast.Attribute) and other.func.attr == 'close' and isinstance(other.func.value, ast.Name) and other.func.value.id in names:
                        managed = True
                if not managed:
                    add('open-without-with', node, 'open() 未见上下文管理或对应句柄的 close()，可能泄漏资源', '中')
            self.generic_visit(node)

    Visitor().visit(tree)
    # Import checks are scope-aware and deliberately exclude __init__ re-exports.
    if not filename.endswith('__init__.py'):
        for region in [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))]:
            nodes: list[ast.AST] = []
            def collect(node: ast.AST) -> None:
                nodes.append(node)
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if node is region:
                            # Global imports can be referenced inside nested functions.
                            nodes.extend(ast.walk(child))
                        continue
                    collect(child)
            collect(region)
            used = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            exported: set[str] = set()
            for n in nodes:
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__all__' for t in n.targets):
                    exported.update(x.value for x in ast.walk(n.value) if isinstance(x, ast.Constant) and isinstance(x.value, str))
            # Only imports directly belonging to this lexical region.
            for n in ast.walk(region):
                if not isinstance(n, (ast.Import, ast.ImportFrom)) or isinstance(n, ast.ImportFrom) and n.module == '__future__':
                    continue
                ancestor = parents.get(n)
                while ancestor is not None and not isinstance(ancestor, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    ancestor = parents.get(ancestor)
                if ancestor is not region:
                    continue
                lineage = []
                ancestor = region
                while ancestor is not None:
                    if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        lineage.append(ancestor.name)
                    ancestor = parents.get(ancestor)
                scopes[:] = ['<module>', *reversed(lineage)]
                for alias in n.names:
                    name = alias.asname or (alias.name.split('.')[0] if isinstance(n, ast.Import) else alias.name)
                    if name != '*' and name not in used | exported:
                        add('unused-import', n, f'导入 {name} 后未使用', '低')
    issues.sort(key=lambda i: (i['line'], i['code']))
    return dict(syntax_ok=True, issues=issues, symbols=symbols, scopes=known_scopes)
