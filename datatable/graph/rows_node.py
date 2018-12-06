#!/usr/bin/env python3
# © H2O.ai 2018; -*- encoding: utf-8 -*-
#   This Source Code Form is subject to the terms of the Mozilla Public
#   License, v. 2.0. If a copy of the MPL was not distributed with this
#   file, You can obtain one at http://mozilla.org/MPL/2.0/.
#-------------------------------------------------------------------------------
import types

import datatable
from datatable.lib import core
from .iterator_node import IteratorNode
from datatable.expr import BaseExpr
from datatable.graph.dtproxy import f
from .context import LlvmEvaluationEngine
from datatable.types import stype, ltype
from datatable.utils.misc import normalize_slice, normalize_range
from datatable.utils.misc import plural_form as plural
from datatable.utils.typechecks import (
    is_type, TValueError, TTypeError, Frame_t, NumpyArray_t
)
from typing import Optional



#===============================================================================

class RFNode:
    """
    Base class for all "Row Filter" nodes (internal).

    A row filter node represents a `rows` argument in the generic datatable
    call, and its primary function is to compute a :class:`core.RowIndex`
    object and place it into the EvaluationEngine.

    A row filter is always applied to some Frame, called "source". Sometimes
    the source is a view, in which case the rowindex must be "uplifted" to the
    parent Frame. The RowIndex created by this node will be the "final"
    one, i.e. it will be indexing the data within the columns of the source
    Frame.

    API:
      - execute(): construct the final RowIndex and store it in the
            EvaluationEngine context.

    The primary way of constructing instances of this class is through the
    factory function :func:`make_rowfilter`.

    Parameters
    ----------
    ee: EvaluationEngine
        The context for the current evaluation.
    ...:
        (Derived classes will typically add their own constructor parameters).
    """
    __slots__ = ["_engine", "_inverse"]

    def __init__(self, ee):
        self._engine = ee
        self._inverse = False

    def negate(self):
        self._inverse = not self._inverse

    def execute(self):
        ee = self._engine
        srcri = self._make_source_rowindex()
        ee.set_source_rowindex(srcri)

        ri_target = ee.dt.internal.rowindex
        rowindex = self._make_final_rowindex(srcri)
        ee.set_final_rowindex(rowindex, ri_target)
        ee.rowindex = rowindex
        f.set_rowindex(rowindex)


    def _make_source_rowindex(self) -> Optional[core.RowIndex]:
        """
        Construct the "source" RowIndex object.

        This method must be implemented in all subclasses. It should return a
        core.RowIndex object as applied to the source Frame, or None if no
        index is necessary.
        """
        raise NotImplementedError  # pragma: no cover


    def _make_final_rowindex(self, ri_source) -> Optional[core.RowIndex]:
        """
        Construct the "final" RowIndex object.

        If the source Frame is a view, then the returned RowIndex is an
        uplifted version of the "source" RowIndex. Otherwise the final RowIndex
        is the same object as the source RowIndex. The returned value may also
        be None indicating absense of any RowIndex.
        """
        _dt = self._engine.dt.internal
        ri_target = _dt.rowindex

        if ri_source is None:
            if self._inverse:
                return core.rowindex_from_slice(0, 0, 0)
            else:
                return ri_target

        ri_final = ri_source
        if self._inverse:
            ri_final = ri_final.inverse(self._engine.dt.nrows)
        if ri_target:
            ri_final = ri_final.uplift(ri_target)
        return ri_final





#===============================================================================

class AllRFNode(RFNode):
    """
    RFNode representing selection of all rows from the datatable.

    Although "all rows" selector can easily be implemented as a slice, we want
    to have a separate class because (1) this is a very common selector type,
    and (2) in some cases useful optimizations can be achieved if we know that
    all rows are selected from a datatable.
    """
    __slots__ = []

    def _make_source_rowindex(self):
        return None




#===============================================================================

