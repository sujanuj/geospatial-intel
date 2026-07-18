"""A small query DSL for filtering the live object stream.

Grammar (informal):

    query      := or_expr
    or_expr    := and_expr ( "OR" and_expr )*
    and_expr   := unary ( "AND" unary )*
    unary      := "NOT" unary | "(" query ")" | comparison
    comparison := FIELD OP VALUE

FIELD is an identifier (e.g. `type`, `status`, `country`, `speed`).
OP is one of  :  =  !=  >  >=  <  <=
VALUE is a bare word, a number, or a "quoted string" (for values with
spaces, e.g. name:"MV Ocean Pioneer").

String fields (type, status, country, name, id):
    field:value   -> case-insensitive SUBSTRING match ("contains")
    field=value   -> case-insensitive EXACT match
    field!=value  -> case-insensitive exact non-match
    <, >, <=, >=  -> not valid on string fields, raises QueryError

Numeric fields (speed, altitude, heading, lat, lon):
    field:value / field=value -> equality
    field!=value, field>value, field<value, field>=value, field<=value
                  -> standard numeric comparison

Examples:
    type:ship AND status:threat
    country:Iran OR country:Iraq
    speed>500 AND type:aircraft
    NOT (status:active) AND altitude>30000
    name:"Pacific Star"
"""

from dataclasses import dataclass
from typing import List, Union


class QueryError(ValueError):
    """Raised for any malformed or semantically invalid query string.

    Deliberately a subclass of ValueError with a human-readable message —
    callers (e.g. main.py's REST/WebSocket handlers) can catch this and
    return the message directly to the client instead of a raw traceback.
    """


STRING_FIELDS = {"type", "status", "country", "name", "id"}
NUMERIC_FIELDS = {"speed", "altitude", "heading", "lat", "lon"}
KNOWN_FIELDS = STRING_FIELDS | NUMERIC_FIELDS


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

@dataclass
class Token:
    kind: str   # "FIELD", "OP", "VALUE", "AND", "OR", "NOT", "LPAREN", "RPAREN", "EOF"
    text: str


_OPERATORS = [">=", "<=", "!=", ":", "=", ">", "<"]  # longest-first, so >= isn't split into > and =
_KEYWORDS = {"and": "AND", "or": "OR", "not": "NOT"}


