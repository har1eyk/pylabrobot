"""Small binary32 spectral least-squares calculations without a vendor runtime."""

import math

from .calibration import _float32 as f32


def _add(a: float, b: float) -> float:
  """Add with one binary32 rounding."""
  return f32(a + b)


def _sub(a: float, b: float) -> float:
  """Subtract with one binary32 rounding."""
  return f32(a - b)


def _mul(a: float, b: float) -> float:
  """Multiply with one binary32 rounding."""
  return f32(a * b)


def _div(a: float, b: float) -> float:
  """Divide with one binary32 rounding, rejecting a singular intermediate."""
  if b == 0:
    raise ValueError("Singular spectral matrix")
  return f32(a / b)


def _signed(magnitude: float, sign: float) -> float:
  """Transfer the sign, treating negative zero as nonnegative."""
  return abs(magnitude) if sign >= 0 else -abs(magnitude)


def _hypot(a: float, b: float) -> float:
  """Compute the scaled norm with a binary32 ratio and binary64 square root."""
  high, low = max(abs(a), abs(b)), min(abs(a), abs(b))
  return f32(high * math.sqrt(1 + _div(low, high) ** 2)) if high else 0.0


def _dot(left: list[float], right: list[float], *, reverse: bool = False) -> float:
  """Accumulate products with separate binary32 multiply/add operations."""
  result = 0.0
  indices = range(len(left) - 1, -1, -1) if reverse else range(len(left))
  for index in indices:
    result = _add(result, _mul(left[index], right[index]))
  return result


def _rotate(matrix: list[list[float]], first: int, second: int, cosine: float, sine: float) -> None:
  """Apply a plane rotation to two columns with explicit binary32 rounding."""
  for row in matrix:
    x, y = row[first], row[second]
    row[first] = _add(_mul(x, cosine), _mul(y, sine))
    row[second] = _sub(_mul(y, cosine), _mul(x, sine))


def _svd(
  matrix: tuple[tuple[float, ...], ...],
) -> tuple[list[list[float]], list[float], list[list[float]]]:
  """Bidiagonalize a small tall matrix and diagonalize it by bounded QR iteration.

  U and V contain singular vectors as columns. All arithmetic except the scaled norms
  and square roots uses binary32 operations. Singular values retain their iteration order.
  """
  u = [list(map(f32, row)) for row in matrix]
  rows, columns = len(u), len(u[0])
  v = [[0.0] * columns for _ in range(columns)]
  diagonal, off = [0.0] * columns, [0.0] * columns
  scale = previous = norm = 0.0
  for index in range(columns):
    off[index] = _mul(scale, previous)
    scale = 0.0
    for row in range(index, rows):
      scale = _add(scale, abs(u[row][index]))
    previous = 0.0
    if scale:
      for row in range(index, rows):
        u[row][index] = _div(u[row][index], scale)
      column = [u[row][index] for row in range(index, rows)]
      squared = _dot(column, column)
      first = u[index][index]
      previous = -_signed(f32(math.sqrt(squared)), first)
      denominator = _sub(_mul(first, previous), squared)
      u[index][index] = _sub(first, previous)
      for other in range(index + 1, columns):
        product = _dot(
          [u[row][index] for row in range(index, rows)],
          [u[row][other] for row in range(index, rows)],
        )
        factor = _div(product, denominator)
        for row in range(index, rows):
          u[row][other] = _add(u[row][other], _mul(factor, u[row][index]))
      for row in range(index, rows):
        u[row][index] = _mul(u[row][index], scale)
    diagonal[index] = _mul(scale, previous)
    scale = previous = 0.0
    if index + 1 < columns:
      for column_index in range(index + 1, columns):
        scale = _add(scale, abs(u[index][column_index]))
      if scale:
        for column_index in range(index + 1, columns):
          u[index][column_index] = _div(u[index][column_index], scale)
        segment = u[index][index + 1 :]
        squared = _dot(segment, segment)
        first = u[index][index + 1]
        previous = -_signed(f32(math.sqrt(squared)), first)
        denominator = _sub(_mul(first, previous), squared)
        u[index][index + 1] = _sub(first, previous)
        for column_index in range(index + 1, columns):
          off[column_index] = _div(u[index][column_index], denominator)
        for row in range(index + 1, rows):
          product = _dot(u[row][index + 1 :], u[index][index + 1 :])
          for column_index in range(index + 1, columns):
            u[row][column_index] = _add(u[row][column_index], _mul(product, off[column_index]))
        for column_index in range(index + 1, columns):
          u[index][column_index] = _mul(u[index][column_index], scale)
    norm = max(norm, _add(abs(diagonal[index]), abs(off[index])))

  # Accumulate the right and left Householder transformations.
  for index in range(columns - 1, -1, -1):
    if index + 1 < columns:
      if previous:
        for row in range(index + 1, columns):
          v[row][index] = _div(_div(u[index][row], u[index][index + 1]), previous)
        for column_index in range(index + 1, columns):
          product = _dot(
            u[index][index + 1 :],
            [v[row][column_index] for row in range(index + 1, columns)],
          )
          for row in range(index + 1, columns):
            v[row][column_index] = _add(v[row][column_index], _mul(product, v[row][index]))
      for row in range(index + 1, columns):
        v[row][index] = v[index][row] = 0.0
    v[index][index] = 1.0
    previous = off[index]
  for index in range(columns - 1, -1, -1):
    divisor = diagonal[index]
    for column_index in range(index + 1, columns):
      u[index][column_index] = 0.0
    if divisor:
      inverse = _div(1.0, divisor)
      for column_index in range(index + 1, columns):
        product = _dot(
          [u[row][index] for row in range(index + 1, rows)],
          [u[row][column_index] for row in range(index + 1, rows)],
        )
        factor = _mul(_div(product, u[index][index]), inverse)
        for row in range(index, rows):
          u[row][column_index] = _add(u[row][column_index], _mul(factor, u[row][index]))
      for row in range(index, rows):
        u[row][index] = _mul(u[row][index], inverse)
    else:
      for row in range(index, rows):
        u[row][index] = 0.0
    u[index][index] = _add(u[index][index], 1.0)

  for end in range(columns - 1, -1, -1):
    for _ in range(30):
      split = end
      cancel = True
      while split >= 0:
        if _add(abs(off[split]), norm) == norm:
          cancel = False
          break
        if split == 0:
          raise ValueError("Invalid spectral SVD split")
        if _add(abs(diagonal[split - 1]), norm) == norm:
          break
        split -= 1
      if cancel:
        cosine, sine = 0.0, 1.0
        for index in range(split, end + 1):
          carried = _mul(sine, off[index])
          off[index] = _mul(cosine, off[index])
          if _add(abs(carried), norm) == norm:
            break
          value = diagonal[index]
          diagonal[index] = _hypot(carried, value)
          inverse = _div(1.0, diagonal[index])
          cosine, sine = _mul(value, inverse), _mul(-carried, inverse)
          _rotate(u, split - 1, index, cosine, sine)
      last = diagonal[end]
      if split == end:
        if last < 0:
          diagonal[end] = -last
          for vector in v:
            vector[end] = -vector[end]
        break
      first, penultimate = diagonal[split], diagonal[end - 1]
      before, tail = off[end - 1], off[end]
      shift = _div(
        _add(
          _mul(_sub(penultimate, last), _add(penultimate, last)),
          _mul(_sub(before, tail), _add(before, tail)),
        ),
        _mul(_mul(2.0, tail), penultimate),
      )
      hypotenuse = _hypot(shift, 1.0)
      carried = _div(
        _add(
          _mul(_sub(first, last), _add(first, last)),
          _mul(tail, _sub(_div(penultimate, _add(shift, _signed(hypotenuse, shift))), tail)),
        ),
        first,
      )
      cosine = sine = 1.0
      for index in range(split, end):
        value, next_diagonal = off[index + 1], diagonal[index + 1]
        height = _mul(sine, value)
        value = _mul(cosine, value)
        off[index] = _hypot(carried, height)
        cosine, sine = _div(carried, off[index]), _div(height, off[index])
        carried = _add(_mul(first, cosine), _mul(value, sine))
        value = _sub(_mul(value, cosine), _mul(first, sine))
        height = _mul(next_diagonal, sine)
        next_diagonal = _mul(next_diagonal, cosine)
        _rotate(v, index, index + 1, cosine, sine)
        diagonal[index] = _hypot(carried, height)
        if diagonal[index]:
          inverse = _div(1.0, diagonal[index])
          cosine, sine = _mul(carried, inverse), _mul(height, inverse)
        carried = _add(_mul(cosine, value), _mul(sine, next_diagonal))
        first = _sub(_mul(cosine, next_diagonal), _mul(sine, value))
        _rotate(u, index, index + 1, cosine, sine)
      off[split], off[end], diagonal[end] = 0.0, carried, first
    else:
      raise ValueError("Spectral SVD did not converge in 30 iterations")
  return u, diagonal, v


