"""规范 JSON（RFC 8785 子集）验收。

摘要只有在"同样的值永远算出同样的字节"时才可信。常见的三个坑：
键顺序、转义形式、以及 UTF-16 码元序与码点序的区别。
"""

from __future__ import annotations

import pytest

from tianshu.canonical import JcsError, jcs_bytes, jcs_dumps, jcs_sha256


def test_object_keys_are_sorted():
    assert jcs_dumps({"b": 1, "a": 2, "c": 3}) == '{"a":2,"b":1,"c":3}'


def test_output_has_no_whitespace():
    assert jcs_dumps({"a": [1, 2, {"b": None}]}) == '{"a":[1,2,{"b":null}]}'


def test_primitives():
    assert jcs_dumps(None) == "null"
    assert jcs_dumps(True) == "true"
    assert jcs_dumps(False) == "false"
    assert jcs_dumps(0) == "0"
    assert jcs_dumps(-7) == "-7"
    assert jcs_dumps("x") == '"x"'


def test_bool_is_not_rendered_as_integer():
    """Python 里 bool 是 int 的子类，顺序写错就会输出 1/0。"""
    assert jcs_dumps([True, False, 1, 0]) == "[true,false,1,0]"


def test_string_escapes():
    assert jcs_dumps('"') == '"\\""'
    assert jcs_dumps("\\") == '"\\\\"'
    assert jcs_dumps("\b\f\n\r\t") == '"\\b\\f\\n\\r\\t"'
    assert jcs_dumps("\x00\x1f") == '"\\u0000\\u001f"'


def test_non_ascii_is_preserved_not_escaped():
    assert jcs_dumps("€中😀") == '"€中😀"'
    assert jcs_bytes("中") == '"中"'.encode()


def test_key_order_uses_utf16_code_units():
    """U+10000 在 UTF-16 里是 D800 DC00，应排在 U+FFFF 之前。

    按 Unicode 码点排序会得到相反顺序——这正是不能用 sort_keys=True 的原因。
    """
    text = jcs_dumps({"\uffff": 1, "\U00010000": 2})
    assert text.index("\U00010000") < text.index("\uffff")


def test_nested_structures():
    value = {"z": [3, {"b": True}], "a": {"y": None, "x": "s"}}
    assert jcs_dumps(value) == '{"a":{"x":"s","y":null},"z":[3,{"b":true}]}'


def test_deterministic_regardless_of_insertion_order():
    a = {"one": 1, "two": {"x": 1, "y": 2}}
    b = {"two": {"y": 2, "x": 1}, "one": 1}
    assert jcs_bytes(a) == jcs_bytes(b)
    assert jcs_sha256(a) == jcs_sha256(b)


def test_tuples_behave_like_arrays():
    assert jcs_dumps(("a", 1)) == jcs_dumps(["a", 1])


@pytest.mark.parametrize(
    "bad",
    [
        1.5,
        float("nan"),
        float("inf"),
        2**53,
        -(2**53),
        "\ud800",
    ],
)
def test_unsupported_inputs_are_rejected(bad):
    """宁可拒绝，也不要算出一个别的实现会算成不同值的摘要。"""
    with pytest.raises(JcsError):
        jcs_dumps(bad)


def test_non_string_key_is_rejected():
    with pytest.raises(JcsError):
        jcs_dumps({1: "a"})


def test_unknown_type_is_rejected():
    class Weird:
        pass

    with pytest.raises(JcsError):
        jcs_dumps(Weird())


def test_max_safe_integer_is_allowed():
    assert jcs_dumps(2**53 - 1) == str(2**53 - 1)


def test_digest_is_sha256_of_canonical_bytes():
    import hashlib

    value = {"a": 1, "b": ["x"]}
    assert jcs_sha256(value) == hashlib.sha256(jcs_bytes(value)).hexdigest()
    assert len(jcs_sha256(value)) == 64