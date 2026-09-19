# Parsimonious：从 grammar 文本到 NodeVisitor 的完整调用链分析

适用代码：本仓库当前提交（`f7189d1`，parsimonious 0.11.0）。所有行号均以**当前工作树**为准；本次工作只新增了一个测试文件，未改动任何库源码，因此下列行号与初始提交一致，可用 `nl -ba <文件>` 逐条核对。

验证环境：Python 3.13.14，pytest 9.1.1，`regex` 已安装。

## 0. 复跑命令

准备阶段（不计入演示，需要 `regex` 与 `pytest`）：

```bash
python3 -m pip install -e '.[testing]'
```

验收（从仓库根目录直接运行，退出码 0）：

```bash
python3 -m pytest -q
# 85 passed, 2 skipped in 0.1x s；exit=0
```

只跑本次新增的契约测试（显示用例名称）：

```bash
python3 -m pytest parsimonious/tests/test_visitation_contract.py -v
# parsimonious/tests/test_visitation_contract.py::test_unwrapped_exceptions_honor_inheritance_and_keep_node_context PASSED
```

不需要外部服务、环境变量或网络。

## 1. 贯穿全文的示例 grammar

```peg
block = "(" item+ !")"
item  = "a" / "b"
```

它同时覆盖了三类构造：

- 命名规则：`block`、`item`（`RuleVisitor.visit_rule` 给顶层表达式赋名，`parsimonious/grammar.py:356-360`）；
- 量词：`item+` 被编译为 `Quantifier(item, min=1, max=inf)`（`visit_quantified`，`parsimonious/grammar.py:334-346`；匹配逻辑 `parsimonious/expressions.py:431-445`）；
- lookahead：`!")"` 被编译为负向 `Lookahead(Literal(')'), negative=True)`（`visit_not_term`，`parsimonious/grammar.py:352-354`，工厂 `Not` 在 `parsimonious/expressions.py:416-417`）。

成功输入 `"(aab"` 的真实树（`node.prettily()` 实测）：

```
<Node called "block" matching "(aab">
    <Node matching "(">
    <Node matching "aab">
        <Node called "item" matching "a">
            <Node matching "a">
        <Node called "item" matching "a">
            <Node matching "a">
        <Node called "item" matching "b">
            <Node matching "b">
    <Node matching "">
```

最后的零宽 `<Node matching "">` 就是负向 lookahead 节点（`Lookahead._uncached_match` 返回 `Node(self, text, pos, pos)`，`parsimonious/expressions.py:402-405`）。

## 2. 构造阶段：grammar 文本 → 表达式图

调用链：

1. `Grammar.__init__`（`parsimonious/grammar.py:47-68`）先用 `expression(...)` 把 kwargs 里的可调用对象包装成 `AdHocExpression`（`parsimonious/expressions.py:82-99`），再调用 `_expressions_from_rules`。
2. `Grammar._expressions_from_rules`（`parsimonious/grammar.py:89-94`）用模块级单例 `rule_grammar`（`parsimonious/grammar.py:506-512`，先 bootstrap 再用自身重编译）把规则文本 parse 成 Node 树，然后交给 `RuleVisitor(custom_rules).visit(tree)`。
3. `RuleVisitor` 的各 `visit_*` 方法把语法树节点翻译成表达式对象：`visit_sequence`（`grammar.py:362-366`）、`visit_ored`（368-374）、`visit_quantified`（334-346）、`visit_not_term` / `visit_lookahead_term`（348-354）、`visit_reference` 放一个 `LazyReference(str)` 占位符（393-400）。
4. `visit_rules`（`grammar.py:453-487`）用 `OrderedDict((expr.name, expr) ...)` 汇总；**同名规则后者覆盖前者**（465-469），custom kwargs 再整体覆盖（471-474）。
5. 随后对每个规则调用 `resolve_refs`（`grammar.py:476-481`）：`LazyReference.resolve_refs`（`grammar.py:265-291`）沿着 `rule_map` 一路追随字符串名，直到落到具体表达式；因此**前向引用可以工作**（在 `rule_map` 全部建好之后才解析）。纯 LazyReference 环（如 `foo = bar`、`bar = foo`）在 282-283 行抛 `BadGrammar`；名字不存在时 288-289 行抛 `UndefinedLabel`。
6. `Compound.resolve_refs` 会**就地重写** `self.members`（`expressions.py:331-333`），把每个成员里的 LazyReference 替换成 `rule_map` 里的规则对象。完成后 `Grammar` 自身就是名字→表达式的有序映射，第一条规则成为 `default_rule`（`grammar.py:486-487`）。

