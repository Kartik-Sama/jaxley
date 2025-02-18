# This file is part of Jaxley, a differentiable neuroscience simulator. Jaxley is
# licensed under the Apache License Version 2.0, see <https://www.apache.org/licenses/>

from math import pi
from typing import Callable, Dict, List, Optional, Tuple

import jax.numpy as jnp
import numpy as np
import pandas as pd
from jax import vmap

from jaxley.utils.misc_utils import cumsum_leading_zero


def build_radiuses_from_xyzr(
    radius_fns: List[Callable],
    branch_indices: List[int],
    min_radius: Optional[float],
    ncomp: int,
) -> jnp.ndarray:
    """Return the radiuses of branches given SWC file xyzr.

    Returns an array of shape `(num_branches, ncomp)`.

    Args:
        radius_fns: Functions which, given compartment locations return the radius.
        branch_indices: The indices of the branches for which to return the radiuses.
        min_radius: If passed, the radiuses are clipped to be at least as large.
        ncomp: The number of compartments that every branch is discretized into.
    """
    # Compartment locations are at the center of the internal nodes.
    non_split = 1 / ncomp
    range_ = np.linspace(non_split / 2, 1 - non_split / 2, ncomp)

    # Build radiuses.
    radiuses = np.asarray([radius_fns[b](range_) for b in branch_indices])
    radiuses_each = radiuses.ravel(order="C")
    if min_radius is None:
        assert np.all(
            radiuses_each > 0.0
        ), "Radius 0.0 in SWC file. Set `read_swc(..., min_radius=...)`."
    else:
        radiuses_each[radiuses_each < min_radius] = min_radius

    return radiuses_each


def equal_segments(branch_property: list, ncomp_per_branch: int):
    """Generates segments where some property is the same in each segment.

    Args:
        branch_property: List of values of the property in each branch. Should have
            `len(branch_property) == num_branches`.
    """
    assert isinstance(branch_property, list), "branch_property must be a list."
    return jnp.asarray([branch_property] * ncomp_per_branch).T


def linear_segments(
    initial_val: float, endpoint_vals: list, parents: jnp.ndarray, ncomp_per_branch: int
):
    """Generates segments where some property is linearly interpolated.

    Args:
        initial_val: The value at the tip of the soma.
        endpoint_vals: The value at the endpoints of each branch.
    """
    branch_property = endpoint_vals + [initial_val]
    num_branches = len(parents)
    # Compute radiuses by linear interpolation.
    endpoint_radiuses = jnp.asarray(branch_property)

    def compute_rad(branch_ind, loc):
        start = endpoint_radiuses[parents[branch_ind]]
        end = endpoint_radiuses[branch_ind]
        return (end - start) * loc + start

    branch_inds_of_each_comp = jnp.tile(jnp.arange(num_branches), ncomp_per_branch)
    locs_of_each_comp = jnp.linspace(1, 0, ncomp_per_branch).repeat(num_branches)
    rad_of_each_comp = compute_rad(branch_inds_of_each_comp, locs_of_each_comp)

    return jnp.reshape(rad_of_each_comp, (ncomp_per_branch, num_branches)).T


def merge_cells(
    cumsum_num_branches: List[int],
    cumsum_num_branchpoints: List[int],
    arrs: List[List[np.ndarray]],
    exclude_first: bool = True,
) -> np.ndarray:
    """
    Build full list of which branches are solved in which iteration.

    From the branching pattern of single cells, this "merges" them into a single
    ordering of branches.

    Args:
        cumsum_num_branches: cumulative number of branches. E.g., for three cells with
            10, 15, and 5 branches respectively, this will should be a list containing
            `[0, 10, 25, 30]`.
        arrs: A list of a list of arrays that should be merged.
        exclude_first: If `True`, the first element of each list in `arrs` will remain
            unchanged. Useful if a `-1` (which indicates "no parent") entry should not
            be changed.

    Returns:
        A list of arrays which contain the branch indices that are computed at each
        level (i.e., iteration).
    """
    ps = []
    for i, att in enumerate(arrs):
        p = att
        if exclude_first:
            raise NotImplementedError
            p = [p[0]] + [p_in_level + cumsum_num_branches[i] for p_in_level in p[1:]]
        else:
            p = [
                p_in_level
                + np.asarray([cumsum_num_branches[i], cumsum_num_branchpoints[i]])
                for p_in_level in p
            ]
        ps.append(p)

    max_len = max([len(att) for att in arrs])
    combined_parents_in_level = []
    for i in range(max_len):
        current_ps = []
        for p in ps:
            if len(p) > i:
                current_ps.append(p[i])
        combined_parents_in_level.append(np.concatenate(current_ps))

    return combined_parents_in_level