class SliceRFNode(RFNode):
    """
    RFNode representing a slice subset of target's rows.

    Parameters
    ----------
    ee: EvaluationEngine
        Current evaluation context.

    start, count, step: int
        The parameters of the slice. The slice represents a list of integers
        `[start + i*step for i in range(count)]`. Here `step` can be positive,
        negative, or even zero; however all indices generated by the slice must
        be in the range `[0; dt.nrows)`.
    """
    __slots__ = ["_triple"]

    def __init__(self, ee, start, count, step):
        super().__init__(ee)
        assert start >= 0
        assert count >= 0
        assert start + (count - 1) * step >= 0
        self._triple = (start, count, step)

    def _make_source_rowindex(self):
        return core.rowindex_from_slice(*self._triple)



#===============================================================================

class ArrayRFNode(RFNode):
    """
    RFNode selecting rows from the target via an explicit list of indices.

    Parameters
    ----------
    ee: EvaluationEngine
        Current evaluation context.

    array: List[int]
        The list of row indices that should be selected from the target
        Frame. The indices must be in the `range(dt.nrows)` (however this
        constraint is not verified here).
    """
    __slots__ = ["_array"]

    def __init__(self, ee, array):
        super().__init__(ee)
        self._array = array

    def _make_source_rowindex(self):
        return core.rowindex_from_array(self._array)




#===============================================================================

class MultiSliceRFNode(RFNode):
    """
    RFNode representing selection of rows via a list of slices.

    This class is a generalized version of :class:`SliceRFNode` and
    :class:`ArrayRFNode`.

    Parameters
    ----------
    ee: EvaluationEngine
        Current evaluation context.

    bases, counts, steps: List[int]
        Three lists describing the row slices to be selected. In particular,
        each triple `(bases[i], counts[i], steps[i])` describes one slice. Lists
        `counts` and `steps` must have equal lengths, but may be shorter than
        `bases` (in which case it is assumed that missing elements in `counts`
        and `steps` are equal to 1).
    """
    __slots__ = ["_bases", "_counts", "_steps"]

    def __init__(self, ee, bases, counts, steps):
        super().__init__(ee)
        self._bases = bases
        self._counts = counts
        self._steps = steps

    def _make_source_rowindex(self):
        return core.rowindex_from_slicelist(
            self._bases, self._counts, self._steps
        )




#===============================================================================

class BooleanColumnRFNode(RFNode):
    """
    RFNode that selects rows according to the provided boolean mask.

    Parameters
    ----------
    ee: EvaluationEngine
        Current evaluation context.

    col: Frame
        The "mask" Frame containing a single boolean column of the same
        length as the target Frame. Only rows corresponding to the `True`
        values in the mask will be selected.
    """
    __slots__ = ["_coldt"]

    def __init__(self, ee, col):
        super().__init__(ee)
        assert col.shape == (ee.dt.nrows, 1)
        self._coldt = col

    def _make_source_rowindex(self):
        col = self._coldt.internal.column(0)
        return core.rowindex_from_column(col)




#===============================================================================

class IntegerColumnRFNode(RFNode):
    """
    RFNode that treats the provided integer column as a RowIndex.

    Parameters
    ----------
    ee: EvaluationEngine
        Current evaluation context.

    coldt: Frame
        Frame containing a single integer column, the values in this column
        will be treated as row indices to select.
    """
    __slots__ = ["_coldt"]

    def __init__(self, ee, coldt):
        super().__init__(ee)
        assert coldt.ncols == 1
        self._coldt = coldt

    def _make_source_rowindex(self):
        col = self._coldt.internal.column(0)
        ri = core.rowindex_from_column(col)
        if ri.max >= self._engine.dt.nrows:
            raise ValueError("The data column contains index %d which is "
                             "not allowed for a Frame with %d rows"
                             % (ri.max, self._engine.dt.nrows))
        return ri




#===============================================================================