### 表达式对象何时复用、何时新建（实测）

- **所有对同一命名规则的引用共享同一个对象**：解析后引用处直接换成 `rule_map` 里的那一个表达式。实测：示例 grammar 中 `block` 内 `item+` 被包装的成员 `is g['item']` 为 `True`。这是前向引用与对象复用的主要机制。
- **重复出现的匿名子表达式不会被 intern**。`Grammar` 的 docstring 声称会做 “factoring up repeated subexpressions into a single object” 这类优化，但同一段文字自己就标注了 “[Is this implemented yet?]”（`grammar.py:43-45`）。实测 `Grammar("foo = 'x' 'x'\nbar = 'x'")` 中两个 `'x'` 的 `is` 比较为 `False`，且都与 `g['bar']` 不是同一对象；它们只是按值相等（`__eq__` 比较 `identity_tuple`，`expressions.py:121-130`、257-260）。
- 缓存键用的是 `id(expr)`（见下节），所以“按值相等但不是同一对象”的两个表达式各有独立缓存槽——这是风险点 R3 的根源。
- 每次构造 `Grammar` 都从零建一套新表达式；但解析 grammar 文本用的 `rule_grammar` 是模块级单例，被所有 `Grammar(...)` 共享。

## 3. Parse 阶段：packrat 匹配、缓存与错误选址

调用链（`Grammar.parse('(aab')`）：

`Grammar.parse`（`grammar.py:105-112`）→ `default_rule.parse`（`expressions.py:136-147`）→ `Expression.match`（149-162）：new 一个 `ParseError(text)`（初始 `pos=-1, expr=None`，`exceptions.py:14`）和一个 `defaultdict(dict)` 缓存，调用 `match_core`；若整体返回 `None` 就在 160-161 行 `raise error`；`parse()` 再检查 `node.end < len(text)`，否则抛 `IncompleteParseError`（145-146，`exceptions.py:67-77`）。

核心是 `Expression.match_core`（`expressions.py:164-215`）：

```python
expr_cache = cache[id(self)]                # 195
if pos in expr_cache:                       # 196
    node = expr_cache[pos]                  # 197  命中：连 _uncached_match 都不调用
else:
    expr_cache[pos] = IN_PROGRESS           # 200  左递归哨兵
    node = expr_cache[pos] = self._uncached_match(text, pos, cache, error)  # 201
if node is IN_PROGRESS:
    raise LeftRecursionError(text, pos=-1, expr=self)  # 202-203
if node is None and pos >= error.pos and (
        self.name or getattr(error.expr, 'name', None) is None):  # 206-207
    error.expr = self
    error.pos = pos                         # 212-213
```

### match 缓存的键和值

- **外层 cache** 是 `defaultdict(dict)`，键为 `id(expression)`，值是该表达式的位置字典；**内层键是文本位置 `pos`**（`expressions.py:159`、195-197）。即逻辑键为 `(id(expr), pos)`，与 docstring 描述一致（170-172）。
- **值有三种**：
  1. 成功：`Node`（`Regex` 产生带 `match` 属性的 `RegexNode`，`expressions.py:302-310`，`nodes.py:124-129`）；
  2. 失败：`None`（失败也被缓存，命中时直接返回 `None`，不重新执行 `_uncached_match`）；
  3. 进行中：模块级哨兵 `IN_PROGRESS`（`expressions.py:102`、200），同一表达式同一位置重入时读到它，202-203 行抛 `LeftRecursionError`——这就是左递归防护的全部机制。
- 缓存是**每次 `match()` 新建、用完即弃**的（159 行），不在多次 parse 之间共享。
- `"(aab"` 的真实匹配轨迹（按 `match_core` 进入顺序，实测；本输入无 HIT）：

```text
NEW pos=0 (0,1) Literal '('
NEW pos=1 (1,2) Literal 'a'      NEW pos=1 (1,2) item
NEW pos=2 (2,3) Literal 'a'      NEW pos=2 (2,3) item
NEW pos=3  None Literal 'a'      NEW pos=3 (3,4) Literal 'b'   NEW pos=3 (3,4) item
NEW pos=1 (1,4) Quantifier(item+)
NEW pos=4  None Literal ')'      NEW pos=4 (4,4) Lookahead(!)   （内部失败 == 负向成功）
NEW pos=0 (0,4) block
```