def compute_levels(parents):
    levels = np.zeros_like(parents)

    for i, p in enumerate(parents):
        if p == -1:
            levels[i] = 0
        else:
            levels[i] = levels[p] + 1
    return levels


def compute_children_in_level(
    levels: np.ndarray, children_row_and_col: np.ndarray
) -> List[np.ndarray]:
    num_branches = len(levels)
    children_in_each_level = []
    for l in range(1, np.max(levels) + 1):
        children_in_current_level = []
        for b in range(num_branches):
            if levels[b] == l:
                children_in_current_level.append(children_row_and_col[b - 1])
        children_in_current_level = np.asarray(children_in_current_level)
        children_in_each_level.append(children_in_current_level)
    return children_in_each_level


def compute_parents_in_level(levels, par_inds, parents_row_and_col):
    level_of_parent = levels[par_inds]
    parents_in_each_level = []
    for l in range(np.max(levels)):
        parents_inds_in_current_level = np.where(level_of_parent == l)[0]
        parents_in_current_level = parents_row_and_col[parents_inds_in_current_level]
        parents_in_current_level = np.asarray(parents_in_current_level)
        parents_in_each_level.append(parents_in_current_level)
    return parents_in_each_level


def _compute_num_children(parents):
    num_branches = len(parents)
    num_children = []
    for b in range(num_branches):
        n = np.sum(np.asarray(parents) == b)
        num_children.append(n)
    return num_children


def _compute_index_of_child(parents):
    """For every branch, it returns the how many-eth child of its parent it is.

    Example:
    ```
    parents = [-1, 0, 0, 1, 1, 1]
    _compute_index_of_child(parents) -> [-1, 0, 1, 0, 1, 2]
    ```
    """
    num_branches = len(parents)
    current_num_children_for_each_branch = np.zeros((num_branches,), np.dtype("int"))
    index_of_child = [-1]
    for b in range(1, num_branches):
        index_of_child.append(current_num_children_for_each_branch[parents[b]])
        current_num_children_for_each_branch[parents[b]] += 1
    return index_of_child


def compute_children_indices(parents) -> List[jnp.ndarray]:
    """Return all children indices of every branch.

    Example:
    ```
    parents = [-1, 0, 0]
    compute_children_indices(parents) -> [[1, 2], [], []]
    ```
    """
    num_branches = len(parents)
    child_indices = []
    for b in range(num_branches):
        child_indices.append(np.where(parents == b)[0])
    return child_indices


def get_num_neighbours(
    num_children: jnp.ndarray,
    ncomp_per_branch: int,
    num_branches: int,
):
    """
    Number of neighbours of each compartment.
    """
    num_neighbours = 2 * jnp.ones((num_branches * ncomp_per_branch))
    num_neighbours = num_neighbours.at[ncomp_per_branch - 1].set(1.0)
    num_neighbours = num_neighbours.at[jnp.arange(num_branches) * ncomp_per_branch].set(
        num_children + 1.0
    )
    return num_neighbours


def local_index_of_loc(
    loc: float, global_branch_ind: int, ncomp_per_branch: int
) -> int:
    """Returns the local index of a comp given a loc [0, 1] and the index of a branch.

    This is used because we specify locations such as synapses as a value between 0 and
    1. We have to convert this onto a discrete segment here.

    Args:
        branch_ind: Index of the branch.
        loc: Location (in [0, 1]) along that branch.
        ncomp_per_branch: Number of segments of each branch.

    Returns:
        The local index of the compartment.
    """
    ncomp = ncomp_per_branch[global_branch_ind]  # only for convenience.
    possible_locs = np.linspace(0.5 / ncomp, 1 - 0.5 / ncomp, ncomp)
    ind_along_branch = np.argmin(np.abs(possible_locs - loc))
    return ind_along_branch


def loc_of_index(global_comp_index, global_branch_index, ncomp_per_branch):
    """Return location corresponding to global compartment index."""
    cumsum_ncomp = cumsum_leading_zero(ncomp_per_branch)
    index = global_comp_index - cumsum_ncomp[global_branch_index]
    ncomp = ncomp_per_branch[global_branch_index]
    return (0.5 + index) / ncomp


def compute_coupling_cond(rad1, rad2, r_a1, r_a2, l1, l2):
    """Return the coupling conductance between two compartments.

    Equations taken from `https://en.wikipedia.org/wiki/Compartmental_neuron_models`.

    `radius`: um
    `r_a`: ohm cm
    `length_single_compartment`: um
    `coupling_conds`: S * um / cm / um^2 = S / cm / um -> *10**7 -> mS / cm^2
    """
    # Multiply by 10**7 to convert (S / cm / um) -> (mS / cm^2).
    return rad1 * rad2**2 / (r_a1 * rad2**2 * l1 + r_a2 * rad1**2 * l2) / l1 * 10**7