class FilterExprRFNode(RFNode):
    """
    RFNode that creates a RowIndex out of the provided expression.

    This node will select those rows for which the provided expression returns
    True when evaluated. Thus, it is equivalent to first evaluating the provided
    expression as a boolean column, and then passing it to a
    :class:`BooleanColumnRFNode`.

    Parameters
    ----------
    ee: EvaluationEngine
        Current evaluation context.

    expr: BaseExpr
        Expression (yielding a boolean column) that will be evaluated in order
        to construct the RowIndex.
    """
    __slots__ = ["_fnname", "_expr"]

    def __init__(self, ee, expr):
        super().__init__(ee)
        expr.resolve()
        assert expr.stype == stype.bool8
        self._expr = expr
        self._fnname = None
        if isinstance(ee, LlvmEvaluationEngine):
            self._fnname = ee.make_variable_name("make_rowindex")
            ee.add_node(self)


    def _make_final_rowindex(self, ri_source):
        assert ri_source == NotImplemented
        ee = self._engine
        nrows = ee.dt.nrows
        if isinstance(ee, LlvmEvaluationEngine):
            ptr = ee.get_result(self._fnname)
            return core.rowindex_from_filterfn(ptr, nrows)
        else:
            col = self._expr.evaluate_eager(self._engine)
            rowindex = core.rowindex_from_column(col)
            if self._inverse:
                rowindex = rowindex.inverse(nrows)
            if ee.rowindex:
                rowindex = rowindex.uplift(ee.rowindex)
            return rowindex


    def _make_source_rowindex(self):
        return NotImplemented


    def generate_c(self) -> None:
        """
        This method will be invoked by LlvmEvaluationEngine during code
        generation.
        """
        dt = self._engine.dt
        ee = self._engine
        assert isinstance(ee, LlvmEvaluationEngine)
        inode = IteratorNode(dt, ee, name=self._fnname)
        v = self._expr.value_or_0(inode=inode)
        inode.addto_preamble("size_t j = 0;")
        inode.addto_mainloop("if (%s) {" % v)
        inode.addto_mainloop("    out[j++] = i;")
        inode.addto_mainloop("}")
        inode.addto_epilogue("*n_outs = j;")
        inode.set_extra_args("int32_t* out, size_t* n_outs")
        inode.generate_c()

        # rowindex_name = ee.make_variable_name("rowindex")
        # ee.add_global(rowindex_name, "void*", "NULL")
        # ee.add_function(
        #     self._fnname,
        #     "void* {fnname}(void) {{\n"
        #     "    if (!{riname})\n"
        #     "        {riname} = rowindex_from_filterfn32(\n"
        #     "                       (void*) {filter}, {nrows}, {sorted});\n"
        #     "    return {riname};\n"
        #     "}}".format(fnname=self._fnname,
        #                 riname=rowindex_name,
        #                 filter=inode.fnname,
        #                 sorted=int(not dt.internal.isview),
        #                 nrows=dt.nrows))




#===============================================================================

class SortedRFNode(RFNode):

    def __init__(self, sort_node):
        super().__init__(sort_node.engine)
        self._sortnode = sort_node

    def execute(self):
        ee = self._engine
        _dt = ee.dt.internal
        ri_target = _dt.column(self._sortnode.colidx).rowindex

        finalri = self._sortnode.make_rowindex()
        ee.set_source_rowindex(NotImplemented)
        ee.set_final_rowindex(finalri, ri_target)
        ee.rowindex = finalri
        f.set_rowindex(finalri)




#===============================================================================
# Factory function
#===============================================================================