注意 `pos=3`：`OneOf` 依次尝试 `"a"`（失败并写入错误候选）再尝试 `"b"`（成功），失败候选不影响控制流（`expressions.py:381-386`）。

### 失败位置为什么能选出“最有用”的 ParseError

错误记录规则在 `expressions.py:206-213`：只有 `node is None` 且 `pos >= error.pos` 才考虑更新；并且**当前表达式有名字、或目前记录的表达式还没有名字**时才真正覆盖。由此得到两条可核对的选址语义：

1. **越靠后的失败优先**（`>=`，不是 `>`）：PEG 在尝试更长备选时到达的最深位置会胜出。
2. **同位置时命名规则优先于匿名子表达式**：匿名字面量先记录到 `pos`，随后同一 `pos` 的命名父表达式（如 `item`、`block`）会把它覆盖掉。实测 `Grammar("rule = named / 'z'","named = 'a'")` 解析 `'b'`，最终报告的是 `Rule 'rule' didn't match ...`，而非字面量 `'z'`；示例 grammar 解析 `"(a)"` 的报错是 `Rule 'item' didn't match at ')'`。

初始 `error.pos = -1` 保证第一个失败一定会被记录，因此最终 `match()` 失败时 `raise error`（161 行）一定带有某个具体表达式和位置。

### 量词与 lookahead 的匹配要点

- `Quantifier` 循环调用成员，成员失败即停止；成员数量达到 `min` 即成功（`expressions.py:431-445`）。零宽成员的防死循环在 441-442：只有已满足 `min` 且本次长度为 0 才 break。
- 正/负 lookahead 都不消耗文本（402-405）；负向时“内部失败”反而是成功，但内部匹配失败的过程照常写入共享 `error`（见 R2）。
- `Sequence` 任一成员失败立即返回 `None`（357-368）；`OneOf` 第一个成功的成员被包进一个 Node 返回（381-386）。

## 4. 访问阶段：返回值如何向父节点传递

入口 `NodeVisitor.visit`（`parsimonious/nodes.py:194-225`）：

```python
method = getattr(self, 'visit_' + node.expr_name, self.generic_visit)  # 208
try:
    return method(node, [self.visit(n) for n in node])                 # 213
except (VisitationError, UndefinedLabel):                              # 214
    raise                                                            # 216
except Exception as exc:                                              # 217
    if isinstance(exc, self.unwrapped_exceptions):                    # 220
        raise                                                        # 221
    exc_class = type(exc)
    raise VisitationError(exc, exc_class, node) from exc              # 225
```

- 派发依据是节点的 `expr.name`（`node.expr_name`，`nodes.py:47-50`），没有对应方法就用 `generic_visit`（227-240，默认抛 `NotImplementedError`）。
- **子节点先全部访问完，列表再传给父方法**：213 行的列表推导先对 `node` 的每个孩子递归调用 `self.visit(n)`，父方法拿到的第二参数是“访问结果”的列表，不是 Node 列表。某个孩子抛异常时，父方法体根本不会进入。
- **返回值靠普通的 `return` 逐层向上**：叶子方法返回什么，那个值就成为父节点 `visited_children` 列表中的一项；没有任何装箱或类型约束。`NodeVisitor.lift_child`（`nodes.py:266-269`）就是取唯一孩子直接返回，`grammar.py:308` 用它处理 `expression`/`term`/`atom`。
- 实测量词与组合节点的子值形态：
  - `b?` 缺省时父方法收到 `[]`，存在时收到 `[<b 的访问结果>]`（不是 `None`）；
  - 无自定义方法的节点走 `generic_visit`，返回 `visited_children or node`（`grammar.py:437-451` 是 `RuleVisitor` 自己的同款实现：空列表为假则保留原 Node；基类的 `generic_visit` 是 227-240 的抛错版本）。
- `NodeVisitor.parse/match` 只是 `self.visit(self.grammar.parse(...))` 的快捷方式（`nodes.py:244-262`、273-287）；未设置 `grammar` 时抛 `RuntimeError`（280-286）。

## 5. 异常的两条路径

### 5.1 普通异常（未声明为可直接抛出）

