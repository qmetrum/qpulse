"""Gaussian, t, and MPS copulas with fit + log-probability.

All three expose the same interface:

    class Copula:
        def fit(self, returns: np.ndarray) -> None                    # (T, N) log-returns
        def log_prob(self, returns: np.ndarray) -> np.ndarray         # (T,) log-density per obs

Marginals are handled identically across methods: empirical rank → uniform.
Copulas model dependence only; per-observation log density is the copula
density evaluated at the rank-transformed point (no marginal contribution,
consistent across methods — what's compared is joint dependence structure).
"""
from __future__ import annotations

import numpy as np
from scipy import stats
from scipy.stats import kendalltau


# ---------------------------------------------------------------------------
# Shared: marginal rank transform (empirical CDF)
# ---------------------------------------------------------------------------

def rank_to_uniform(X: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    """Per-column empirical CDF transform to uniform (0,1). Nudged off the
    boundary to keep inverse transforms finite."""
    T, N = X.shape
    U = np.empty_like(X, dtype=np.float64)
    for i in range(N):
        ranks = stats.rankdata(X[:, i], method="average")
        u = ranks / (T + 1)
        U[:, i] = np.clip(u, eps, 1 - eps)
    return U


def fit_marginals(X: np.ndarray):
    """Cache reference-window sorted values per column for out-of-sample
    marginal CDF evaluation. Returns a callable that maps new X → U."""
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


# ---------------------------------------------------------------------------
# Gaussian copula
# ---------------------------------------------------------------------------

def _kendall_corr(U: np.ndarray) -> np.ndarray:
    """Correlation matrix via Kendall tau → Pearson for elliptical copulas.
    R_ij = sin(pi * tau_ij / 2). Nearest-PD projection to keep solvable."""
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
    """Project to nearest PSD matrix, then add small jitter."""
    R = 0.5 * (R + R.T)
    w, V = np.linalg.eigh(R)
    w = np.maximum(w, 1e-6)
    R = V @ np.diag(w) @ V.T
    # Rescale diagonal to 1 (correlation matrix)
    D = np.diag(1.0 / np.sqrt(np.clip(np.diag(R), 1e-12, None)))
    R = D @ R @ D
    return R


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
        sign, self.log_det = np.linalg.slogdet(self.R)
        if sign <= 0:
            raise RuntimeError("Gaussian copula correlation not PD")

    def log_prob(self, X: np.ndarray) -> np.ndarray:
        U = self.marg(X)
        Z = stats.norm.ppf(U)
        N = Z.shape[1]
        # log c(u; R) = -0.5 log|R| - 0.5 z' (R^-1 - I) z
        quad = np.einsum("ti,ij,tj->t", Z, self.R_inv - np.eye(N), Z)
        return -0.5 * self.log_det - 0.5 * quad


# ---------------------------------------------------------------------------
# Student t copula
# ---------------------------------------------------------------------------

class TCopula:
    """t-copula with Kendall-tau correlation and grid-search df.
    df candidates: {3, 5, 10, 20} — covers heavy to near-Gaussian tails."""

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
        # Pick best df on training data itself (then used on eval)
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
        from scipy.special import gammaln
        # log c(u; R, df) = log Γ((df+N)/2) + (N-1) log Γ(df/2) - N log Γ((df+1)/2)
        #                 - 0.5 log|R|
        #                 - (df+N)/2 log(1 + q/df)
        #                 + sum_i (df+1)/2 log(1 + z_i^2/df)
        const = (gammaln((df + N) / 2) + (N - 1) * gammaln(df / 2)
                 - N * gammaln((df + 1) / 2) - 0.5 * self.log_det)
        body = -(df + N) / 2 * np.log1p(quad / df)
        marg = ((df + 1) / 2 * np.log1p(Z ** 2 / df)).sum(axis=1)
        return const + body + marg

    def log_prob(self, X: np.ndarray) -> np.ndarray:
        U = self.marg(X)
        return self._ll(U, self.df)


# ---------------------------------------------------------------------------
# MPS copula — reuses the discretise → joint tensor → SVD chain,
# and adds a log_prob by MPS contraction.
# ---------------------------------------------------------------------------

class MPSCopula:
    """MPS-copula via empirical joint histogram compressed to MPS of bond χ.

    Probability of a new observation = contraction of the MPS along the
    discretized bin indices. Uses reference-period quantile bin edges
    for all out-of-sample evaluation.
    """

    FLOOR = 1e-9  # floor for log to handle SVD-induced near-zero values

    def __init__(self, chi: int = 8, d: int = 4):
        self.chi = chi
        self.d = d
        self.mps = None
        self.bin_edges = None  # list of length N, each array of length d+1
        self.total_mass = 1.0  # normalizer after SVD truncation

    def fit(self, X: np.ndarray) -> None:
        T, N = X.shape
        if N > 8:
            raise ValueError(f"MPS dense-tensor fit limited to N<=8 (got N={N})")
        # Standardize each column (mean 0 std 1) for bin-edge computation
        mu = X.mean(axis=0)
        sd = X.std(axis=0) + 1e-12
        Z = (X - mu) / sd
        edges_list = []
        binned = np.empty_like(X, dtype=np.int64)
        for i in range(N):
            e = np.quantile(Z[:, i], np.linspace(0, 1, self.d + 1))
            e[0], e[-1] = -np.inf, np.inf
            edges_list.append(e)
            binned[:, i] = np.clip(
                np.searchsorted(e, Z[:, i], side="right") - 1, 0, self.d - 1
            )
        # Empirical joint pmf
        joint = np.zeros([self.d] * N, dtype=float)
        for row in binned:
            joint[tuple(row)] += 1.0
        joint /= max(joint.sum(), 1e-12)
        # SVD chain compression
        self.mps = self._tensor_to_mps(joint, self.chi)
        self.bin_edges = edges_list
        self._marg_mu = mu
        self._marg_sd = sd
        # Total mass after truncation (should be ~1; compute explicitly)
        self.total_mass = self._mps_mass(self.mps)

    @staticmethod
    def _tensor_to_mps(joint: np.ndarray, chi: int):
        N = joint.ndim
        d = joint.shape[0]
        mats = []
        residual = joint.reshape(d, -1)
        left_dim = 1
        for site in range(N - 1):
            residual = residual.reshape(left_dim * d, -1)
            U, S, Vt = np.linalg.svd(residual, full_matrices=False)
            k = min(chi, len(S))
            U = U[:, :k]
            S = S[:k]
            Vt = Vt[:k, :]
            site_tensor = U.reshape(left_dim, d, k)
            mats.append(site_tensor)
            residual = np.diag(S) @ Vt
            left_dim = k
        mats.append(residual.reshape(left_dim, d, 1))
        return mats

    def _bin(self, X: np.ndarray) -> np.ndarray:
        """Bin new returns using reference bin edges."""
        Z = (X - self._marg_mu) / self._marg_sd
        T, N = X.shape
        out = np.empty((T, N), dtype=np.int64)
        for i in range(N):
            e = self.bin_edges[i]
            out[:, i] = np.clip(np.searchsorted(e, Z[:, i], side="right") - 1, 0, self.d - 1)
        return out

    @staticmethod
    def _mps_mass(mps) -> float:
        """Sum over all physical indices: the MPS's total probability mass."""
        L = np.array([[1.0]])  # shape (1, 1)
        for M in mps:
            summed = M.sum(axis=1)      # (left, right)
            L = L @ summed              # (1, right)
        return float(L.item())

    def _mps_prob_single(self, bins: np.ndarray) -> float:
        """Probability mass at a single bin tuple (length N)."""
        v = np.array([1.0])
        for k, b in enumerate(bins):
            A = self.mps[k][:, b, :]    # (left, right)
            v = v @ A
        return float(v.item())

    def log_prob(self, X: np.ndarray) -> np.ndarray:
        binned = self._bin(X)
        probs = np.array([self._mps_prob_single(row) for row in binned])
        probs = np.maximum(probs / max(self.total_mass, self.FLOOR), self.FLOOR)
        return np.log(probs)
