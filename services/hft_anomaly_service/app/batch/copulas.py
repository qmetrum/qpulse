"""Gaussian and t copula for Qpulse's cross-asset batch path.

Classical only - MPS was validated against these in gate/ and did not win
(see gate/results/verdict.md). Interface mirrors the gate's copulas.py:

    class Copula:
        def fit(self, returns: np.ndarray) -> None                  # (T, N)
        def log_prob(self, returns: np.ndarray) -> np.ndarray       # (T,)
"""
from __future__ import annotations

import numpy as np
from scipy import stats
from scipy.special import gammaln
from scipy.stats import kendalltau


def rank_to_uniform(X: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    T, N = X.shape
    U = np.empty_like(X, dtype=np.float64)
    for i in range(N):
        ranks = stats.rankdata(X[:, i], method="average")
        U[:, i] = np.clip(ranks / (T + 1), eps, 1 - eps)
    return U


def fit_marginals(X: np.ndarray):
    T, N = X.shape
    sorted_cols = [np.sort(X[:, i]) for i in range(N)]

    def transform(Y: np.ndarray, eps: float = 1e-6) -> np.ndarray:
        Tb, Nb = Y.shape
        assert Nb == N
        U = np.empty_like(Y, dtype=np.float64)
        for i in range(N):
            idx = np.searchsorted(sorted_cols[i], Y[:, i], side="right")
            U[:, i] = np.clip(idx / (T + 1), eps, 1 - eps)
        return U

    return transform


def _kendall_corr(U: np.ndarray) -> np.ndarray:
    N = U.shape[1]
    R = np.eye(N)
    for i in range(N):
        for j in range(i + 1, N):
            tau, _ = kendalltau(U[:, i], U[:, j])
            if np.isnan(tau):
                tau = 0.0
            R[i, j] = R[j, i] = np.sin(np.pi * tau / 2)
    return _nearest_pd(R)


def _nearest_pd(R: np.ndarray) -> np.ndarray:
    R = 0.5 * (R + R.T)
    w, V = np.linalg.eigh(R)
    w = np.maximum(w, 1e-6)
    R = V @ np.diag(w) @ V.T
    D = np.diag(1.0 / np.sqrt(np.clip(np.diag(R), 1e-12, None)))
    return D @ R @ D


class GaussianCopula:
    def __init__(self):
        self.R = None
        self.R_inv = None
        self.log_det = None
        self.marg = None

    def fit(self, X: np.ndarray) -> None:
        self.marg = fit_marginals(X)
        U = rank_to_uniform(X)
        self.R = _kendall_corr(U)
        self.R_inv = np.linalg.inv(self.R)
        _, self.log_det = np.linalg.slogdet(self.R)

    def log_prob(self, X: np.ndarray) -> np.ndarray:
        U = self.marg(X)
        Z = stats.norm.ppf(U)
        N = Z.shape[1]
        quad = np.einsum("ti,ij,tj->t", Z, self.R_inv - np.eye(N), Z)
        return -0.5 * self.log_det - 0.5 * quad


class TCopula:
    """t-copula with Kendall-tau correlation and grid-search df.
    df candidates: {3, 5, 10, 20} - from heavy-tailed to near-Gaussian."""

    DF_GRID = (3.0, 5.0, 10.0, 20.0)

    def __init__(self):
        self.R = None
        self.R_inv = None
        self.log_det = None
        self.df = None
        self.marg = None

    def fit(self, X: np.ndarray) -> None:
        self.marg = fit_marginals(X)
        U = rank_to_uniform(X)
        self.R = _kendall_corr(U)
        self.R_inv = np.linalg.inv(self.R)
        _, self.log_det = np.linalg.slogdet(self.R)
        best_ll, best_df = -np.inf, self.DF_GRID[0]
        for df in self.DF_GRID:
            ll = self._ll(U, df).sum()
            if ll > best_ll:
                best_ll, best_df = ll, df
        self.df = best_df

    def _ll(self, U: np.ndarray, df: float) -> np.ndarray:
        Z = stats.t.ppf(U, df=df)
        N = Z.shape[1]
        quad = np.einsum("ti,ij,tj->t", Z, self.R_inv, Z)
        const = (gammaln((df + N) / 2) + (N - 1) * gammaln(df / 2)
                 - N * gammaln((df + 1) / 2) - 0.5 * self.log_det)
        body = -(df + N) / 2 * np.log1p(quad / df)
        marg = ((df + 1) / 2 * np.log1p(Z ** 2 / df)).sum(axis=1)
        return const + body + marg

    def log_prob(self, X: np.ndarray) -> np.ndarray:
        U = self.marg(X)
        return self._ll(U, self.df)