def tokenize(query: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(query)

    while i < n:
        ch = query[i]

        if ch.isspace():
            i += 1
            continue

        if ch == "(":
            tokens.append(Token("LPAREN", ch))
            i += 1
            continue

        if ch == ")":
            tokens.append(Token("RPAREN", ch))
            i += 1
            continue

        if ch == '"':
            end = query.find('"', i + 1)
            if end == -1:
                raise QueryError(f"unterminated quoted string starting at position {i}")
            tokens.append(Token("VALUE", query[i + 1:end]))
            i = end + 1
            continue

        matched_op = None
        for op in _OPERATORS:
            if query[i:i + len(op)] == op:
                matched_op = op
                break
        if matched_op:
            tokens.append(Token("OP", matched_op))
            i += len(matched_op)
            continue

        # bare word: field name, keyword, or unquoted value
        j = i
        while j < n and not query[j].isspace() and query[j] not in "()\"" and not any(
            query[j:j + len(op)] == op for op in _OPERATORS
        ):
            j += 1
        if j == i:
            raise QueryError(f"unexpected character {query[i]!r} at position {i}")
        word = query[i:j]
        lowered = word.lower()
        if lowered in _KEYWORDS:
            tokens.append(Token(_KEYWORDS[lowered], word))
        else:
            # Emitted as VALUE by default; the parser reinterprets the
            # token immediately before an OP as a FIELD instead.
            tokens.append(Token("VALUE", word))
        i = j

    tokens.append(Token("EOF", ""))
    return tokens


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

@dataclass
class Comparison:
    field: str
    op: str
    value: str


@dataclass
class And:
    left: "Node"
    right: "Node"


@dataclass
class Or:
    left: "Node"
    right: "Node"


@dataclass
class Not:
    expr: "Node"


Node = Union[Comparison, And, Or, Not]


# ---------------------------------------------------------------------------
# Parser (recursive descent)
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> Token:
        return self.tokens[self.pos]

    def _advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _expect(self, kind: str) -> Token:
        tok = self._peek()
        if tok.kind != kind:
            raise QueryError(f"expected {kind} but found {tok.kind!r} ({tok.text!r})")
        return self._advance()

    def parse(self) -> Node:
        node = self._or_expr()
        self._expect("EOF")
        return node

    def _or_expr(self) -> Node:
        node = self._and_expr()
        while self._peek().kind == "OR":
            self._advance()
            node = Or(node, self._and_expr())
        return node

    def _and_expr(self) -> Node:
        node = self._unary()
        while self._peek().kind == "AND":
            self._advance()
            node = And(node, self._unary())
        return node

    def _unary(self) -> Node:
        if self._peek().kind == "NOT":
            self._advance()
            return Not(self._unary())
        if self._peek().kind == "LPAREN":
            self._advance()
            node = self._or_expr()
            self._expect("RPAREN")
            return node
        return self._comparison()

    def _comparison(self) -> Node:
        field_tok = self._peek()
        if field_tok.kind != "VALUE":
            raise QueryError(
                f"expected a field name but found {field_tok.kind!r} ({field_tok.text!r})"
            )
        self._advance()
        field = field_tok.text.lower()
        if field not in KNOWN_FIELDS:
            raise QueryError(
                f"unknown field {field_tok.text!r} — known fields are: "
                + ", ".join(sorted(KNOWN_FIELDS))
            )

        op_tok = self._peek()
        if op_tok.kind != "OP":
            raise QueryError(
                f"expected an operator after field {field!r} but found "
                f"{op_tok.kind!r} ({op_tok.text!r})"
            )
        self._advance()
        op = op_tok.text

        value_tok = self._peek()
        if value_tok.kind != "VALUE":
            raise QueryError(
                f"expected a value after '{field}{op}' but found "
                f"{value_tok.kind!r} ({value_tok.text!r})"
            )
        self._advance()

        if field in STRING_FIELDS and op in (">", "<", ">=", "<="):
            raise QueryError(
                f"operator {op!r} is not valid on string field {field!r} "
                f"(only : = != are allowed on {', '.join(sorted(STRING_FIELDS))})"
            )
        if field in NUMERIC_FIELDS:
            try:
                float(value_tok.text)
            except ValueError:
                raise QueryError(
                    f"field {field!r} is numeric but {value_tok.text!r} is not a number"
                )

        return Comparison(field=field, op=op, value=value_tok.text)


def parse_query(query: str) -> Node:
    """Parse a query string into an AST. Raises QueryError on any syntax
    or semantic problem (unknown field, bad operator, malformed value)."""
    if not query or not query.strip():
        raise QueryError("empty query")
    tokens = tokenize(query)
    return _Parser(tokens).parse()


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def _eval_comparison(node: Comparison, obj: dict) -> bool:
    if node.field not in obj:
        # Every field in KNOWN_FIELDS is expected to exist on every object
        # dict produced by GeoObject.to_dict(); if it's missing, that's a
        # real data-shape mismatch worth surfacing loudly rather than
        # quietly treating as "doesn't match."
        raise QueryError(f"object is missing expected field {node.field!r}")

    actual = obj[node.field]

    if node.field in STRING_FIELDS:
        actual_s = str(actual).lower()
        value_s = node.value.lower()
        if node.op in (":",):
            return value_s in actual_s
        if node.op == "=":
            return actual_s == value_s
        if node.op == "!=":
            return actual_s != value_s
        raise QueryError(f"unsupported operator {node.op!r} on string field")

    # numeric field
    actual_f = float(actual)
    value_f = float(node.value)
    if node.op in (":", "="):
        return actual_f == value_f
    if node.op == "!=":
        return actual_f != value_f
    if node.op == ">":
        return actual_f > value_f
    if node.op == ">=":
        return actual_f >= value_f
    if node.op == "<":
        return actual_f < value_f
    if node.op == "<=":
        return actual_f <= value_f
    raise QueryError(f"unsupported operator {node.op!r} on numeric field")


def evaluate(node: Node, obj: dict) -> bool:
    if isinstance(node, Comparison):
        return _eval_comparison(node, obj)
    if isinstance(node, And):
        return evaluate(node.left, obj) and evaluate(node.right, obj)
    if isinstance(node, Or):
        return evaluate(node.left, obj) or evaluate(node.right, obj)
    if isinstance(node, Not):
        return not evaluate(node.expr, obj)
    raise QueryError(f"unknown AST node type: {type(node).__name__}")


def filter_objects(objects: List[dict], query: str) -> List[dict]:
    """Parse `query` and return the subset of `objects` that match it.
    Raises QueryError if the query is malformed."""
    ast = parse_query(query)
    return [obj for obj in objects if evaluate(ast, obj)]