def spectral_inverse(
  matrix: tuple[tuple[float, ...], ...], max_condition: float
) -> tuple[tuple[tuple[float, ...], ...], float]:
  """Return the binary32 least-squares inverse and condition number.

  Require at least as many measured filters as dyes. Reject singular or ill-conditioned
  matrices instead of amplifying noise through unbounded reciprocal singular values.
  """
  if (
    not matrix
    or not matrix[0]
    or len(matrix) > 5
    or len(matrix[0]) > len(matrix)
    or any(len(row) != len(matrix[0]) for row in matrix)
    or any(not math.isfinite(value) for row in matrix for value in row)
    or not math.isfinite(max_condition)
    or max_condition < 1
  ):
    raise ValueError("Expected a finite spectral matrix with 1–5 filters and no more dyes")
  u, values, v = _svd(matrix)
  if min(values) <= 0:
    raise ValueError("Singular spectral matrix")
  condition = _div(max(values), min(values))
  if condition > max_condition:
    raise ValueError("Ill-conditioned spectral matrix")
  scaled = [[_mul(value, _div(1.0, values[i])) for i, value in enumerate(row)] for row in v]
  inverse = tuple(tuple(_dot(row, u_row, reverse=True) for u_row in u) for row in scaled)
  return inverse, condition


def spectral_signals(
  inverse: tuple[tuple[float, ...], ...], measured: tuple[float, ...]
) -> tuple[float, ...]:
  """Apply an inverse to one binary32 filter vector without clipping negative components."""
  return tuple(_dot(list(row), list(measured), reverse=True) for row in inverse)


def spectral_residual(
  matrix: tuple[tuple[float, ...], ...],
  measured: tuple[float, ...],
  signals: tuple[float, ...],
) -> float | None:
  """Return the residual standard error; a square system has no residual degrees of freedom."""
  if len(measured) == len(signals):
    return None
  squared = 0.0
  for row, value in zip(matrix, measured):
    difference = _sub(value, _dot(list(row), list(signals)))
    squared = f32(squared + difference**2)
  return f32(math.sqrt(_div(squared, float(len(measured) - len(signals)))))
