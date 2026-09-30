"""Shared placement faults for normal inputs and continuous transactions."""
import time


def geometry_signature(board, layout):
    return (board.scene, board.rows, layout.client_w, layout.client_h,
            layout.grid_left, layout.grid_top, layout.cell_w, layout.row_height(),
            getattr(layout, 'roof_slope_h', None), getattr(layout, 'roof_flat_col', 4))


def record_geometry_failure(agent, board, row, col):
    """A confirmed wrong cell stays blocked until geometry or battle changes."""
    signature = geometry_signature(board, agent.layout)
    old = getattr(agent, '_geometry_error', None)
    cells = set(old['cells']) if isinstance(old, dict) and old.get('geometry') == signature else set()
    cells.add((row, col))
    agent._geometry_error = {'geometry': signature, 'cells': cells,
                             'observed_at': time.time()}


def geometry_blocked(agent, board, row, col):
    fault = getattr(agent, '_geometry_error', None)
    signature = geometry_signature(board, agent.layout)
    if isinstance(fault, dict):
        return fault.get('geometry') == signature and (row, col) in fault.get('cells', ())
    # Both historical writers are understood. Time passing is not evidence that
    # the transform was repaired; never treat a row count as a Unix timestamp.
    if isinstance(fault, tuple) and len(fault) == 2 and isinstance(fault[0], tuple):
        fault = fault[0]
    return fault is not None and fault in (signature, signature[:8])
