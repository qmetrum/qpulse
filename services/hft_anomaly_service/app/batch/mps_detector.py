"""Tensor-network (Matrix Product State) anomaly detector — quantum-inspired.

Born-machine MPS over discretized cross-asset daily log-returns. Each asset is
binned into d quantile buckets; the joint distribution is parameterized as a
chain of low-rank site tensors A^[i] of shape (D_left, d, D_right). For a
configuration x = (x_1, ..., x_N):

    psi(x) = A^[1][:, x_1, :] @ A^[2][:, x_2, :] @ ... @ A^[N][:, x_N, :]
    P(x)   = |psi(x)|^2 / Z
    Z      = sum_x |psi(x)|^2  (computed in O(N * chi^4 * d) by contracting the
                                doubled-bond transfer matrices)

Born machines and DMRG-style fitting come straight from quantum many-body /
QML literature; we don't run on a quantum computer, we just borrow the math.

Public API mirrors copulas.TCopula so a future integration into the batch
path is a drop-in:

    m = MPSBornMachine(chi=4, d=8)
    m.fit(returns_2d_ndarray)   # rows = days, cols = assets
    ll = m.log_prob(returns_2d) # log P(x) per row, summed over assets

Gate hyperparameters live in docs/tn_anomaly_gate.md and must not be tuned
after seeing gate results.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
from scipy.optimize import minimize


def _quantile_bins(x: np.ndarray, d: int) -> np.ndarray:
    """Return d-1 quantile edges for x. Edges include neither -inf nor +inf."""
    qs = np.linspace(0.0, 1.0, d + 1)[1:-1]  # interior quantiles
    return np.quantile(x, qs)


class MPSBornMachine:
    """Born-machine MPS for joint distribution of N discretized assets."""

    __slots__ = ("chi", "d", "n_assets", "_tensors", "_bin_edges", "_log_Z", "_fitted")

    def __init__(self, chi: int = 4, d: int = 8):
        self.chi = int(chi)
        self.d = int(d)
        self.n_assets: int = 0
        self._tensors: List[np.ndarray] = []
        self._bin_edges: List[np.ndarray] = []
        self._log_Z: float = 0.0
        self._fitted = False

    # -- discretization -----------------------------------------------------

    def _digitize(self, returns: np.ndarray) -> np.ndarray:
        """returns: (T, N). Output: (T, N) int bin indices in [0, d-1]."""
        T, N = returns.shape
        out = np.empty((T, N), dtype=np.int64)
        for j in range(N):
            edges = self._bin_edges[j]
            idx = np.searchsorted(edges, returns[:, j], side="right")
            out[:, j] = np.clip(idx, 0, self.d - 1)
        return out

    # -- MPS contractions ---------------------------------------------------

    def _site_shape(self, i: int) -> tuple:
        """(D_left, d, D_right) for site i."""
        D_left = 1 if i == 0 else self.chi
        D_right = 1 if i == self.n_assets - 1 else self.chi
        return (D_left, self.d, D_right)

    def _flat_size(self) -> int:
        return sum(int(np.prod(self._site_shape(i))) for i in range(self.n_assets))

    def _unflatten(self, flat: np.ndarray) -> List[np.ndarray]:
        tensors = []
        offset = 0
        for i in range(self.n_assets):
            shp = self._site_shape(i)
            n = int(np.prod(shp))
            tensors.append(flat[offset : offset + n].reshape(shp))
            offset += n
        return tensors

    def _flatten(self, tensors: List[np.ndarray]) -> np.ndarray:
        return np.concatenate([t.ravel() for t in tensors])

    @staticmethod
    def _psi(tensors: List[np.ndarray], bins_row: np.ndarray) -> float:
        """Amplitude psi(x) for one configuration."""
        v = tensors[0][:, bins_row[0], :]  # (1, chi)
        for i in range(1, len(tensors)):
            v = v @ tensors[i][:, bins_row[i], :]
        return float(v.ravel()[0])

    @staticmethod
    def _psi_batch(tensors: List[np.ndarray], bins: np.ndarray) -> np.ndarray:
        """Amplitudes for many configurations. bins: (T, N). Out: (T,)."""
        T = bins.shape[0]
        v = tensors[0][:, bins[:, 0], :]            # (1, T, chi)
        v = v.transpose(1, 0, 2).reshape(T, -1)     # (T, chi)
        for i in range(1, len(tensors)):
            A = tensors[i][:, bins[:, i], :]        # (Dl, T, Dr)
            A = A.transpose(1, 0, 2)                # (T, Dl, Dr)
            v = np.einsum("tk,tkj->tj", v, A)
        return v.ravel()

    @staticmethod
    def _log_norm_sq(tensors: List[np.ndarray]) -> float:
        """log Z where Z = sum_x |psi(x)|^2.

        Z = Tr( prod_i M^[i] ),  M^[i]_(L,L'),(R,R') = sum_x A^[i]*[L,x,R] A^[i][L',x,R'].
        """
        # First site has D_left=1.
        A = tensors[0]                              # (1, d, Dr)
        # M_0: (1, 1, Dr, Dr) collapsed into (Dr, Dr) since D_left=1
        M = np.einsum("axc,axd->cd", A, A)
        for i in range(1, len(tensors)):
            B = tensors[i]                          # (Dl, d, Dr)
            # contract right index of M with left of doubled B: result (Dr, Dr)
            #   M (Dl, Dl') · sum_x B*[Dl, x, Dr] B[Dl', x, Dr']  -> (Dr, Dr')
            BB = np.einsum("axc,bxd->abcd", B, B)   # (Dl, Dl, Dr, Dr)
            M = np.einsum("ab,abcd->cd", M, BB)
        # Last site has D_right=1, so M is (1,1)
        Z = float(np.abs(M.ravel()[0]))
        return float(np.log(max(Z, 1e-300)))

    # -- log-likelihood + gradient via finite-diff (slow but bounded) ------

    def _neg_ll(self, flat: np.ndarray, bins_train: np.ndarray) -> float:
        tensors = self._unflatten(flat)
        psi = self._psi_batch(tensors, bins_train)
        log_psi2 = np.log(psi * psi + 1e-300)
        log_Z = self._log_norm_sq(tensors)
        nll = -float(np.mean(log_psi2 - log_Z))
        if not np.isfinite(nll):
            return 1e12
        return nll

    # -- public: fit / log_prob --------------------------------------------

    def fit(self, returns: np.ndarray, n_restarts: int = 3, max_iter: int = 500) -> None:
        """Fit MPS to returns; returns shape (T, N)."""
        returns = np.asarray(returns, dtype=float)
        if returns.ndim != 2:
            raise ValueError(f"returns must be 2D (T, N); got {returns.shape}")
        T, N = returns.shape
        self.n_assets = N
        self._bin_edges = [_quantile_bins(returns[:, j], self.d) for j in range(N)]
        bins_train = self._digitize(returns)

        size = self._flat_size()
        rng = np.random.default_rng(0)

        best_nll = np.inf
        best_flat: Optional[np.ndarray] = None
        for restart in range(n_restarts):
            init = rng.standard_normal(size) * (0.3 / np.sqrt(self.chi))
            res = minimize(
                self._neg_ll, init, args=(bins_train,),
                method="L-BFGS-B",
                options={"maxiter": max_iter, "ftol": 1e-7, "gtol": 1e-5},
            )
            if res.fun < best_nll:
                best_nll = float(res.fun)
                best_flat = res.x
        if best_flat is None:
            raise RuntimeError("MPS fit did not converge in any restart")

        self._tensors = self._unflatten(best_flat)
        self._log_Z = self._log_norm_sq(self._tensors)
        self._fitted = True

    def log_prob(self, returns: np.ndarray) -> np.ndarray:
        """Per-row log P(x). returns shape (T, N) → output shape (T,)."""
        if not self._fitted:
            raise RuntimeError("MPS not fitted")
        returns = np.asarray(returns, dtype=float)
        if returns.ndim != 2 or returns.shape[1] != self.n_assets:
            raise ValueError(
                f"expected (T, {self.n_assets}); got {returns.shape}"
            )
        bins = self._digitize(returns)
        psi = self._psi_batch(self._tensors, bins)
        log_psi2 = np.log(psi * psi + 1e-300)
        return log_psi2 - self._log_Z

    def sample(self, n: int, rng: Optional[np.random.Generator] = None) -> np.ndarray:
        """Sample n bin-configurations from the fitted distribution.

        Used by the gate's KL-divergence stability check (C4). Computes per-bin
        marginals conditioned on already-sampled prefix via right-environment
        contractions.
        """
        if not self._fitted:
            raise RuntimeError("MPS not fitted")
        rng = rng or np.random.default_rng(0)
        N = self.n_assets

        # Right environments: R[i] = product of doubled tensors from site i+1 to N.
        # R[N] = scalar 1; we store as (1,1) for uniform code.
        R = [np.ones((1, 1))] * (N + 1)
        for i in range(N - 1, -1, -1):
            B = self._tensors[i]                    # (Dl, d, Dr)
            BB = np.einsum("axc,bxd->abcd", B, B)   # (Dl, Dl, Dr, Dr)
            R[i] = np.einsum("abcd,cd->ab", BB, R[i + 1])
        Z = float(R[0].ravel()[0])
        if Z <= 0:
            raise RuntimeError("MPS norm is non-positive; fit is degenerate")

        out = np.empty((n, N), dtype=np.int64)
        for s in range(n):
            # Left state vector L (Dl, Dl) starting at (1,1).
            L = np.ones((1, 1))
            for i in range(N):
                A = self._tensors[i]                # (Dl, d, Dr)
                # Marginal P(x_i | prefix) ∝ sum_{Dr,Dr'} L · A[:,x,:] · R[i+1] · A[:,x,:]
                # Compute for all x:
                AA = np.einsum("axc,bxd->xabcd", A, A)              # (d, Dl, Dl, Dr, Dr)
                num = np.einsum("ab,xabcd,cd->x", L, AA, R[i + 1])  # (d,)
                num = np.maximum(num, 0)
                ssum = num.sum()
                if ssum <= 0:
                    # numerical underflow — sample uniformly
                    probs = np.ones(self.d) / self.d
                else:
                    probs = num / ssum
                xi = int(rng.choice(self.d, p=probs))
                out[s, i] = xi
                # Update L for next site
                Ax = A[:, xi, :]                                    # (Dl, Dr)
                L = np.einsum("ab,ac,bd->cd", L, Ax, Ax)
            # end per-asset loop
        return out

    # -- introspection ------------------------------------------------------

    def n_params(self) -> int:
        return self._flat_size()