在任何一个 `visit` 帧里，只要异常不匹配 214 行的 `(VisitationError, UndefinedLabel)`、也不是 220 行 `isinstance(exc, self.unwrapped_exceptions)` 的实例，就在**抛出该异常的那一帧**被包装成 `VisitationError`（225 行，`__init__` 在 `exceptions.py:91-105`，消息里嵌入 `node.prettily(error=node)`，见 `nodes.py:65-83`），并带 `from exc` 异常链。`VisitationError.original_class` 保留原类型（`exceptions.py:98`）。

包装发生后，祖先帧的列表推导收到它，214 行命中 `VisitationError` 直接 `raise`，**不会二次包装**，所以树上下文固定为最深的失败节点（`node.prettily(error=node)` 的渲染在 `nodes.py:68-85`）——这就是“未配置的异常仍保留节点上下文”的实现机制，由新增测试 Phase 2 锁定。

### 5.2 声明为可直接抛出的异常（`unwrapped_exceptions`）

220 行用的是 `isinstance(exc, self.unwrapped_exceptions)`，是**子类匹配**而非精确类型相等。因此把基类写进 `unwrapped_exceptions`、在访问方法里抛其子类，异常原样穿过所有帧直接到达调用方，不携带 `VisitationError`。这是标准 Python 语义，但库文档只说“classes of exceptions”，没有明说继承关系也生效；新增测试 Phase 1 把这条契约固定下来。

两条永远优先的旁路（不受 `unwrapped_exceptions` 影响）：

- `VisitationError` 与 `UndefinedLabel` 在 214-216 行无条件重抛；
- 217 行只捕获 `Exception`，`KeyboardInterrupt`/`SystemExit` 等 `BaseException` 不被拦截。

判定只看**当前捕获帧所属 visitor 实例**的配置（`self`），没有任何全局注册表；嵌套使用两个不同 visitor 时，每个实例各管各的帧（实测确认：内层 visitor 允许的异常在它自己的帧裸奔，不会被“提升”为外层策略）。

### 5.3 构造期与 parse 期的异常不经过访问包装

- `LazyReference.resolve_refs` 抛的 `BadGrammar`（循环引用，`grammar.py:282-283`）发生在 `RuleVisitor.visit` 内部，因此被 225 行包装成 `VisitationError`——现有测试 `test_circular_toplevel_reference`（`parsimonious/tests/test_grammar.py:328-352`）正是断言 `VisitationError`。
- 同一处抛的 `UndefinedLabel`（`grammar.py:288-289`）却因 214 行的显式旁路**裸奔**出 `Grammar(...)` 构造函数（实测：`Grammar("foo = missing")` 直接得到 `UndefinedLabel`）。
- parse 阶段的失败不靠异常传播控制流：失败是 `None` 返回值，只有最外层 `match()` 在 160-161 行 `raise error`；`LeftRecursionError` 是唯一在匹配中途直接抛出的异常（203 行），它会原样穿过所有 `match_core` 与 `Grammar.parse`。
- 自定义规则（5 参数 callable）内部抛出的异常同样**不会**被转成 `ParseError`，而是直接穿过 `AdHocExpression._uncached_match`（`expressions.py:83-94`）与 `Grammar.parse`（实测，见 R6）。

## 6. 风险点（每个都有文件:行号、最小输入、可观察结果）

以下 6 项均在当前工作树上实测复现；“观察方式”给出的命令/代码可独立重跑。

### R1 左递归错误位置被硬编码为 -1，行列信息错误

- 位置：`parsimonious/expressions.py:202-203`（构造时实参 `pos=-1`，尽管当前位置就在局部变量 `pos` 里）；消息格式化在 `parsimonious/exceptions.py:54-64`。
- 最小输入：
  ```python
  g = Grammar("expr = expr2 / lit\nexpr2 = expr '+' '1'\nlit = ~r'[0-9]+'")
  g['expr2'].parse("1+1")
  ```
- 可观察结果：抛出 `LeftRecursionError`，其 `e.pos == -1`（不是真实重入位置 0），`str(e)` 为
  `Left recursion in rule 'expr2' at '1' (line 1, column 0).`——文本窗口用 `text[-1:]` 侥幸取到了 `'1'`，但 `column() == 0`（`exceptions.py:44-50` 对 -1 走到 `rindex` 的 `ValueError` 分支返回 `pos + 1`），与“1-based column”的文档约定矛盾。现有测试 `test_left_associative`（`tests/test_grammar.py:688-706`）只断言消息子串，未覆盖位置。