def compute_coupling_cond_branchpoint(rad, r_a, l):
    r"""Return the coupling conductance between one compartment and a comp with l=0.

    From https://en.wikipedia.org/wiki/Compartmental_neuron_models

    If one compartment has l=0.0 then the equations simplify.

    R_long = \sum_i r_a * L_i/2 / crosssection_i

    with crosssection = pi * r**2

    For a single compartment with L>0, this turns into:
    R_long = r_a * L/2 / crosssection

    Then, g_long = crosssection * 2 / L / r_a

    Then, the effective conductance is g_long / zylinder_area. So:
    g = pi * r**2 * 2 / L / r_a / 2 / pi / r / L
    g = r / r_a / L**2
    """
    return rad / r_a / l**2 * 10**7  # Convert (S / cm / um) -> (mS / cm^2)


def compute_impact_on_node(rad, r_a, l):
    r"""Compute the weight with which a compartment influences its node.

    In order to satisfy Kirchhoffs current law, the current at a branch point must be
    proportional to the crosssection of the compartment. We only require proportionality
    here because the branch point equation reads:
    `g_1 * (V_1 - V_b) + g_2 * (V_2 - V_b) = 0.0`

    Because R_long = r_a * L/2 / crosssection, we get
    g_long = crosssection * 2 / L / r_a \propto rad**2 / L / r_a

    This equation can be multiplied by any constant."""
    return rad**2 / r_a / l


def remap_to_consecutive(arr):
    """Maps an array of integers to an array of consecutive integers.

    E.g. `[0, 0, 1, 4, 4, 6, 6] -> [0, 0, 1, 2, 2, 3, 3]`
    """
    _, inverse_indices = jnp.unique(arr, return_inverse=True)
    return inverse_indices


v_interp = vmap(jnp.interp, in_axes=(None, None, 1))


def interpolate_xyzr(loc: float, coords: np.ndarray):
    """Perform a linear interpolation between xyz-coordinates.

    Args:
        loc: The location in [0,1] along the branch.
        coords: Array containing the reconstructed xyzr points of the branch.

    Return:
        Interpolated xyz coordinate at `loc`, shape `(3,).
    """
    dl = np.sqrt(np.sum(np.diff(coords[:, :3], axis=0) ** 2, axis=1))
    pathlens = np.insert(np.cumsum(dl), 0, 0)  # cummulative length of sections
    norm_pathlens = pathlens / np.maximum(1e-8, pathlens[-1])  # norm lengths to [0,1].

    return v_interp(loc, norm_pathlens, coords)


def params_to_pstate(
    params: List[Dict[str, jnp.ndarray]],
    indices_set_by_trainables: List[jnp.ndarray],
):
    """Make outputs `get_parameters()` conform with outputs of `.data_set()`.

    `make_trainable()` followed by `params=get_parameters()` does not return indices
    because these indices would also be differentiated by `jax.grad` (as soon as
    the `params` are passed to `def simulate(params)`. Therefore, in `jx.integrate`,
    we run the function to add indices to the dict. The outputs of `params_to_pstate`
    are of the same shape as the outputs of `.data_set()`."""
    return [
        {"key": list(p.keys())[0], "val": list(p.values())[0], "indices": i}
        for p, i in zip(params, indices_set_by_trainables)
    ]


def convert_point_process_to_distributed(
    current: jnp.ndarray, radius: jnp.ndarray, length: jnp.ndarray
) -> jnp.ndarray:
    """Convert current point process (nA) to distributed current (uA/cm2).

    This function gets called for synapses and for external stimuli.

    Args:
        current: Current in `nA`.
        radius: Compartment radius in `um`.
        length: Compartment length in `um`.

    Return:
        Current in `uA/cm2`.
    """
    area = 2 * pi * radius * length
    current /= area  # nA / um^2
    return current * 100_000  # Convert (nA / um^2) to (uA / cm^2)


def build_branchpoint_group_inds(
    num_branchpoints, child_belongs_to_branchpoint, start_ind_for_branchpoints
):
    branchpoint_inds_parents = start_ind_for_branchpoints + jnp.arange(num_branchpoints)
    branchpoint_inds_children = (
        start_ind_for_branchpoints + child_belongs_to_branchpoint
    )

    all_branchpoint_inds = jnp.concatenate(
        [branchpoint_inds_parents, branchpoint_inds_children]
    )
    branchpoint_group_inds = remap_to_consecutive(all_branchpoint_inds)
    return branchpoint_group_inds


