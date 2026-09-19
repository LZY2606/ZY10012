"""Contract tests for visitor exception propagation.

These pin down two claims documented in ANALYSIS.md:

1. ``unwrapped_exceptions`` is matched with ``isinstance``, so configuring a
   base class lets a raised subclass propagate unwrapped, even from deeper
   frames whose ancestor methods do not declare it.
2. An exception that is not configured is still wrapped at the frame where it
   is raised, so the wrapping preserves that node's context.
"""
from parsimonious import Grammar, NodeVisitor, VisitationError


class _ConfiguredBase(Exception):
    pass


class _RaisedSubclass(_ConfiguredBase):
    pass


class _Unconfigured(Exception):
    pass


_GRAMMAR = Grammar(
    'root = leaf\n'
    'leaf = "x"\n'
)


def test_unwrapped_exceptions_honor_inheritance_and_keep_node_context():
    class Visitor(NodeVisitor):
        grammar = _GRAMMAR
        unwrapped_exceptions = (_ConfiguredBase,)

        def visit_leaf(self, node, visited_children):
            raise _RaisedSubclass('subclass of the configured base class')

    # Phase 1: a subclass of an exception configured on an ancestor frame
    # propagates unwrapped all the way out of visit().
    try:
        Visitor().parse('x')
    except _RaisedSubclass:
        pass
    except VisitationError as exc:  # pragma: no cover - contract failure
        raise AssertionError(
            'subclass of an unwrapped base was wrapped: %r' % (exc,)
        )
    else:  # pragma: no cover - nothing was raised
        raise AssertionError('visitor swallowed the raised exception')

    # Phase 2: an unconfigured exception raised at the same node is wrapped at
    # that frame, so the VisitationError keeps the leaf node's context.
    class StrictVisitor(Visitor):
        def visit_leaf(self, node, visited_children):
            raise _Unconfigured('not declared anywhere')

    try:
        StrictVisitor().parse('x')
    except VisitationError as exc:
        assert exc.original_class is _Unconfigured
        assert 'called "leaf"' in str(exc)
    else:  # pragma: no cover - nothing was raised
        raise AssertionError('unconfigured exception propagated without context')
