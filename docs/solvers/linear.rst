ott.solvers.linear
==================
.. module:: ott.solvers.linear
.. currentmodule:: ott.solvers.linear

Linear solvers are the bread-and-butter of OT solvers. They can be called on
their own, either the Sinkhorn
:class:`ott.solvers.linear.sinkhorn.Sinkhorn` or Low-Rank
:class:`ott.solvers.linear.sinkhorn_lr.LRSinkhorn` solvers, to match two
datasets. They also appear as subroutines for more advanced solvers in the
:mod:`ott.solvers` module, notably :mod:`ott.solvers.quadratic` or
:mod:`ott.solvers.nn`.

Sinkhorn Solvers
----------------
.. autosummary::
    :toctree: _autosummary

    sinkhorn.solve
    sinkhorn.Sinkhorn
    sinkhorn.SinkhornState
    sinkhorn.SinkhornOutput
    sinkhorn_lr.LRSinkhorn
    sinkhorn_lr.LRSinkhornState
    sinkhorn_lr.LRSinkhornOutput

Barycenter Solvers
------------------
.. autosummary::
    :toctree: _autosummary

    continuous_barycenter.FreeWassersteinBarycenter
    continuous_barycenter.FreeBarycenterState
    discrete_barycenter.FixedBarycenter
    discrete_barycenter.SinkhornBarycenterOutput

Sinkhorn Acceleration
---------------------
.. autosummary::
    :toctree: _autosummary

    acceleration.Momentum
    acceleration.AndersonAcceleration

Implicit Differentiation
------------------------
.. autosummary::
    :toctree: _autosummary

    implicit_differentiation.ImplicitDiff