def make_rowfilter(rows, ee, _nested=False) -> RFNode:
    """
    Create an :class:`RFNode` from the provided expression.

    This is a factory function that instantiates an appropriate subclass of
    :class:`RFNode`, depending on the provided argument `rows`.

    Parameters
    ----------
    rows:
        An expression that will be converted into one of the RFNodes. This can
        have a variety of different types, see `help(Frame.__call__)` for
        more information.

    ee: EvaluationEngine
        The evaluation context within which the expression should be computed.

    _nested: bool, default False
        Internal attribute, used to avoid deep recursion when `make_rowfilter()`
        calls itself. When this attribute is False recursion is allowed,
        otherwise not.
    """
    nrows = ee.dt.nrows
    if rows is Ellipsis or rows is None:
        return AllRFNode(ee)

    if rows is True or rows is False:
        # Note: True/False are integer objects in Python
        raise TTypeError("Boolean value cannot be used as a `rows` selector")

    if isinstance(rows, (int, slice, range)):
        rows = [rows]

    from_generator = False
    if isinstance(rows, types.GeneratorType):
        # If an iterator is given, materialize it first. Otherwise there
        # is no way to ensure that the produced indices are valid.
        rows = list(rows)
        from_generator = True

    if isinstance(rows, (list, tuple, set)):
        bases = []
        counts = []
        steps = []
        for i, elem in enumerate(rows):
            if isinstance(elem, int):
                if -nrows <= elem < nrows:
                    # `elem % nrows` forces the row number to become positive
                    bases.append(elem % nrows)
                else:
                    raise TValueError(
                        "Row `%d` is invalid for datatable with %s"
                        % (elem, plural(nrows, "row")))
            elif isinstance(elem, (range, slice)):
                if not all(x is None or isinstance(x, int)
                           for x in (elem.start, elem.stop, elem.step)):
                    raise TValueError("%r is not integer-valued" % elem)
                if isinstance(elem, range):
                    res = normalize_range(elem, nrows)
                    if res is None:
                        raise TValueError(
                            "Invalid %r for a datatable with %s"
                            % (elem, plural(nrows, "row")))
                else:
                    res = normalize_slice(elem, nrows)
                start, count, step = res
                assert count >= 0
                if count == 0:
                    pass  # don't do anything
                elif count == 1:
                    bases.append(start)
                else:
                    if len(counts) < len(bases):
                        counts += [1] * (len(bases) - len(counts))
                        steps += [1] * (len(bases) - len(steps))
                    bases.append(start)
                    counts.append(count)
                    steps.append(step)
            else:
                if from_generator:
                    raise TValueError(
                        "Invalid row selector %r generated at position %d"
                        % (elem, i))
                else:
                    raise TValueError(
                        "Invalid row selector %r at element %d of the "
                        "`rows` list" % (elem, i))
        if not counts:
            if len(bases) == 1:
                if bases[0] == 0 and nrows == 1:
                    return AllRFNode(ee)
                return SliceRFNode(ee, bases[0], 1, 1)
            else:
                return ArrayRFNode(ee, bases)
        elif len(bases) == 1:
            if bases[0] == 0 and counts[0] == nrows and steps[0] == 1:
                return AllRFNode(ee)
            else:
                return SliceRFNode(ee, bases[0], counts[0], steps[0])
        else:
            return MultiSliceRFNode(ee, bases, counts, steps)

    if is_type(rows, NumpyArray_t):
        arr = rows
        if not (len(arr.shape) == 1 or
                len(arr.shape) == 2 and min(arr.shape) == 1):
            raise TValueError("Only a single-dimensional numpy.array is allowed"
                              " as a `rows` argument, got %r" % arr)
        if len(arr.shape) == 2 and arr.shape[1] > 1:
            arr = arr.T
        if not (str(arr.dtype) == "bool" or str(arr.dtype).startswith("int")):
            raise TValueError("Either a boolean or an integer numpy.array is "
                              "expected for `rows` argument, got %r" % arr)
        if str(arr.dtype) == "bool" and arr.shape[-1] != nrows:
            raise TValueError("Cannot apply a boolean numpy array of length "
                              "%d to a datatable with %s"
                              % (arr.shape[-1], plural(nrows, "row")))
        rows = datatable.Frame(arr)
        assert rows.ncols == 1
        assert rows.ltypes[0] == ltype.bool or rows.ltypes[0] == ltype.int

    if is_type(rows, Frame_t):
        if rows.ncols != 1:
            raise TValueError("`rows` argument should be a single-column "
                              "datatable, got %r" % rows)
        col0type = rows.ltypes[0]
        if col0type == ltype.bool:
            if rows.nrows != nrows:
                s1rows = plural(rows.nrows, "row")
                s2rows = plural(nrows, "row")
                raise TValueError("`rows` datatable has %s, but applied to a "
                                  "datatable with %s" % (s1rows, s2rows))
            return BooleanColumnRFNode(ee, rows)
        elif col0type == ltype.int:
            return IntegerColumnRFNode(ee, rows)
        else:
            raise TTypeError("`rows` datatable should be either a boolean or "
                             "an integer column, however it has type %s"
                             % col0type)

    if isinstance(rows, types.FunctionType):
        return make_rowfilter(rows(f), ee, _nested=True)

    if isinstance(rows, BaseExpr):
        return FilterExprRFNode(ee, rows)

    if _nested:
        raise TTypeError("Unexpected result produced by the `rows` "
                         "function: %r" % (rows, ))
    else:
        raise TTypeError("Unexpected `rows` argument: %r" % (rows, ))