### R2 负向 lookahead 的“预期失败”污染最远错误，报错指向语法里禁止出现的东西

- 位置：`Lookahead._uncached_match` 调成员时共用同一个 `error`（`parsimonious/expressions.py:402-405`）；选址规则 `expressions.py:206-213`。
- 最小输入：
  ```python
  g = Grammar("rule = !('a' 'b' 'c') tail\ntail = 'z'")
  try: g.parse('abx')
  except ParseError as e: print(type(e.expr).__name__, getattr(e.expr, 'literal', None), e.pos)
  ```
- 可观察结果：输入在位置 2 其实是 `tail` 想要 `'z'` 却拿到 `'x'`；但负向 lookahead 内部在位置 2 期待 `'c'` 的失败把 `error.pos` 推到 2，最终打印
  `Rule <Literal 'c'> didn't match at 'x' (line 1, column 3).`
  ——它报告的是被禁止序列里未匹配到的 `'c'`，而不是真正需要的 `'z'`。这是 packrat“最远失败”策略与 lookahead 语义冲突的已知味道，但库目前没有任何辅助信息区分二者（206-211 的注释承认只记录一种 expr）。

### R3 相同的匿名子表达式不共享对象，packrat 缓存被按 `id` 重复占用

- 位置：缓存键 `cache[id(self)]`（`parsimonious/expressions.py:195`）；表达式构造各自 new（`visit_spaceless_literal` 每次都 `return Literal(...)`，`grammar.py:416-430`），未做 interning，docstring 自己标注优化未实现（`parsimonious/grammar.py:43-44`）。
- 最小输入：
  ```python
  g = Grammar("rule = 'q' / 'q'")   # 两个 'q' 是不同的 Expression 对象
  g['rule'].members[0] is g['rule'].members[1]   # False
  ```
- 可观察结果：解析 `'z'` 时两个 `'q'` 字面量在同一位置 0 各执行一次 `_uncached_match`（在 `Literal._uncached_match` 打点可观察到两次调用，id 不同），产生两份独立的失败缓存；任何语法里重复的公共子表达式（大 grammar 中很常见）都无法共享记忆化结果。功能正确，但缓存命中率与内存占用劣化。

### R4 同一 `(规则, 位置)` 返回同一个 Node 对象，改一个槽位会从兄弟位置可见

- 位置：缓存命中直接返回缓存里的 Node（`expressions.py:196-197`）；`Node` 文档明确警告别名（`parsimonious/nodes.py:18-21`），`children` 是可变 list（`nodes.py:33-41`）。
- 最小输入：
  ```python
  g = Grammar("rule = empty empty 'x'\nempty = ''")
  n = g.parse('x')
  n.children[0] is n.children[1]          # True，同一个零宽 Node
  n.children[0].children.append('X')
  print(n.children[1].children)           # ['X']
  ```
- 可观察结果：两次引用 `empty` 在位置 0 命中同一缓存条目，父 Sequence 的 children 里是同一对象的两个引用；对其中一个的 `children` 做原地修改，立刻从另一个“兄弟”可见。visitor 默认不原地改树（`nodes.py:170-177` 说明了原因），但直接消费 Node 的用户代码会踩到。

### R5 同位置失败的 tie-break 让命名父规则吞掉具体期望（报“rule 不匹配”而不是缺哪个 token）

- 位置：`parsimonious/expressions.py:206-207` 的条件 `(self.name or error.expr 无名)`；OneOf 每个备选都从同一 `pos` 试（381-386）。
- 最小输入：
  ```python
  g = Grammar("rule = named / 'z'\nnamed = 'a'")
  try: g.parse('b')
  except ParseError as e: print(e.expr.name, type(e.expr).__name__)
  ```
- 可观察结果：位置 0 上匿名 `Literal 'z'` 先记录错误，随后命名的 `OneOf('rule')` 以同一 `pos`（条件是 `>=`）覆盖它，最终消息只有 `Rule 'rule' didn't match at 'b' ...`，用户看不到“期望的是 a 或 z”。R2 是 lookahead 特化，R5 是通用的信息丢失路径。

### R6 自定义规则内部抛出的异常裸奔出 parse，不被翻译为 ParseError 也不带位置