def compute_morphology_indices_in_levels(
    num_branchpoints,
    child_belongs_to_branchpoint,
    par_inds,
    child_inds,
):
    """Return (row, col) to build the sparse matrix defining the voltage eqs.

    This is run at `init`, not during runtime.
    """
    branchpoint_inds_parents = jnp.arange(num_branchpoints)
    branchpoint_inds_children = child_belongs_to_branchpoint
    branch_inds_parents = par_inds
    branch_inds_children = child_inds

    children = jnp.stack([branch_inds_children, branchpoint_inds_children])
    parents = jnp.stack([branch_inds_parents, branchpoint_inds_parents])

    return {"children": children.T, "parents": parents.T}


def group_and_sum(
    values_to_sum: jnp.ndarray, inds_to_group_by: jnp.ndarray, num_branchpoints: int
) -> jnp.ndarray:
    """Group values by whether they have the same integer and sum values within group.

    This is used to construct the last diagonals at the branch points.

    Written by ChatGPT.
    """
    # Initialize an array to hold the sum of each group
    group_sums = jnp.zeros(num_branchpoints)

    # `.at[inds]` requires that `inds` is not empty, so we need an if-case here.
    # `len(inds) == 0` is the case for branches and compartments.
    if num_branchpoints > 0:
        group_sums = group_sums.at[inds_to_group_by].add(values_to_sum)

    return group_sums


def query_channel_states_and_params(d, keys, idcs):
    """Get dict with subset of keys and values from d.

    This is used to restrict a dict where every item contains __all__ states to only
    the ones that are relevant for the channel. E.g.

    ```states = {'eCa': Array([ 0.,  0., nan]}```

    will be
    ```states = {'eCa': Array([ 0.,  0.]}```

    Only loops over necessary keys, as opposed to looping over `d.items()`."""
    return dict(zip(keys, (v[idcs] for v in map(d.get, keys))))


def compute_axial_conductances(
    comp_edges: pd.DataFrame, params: Dict[str, jnp.ndarray]
) -> jnp.ndarray:
    """Given `comp_edges`, radius, length, r_a, cm, compute the axial conductances.

    Note that the resulting axial conductances will already by divided by the
    capacitance `cm`.
    """
    # `Compartment-to-compartment` (c2c) axial coupling conductances.
    condition = comp_edges["type"].to_numpy() == 0
    source_comp_inds = np.asarray(comp_edges[condition]["source"].to_list())
    sink_comp_inds = np.asarray(comp_edges[condition]["sink"].to_list())

    if len(sink_comp_inds) > 0:
        conds_c2c = (
            vmap(compute_coupling_cond, in_axes=(0, 0, 0, 0, 0, 0))(
                params["radius"][sink_comp_inds],
                params["radius"][source_comp_inds],
                params["axial_resistivity"][sink_comp_inds],
                params["axial_resistivity"][source_comp_inds],
                params["length"][sink_comp_inds],
                params["length"][source_comp_inds],
            )
            / params["capacitance"][sink_comp_inds]
        )
    else:
        conds_c2c = jnp.asarray([])

    # `branchpoint-to-compartment` (bp2c) axial coupling conductances.
    condition = comp_edges["type"].isin([1, 2])
    sink_comp_inds = np.asarray(comp_edges[condition]["sink"].to_list())

    if len(sink_comp_inds) > 0:
        conds_bp2c = (
            vmap(compute_coupling_cond_branchpoint, in_axes=(0, 0, 0))(
                params["radius"][sink_comp_inds],
                params["axial_resistivity"][sink_comp_inds],
                params["length"][sink_comp_inds],
            )
            / params["capacitance"][sink_comp_inds]
        )
    else:
        conds_bp2c = jnp.asarray([])

    # `compartment-to-branchpoint` (c2bp) axial coupling conductances.
    condition = comp_edges["type"].isin([3, 4])
    source_comp_inds = np.asarray(comp_edges[condition]["source"].to_list())

    if len(source_comp_inds) > 0:
        conds_c2bp = vmap(compute_impact_on_node, in_axes=(0, 0, 0))(
            params["radius"][source_comp_inds],
            params["axial_resistivity"][source_comp_inds],
            params["length"][source_comp_inds],
        )
        # For numerical stability. These values are very small, but their scale
        # does not matter.
        conds_c2bp *= 1_000
    else:
        conds_c2bp = jnp.asarray([])

    # All axial coupling conductances.
    return jnp.concatenate([conds_c2c, conds_bp2c, conds_c2bp])


def compute_children_and_parents(
    branch_edges: pd.DataFrame,
) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, int]:
    """Build indices used during `._init_morph_custom_spsolve()."""
    par_inds = branch_edges["parent_branch_index"].to_numpy()
    child_inds = branch_edges["child_branch_index"].to_numpy()
    child_belongs_to_branchpoint = remap_to_consecutive(par_inds)
    par_inds = np.unique(par_inds)
    return par_inds, child_inds, child_belongs_to_branchpoint
