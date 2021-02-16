# coding=utf-8
# Copyright 2021 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""A Jax version of Sinkhorn's algorithm."""

import collections
import functools
import numbers
from typing import Optional, Sequence, Union

import jax
import jax.numpy as np

import numpy as onp
from ott.core import fixed_point_loop
from ott.core.geometry import geometry


SinkhornOutput = collections.namedtuple(
    'SinkhornOutput', ['f', 'g', 'reg_ot_cost', 'errors', 'converged'])


def sinkhorn(geom: geometry.Geometry,
             a: Optional[np.ndarray] = None,
             b: Optional[np.ndarray] = None,
             tau_a: float = 1.0,
             tau_b: float = 1.0,
             threshold: float = 1e-3,
             norm_error: int = 1,
             inner_iterations: int = 10,
             min_iterations: int = 0,
             max_iterations: int = 2000,
             momentum_strategy: Optional[Union[float, str]] = None,
             lse_mode: bool = True,
             implicit_differentiation: bool = True) -> SinkhornOutput:
  """Solves regularized OT problems using Sinkhorn iterations.

  The Sinkhorn algorithm is a fixed point algorithm that seeks a pair of
  variables that optimize a regularized optimal transport (reg-OT) problem. This
  function outputs this pair of optimal solutions, in addition to the objective
  that is reached, a vector of errors computed during iterations and a flag.

  The reg-OT problem is specified by two measures, of respective sizes n and m.
  From the viewpoint of this function, these two measures are only seen through
  a geometry (a cost or kernel structure between their respective points)
  and marginal probability vectors a and b. Essentially, all of the heavy
  computations are carried out by the geometry object itself, as requested by
  the sinkhorn function.

  Given a geometry, which provides either a cost matrix C with its
  regularization parameter 𝜀, (resp. a cost matrix K) the reg-OT problem
  solves for two vectors f, g of size n, m

  argmax_{f,g} - <a,φ_a*(-f)> + <b,φ_b*(-g)> - 𝜀 <e^{f / 𝜀}, e^{-C/𝜀} e^{g / 𝜀}>

  (respectively, written the space of positive scaling vectors u, v of size n, m
  argmax_{u,v} - <a,φ_a*(-log u)> + <b,φ_b*(-log v)> -  <u, K v> )

  where φ_a(z) = ⍴_a z(log z - 1) is a scaled entropy. This problem corresponds,
  in a so-called primal representation, to solving the unbalanced optimal
  transport problem with a variable matrix P of size n x m:

  argmin_{P} <P,C> - 𝜀H(P) + ⍴_a KL(P1 | a) + ⍴_b KL(P'1 | b)

  (resp.
  argmin_{P} KL(P|K) + ⍴_a KL(P1 | a) + ⍴_b KL(P'1 | b)   )

  The *balanced* regularized OT problem is recovered when ⍴_a, ⍴_b → ∞.

  The *original* (not regularized) OT problem is recovered when 𝜀 → 0 using the
  cost formulation. This problem is not handled for now in this toolbox, which
  focuses exclusively on 𝜀 > 0.

  To allow for the option ⍴_a, ⍴_b → ∞, the sinkhorn function uses parameters
  tau_a := ⍴_a / (𝜀 + ⍴_a) and tau_b := ⍴_b / (𝜀 + ⍴_b)
  instead. Setting these parameters to 1 corresponds to setting ⍴ to ∞ above.

  The Sinkhorn algorithm solves the reg-OT problem by seeking optimal f, g
  potentials (or alternatively their parameterization as positive scalings u, v)
  rather than solving it directly for a matrix P. This is mostly for efficiency
  (potentials and scalings have a n + m memory footprint, rather than n x m
  required to store P) and also because both problems are in fact equivalent,
  since the optimal transport P* can be recovered from optimal potentials f*, g*
  or scalings u*, v*, using the geometry's cost or kernel matrices respectively:

    P* = np.exp {( f* + g* - C )/ 𝜀} or P* = diag(u*) K diag(v*)

  The Sinkhorn algorithm solves this dual problem in f,g or u,v using block
  coordinate ascent, i.e. devising an update for each f and g (resp. u and v)
  that cancels their respective gradients, one at a time. These two iterations
  are repeated `inner_iterations` times, after which the norm of these gradients
  will be evaluated and compared with the `threshold` value. The iterations are
  then repeated as long as that errors does not go below `threshold`.

  The boolean flag lse_mode sets whether the algorithm is run in either:

    - log-sum-exp mode (lse_mode=True), in which case it is directly defined
  in terms of updates to f and g, using log-sum-exp computations. This requires
  access to the cost matrix C, as stored or computed on the fly by the geometry.

    - kernel mode (lse_mode=False), in which case it will require access to a
  matrix vector multiplication operator z → K z, where K is either instantiated
  from C as e^{-C/𝜀}, or provided directly. In that case, rather than optimizing
  on f and g directly, it is more convenient to optimize on
  their so called scaling formulations, u :=e^{f / 𝜀} & v :=e^{g / 𝜀}. While
  faster (applying matrices is faster than applying lse repeatedly over lines),
  this mode is also less stable numerically, notably for smaller 𝜀.

  In the code below, the variables f_u or g_v can be either regarded as
  potentials (real) or scalings (positive) vectors, depending on the choice
  of lse_mode by the end user.

  In addition to standard Sinkhorn updates, the user can also change them with
  a `momentum_strategy` parameter in ]0,2[. We also implement a strategy that
  tries to set that parameter adaptively, as a function of progress in the
  error, as proposed in the literature.

  The sinkhorn iterations are wrapped in a fixed point iteration loop defined in
  fixed_point_loop loop, rather than a standard while loop. This is to ensure
  the end result of this fixed point loop can be differentiated if needed using
  standard Jax operations. To do so, if backprop differentiability is used,
  fixed_point_loop.fixpoint_iter_backprop does checkpointing of state variables
  (here f_u and g_v) every inner_iterations, and backpropagates automatically,
  block by block, through blocks of inner_iterations at a time.

  Alternatively, differentiation through the Sinkhorn algorithm can be carried
  out using implicit differentiation of the optimality conditions, by setting
  the implicit_differentiation flag to True. In that case the termination
  criterion used to stop Sinkhorn (cancellation of gradient of objective w.r.t.
  f and g) is used to differentiate inputs given a desired change in the
  outputs. This is the behaviour by default of sinkhorn.

  The Sinkhorn algorithm may not converge within the maximum number of
  iterations for possibly two reasons:
    1. the regularizer (defined as epsilon in the geometry geom object) is
      too small. Consider switching to lse_mode = True (at the price of a slower
      execution), increasing epsilon, or, alternatively, if you are sure that
      value epsilon is correct, or your cannot modify it,
      either increase max_iterations or threshold.
    2. the probability weights a and b do not have the same total mass, while
      using a balanced (tau_a = tau_b = 1.0) setup. Consider either normalizing
      a and b, or set either tau_a and/or tau_b <1.0

  Args:
    geom: a Geometry object.
    a: np.ndarray<float>[num_a,] or np.ndarray<float>[batch,num_a] weights.
    b: np.ndarray<float>[num_b,] or np.ndarray<float>[batch,num_b] weights.
    tau_a: float, ratio lam/(lam+eps) between KL divergence regularizer to first
     marginal and itself + epsilon regularizer used in the unbalanced
     formulation.
   tau_b: float, ratio lam/(lam+eps) between KL divergence regularizer to first
     marginal and itself + epsilon regularizer used in the unbalanced
     formulation.
   threshold: (float) tolerance used to stop the Sinkhorn iterations. This is
     typically the deviation between a target marginal and the marginal of the
     current primal solution when either or both tau_a and tau_b are 1.0
     (balanced or semi-balanced problem), or the relative change between two
     successive solutions in the unbalanced case.
   norm_error: int, power used to define p-norm of error from marginal to target
   inner_iterations: (int32) the Sinkhorn error is not recomputed at each
     iteration but every inner_num_iter instead.
   min_iterations: (int32) the minimum number of Sinkhorn iterations carried
     out before the error is computed and monitored.
   max_iterations: (int32) the maximum number of Sinkhorn iterations.
   momentum_strategy: either a float between ]0,2[ or a string.
   lse_mode: True for log-sum-exp computations, False for kernel multiplication.
   implicit_differentiation: True if using implicit diff, False if backprop.

  Returns:
    a SinkhornOutput named tuple.

  Raises:
    ValueError: If momentum parameter is not set correctly, or to a wrong value.
  """
  num_a, num_b = geom.shape
  a = np.ones((num_a,)) / num_a if a is None else a
  b = np.ones((num_b,)) / num_b if b is None else b

  if momentum_strategy is None:
    momentum_strategy = 1.0

  if (isinstance(momentum_strategy, str) and
      momentum_strategy.lower() == 'lehmann'):
    # check the unbalanced formulation is not selected.
    if tau_a != 1 and tau_b != 1:
      raise ValueError('The Lehmann momentum strategy cannot be selected for '
                       'unbalanced transport problems (namely when either '
                       'tau_a or tau_b < 1).')
    # The Lehmann strategy needs to keep track of errors in ||.||_1 norm.
    # In that case, we add this exponent to the list of errors to compute,
    # if that was not the error requested by the user.
    norm_error = (norm_error,) if norm_error == 1 else (norm_error, 1)
    momentum_default = 1.0
    chg_momentum_from = onp.maximum(
        (min_iterations + 100) // inner_iterations, 2)
  elif isinstance(momentum_strategy, numbers.Number):
    if not 0 < momentum_strategy < 2:
      raise ValueError('Momentum parameter must be strictly between 0 and 2.')
    momentum_default, chg_momentum_from = momentum_strategy, max_iterations + 1
    norm_error = (norm_error,)
  else:
    raise ValueError('Momentum parameter must be either a float in ]0,2[ (when'
                     ' set to 1 one recovers the usual Sinkhorn updates) or '
                     'a valid string.')
  if implicit_differentiation:
    f, g, errors = _sinkhorn_iterations_implicit(
        (threshold, norm_error, tau_a, tau_b, inner_iterations, min_iterations,
         max_iterations, momentum_default, chg_momentum_from, lse_mode, True),
        (geom, a, b))
  else:
    f, g, errors = _sinkhorn_iterations(
        geom, a, b, threshold, norm_error, tau_a, tau_b, inner_iterations,
        min_iterations, max_iterations, momentum_default, chg_momentum_from,
        lse_mode, False)

  reg_ot_cost = ent_reg_cost(geom, a, b, tau_a, tau_b, f, g)
  converged = np.logical_and(
      np.sum(errors == -1) > 0,
      np.sum(np.isnan(errors)) == 0)
  return SinkhornOutput(f, g, reg_ot_cost, errors, converged)


@functools.partial(jax.jit, static_argnums=(4, 5, 6, 7, 8, 9, 10, 11, 12, 13))
def _sinkhorn_iterations(geom: geometry.Geometry,
                         a: np.ndarray,
                         b: np.ndarray,
                         threshold: float,
                         norm_error: Sequence[int],
                         tau_a: float,
                         tau_b: float,
                         inner_iterations,
                         min_iterations,
                         max_iterations,
                         momentum_default,
                         chg_momentum_from,
                         lse_mode,
                         implicit) -> SinkhornOutput:
  """Backprop friendly / implicitly differentiated, Jit'ed Sinkhorn loop.

  Args:
    geom: a Geometry object.
    a: np.ndarray<float>[num_a,] or np.ndarray<float>[batch,num_a] weights.
    b: np.ndarray<float>[num_b,] or np.ndarray<float>[batch,num_b] weights.
    threshold: (float) the relative threshold on the Sinkhorn error to stop the
      Sinkhorn iterations.
    norm_error: t-uple of int, p-norms of marginal / target errors to track
    tau_a: float, ratio lam/(lam+eps) between KL divergence regularizer to first
     marginal and itself + epsilon regularizer used in the unbalanced
     formulation.
    tau_b: float, ratio lam/(lam+eps) between KL divergence regularizer to first
     marginal and itself + epsilon regularizer used in the unbalanced
     formulation.
    inner_iterations: (int32) the Sinkhorn error is not recomputed at each
       iteration but every inner_num_iter instead.
    min_iterations: (int32) the minimum number of Sinkhorn iterations.
    max_iterations: (int32) the maximum number of Sinkhorn iterations.
    momentum_default: float, a float between ]0,2[
    chg_momentum_from: int, # of iterations after which momentum is computed
    lse_mode: True for log-sum-exp computations, False for kernel
      multiplication.
    implicit: True if implicit mode differentiation
  Returns:
    a SinkhornOutput named tuple.
  """
  num_a, num_b = geom.shape
  if lse_mode:
    f_u, g_v = np.zeros_like(a), np.zeros_like(b)
  else:
    f_u, g_v = np.ones_like(a) / num_a, np.ones_like(b) / num_b

  errors = -np.ones((onp.ceil(max_iterations / inner_iterations).astype(int),
                     len(norm_error)))
  const = (geom, a, b, threshold)

  def cond_fn(iteration, const, state):  # pylint: disable=unused-argument
    threshold = const[-1]
    errors = state[0]
    err = errors[iteration // inner_iterations-1, 0]

    return np.logical_or(iteration == 0,
                         np.logical_and(np.isfinite(err), err > threshold))

  def get_momentum(errors, idx):
    """momentum formula, https://arxiv.org/pdf/2012.12562v1.pdf, p.7 and (5)."""
    error_ratio = np.minimum(errors[idx - 1, -1] / errors[idx - 2, -1], .99)
    power = 1.0 / inner_iterations
    return 2.0 / (1.0 + np.sqrt(1.0 - error_ratio ** power))

  def body_fn(iteration, const, state, last):
    """Carries out sinkhorn iteration.

    Depending on lse_mode, these iterations can be either in:
      - log-space for numerical stability.
      - scaling space, using standard kernel-vector multiply operations.

    Args:
      iteration: iteration number
      const: tuple of constant parameters that do not change throughout the
        loop, here the geometry and the marginals a, b.
      state: optimization variables updated in the loop with errors.
      last: flag to indicate this is the last iteration in the inner loop

    Returns:
      state variables.
    """
    geom, a, b, _ = const
    errors, f_u, g_v = state

    # compute momentum term if needed, using previously seen errors.
    w = jax.lax.stop_gradient(np.where(iteration >= (
        inner_iterations * chg_momentum_from + min_iterations),
                                       get_momentum(errors, chg_momentum_from),
                                       momentum_default))

    # (momentum) sinkhorn updates, in either scaling or potential form.
    if lse_mode:
      g_v = (1.0 - w) * g_v + w * tau_b * geom.update_potential(
          f_u, g_v, np.log(b), iteration,
          axis=0)
      f_u = (1.0 - w) * f_u + w * tau_a * geom.update_potential(
          f_u, g_v, np.log(a), iteration,
          axis=1)
    else:
      g_v = g_v ** (1.0 - w) * (geom.update_scaling(
          f_u, b, iteration, axis=0)**tau_b) ** w
      f_u = f_u ** (1.0 - w) * (geom.update_scaling(
          g_v, a, iteration, axis=1)**tau_a) ** w

    # re-compute errors if this is the last of inner_iterations and above the
    # min_iterations required before computing errors for the first time.
    if last:
      err = np.where(iteration >= min_iterations,
                     marginal_error(geom, a, b, tau_a, tau_b,
                                    f_u, g_v, norm_error, lse_mode),
                     np.inf)
      errors = jax.ops.index_update(
          errors, jax.ops.index[iteration // inner_iterations, :], err)
    return errors, f_u, g_v

  # either run the fixpoint_iter loop in a straightforward manner (for implicit
  # differentiation, which implements vjp operations at a higher level) or using
  # backprop, using the custom vjp mechanism of the fixpoint_iter loop.
  if implicit:
    fix_point = fixed_point_loop.fixpoint_iter
  else:
    fix_point = fixed_point_loop.fixpoint_iter_backprop
  errors, f_u, g_v = fix_point(
      cond_fn, body_fn, min_iterations, max_iterations, inner_iterations, const,
      (errors, f_u, g_v))

  f = f_u if lse_mode else geom.potential_from_scaling(f_u)
  g = g_v if lse_mode else geom.potential_from_scaling(g_v)

  return f, g, errors[:, 0]


@functools.partial(jax.custom_vjp, nondiff_argnums=(0,))
def _sinkhorn_iterations_implicit(aux, constants) -> SinkhornOutput:
  """Naked forward pass of the Sinkhorn algorithm when not differentiated."""
  geom, a, b = constants
  return _sinkhorn_iterations(geom, a, b, *aux)


def _sinkhorn_iterations_implicit_fwd(aux, constants):
  """Runs forward pass of the Sinkhorn algorithm storing side information."""
  geom, a, b = constants

  f, g, errors = _sinkhorn_iterations(geom, a, b, *aux)
  return (f, g, errors), (f, g, geom, a, b)


def _sinkhorn_iterations_implicit_bwd(aux, res, gr) -> SinkhornOutput:
  """Runs Sinkhorn in backward mode, using implicit differentiation.

  Args:
    aux: auxiliary data that was used, as is, in the forward pass. Most of it
      ends up un-used here, except for parameters used to define first order
      conditions.
    res: residual data sent from fwd pass, used for computations below. In this
      case consists in the output itself, as well as inputs against which we
      wish to differentiate.
    gr: gradients w.r.t outputs of fwd pass, here w.r.t size f, g, errors. Note
      that differentiability w.r.t. errors is not handled, and only f, g is
      considered.

  Returns:
    a tuple of gradients: PyTree for geom, one np.ndarray for each of a and b.
  """
  _, _, tau_a, tau_b, _, _, _, _, _, lse_mode, _ = aux
  f, g, geom, a, b = res
  f_g = np.concatenate((f, g))
  # Ignores gradients info with respect to 'errors' output.
  gr = gr[0], gr[1]
  ridge = 1e-10  #  to regularize the linear system in implicit function diff

  if lse_mode:
    marginal_a = lambda geom, f, g: geom.marginal_from_potentials(f, g, 1)
    marginal_b = lambda geom, f, g: geom.marginal_from_potentials(f, g, 0)
  else:
    marginal_a = lambda geom, f, g: geom.marginal_from_scalings(
        geom.scaling_from_potential(f), geom.scaling_from_potential(g), 1)

    marginal_b = lambda geom, f, g: geom.marginal_from_scalings(
        geom.scaling_from_potential(f), geom.scaling_from_potential(g), 0)

  n, _ = geom.shape

  def first_order_conditions(geom: geometry.Geometry,
                             a: np.ndarray,
                             b: np.ndarray,
                             fg: np.ndarray):
    """Computes vector of first order conditions for the reg-OT problem.

    The output of this vector should be close to zero at optimality.
    Upon completion of the Sinkhorn forward pass, its norm (as computed using
    the norm_error setting) should be below the threshold parameter.

    This error will be itself assumed to be close to zero when using implicit
    differentiation.

    Args:
      geom: a geometry object
      a: np.ndarray, first marginal
      b: np.ndarray, second marginal
      fg: concatenated vector of two potentials (total size equals the sum of
        that of a and b)
    Returns:
      a np.ndarray of the size of fg quantifying deviation from optimality.
    """
    marginals = np.concatenate(
        (marginal_a(geom, fg[:n], fg[n:]), marginal_b(geom, fg[:n], fg[n:])))
    gradients_fit = np.concatenate(
        grad_of_marginal_fit(a, b, fg[:n], fg[n:], tau_a, tau_b, geom))
    return marginals - gradients_fit

  foc_fg = lambda fg: first_order_conditions(geom, a, b, fg)
  foc_geom_a_b = lambda geom, a, b: first_order_conditions(geom, a, b, f_g)

  # Carries out implicit differentiation of F.O.C. using inversion of VJP
  # computed here using automatic differentiation of the F.O.C vector.
  _, pull_fg = jax.vjp(foc_fg, f_g)
  # Adds a small regularizer to improve conditioning when solving linear system
  pull_fg_0 = lambda vec: pull_fg(vec)[0] + ridge * np.sum(vec ** 2)
  vjp_gr = -jax.scipy.sparse.linalg.cg(pull_fg_0, np.concatenate(gr))[0]

  # Carries pullback onto original inputs, here geom, a and b.
  _, pull_geom_a_b = jax.vjp(foc_geom_a_b, geom, a, b)
  g_geom, g_a, g_b = pull_geom_a_b(vjp_gr)
  return (g_geom, g_a, g_b),

_sinkhorn_iterations_implicit.defvjp(_sinkhorn_iterations_implicit_fwd,
                                     _sinkhorn_iterations_implicit_bwd)


def marginal_error(geom: geometry.Geometry,
                   a: np.ndarray,
                   b: np.ndarray,
                   tau_a: float,
                   tau_b: float,
                   f_u: np.ndarray,
                   g_v: np.ndarray,
                   norm_error: int,
                   lse_mode) -> np.ndarray:
  """Conputes marginal error, the stopping criterion used to terminate Sinkhorn.

  Args:
    geom: a Geometry object.
    a: np.ndarray<float>[num_a,] or np.ndarray<float>[batch,num_a] weights.
    b: np.ndarray<float>[num_b,] or np.ndarray<float>[batch,num_b] weights.
    tau_a: float, ratio lam/(lam+eps) between KL divergence regularizer to first
     marginal and itself + epsilon regularizer used in the unbalanced
     formulation.
    tau_b: float, ratio lam/(lam+eps) between KL divergence regularizer to first
     marginal and itself + epsilon regularizer used in the unbalanced
     formulation.
    f_u: np.ndarray, potential or scaling
    g_v: np.ndarray, potential or scaling
    norm_error: int, p-norm used to compute error.
    lse_mode: True if log-sum-exp operations, False if kernel vector producs.

  Returns:
    a positive number quantifying how far from convergence the algorithm stands.

  """
  if tau_b == 1.0:
    err = geom.error(f_u, g_v, b, 0, norm_error, lse_mode)
  elif tau_a == 1.0:
    err = geom.error(f_u, g_v, a, 1, norm_error, lse_mode)
  else:
    # In the unbalanced case, we compute the norm of the gradient.
    # the gradient is equal to the marginal of the current plan minus
    # the gradient of < z, rho_z(exp^(-h/rho_z) -1> where z is either a or b
    # and h is either f or g. Note this is equal to z if rho_z → inf, which
    # is the case when tau_z → 1.0
    if lse_mode:
      target = grad_of_marginal_fit(a, b, f_u, g_v, tau_a, tau_b, geom)
    else:
      target = grad_of_marginal_fit(a, b,
                                    geom.potential_from_scaling(f_u),
                                    geom.potential_from_scaling(g_v),
                                    tau_a, tau_b, geom)
    err = geom.error(f_u, g_v, target[0], 1, norm_error, lse_mode)
    err += geom.error(f_u, g_v, target[1], 0, norm_error, lse_mode)
  return err


def ent_reg_cost(geom: geometry.Geometry,
                 a: np.ndarray,
                 b: np.ndarray,
                 tau_a: float,
                 tau_b: float,
                 f: np.ndarray,
                 g: np.ndarray) -> np.ndarray:
  """Computes objective of regularized OT given dual solutions f,g."""
  if tau_a == 1.0:
    div_a = np.sum((f - geom.potential_from_scaling(a)) * a)
  else:
    rho_a = geom.epsilon * (tau_a / (1 - tau_a))
    div_a = np.sum(a * (rho_a - (rho_a + geom.epsilon/2) *
                        np.exp(-(f - geom.potential_from_scaling(a))/ rho_a)))

  if tau_b == 1.0:
    div_b = np.sum((g - geom.potential_from_scaling(b)) * b)
  else:
    rho_b = geom.epsilon * (tau_b / (1 - tau_b))
    div_b = np.sum(b * (rho_b - (rho_b + geom.epsilon/2) *
                        np.exp(-(g - geom.potential_from_scaling(b))/ rho_b)))

  # Using https://arxiv.org/pdf/1910.12958.pdf Eq. 30
  return div_a + div_b + geom.epsilon * np.sum(a) * np.sum(b)


def grad_of_marginal_fit(a, b, f, g, tau_a, tau_b, geom):
  """Computes grad of terms linked to marginals a, b in objective.

  Computes gradient w.r.t. f and g of terms in
  https://arxiv.org/pdf/1910.12958.pdf, left-hand-side of Eq. 15
  (terms involving phi_star)

  Args:
    a: np.ndarray, first target marginal
    b: np.ndarray, second target marginal
    f: np.ndarray, potential
    g: np.ndarray, potential
    tau_a: float, strength (in ]0,1]) of regularizer w.r.t. marginal a.
    tau_b: float, strength (in ]0,1]) of regularizer w.r.t. marginal b.
    geom: geometry object.
  Returns:
    a vector of size concatenate((f,g)).
  """
  if tau_a == 1.0:
    grad_a = a
  else:
    rho_a = geom.epsilon * tau_a / (1 - tau_a)
    grad_a = a * derivative_phi_star(-f, rho_a)

  if tau_b == 1.0:
    grad_b = b
  else:
    rho_b = geom.epsilon * tau_b / (1 - tau_b)
    grad_b = b * derivative_phi_star(-g, rho_b)
  return grad_a, grad_b


def phi_star(f: np.ndarray, rho: float) -> np.ndarray:
  """Legendre transform of KL, https://arxiv.org/pdf/1910.12958.pdf p.9."""
  return rho * (np.exp(f / rho) - 1)


def derivative_phi_star(f: np.ndarray, rho: float) -> np.ndarray:
  """Derivative of Legendre transform of KL, see phi_star."""
  return np.exp(f / rho)
