import pytest

from weaver.frontend.typestr import contains_pointer, parse_type, resolve_typedefs


@pytest.mark.parametrize(
    "spelling, kind, inner",
    [
        ("int", "base", None),
        ("int *", "pointer", "base"),
        ("const int *", "pointer", "base"),
        ("int *const", "pointer", "base"),
        ("void (*)(int)", "pointer", "function"),
        ("int *[4]", "array", "pointer"),
        ("int (*)[4]", "pointer", "array"),
        ("char **", "pointer", "pointer"),
        ("int (*[3])(void)", "array", "pointer"),
        ("struct (unnamed at b.c:2:1) *", "pointer", "base"),
        ("_Atomic(int) *", "pointer", "base"),
        ("__attribute__((address_space(1))) int *", "pointer", "base"),
        ("int (int, char *, ...)", "function", "base"),
        ("int [2][3]", "array", "array"),
    ],
)
def test_structure(spelling, kind, inner):
    t = parse_type(spelling)
    assert t.kind == kind
    assert (t.inner.kind if t.inner else None) == inner


def test_qualifiers_attach_to_the_right_level():
    t = parse_type("volatile unsigned int *const volatile")
    assert t.quals == {"const", "volatile"}
    assert t.inner.quals == {"volatile"}
    assert t.inner.name == "unsigned int"


def test_function_pointer_returning_function_pointer():
    t = parse_type("void (*(*)(int))(char *)")
    assert t.is_function_pointer
    ret = t.inner.inner
    assert ret.is_function_pointer
    assert ret.inner.params[0].kind == "pointer"


def test_atomic_marks_pointee():
    t = parse_type("_Atomic(int) *")
    assert t.inner.atomic


def test_typedef_hidden_pointers():
    typedefs = {"iptr": "int *", "arr_t": "iptr [4]"}
    assert contains_pointer(parse_type("iptr"), typedefs)
    assert contains_pointer(parse_type("arr_t"), typedefs)
    assert not contains_pointer(parse_type("int"), typedefs)
    assert resolve_typedefs(parse_type("const iptr"), typedefs).kind == "pointer"