- 位置：`AdHocExpression._uncached_match` 直接调用 `callable(...)`，无 try/except（`parsimonious/expressions.py:83-94`，85 行是 5 参数调用点）；`match_core` 只处理 `None` 与 `IN_PROGRESS`（195-213）。
- 最小输入：
  ```python
  def boom(text, pos, cache, error, grammar): raise ValueError('boom')
  g = Grammar("root = boom", boom=boom)
  g.parse('')          # ValueError: boom 直接抛出
  ```
- 可观察结果：调用方拿到的是裸 `ValueError`，既不是 `ParseError`（无 `pos`/`expr`），也没有 packrat 缓存被清理的保证（异常发生在 201 行赋值之前，对应槽位留下 `IN_PROGRESS`，但缓存随本次 `match()` 一起丢弃，故仅在自定义规则长期持有 `cache` 引用时才可能观察到陈旧哨兵——此项后果为推测，见第 8 节 S1）。对比之下，访问阶段的同类异常会被包装（第 5 节），两条路径的体验不一致。

## 7. 新增的最小契约测试

文件：`parsimonious/tests/test_visitation_contract.py`（新增，未修改任何现有测试、公开 API 或错误文本）。

用例：`test_unwrapped_exceptions_honor_inheritance_and_keep_node_context`，一个测试函数内两个阶段：

- Phase 1 固定 5.2 的结论：visitor 配置 `unwrapped_exceptions = (_ConfiguredBase,)`，`visit_leaf` 抛其子类 `_RaisedSubclass`；断言抛出的就是 `_RaisedSubclass` 本身（若是 `VisitationError` 则 `AssertionError`）。
- Phase 2 固定 5.1 的结论：同一节点改抛未配置的 `_Unconfigured`；断言得到 `VisitationError`，`original_class is _Unconfigured`，且 `str(exc)` 中包含 `called "leaf"`——证明上下文是在叶子那一帧保留的，而不是被祖先帧重新包装。

实测输出：

```text
$ python3 -m pytest parsimonious/tests/test_visitation_contract.py -v
parsimonious/tests/test_visitation_contract.py::test_unwrapped_exceptions_honor_inheritance_and_keep_node_context PASSED [100%]
1 passed in 0.01s
```

## 8. 无法从源码/测试确认、单独标明的推测

- **S1（对应 R6）**：自定义规则在 `expressions.py:201` 的赋值完成前抛出，理论上会让 `expr_cache[pos]` 停留在 `IN_PROGRESS`；但 `cache` 由 `Expression.match` 在 159 行新建、异常后随栈帧丢弃，正常 API 路径无法复用它。只有用户把 5 参数 callable 收到的 `cache` 长期保存并再次使用时，陈旧哨兵才可能在后续 `match_core` 中误报左递归。未编写复现，属于推测。
- **S2**：docstring 宣称的“公共子表达式合并”优化（`grammar.py:43-45` 自标 `[Is this implemented yet?]`）在代码中确实没有对应实现（grep 不到任何 interning/dedup 逻辑），本文按“未实现”陈述；未来版本若加入，R3 的现象会改变但源码行仍可核对。
- **S3**：`RegexNode.match` 把 `re.Match` 对象挂在节点上一起进缓存（`expressions.py:302-309`，代码里自带 `# TODO: A terrible idea for cache size?`）。缓存是一次性的，正常使用观察不到内存问题；仅在手工长期持有节点时可能多占内存，未做基准测量。

## 9. 可核对性与实际输出摘要

文档中的断言来源只有两类：(a) 引用的源码行（可用 `nl -ba parsimonious/{grammar,expressions,nodes,exceptions}.py` 对照）；(b) 本节及第 2-7 节中贴出的实测输出。完整验收输出（仓库根目录）：

```text
$ python3 -m pytest -q
....................................s................................... [ 82%]
.........s.....                                                           [100%]
85 passed, 2 skipped in 0.11s
$ echo $?
0
```

其中新增用例名称（verbose 单跑）：

```text
parsimonious/tests/test_visitation_contract.py::test_unwrapped_exceptions_honor_inheritance_and_keep_node_context PASSED
```

变更清单：

- 新增 `ANALYSIS.md`（本文档）；
- 新增 `parsimonious/tests/test_visitation_contract.py`（1 个测试、2 个阶段）；
- 未修改库源码、公开 API、异常文本及任何既有测试，因此文档行号在验收后依然有效。
