# Parsimonious 调用链分析：从 grammar 文本到访问完成

本文以一条同时包含**命名规则、量词、lookahead** 的 grammar 为例，跟踪它从构造、
parse 到 NodeVisitor 访问完成的完整调用链。所有行号以当前 commit
（`f7189d1` + 本次新增的测试）为准，文件指仓库根目录下的 `parsimonious/` 包。

示例 grammar 与输入：

```
greeting = "hi" name+ punct
name     = ~"[a-z]+"
punct    = &"!" "!"
```

```python
g = Grammar(<上述文本>)
tree = g.parse("hibob!")          # 构造 Node 树
MyVisitor().visit(tree)           # 访问并产出结果
```

## 一、构造阶段：grammar 文本 → 表达式图

1. `Grammar.__init__`（grammar.py:47）把字符串规则交给
   `_expressions_from_rules`（grammar.py:89），后者先用
   `rule_grammar.parse(rules)`（grammar.py:102）把 grammar 文本本身解析成一棵
   Node 树。`rule_grammar` 在 import 时两级自举：先用硬编码表达式构造
   `BootstrappingGrammar`（grammar.py:507，硬编码部分在 grammar.py:174-204，
   自举 parse 在 grammar.py:211），再用它解析 `rule_syntax` 得到正式的
   `rule_grammar`（grammar.py:512）。
2. `RuleVisitor(custom_rules).visit(tree)`（grammar.py:103）把这棵"语法的语法树"
   转换成表达式对象。它本身就是 `NodeVisitor` 的子类，走第三节描述的同一套
   visit 分发（nodes.py:194-225）。关键 visit 方法：
   - `visit_rule`（grammar.py:356）：给表达式赋 `name`。
   - `visit_quantified`（grammar.py:334）：`+` → `OneOrMore`，即
     `Quantifier(member, min=1, max=inf)`（expressions.py:472-473）；
     `{n,m}` 形式走 grammar.py:340-346。
   - `visit_lookahead_term`（grammar.py:348）：`&x` → `Lookahead(x)`；
     `visit_not_term`（grammar.py:352）：`!x` → `Lookahead(x, negative=True)`
     （expressions.py:416-417）。
   - `visit_reference`（grammar.py:393）：规则引用先放 `LazyReference`
     占位符（grammar.py:259），这是前向引用能被容忍的原因。
   - `visit_spaceless_literal`（grammar.py:416）→ `Literal`；
     `visit_regex`（grammar.py:402）→ `Regex`。
3. `visit_rules`（grammar.py:453）收尾：按名字建 `rule_map`（grammar.py:469，
   同名后定义覆盖先定义），自定义规则（kwargs 传入的）再覆盖字符串规则
   （grammar.py:474），然后对每个规则调 `resolve_refs`（grammar.py:477-481）：
   - `LazyReference.resolve_refs`（grammar.py:265-291）沿引用链走到具体表达式；
     成环抛 `BadGrammar`（grammar.py:283），未定义抛 `UndefinedLabel`
     （grammar.py:289）。
   - `Compound.resolve_refs`（expressions.py:331-333）**就地**把 members 里的
     占位符替换成真实表达式。

示例最终得到的表达式图（缩进表示 members）：

```
Sequence(name='greeting')
├── Literal('hi')
├── Quantifier(min=1, max=inf)          # name+
│   └── Regex('[a-z]+', name='name')    # resolve_refs 后的同一对象
└── Sequence(name='punct')
    ├── Lookahead(Literal('!'))         # &"!"
    └── Literal('!')                    # 与上一个 '!' 是两个不同对象
```

### 表达式对象何时复用

- **同一规则被多处引用时复用同一对象**：`resolve_refs` 返回的是 `rule_map`
  里那一份对象，所以所有引用点共享同一个 `Expression`。已验证：
  `Grammar('a = b b\nb = "x"')` 中 `g['a'].members[0] is g['a'].members[1] is g['b']`
  为 `True`。这正是 match 缓存用 `id(self)` 做键能有效的原因。
- **文本相同的子表达式不会合并**：`Grammar('b = "x"\nc = "x"')` 中
  `g['b'] is g['c']` 为 `False`（已验证）。每次 `visit_spaceless_literal`
  都新建 `Literal`。grammar.py:44 的 docstring 里 "factoring up repeated
  subexpressions ... [Is this implemented yet?]" 也承认了这一点——**没有**实现。
- `Grammar._copy`（grammar.py:76-87）是浅拷贝，`default()` 派生的 grammar
  与原 grammar 共享全部表达式对象。

## 二、parse 阶段：表达式图 → Node 树

调用链：

```
Grammar.parse            grammar.py:105  → _check_default_rule (grammar.py:124)
Expression.parse         expressions.py:136
Expression.match         expressions.py:149  ← 每次 match 新建 ParseError 和空 cache
Expression.match_core    expressions.py:164  ← packrat 缓存 + 错误记录
  └─ _uncached_match     各子类：Sequence expressions.py:357 / OneOf :381 /
                          Lookahead :402 / Quantifier :431 / Literal :262 / Regex :302
```

