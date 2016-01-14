from __future__ import division
from datetime import date
from threading import Thread
from time import sleep

from faunadb.errors import BadRequest, NotFound
from faunadb.objects import Event, FaunaTime, Set
from faunadb import query
from tests.helpers import FaunaTestCase

class QueryTest(FaunaTestCase):
  def setUp(self):
    super(QueryTest, self).setUp()

    self.class_ref = self.client.post("classes", {"name": "widgets"})["ref"]

    self.n_index_ref = self.client.post("indexes", {
      "name": "widgets_by_n",
      "source": self.class_ref,
      "path": "data.n",
      "active": True
    })["ref"]

    self.m_index_ref = self.client.post("indexes", {
      "name": "widgets_by_m",
      "source": self.class_ref,
      "path": "data.m",
      "active": True
    })["ref"]

    self.ref_n1 = self._create(n=1)["ref"]
    self.ref_m1 = self._create(m=1)["ref"]
    self.ref_n1m1 = self._create(n=1, m=1)["ref"]

    self.thimble_class_ref = self.client.post("classes", {"name": "thimbles"})["ref"]

  #region Helpers

  def _set_n(self, n):
    return query.match(n, self.n_index_ref)

  def _set_m(self, m):
    return query.match(m, self.m_index_ref)

  def _create(self, n=0, m=None):
    data = {"n": n} if m is None else {"n": n, "m": m}
    return self._q(query.create(self.class_ref, query.quote({"data": data})))

  def _create_thimble(self, data):
    return self._q(query.create(self.thimble_class_ref, query.quote({"data": data})))

  def _q(self, query_json):
    return self.client.query(query_json)

  def _set_to_list(self, _set):
    return self._q(query.paginate(_set, size=1000))["data"]

  def _assert_bad_query(self, q):
    self.assertRaises(BadRequest, lambda: self._q(q))

  #endregion

  #region Basic forms

  def test_let_var(self):
    assert self._q(query.let({"x": 1}, query.var("x"))) == 1

  def test_if(self):
    assert self._q(query.if_expr(True, "t", "f")) == "t"
    assert self._q(query.if_expr(False, "t", "f")) == "f"

  def test_do(self):
    ref = self._create()["ref"]
    assert self._q(query.do(query.delete(ref), 1)) == 1
    assert self._q(query.exists(ref)) is False

  def test_object(self):
    # Unlike query.quote, contents are evaluated.
    assert self._q(query.object(x=query.let({"x": 1}, query.var("x")))) == {"x": 1}

  def test_quote(self):
    quoted = query.let({"x": 1}, query.var("x"))
    assert self._q(query.quote(quoted)) == quoted

  def test_lambda_query(self):
    assert query.lambda_query(lambda a: query.add(a, a)) == {
      "lambda": "auto0", "expr": {"add": ({"var": "auto0"}, {"var": "auto0"})}
    }

    # pylint: disable=undefined-variable
    expected = query.lambda_query(
      lambda a: query.lambda_query(
        lambda b: query.lambda_query(
          lambda c: [a, b, c])))
    assert expected == {
      "lambda": "auto0",
      "expr": {
        "lambda": "auto1",
        "expr": {
          "lambda": "auto2",
          "expr": [{"var": "auto0"}, {"var": "auto1"}, {"var": "auto2"}]
        }
      }
    }

    # Error in lambda should not affect future queries.
    with self.assertRaises(Exception):
      def fail():
        raise Exception("foo")
      query.lambda_query(lambda a: fail())
    assert query.lambda_query(lambda a: a) == {"lambda": "auto0", "expr": {"var": "auto0"}}

  def test_lambda_query_multiple_args(self):
    expected = query.lambda_query(lambda a, b: [b, a])
    assert expected == {
      "lambda": ["auto0", "auto1"],
      "expr": [{"var": "auto1"}, {"var": "auto0"}]
    }

  def test_lambda_query_multithreaded(self):
    """Test that lambda_query works in simultaneous threads."""
    events = []

    def do_a():
      def do_lambda(a):
        events.append(0)
        sleep(1)
        events.append(2)
        return a
      assert query.lambda_query(do_lambda) == {"lambda": "auto0", "expr": {"var": "auto0"}}

    def do_b():
      # This happens while thread 'a' has incremented its auto name to auto1,
      # but that doesn't affect thread 'b'.
      assert query.lambda_query(lambda a: a) == {"lambda": "auto0", "expr": {"var": "auto0"}}
      events.append(1)

    t = Thread(name="a", target=do_a)
    t2 = Thread(name="b", target=do_b)
    t.start()
    t2.start()
    t.join()
    t2.join()

    # Assert that events happened in the order expected.
    assert events == [0, 1, 2]

  #endregion

  #region Collection functions

  def test_map(self):
    # This is also test_lambda_expr (can't test that alone)
    assert self._q(query.map_expr(lambda a: query.multiply(2, a), [1, 2, 3])) == [2, 4, 6]

    page = query.paginate(self._set_n(1))
    ns = query.map_expr(lambda a: query.select(["data", "n"], query.get(a)), page)
    assert self._q(ns) == {"data": [1, 1]}

  def test_foreach(self):
    refs = [self._create()["ref"], self._create()["ref"]]
    self._q(query.foreach(query.delete, refs))
    for ref in refs:
      assert self._q(query.exists(ref)) is False

  def test_filter(self):
    evens = query.filter_expr(lambda a: query.equals(query.modulo(a, 2), 0), [1, 2, 3, 4])
    assert self._q(evens) == [2, 4]

    # Works on page too
    page = query.paginate(self._set_n(1))
    refs_with_m = query.filter_expr(lambda a: query.contains(["data", "m"], query.get(a)), page)
    assert self._q(refs_with_m) == {"data": [self.ref_n1m1]}

  def test_take(self):
    assert self._q(query.take(1, [1, 2])) == [1]
    assert self._q(query.take(3, [1, 2])) == [1, 2]
    assert self._q(query.take(-1, [1, 2])) == []

  def test_drop(self):
    assert self._q(query.drop(1, [1, 2])) == [2]
    assert self._q(query.drop(3, [1, 2])) == []
    assert self._q(query.drop(-1, [1, 2])) == [1, 2]

  def test_prepend(self):
    assert self._q(query.prepend([1, 2, 3], [4, 5, 6])) == [1, 2, 3, 4, 5, 6]

  def test_append(self):
    assert self._q(query.append([4, 5, 6], [1, 2, 3])) == [1, 2, 3, 4, 5, 6]

  #endregion

  #region Read functions

  def test_get(self):
    instance = self._create()
    assert self._q(query.get(instance["ref"])) == instance

  def test_paginate(self):
    test_set = self._set_n(1)
    control = [self.ref_n1, self.ref_n1m1]
    assert self._q(query.paginate(test_set)) == {"data": control}

    data = []
    page1 = self._q(query.paginate(test_set, size=1))
    data.extend(page1["data"])
    page2 = self._q(query.paginate(test_set, size=1, after=page1["after"]))
    data.extend(page2["data"])
    assert data == control

    assert self._q(query.paginate(test_set, sources=True)) == {
      "data": [
        {"sources": [Set(test_set)], "value": self.ref_n1},
        {"sources": [Set(test_set)], "value": self.ref_n1m1}
      ]
    }

  def test_exists(self):
    ref = self._create()["ref"]
    assert self._q(query.exists(ref)) is True
    self._q(query.delete(ref))
    assert self._q(query.exists(ref)) is False

  def test_count(self):
    self._create(123)
    self._create(123)
    instances = self._set_n(123)
    # `count` is currently only approximate. Should be 2.
    assert isinstance(self._q(query.count(instances)), int)

  #endregion

  #region Write functions

  def test_create(self):
    instance = self._create()
    assert "ref" in instance
    assert "ts" in instance
    assert instance["class"] == self.class_ref

  def test_update(self):
    ref = self._create()["ref"]
    got = self._q(query.update(ref, query.quote({"data": {"m": 1}})))
    assert got["data"] == {"n": 0, "m": 1}

  def test_replace(self):
    ref = self._create()["ref"]
    got = self._q(query.replace(ref, query.quote({"data": {"m": 1}})))
    assert got["data"] == {"m": 1}

  def test_delete(self):
    ref = self._create()["ref"]
    self._q(query.delete(ref))
    assert self._q(query.exists(ref)) is False

  #endregion

  #region Sets

  def test_insert(self):
    instance = self._create_thimble({"weight": 1})
    ref = instance["ref"]
    ts = instance["ts"]
    prev_ts = ts - 1

    # Add previous event
    inserted = query.quote({"data": {"weight": 0}})
    add = query.insert(ref, prev_ts, "create", inserted)
    self._q(add)
    # Test alternate syntax
    assert query.insert_event(Event(ref, prev_ts, "create"), inserted) == add

    # Get version from previous event
    old = self._q(query.get(ref, ts=prev_ts))
    assert old["data"] == {"weight": 0}

  def test_remove(self):
    instance = self._create_thimble({"weight": 0})
    ref = instance["ref"]

    # Change it
    new_instance = self._q(query.replace(ref, query.quote({"data": {"weight": 1}})))
    assert self._q(query.get(ref)) == new_instance

    # Delete that event
    remove = query.remove(ref, new_instance["ts"], "create")
    self._q(remove)
    # Test alternate syntax
    assert query.remove_event(Event(ref, new_instance["ts"], "create")) == remove

    # Assert that it was undone
    assert self._q(query.get(ref)) == instance

  def test_match(self):
    q = self._set_n(1)
    assert self._set_to_list(q) == [self.ref_n1, self.ref_n1m1]

  def test_union(self):
    q = query.union(self._set_n(1), self._set_m(1))
    assert self._set_to_list(q) == [self.ref_n1, self.ref_m1, self.ref_n1m1]

  def test_intersection(self):
    q = query.intersection(self._set_n(1), self._set_m(1))
    assert self._set_to_list(q) == [self.ref_n1m1]

  def test_difference(self):
    q = query.difference(self._set_n(1), self._set_m(1))
    assert self._set_to_list(q) == [self.ref_n1] # but not self.ref_n1m1

  def test_join(self):
    referenced = [self._create(n=12)["ref"], self._create(n=12)["ref"]]
    referencers = [self._create(m=referenced[0])["ref"], self._create(m=referenced[1])["ref"]]

    source = self._set_n(12)
    assert self._set_to_list(source) == referenced

    # For each obj with n=12, get the set of elements whose data.m refers to it.
    joined = query.join(source, lambda a: query.match(a, self.m_index_ref))
    assert self._set_to_list(joined) == referencers

  #endregion

  #region String functions

  def test_concat(self):
    assert self._q(query.concat(["a", "b", "c"])) == "abc"
    assert self._q(query.concat([])) == ""
    assert self._q(query.concat(["a", "b", "c"], ".")) == "a.b.c"

  def test_casefold(self):
    assert self._q(query.casefold("Hen Wen")) == "hen wen"

  #endregion

  #region Time and date functions

  def test_time(self):
    time = "1970-01-01T00:00:00.123456789Z"
    assert self._q(query.time(time)) == FaunaTime(time)

    # "now" refers to the current time.
    assert isinstance(self._q(query.time("now")), FaunaTime)

  def test_epoch(self):
    assert self._q(query.epoch(12, "second")) == FaunaTime("1970-01-01T00:00:12Z")
    nano_time = FaunaTime("1970-01-01T00:00:00.123456789Z")
    assert self._q(query.epoch(123456789, "nanosecond")) == nano_time

  def test_date(self):
    assert self._q(query.date("1970-01-01")) == date(1970, 1, 1)

  #endregion

  #region Miscellaneous functions

  def test_equals(self):
    assert self._q(query.equals(1, 1, 1)) is True
    assert self._q(query.equals(1, 1, 2)) is False
    assert self._q(query.equals(1)) is True
    self._assert_bad_query(query.equals())

  def test_contains(self):
    obj = query.quote({"a": {"b": 1}})
    assert self._q(query.contains(["a", "b"], obj)) is True
    assert self._q(query.contains("a", obj)) is True
    assert self._q(query.contains(["a", "c"], obj)) is False

  def test_select(self):
    obj = query.quote({"a": {"b": 1}})
    assert self._q(query.select("a", obj)) == {"b": 1}
    assert self._q(query.select(["a", "b"], obj)) == 1
    assert self._q(query.select_with_default("c", obj, None)) is None
    self.assertRaises(NotFound, lambda: self._q(query.select("c", obj)))

  def test_select_array(self):
    arr = [1, 2, 3]
    assert self._q(query.select(2, arr)) == 3
    self.assertRaises(NotFound, lambda: self._q(query.select(3, arr)))

  def test_add(self):
    assert self._q(query.add(2, 3, 5)) == 10
    self._assert_bad_query(query.add())

  def test_multiply(self):
    assert self._q(query.multiply(2, 3, 5)) == 30
    self._assert_bad_query(query.multiply())

  def test_subtract(self):
    assert self._q(query.subtract(2, 3, 5)) == -6
    assert self._q(query.subtract(2)) == 2
    self._assert_bad_query(query.subtract())

  def test_divide(self):
    assert self._q(query.divide(2.0, 3, 5)) == 2 / 15
    assert self._q(query.divide(2)) == 2
    self._assert_bad_query(query.divide(1, 0))
    self._assert_bad_query(query.divide())

  def test_modulo(self):
    assert self._q(query.modulo(5, 2)) == 1
    # This is (15 % 10) % 2
    assert self._q(query.modulo(15, 10, 2)) == 1
    assert self._q(query.modulo(2)) == 2
    self._assert_bad_query(query.modulo(1, 0))
    self._assert_bad_query(query.modulo())

  def test_and(self):
    assert self._q(query.and_expr(True, True, False)) is False
    assert self._q(query.and_expr(True, True, True)) is True
    assert self._q(query.and_expr(True)) is True
    assert self._q(query.and_expr(False)) is False
    self._assert_bad_query(query.and_expr())

  def test_or(self):
    assert self._q(query.or_expr(False, False, True)) is True
    assert self._q(query.or_expr(False, False, False)) is False
    assert self._q(query.or_expr(True)) is True
    assert self._q(query.or_expr(False)) is False
    self._assert_bad_query(query.or_expr())

  def test_not(self):
    assert self._q(query.not_expr(True)) is False
    assert self._q(query.not_expr(False)) is True

  #endregion

  def test_varargs(self):
    # Works for lists too
    assert self._q(query.add([2, 3, 5])) == 10
    # Works for a variable equal to a list
    assert self._q(query.let({"x": [2, 3, 5]}, query.add(query.var("x")))) == 10