`match`（expressions.py:158-159）每次调用都新建
`error = ParseError(text)` 和 `cache = defaultdict(dict)`，因此**缓存不跨
parse 调用**。`parse` 在 match 成功后检查 `node.end < len(text)`，未消费完抛
`IncompleteParseError`（expressions.py:145-146）。

### match 缓存的键和值

`match_core`（expressions.py:195-201）：

- **键**：两层 dict。外层键是 `id(self)`——表达式对象的**身份**（不是
  `__eq__`/`__hash__`，所以两个"相等"但不同的表达式不共享缓存）；内层键是
  `pos`（int）。已验证：`g['b'].match_core('x', 0, cache, err)` 后
  `list(cache) == [id(g['b'])]`。
- **值**：三种——
  - `Node`：该表达式在该位置匹配成功的子树；
  - `None`：记录过的失败（失败也被 memoize）；
  - `IN_PROGRESS`（expressions.py:102 的裸 `object()` 哨兵）：进入
    `_uncached_match` 前写入（expressions.py:200），正常返回时被覆盖。
    若递归中再次遇到同一 `(expr, pos)` 的 `IN_PROGRESS`，说明是左递归，
    抛 `LeftRecursionError`（expressions.py:202-203）。

缓存命中时直接返回**同一个 Node 对象**，不会拷贝（见风险点 3）。

### 失败位置为什么能选出最有用的 ParseError

expressions.py:206-213：任何表达式返回 `None` 时，若
`pos >= error.pos` 就更新 `error.expr/error.pos`。PEG 是有序选择，"所有分支都
走到的最远失败点"就是输入开始无法解析的位置，所以**最远失败**最接近人类
想要的报错位置。并列时（`pos == error.pos`）条件
`self.name or getattr(error.expr, 'name', None) is None` 让**命名表达式覆盖
未命名表达式**，报错就能引用用户写得出的规则名。已验证：
`Grammar('a = &"x" "x"').parse('y')` 报 `Rule 'a' didn't match at 'y'`
（`error.expr.name == 'a'`），而不是未命名的 Lookahead/Literal。

## 三、visit 阶段：Node 树 → 用户结果

`NodeVisitor.visit`（nodes.py:194-225）：

1. 按 `node.expr_name` 找到 `visit_<name>` 方法，缺省 `generic_visit`
   （nodes.py:208；`generic_visit` 默认抛 `NotImplementedError`，nodes.py:239）。
2. **先访问子节点**：`[self.visit(n) for n in node]`（nodes.py:213），
   每个子节点的 visit 返回值收集成 `visited_children` 列表。
3. 调 `method(node, visited_children)`（nodes.py:213），其**返回值**成为
   父节点 `visited_children` 中对应的那一项——结果就是这样自底向上冒泡的；
   根节点的返回值就是整个 `visit()` 的返回值。树本身从不被修改
   （nodes.py:170-178 说明了理由）。

### 异常的两条路径

nodes.py:212-225 的 try/except 决定走向：

- **可直接抛出（透传）**：
  - `VisitationError` 和 `UndefinedLabel` 永远不重包（nodes.py:214），
    保证已包装的异常不会被包第二层；
  - `isinstance(exc, self.unwrapped_exceptions)` 为真时原样 `raise`
    （nodes.py:220-221）。注意是 `isinstance`——**配置基类、抛出子类同样
    透传**。这条契约由新增测试
    `parsimonious/tests/test_nodes.py::SpecialCasesTests::test_unwrapped_exceptions_inheritance`
    证明。
- **包装**：其余一切异常被包成 `VisitationError(exc, exc_class, node)`
  （nodes.py:225），`raise ... from exc` 保留 `__cause__`。
  `VisitationError`（exceptions.py:91-105）把原始类名、原始消息和
  `node.prettily(error=node)` 的整棵子树（出错节点带箭头）拼进消息，
  并记录 `original_class`——节点上下文就是这样附加上的。

## 四、真实风险点（均在当前 commit 上复现）

1. **左递归报错的位置信息是无效值。**
   `expressions.py:203` 用 `pos=-1` 抛 `LeftRecursionError`，于是
   `column()`（exceptions.py:44-50）算出 `pos + 1 = 0`——一个不可能的
   1-based 列号。
   最小输入：`Grammar("a = a 'x' / 'y'").parse('y')`
   可观察结果：`LeftRecursionError`，首行为
   `Left recursion in rule 'a' at 'y' (line 1, column 0).`；且即使 `'y'`
   本可匹配第二个分支也会抛错（`OneOf` 不捕获异常，expressions.py:381-386）。

2. **有序选择导致"能匹配的前缀"遮蔽"更长的合法输入"。**
   `OneOf._uncached_match`（expressions.py:381-386）返回第一个成功分支。
   最小输入：`Grammar('a = "x" / "xy"').parse('xy')`
   可观察结果：`IncompleteParseError: Rule 'a' matched in its entirety, but
   it didn't consume all the text. The non-matching portion of the text begins
   with 'y' (line 1, column 2).`

3. **缓存使命中处共享同一个 Node 对象。**
   expressions.py:195-201 命中时原样返回缓存的 Node；nodes.py:19-21 的
   docstring 也警告了这一点。
   最小输入：`t = Grammar('s = a a\na = ""').parse('')`
   可观察结果：`t.children[0] is t.children[1]` 为 `True`；就地修改其中一个
   （如 `children.append(...)`）会"幽灵般"影响另一个。

4. **自定义规则的参数个数错误在构造期才炸，且与函数体无关。**
   `expression()`（expressions.py:70-80）用 `getfullargspec` 数参数，非 2/5
   即 `RuntimeError`。
   最小输入：`Grammar('', foo=lambda t, p, c: None)`
   可观察结果：`RuntimeError: Custom rule functions must take either 2 or 5
   arguments, not 3.`

5. **`BadGrammar` 与 `UndefinedLabel` 的透出路径不对称。**
   `resolve_refs` 在 `RuleVisitor.visit` 内执行（grammar.py:481），而
   nodes.py:214 只放行 `(VisitationError, UndefinedLabel)`，所以循环引用抛的
   `BadGrammar`（grammar.py:283）会被**再包一层** `VisitationError`，
   未定义标签的 `UndefinedLabel`（grammar.py:289）却原样透出。
   最小输入：`Grammar('foo = bar\nbar = foo')` vs `Grammar('foo = bar')`
   可观察结果：前者抛 `VisitationError`（`__cause__` 才是 `BadGrammar:
   Circular Reference resolving foo=bar.`），后者直接抛
   `UndefinedLabel: The label "bar" was never defined.`。用
   `except BadGrammar` 捕获循环引用会扑空（现有测试
   `test_grammar.py::GrammarTests::test_circular_toplevel_reference` 也是按
   `VisitationError` 断言的）。

6. **`VisitationError` 的节点上下文只存在于消息字符串里。**
   exceptions.py:91-105 只保存 `original_class`，node 仅被渲染进
   `prettily()` 文本；原始异常实例只能从 `__cause__` 取回。
   最小输入：一个 `visit_*` 里 `raise ValueError('x')` 的 visitor 去 parse。
   可观察结果：`hasattr(exc, 'node')` 为 `False`；想程序化定位出错节点只能
   解析字符串或重新遍历。

7. **自定义表达式抛异常会把 `IN_PROGRESS` 哨兵留在缓存里。**
   expressions.py:200-201 写入哨兵后没有 try/finally；若 `_uncached_match`
   抛出非 parse 异常，该 `(expr, pos)` 的缓存项永远是 `IN_PROGRESS`。
   最小输入：5 参数自定义规则第一次调用 `raise Boom`，用同一个 `cache`
   再调 `match_core`。
   可观察结果：第二次调用抛出**伪** `LeftRecursionError`（已用脚本复现：
   缓存里残留 `{0: <object object ...>}`）。正常 `match()` 流程中异常会中止
   整个解析、缓存随调用栈丢弃，所以只有复用 cache 的自定义规则能踩到。

## 五、无法从源码确认、标注为推测的条目

- `Expression.__hash__` 用 `identity_tuple`（expressions.py:118-119）而
  `Compound.__hash__` 只用 `(class, name)`（expressions.py:342-346），两者
  与 `__eq__` 的一致性依赖"成员在加入 set/dict 后不再被改"的约定；
  `resolve_refs` 恰好会就地改 members。是否会在用户直接把表达式放进 set
  的场景下产生错误行为，未写测试验证，列为推测。
- `RegexNode` 把整个 `re.Match` 挂在节点上（expressions.py:308，源码自带
  "A terrible idea for cache size?" 的 TODO）对长文本内存占用的实际影响，
  未做基准测量，列为推测。

## 六、复跑方式

准备（一次性）：`python3 -m pip install -e '.[testing]'`

验收命令（仓库根目录）：

```
python3 -m pytest -q
```

实际输出摘要（本机 Python 3.9，退出码 0）：

```
....................................s................................... [ 82%]
.........s.....                                                          [100%]
85 passed, 2 skipped in 0.22s
```

其中新增用例
`parsimonious/tests/test_nodes.py::SpecialCasesTests::test_unwrapped_exceptions_inheritance`
验证了"配置基类、抛出子类仍透传；未配置异常保留节点上下文"的结论，可单独复跑：

```
python3 -m pytest parsimonious/tests/test_nodes.py -q -k unwrapped
# 2 passed, 85 deselected
```

用 `python3 -m pytest --collect-only -q | grep unwrapped` 可看到新增用例全名
`parsimonious/tests/test_nodes.py::SpecialCasesTests::test_unwrapped_exceptions_inheritance`。
