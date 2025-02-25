"""Market underlying the BLP model."""

import functools
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .. import exceptions, options
from ..configurations.iteration import ContractionResults, Iteration
from ..economies.economy import Economy
from ..micro import MicroDataset, Moments
from ..parameters import BetaParameter, GammaParameter, NonlinearCoefficient, Parameter, Parameters, RhoParameter
from ..primitives import Container
from ..utilities.algebra import approximately_invert, approximately_solve
from ..utilities.basics import Array, Bounds, RecArray, Error, Groups, SolverStats, update_matrices


class Market(Container):
    """A market underlying the BLP model."""

    t: Any
    membership_matrix: Optional[Array]
    ownership_matrix: Optional[Array]
    groups: Groups
    unique_nesting_ids: Array
    epsilon_scale: float
    costs_type: str
    J: int
    I: int
    K1: int
    K2: int
    K3: int
    D: int
    H: int
    sigma: Array
    pi: Array
    beta: Optional[Array]
    gamma: Optional[Array]
    rho_size: int
    group_rho: Array
    rho: Array
    delta: Optional[Array]
    mu: Array
    parameters: Parameters

    def __init__(
            self, economy: Economy, t: Any, parameters: Parameters, sigma: Array, pi: Array, rho: Array,
            beta: Optional[Array] = None, gamma: Optional[Array] = None, delta: Optional[Array] = None,
            data_override: Optional[Dict[str, Array]] = None, agents_override: Optional[RecArray] = None) -> None:
        """Store or compute information about formulations, data, parameters, and utility."""

        # structure relevant data
        self.t = t
        super().__init__(
            economy.products[economy._product_market_indices[t]],
            economy.agents[economy._agent_market_indices[t]] if agents_override is None else agents_override
        )

        # membership matrices are computed on-demand
        self.membership_matrix = None

        # store ownership if specified (otherwise it's also computed on-demand)
        self.ownership_matrix = None
        if self.products.ownership.shape[1] > 0:
            self.ownership_matrix = self.products.ownership[:, :self.products.shape[0]]

        # drop unneeded product data fields to save memory
        products_update_mapping = {}
        for key in ['market_ids', 'demand_ids', 'supply_ids', 'clustering_ids', 'X1', 'X3', 'ZD', 'ZS', 'ownership']:
            products_update_mapping[key] = (None, self.products[key].dtype)
        self.products = update_matrices(self.products, products_update_mapping)

        # drop unneeded agent data fields, fill missing columns of integration nodes (associated with zeros in sigma)
        #   with zeros, and drop extra product-specific demographic values for product indices not in this market
        agents_update_mapping: Dict[str, Tuple[Optional[Array], Any]] = {
            'market_ids': (None, self.agents.market_ids.dtype)
        }
        if not parameters.nonzero_sigma_index.all():
            nodes = np.zeros((self.agents.shape[0], economy.K2), self.agents.nodes.dtype)
            nodes[:, parameters.nonzero_sigma_index] = self.agents.nodes[:, :parameters.nonzero_sigma_index.sum()]
            agents_update_mapping['nodes'] = (nodes, nodes.dtype)
        if len(self.agents.demographics.shape) == 3:
            demographics = self.agents.demographics[..., :self.products.size]
            agents_update_mapping['demographics'] = (demographics, demographics.dtype)
        self.agents = update_matrices(self.agents, agents_update_mapping)

        # create nesting groups but keep all nesting IDs for associating parameters with groups
        self.groups = Groups(self.products.nesting_ids)
        self.unique_nesting_ids = economy.unique_nesting_ids

        # store other configuration information
        self.epsilon_scale = economy.epsilon_scale
        self.costs_type = economy.costs_type

        # count dimensions
        self.J = self.products.shape[0]
        self.I = self.agents.shape[0]
        self.K1 = economy.K1
        self.K2 = economy.K2
        self.K3 = economy.K3
        self.D = economy.D
        self.H = self.groups.group_count

        # override any data
        if data_override is not None:
            for name, variable in data_override.items():
                self.products[name][:] = variable[economy._product_market_indices[t]]
            for index, formulation in enumerate(self._X2_formulations):
                if any(n in formulation.names for n in data_override):
                    self.products.X2[:, [index]] = formulation.evaluate(self.products)

        # store parameters (expand rho to all groups and all products)
        self.parameters = parameters
        self.sigma = sigma
        self.pi = pi
        self.beta = beta
        self.gamma = gamma
        self.rho_size = rho.size
        if self.rho_size == 1:
            self.group_rho = np.full((self.H, 1), float(rho))
            self.rho = np.full((self.J, 1), float(rho))
        else:
            self.group_rho = rho[np.searchsorted(economy.unique_nesting_ids, self.groups.unique)]
            self.rho = self.groups.expand(self.group_rho)

        # store delta and compute mu
        self.delta = None if delta is None else delta[economy._product_market_indices[t]]
        with np.errstate(all='ignore'):
            self.mu = self.compute_mu()

    def get_product(self, product_id: Any) -> int:
        """Get the product index associated with a product ID. This assumes that the market has already been validated
        to make sure that the product ID appears exactly once.
        """
        return int(np.argmax(self.products.product_ids == product_id))

    def get_agents_index(self, agent_ids: Sequence[Any]) -> Array:
        """Get an index of agents associated with agent IDs."""
        return np.array([i in agent_ids for i in self.agents.agent_ids])

    def get_membership_matrix(self) -> Array:
        """Build a membership matrix from nesting IDs."""
        if self.membership_matrix is None:
            self.membership_matrix = (self.products.nesting_ids == self.products.nesting_ids.T).astype(options.dtype)
        return self.membership_matrix

    def get_ownership_matrix(self, firm_ids: Optional[Array] = None, ownership: Optional[Array] = None) -> Array:
        """Get a pre-computed ownership matrix or build one. By default, use unchanged firm IDs."""
        if ownership is not None:
            return ownership[:, :self.J]
        if firm_ids is not None:
            return (firm_ids == firm_ids.T).astype(options.dtype)
        if self.ownership_matrix is None:
            if self.products.firm_ids.size == 0:
                raise ValueError("Either firm IDs or an ownership matrix must have been specified.")
            self.ownership_matrix = (self.products.firm_ids == self.products.firm_ids.T).astype(options.dtype)
        return self.ownership_matrix

    def compute_random_coefficients(self, sigma: Optional[Array] = None, pi: Optional[Array] = None) -> Array:
        """Compute all random coefficients. By default, use unchanged parameter values."""
        if sigma is None:
            sigma = self.sigma
        if pi is None:
            pi = self.pi

        coefficients = sigma @ self.agents.nodes.T
        if self.D > 0:
            coefficients = coefficients + pi @ self.agents.demographics.T

        for k, rc_type in enumerate(self.parameters.rc_types):
            if rc_type == 'log':
                if len(coefficients.shape) == 2:
                    coefficients[k] = np.exp(coefficients[k])
                else:
                    assert len(coefficients.shape) == 3
                    coefficients[:, k] = np.exp(coefficients[:, k])

        return coefficients

    def compute_single_random_coefficient(self, k: int) -> Array:
        """Compute a single random coefficient."""
        coefficient = self.sigma[[k], :] @ self.agents.nodes.T
        if self.D > 0:
            coefficient = coefficient + self.pi[[k], :] @ self.agents.demographics.T
            if len(coefficient.shape) == 3:
                coefficient = coefficient.squeeze(axis=1)

        if self.parameters.rc_types[k] == 'log':
            coefficient = np.exp(coefficient)

        return coefficient

    def compute_mu(
            self, X2: Optional[Array] = None, sigma: Optional[Array] = None, pi: Optional[Array] = None) -> Array:
        """Compute mu. By default, use unchanged X2 and parameters."""
        if X2 is None:
            X2 = self.products.X2

        coefficients = self.compute_random_coefficients(sigma, pi)
        if len(coefficients.shape) == 2:
            return X2 @ coefficients

        assert len(coefficients.shape) == 3
        return (X2[..., None] * coefficients).sum(axis=1)

    def update_delta_with_variable(self, name: str, variable: Array) -> Array:
        """Update delta to reflect a changed variable by adding any parameter-weighted characteristic changes to X1."""
        assert self.beta is not None and self.delta is not None

        # if the variable does not contribute to X1, delta remains unchanged
        if not any(name in f.names for f in self._X1_formulations):
            return self.delta

        # if the variable does contribute to X1, delta may change
        delta = self.delta.copy()
        override = {name: variable}
        for index, formulation in enumerate(self._X1_formulations):
            if name in formulation.names:
                change = formulation.evaluate(self.products, override) - formulation.evaluate(self.products)
                delta += self.beta[index] * change

        return delta

    def update_mu_with_variable(self, name: str, variable: Array) -> Array:
        """Update mu to reflect a changed variable by re-computing mu under the changed X2."""

        # if the variable does not contribute to X2, mu remains unchanged
        if not any(name in f.names for f in self._X2_formulations):
            return self.mu

        # if the variable does contribute to X2, mu may change
        X2 = self.products.X2.copy()
        override = {name: variable}
        for index, formulation in enumerate(self._X2_formulations):
            if name in formulation.names:
                X2[:, [index]] = formulation.evaluate(self.products, override)

        return self.compute_mu(X2)

    def update_costs_with_variable(self, costs: Array, name: str, variable: Array) -> Array:
        """Update marginal costs to reflect a changed variable by adding any parameter-weighted characteristic changes
        to X3, taking into account whether costs are linear or log-linear.
        """
        assert self.gamma is not None

        # if the variable does not contribute to X3, costs remain unchanged
        if not any(name in f.names for f in self._X3_formulations):
            return costs

        # if the variable does contribute to X3, costs may change
        costs = costs.copy()
        override = {name: variable}
        for index, formulation in enumerate(self._X3_formulations):
            if name in formulation.names:
                change = formulation.evaluate(self.products, override) - formulation.evaluate(self.products)
                if self.costs_type == 'linear':
                    costs += self.gamma[index] * change
                else:
                    assert self.costs_type == 'log'
                    costs *= np.exp(self.gamma[index] * change)

        return costs

    def compute_X1_derivatives(self, name: str, variable: Optional[Array] = None, order: int = 1) -> Array:
        """Compute derivatives of X1 with respect to a variable. By default, use unchanged variable values."""
        override = None if variable is None else {name: variable}
        derivatives = np.zeros((self.J, self.K1), options.dtype)
        for index, formulation in enumerate(self._X1_formulations):
            if name in formulation.names:
                derivatives[:, [index]] = formulation.evaluate_derivative(name, self.products, override, order)

        return derivatives

    def compute_X2_derivatives(self, name: str, variable: Optional[Array] = None, order: int = 1) -> Array:
        """Compute derivatives of X2 with respect to a variable. By default, use unchanged variable values."""
        override = None if variable is None else {name: variable}
        derivatives = np.zeros((self.J, self.K2), options.dtype)
        for index, formulation in enumerate(self._X2_formulations):
            if name in formulation.names:
                derivatives[:, [index]] = formulation.evaluate_derivative(name, self.products, override, order)

        return derivatives

    def compute_X3_derivatives(self, name: str, variable: Optional[Array] = None, order: int = 1) -> Array:
        """Compute derivatives of X3 with respect to a variable. By default, use unchanged variable values."""
        override = None if variable is None else {name: variable}
        derivatives = np.zeros((self.J, self.K3), options.dtype)
        for index, formulation in enumerate(self._X3_formulations):
            if name in formulation.names:
                derivatives[:, [index]] = formulation.evaluate_derivative(name, self.products, override, order)

        return derivatives

    def compute_utility_derivatives(self, name: str, variable: Optional[Array] = None, order: int = 1) -> Array:
        """Compute derivatives of utility with respect to a variable. By default, use unchanged variable values."""
        assert self.beta is not None
        derivatives = np.tile(self.compute_X1_derivatives(name, variable, order) @ np.nan_to_num(self.beta), self.I)

        if self.K2 > 0:
            X2_derivatives = self.compute_X2_derivatives(name, variable, order)
            coefficients = self.compute_random_coefficients()
            if len(coefficients.shape) == 2:
                derivatives += X2_derivatives @ coefficients
            else:
                assert len(coefficients.shape) == 3
                derivatives += (X2_derivatives[..., None] * coefficients).sum(axis=1)

        if self.epsilon_scale != 1:
            derivatives /= self.epsilon_scale

        return derivatives

    def compute_costs_derivatives(
            self, costs: Array, name: str, variable: Optional[Array] = None, order: int = 1) -> Array:
        """Compute derivatives of costs with respect to a variable. By default, use unchanged variable values."""
        assert self.gamma is not None
        derivatives = self.compute_X3_derivatives(name, variable, order) @ np.nan_to_num(self.gamma)

        if self.costs_type == 'log':
            derivatives *= costs

        return derivatives

    def compute_probabilities(
            self, delta: Array = None, mu: Optional[Array] = None, linear: bool = True, safe: bool = True,
            utility_reduction: Optional[Array] = None, numerator: Optional[Array] = None,
            eliminate_outside: bool = False, eliminate_product: Optional[int] = None) -> Tuple[Array, Optional[Array]]:
        """Compute choice probabilities. By default, use unchanged delta and mu values. If linear is False, delta and mu
        must be specified and already be exponentiated. If safe is True, scale the logit equation by the exponential of
        negative the maximum utility for each agent, and if utility_reduction is specified, it should be values that
        have already been subtracted from the specified utility for each agent. If the numerator is specified, it will
        be used as the numerator in the non-nested logit expression. If any products are eliminated, eliminate the
        outside option, an inside product, or both from the choice set.
        """
        if delta is None:
            assert self.delta is not None
            delta = self.delta
        if mu is None:
            mu = self.mu
        if self.K2 == 0:
            mu = int(not linear)

        # compute exponentiated utilities, optionally re-scaling the logit expression
        if not linear:
            assert self.epsilon_scale == 1
            exp_utilities = np.array(delta * mu)
            if self.H > 0:
                exp_utilities **= 1 / (1 - self.rho)
        else:
            utilities = delta + mu
            if self.H > 0:
                utilities /= 1 - self.rho

            if self.epsilon_scale != 1:
                assert self.H == 0
                utilities /= self.epsilon_scale

            if safe:
                utility_reduction = np.clip(utilities.max(axis=0, keepdims=True), 0, None)
                utilities -= utility_reduction

            exp_utilities = np.exp(utilities)

        # compute any components used to re-scale the logit expression
        scale = scale_weights = 1
        if utility_reduction is not None:
            if self.H == 0:
                scale = np.exp(-utility_reduction)
            else:
                scale = np.exp(-utility_reduction * (1 - self.group_rho))
                if self.rho_size > 1:
                    scale_weights = np.exp(-utility_reduction[None] * (self.group_rho.T - self.group_rho)[..., None])

        # optionally eliminate the outside option from the choice set
        if eliminate_outside:
            scale = 0

        # optionally eliminate a product from the choice set
        if eliminate_product is not None:
            exp_utilities[eliminate_product] = 0

        # compute standard probabilities
        if self.H == 0:
            if numerator is None:
                numerator = exp_utilities
            probabilities = numerator / (scale + exp_utilities.sum(axis=0, keepdims=True))
            return probabilities, None

        # compute nested probabilities
        exp_inclusives = self.groups.sum(exp_utilities)
        with np.errstate(divide='ignore', invalid='ignore'):
            exp_weighted_inclusives = np.exp(np.log(exp_inclusives) * (1 - self.group_rho))
            conditionals = exp_utilities / self.groups.expand(exp_inclusives)
        exp_weighted_inclusives[~np.isfinite(exp_weighted_inclusives)] = 0
        conditionals[~np.isfinite(conditionals)] = 0
        marginals = exp_weighted_inclusives / (scale + (scale_weights * exp_weighted_inclusives[None]).sum(axis=1))
        probabilities = conditionals * self.groups.expand(marginals)

        return probabilities, conditionals

    def compute_eliminated_probabilities(
            self, probabilities: Array, delta: Optional[Array] = None, eliminate_outside: bool = False,
            eliminate_product: Optional[int] = None) -> Array:
        """Convert choice probabilities into probabilities with the outside option, an inside product, or both removed
        from the choice set. If there are any errors, revert to the full derivation of these eliminated probabilities,
        using the delta with which this market was initialized.
        """

        # compute the denominator of the expression
        if eliminate_outside and eliminate_product is not None:
            eliminate_index = np.arange(self.J) != eliminate_product
            denominator = probabilities.sum(axis=0, where=eliminate_index[:, None], keepdims=True)
        elif eliminate_outside:
            denominator = probabilities.sum(axis=0, keepdims=True)
        elif eliminate_product is not None:
            denominator = 1 - probabilities[eliminate_product][None]
        else:
            return probabilities

        # try to compute the eliminated probabilities, ignoring any divisions by zero or underflow
        with np.errstate(all='ignore'):
            eliminated = probabilities / denominator

        # the probability of choosing an eliminated inside product is zero
        if eliminate_product is not None:
            eliminated[eliminate_product] = 0

        # if there were any errors, compute the eliminated probabilities directly
        if not np.isfinite(eliminated).all():
            eliminated, _ = self.compute_probabilities(
                delta, eliminate_outside=eliminate_outside, eliminate_product=eliminate_product
            )

        return eliminated

    def compute_delta(
            self, initial_delta: Array, iteration: Iteration, fp_type: str, shares_bounds: Bounds) -> (
            Tuple[Array, Array, SolverStats, List[Error]]):
        """Compute the mean utility for this market that equates market shares to observed values by solving a fixed
        point problem.
        """
        errors: List[Error] = []

        # default assumption is that no shares were clipped at the end of fixed point iteration
        clipped_shares = np.zeros((self.J, 1), np.bool_)

        # if there is no heterogeneity, use the closed-form solution
        if self.K2 == 0:
            log_shares = np.log(self.products.shares)
            log_outside_share = np.log(1 - self.products.shares.sum())
            delta = log_shares - log_outside_share

            if self.H > 0:
                log_group_shares = np.log(self.groups.expand(self.groups.sum(self.products.shares)))
                delta -= self.rho * (log_shares - log_group_shares)

            if self.epsilon_scale != 1:
                assert self.H == 0
                delta *= self.epsilon_scale

            return delta, clipped_shares, SolverStats(), errors

        # solve for delta with a linear fixed point
        if 'linear' in fp_type:
            log_shares = np.log(self.products.shares)
            compute_probabilities = functools.partial(self.compute_probabilities, safe='safe' in fp_type)

            # define the function used to clip shares outside potentially pre-specified bounds
            clip_shares = lambda _: None
            if np.isfinite(shares_bounds).all():
                def clip_shares(shares: Array) -> None:
                    """Clip shares from below and above."""
                    nonlocal clipped_shares
                    small_shares = shares < shares_bounds[0]
                    shares[small_shares] = shares_bounds[0]
                    large_shares = shares > shares_bounds[1]
                    shares[large_shares] = shares_bounds[1]
                    clipped_shares = small_shares | large_shares

            elif np.isfinite(shares_bounds[0]):
                def clip_shares(shares: Array) -> None:
                    """Clip shares from below."""
                    nonlocal clipped_shares
                    clipped_shares = shares < shares_bounds[0]
                    shares[clipped_shares] = shares_bounds[0]

            elif np.isfinite(shares_bounds[1]):
                def clip_shares(shares: Array) -> None:
                    """Clip shares from above."""
                    nonlocal clipped_shares
                    clipped_shares = shares > shares_bounds[1]
                    shares[clipped_shares] = shares_bounds[1]

            # define the linear contraction
            if self.H == 0:
                def contraction(x: Array) -> ContractionResults:
                    """Compute the next linear delta and optionally its Jacobian."""
                    probabilities = compute_probabilities(x)[0]
                    shares = probabilities @ self.agents.weights
                    clip_shares(shares)
                    x = x + log_shares - np.log(shares)
                    if not iteration._compute_jacobian:
                        return x, None, None
                    weighted_probabilities = self.agents.weights * probabilities.T
                    jacobian = (probabilities @ weighted_probabilities) / shares
                    return x, None, jacobian
            else:
                dampener = 1 - self.rho
                rho_membership = self.rho * self.get_membership_matrix()

                def contraction(x: Array) -> ContractionResults:
                    """Compute the next linear delta and optionally its Jacobian under nesting."""
                    probabilities, conditionals = compute_probabilities(x)
                    shares = probabilities @ self.agents.weights
                    clip_shares(shares)
                    x = x + (log_shares - np.log(shares)) * dampener
                    if not iteration._compute_jacobian:
                        return x, None, None
                    weighted_probabilities = self.agents.weights * probabilities.T
                    probabilities_part = dampener * (probabilities @ weighted_probabilities)
                    conditionals_part = rho_membership * (conditionals @ weighted_probabilities)
                    jacobian = (probabilities_part + conditionals_part) / shares
                    return x, None, jacobian

            # solve the linear fixed point problem
            delta, stats = iteration._iterate(initial_delta, contraction)
            return delta, clipped_shares, stats, errors

        # solve for delta with a nonlinear fixed point
        assert 'nonlinear' in fp_type and self.epsilon_scale == 1
        if 'safe' in fp_type:
            utility_reduction = np.clip(self.mu.max(axis=0, keepdims=True), 0, None)
            exp_mu = np.exp(self.mu - utility_reduction)
            compute_probabilities = functools.partial(
                self.compute_probabilities, mu=exp_mu, utility_reduction=utility_reduction, linear=False
            )
        else:
            exp_mu = np.exp(self.mu)
            compute_probabilities = functools.partial(self.compute_probabilities, mu=exp_mu, linear=False)

        # define the nonlinear contraction
        if self.H == 0:
            def contraction(x: Array) -> ContractionResults:
                """Compute the next exponentiated delta and optionally its Jacobian."""
                probability_ratios = compute_probabilities(x, numerator=exp_mu)[0]
                share_ratios = probability_ratios @ self.agents.weights
                x0, x = x, self.products.shares / share_ratios
                if not iteration._compute_jacobian:
                    return x, None, None
                shares = x0 * share_ratios
                probabilities = x0 * probability_ratios
                weighted_probabilities = self.agents.weights * probabilities.T
                jacobian = x / x0.T * (probabilities @ weighted_probabilities) / shares
                return x, None, jacobian
        else:
            dampener = 1 - self.rho
            rho_membership = self.rho * self.get_membership_matrix()

            def contraction(x: Array) -> ContractionResults:
                """Compute the next exponentiated delta and optionally its Jacobian under nesting."""
                probabilities, conditionals = compute_probabilities(x)
                shares = probabilities @ self.agents.weights
                x0, x = x, x * (self.products.shares / shares) ** dampener
                if not iteration._compute_jacobian:
                    return x, None, None
                weighted_probabilities = self.agents.weights * probabilities.T
                probabilities_part = dampener * (probabilities @ weighted_probabilities)
                conditionals_part = rho_membership * (conditionals @ weighted_probabilities)
                jacobian = x / x0.T * (probabilities_part + conditionals_part) / shares
                return x, None, jacobian

        # solve the nonlinear fixed point problem
        exp_delta, stats = iteration._iterate(np.exp(initial_delta), contraction)
        delta = np.log(exp_delta)
        return delta, clipped_shares, stats, errors

    def compute_capital_lamda_gamma(
            self, probability_utility_derivatives: Array, probabilities: Array, conditionals: Optional[Array]) -> (
            Tuple[Array, Array]):
        """Compute the diagonal of the capital lambda matrix and the dense capital gamma matrix used to decompose the
        Jacobian of market shares with respect to a variable.
        """

        # compute capital lambda
        capital_lamda_diagonal = probability_utility_derivatives @ self.agents.weights
        if self.H > 0:
            capital_lamda_diagonal /= 1 - self.rho

        # compute capital gamma
        weighted_derivatives = self.agents.weights * probability_utility_derivatives.T
        capital_gamma = probabilities @ weighted_derivatives
        if self.H > 0:
            assert conditionals is not None
            weighted_membership = self.rho / (1 - self.rho) * self.get_membership_matrix()
            capital_gamma += weighted_membership * (conditionals @ weighted_derivatives)

        return capital_lamda_diagonal.flatten(), capital_gamma

    def compute_eta(
            self, ownership: Optional[Array] = None, probabilities: Optional[Array] = None,
            conditionals: Optional[Array] = None, keep_capital_delta_inverse: bool = False) -> (
            Tuple[Array, Optional[Array], List[Error]]):
        """Compute the markup term in the BLP-markup equation. By default, get an unchanged ownership matrix, use
        unchanged probabilities, and do not return the inverse of the capital delta matrix (used for computing the
        Jacobian of eta with respect to theta).
        """
        errors: List[Error] = []
        if ownership is None:
            ownership = self.get_ownership_matrix()
        if probabilities is None:
            probabilities, conditionals = self.compute_probabilities()

        utility_derivatives = self.compute_utility_derivatives('prices')
        probability_utility_derivatives = probabilities * utility_derivatives
        capital_lamda_diagonal, capital_delta = self.compute_capital_lamda_gamma(
            probability_utility_derivatives, probabilities, conditionals
        )
        np.einsum('jj->j', capital_delta)[...] -= capital_lamda_diagonal
        capital_delta *= ownership

        # use solve if not keeping the inverse of capital delta
        if not keep_capital_delta_inverse:
            capital_delta_inverse = None
            eta, replacement = approximately_solve(capital_delta, self.products.shares)
            if replacement:
                errors.append(exceptions.IntraFirmJacobianInversionError(capital_delta, replacement))
        else:
            capital_delta_inverse, replacement = approximately_invert(capital_delta)
            if replacement:
                errors.append(exceptions.IntraFirmJacobianInversionError(capital_delta, replacement))
            eta = capital_delta_inverse @ self.products.shares

        return eta, capital_delta_inverse, errors

    def compute_equilibrium_prices(
            self, costs: Array, iteration: Iteration, constant_costs: bool, prices: Optional[Array] = None,
            ownership_matrix: Optional[Array] = None) -> Tuple[Array, SolverStats]:
        """Compute equilibrium prices by iterating over the zeta-markup equation. By default, use unchanged firm IDs
        and use unchanged prices as initial values.
        """
        if ownership_matrix is None:
            ownership_matrix = self.get_ownership_matrix()
        if prices is None:
            prices = self.products.prices

        # costs are always constant if they do not depend on shares
        constant_costs = constant_costs or not any('shares' in f.names for f in self._X3_formulations)

        # derivatives of utilities with respect to prices change during iteration only if they depend on prices
        formulations = self._X1_formulations + self._X2_formulations
        get_derivatives = lambda p: self.compute_utility_derivatives('prices', p)
        get_second_derivatives = lambda p: self.compute_utility_derivatives('prices', p, order=2)
        if not any(s.name == 'prices' for f in formulations for s in f.differentiate('prices').free_symbols):
            derivatives = self.compute_utility_derivatives('prices')
            get_derivatives = lambda _: derivatives
        if not any(s.name == 'prices' for f in formulations for s in f.differentiate('prices', order=2).free_symbols):
            second_derivatives = self.compute_utility_derivatives('prices', order=2)
            get_second_derivatives = lambda _: second_derivatives

        def contraction(x: Array) -> ContractionResults:
            """Compute the next equilibrium prices."""

            # update probabilities and shares
            delta = self.update_delta_with_variable('prices', x)
            mu = self.update_mu_with_variable('prices', x)
            probabilities, conditionals = self.compute_probabilities(delta, mu)
            shares = probabilities @ self.agents.weights

            # optionally update costs
            updated_costs = costs
            if not constant_costs:
                updated_costs = self.update_costs_with_variable(costs, 'shares', shares)

            # compute zeta
            utility_derivatives = get_derivatives(x)
            probability_utility_derivatives = probabilities * utility_derivatives
            capital_lamda_diagonal, capital_gamma = self.compute_capital_lamda_gamma(
                probability_utility_derivatives, probabilities, conditionals
            )
            capital_lamda_inv = np.diag(1 / capital_lamda_diagonal)
            capital_gamma_tilde = ownership_matrix * capital_gamma
            margin = x - updated_costs
            capital_gamma_tilde_margin = capital_gamma_tilde.T @ margin
            zeta = capital_lamda_inv @ capital_gamma_tilde_margin - capital_lamda_inv @ shares

            # weight by the diagonal of capital lambda so that termination is based on profit gradients
            updated_x = updated_costs + zeta
            weights = np.abs(capital_lamda_diagonal)

            # update prices
            if not iteration._compute_jacobian:
                return updated_x, weights, None

            # compute Jacobians of shares, costs, margins with respect to prices
            shares_jacobian = np.diag(capital_lamda_diagonal) - capital_gamma
            costs_jacobian = 0
            margin_jacobian = np.eye(self.J)
            if not constant_costs:
                costs_derivatives = self.compute_costs_derivatives(updated_costs, 'shares', shares)
                costs_jacobian = costs_derivatives * shares_jacobian
                margin_jacobian -= costs_jacobian

            # compute the Jacobian of zeta with respect to prices
            utility_second_derivatives = get_second_derivatives(x)
            capital_lamda_tensor, capital_gamma_tensor = self.compute_capital_lamda_gamma_by_variable_tensor(
                utility_derivatives, utility_second_derivatives, probabilities, conditionals
            )
            capital_lamda_inv_tensor = -capital_lamda_inv @ np.moveaxis(capital_lamda_tensor, 2, 0) @ capital_lamda_inv
            capital_gamma_tilde_tensor = ownership_matrix * np.moveaxis(capital_gamma_tensor, 2, 0)
            capital_gamma_tilde_margin_tensor = (
                capital_gamma_tilde_tensor.swapaxes(1, 2) @ margin +
                capital_gamma_tilde.T @ margin_jacobian.T[..., None]
            )
            zeta_jacobian = (
                capital_lamda_inv_tensor @ capital_gamma_tilde_margin - capital_lamda_inv_tensor @ shares +
                capital_lamda_inv @ capital_gamma_tilde_margin_tensor - capital_lamda_inv @ shares_jacobian.T[..., None]
            )

            # compute the Jacobian of updated prices
            updated_x_jacobian = np.squeeze(zeta_jacobian, axis=2).T
            if not constant_costs:
                updated_x_jacobian += costs_jacobian

            return updated_x, weights, updated_x_jacobian

        # solve the fixed point problem
        prices, stats = iteration._iterate(prices, contraction)
        return prices, stats

    def compute_shares(self, prices: Optional[Array] = None) -> Array:
        """Compute shares evaluated at specific prices. By default, use unchanged prices."""
        if prices is None:
            prices = self.products.prices

        delta = self.update_delta_with_variable('prices', prices)
        mu = self.update_mu_with_variable('prices', prices)
        shares = self.compute_probabilities(delta, mu)[0] @ self.agents.weights
        return shares

    def compute_utility_derivatives_by_parameter_tangent(
            self, parameter: Parameter, X1_derivatives: Array, X2_derivatives: Array) -> Array:
        """Compute the tangent with respect to a parameter of derivatives of utility with respect to a variable."""
        tangent = np.zeros((self.J, self.I), options.dtype)
        if isinstance(parameter, BetaParameter):
            tangent += X1_derivatives[:, [parameter.location[0]]]
        elif isinstance(parameter, NonlinearCoefficient):
            v = parameter.get_agent_characteristic(self)
            if parameter.get_rc_type(self) == 'log':
                v = v * self.compute_single_random_coefficient(parameter.location[0]).T
            tangent += X2_derivatives[:, [parameter.location[0]]] * v.T
        else:
            assert isinstance(parameter, (GammaParameter, RhoParameter))

        if self.epsilon_scale != 1:
            tangent /= self.epsilon_scale

        return tangent

    def compute_probabilities_by_parameter_tangent_mapping(
            self, probabilities: Array, conditionals: Optional[Array], delta: Optional[Array] = None,
            keep_conditionals: bool = True) -> (
            Tuple[Dict[int, Array], Dict[int, Optional[Array]]]):
        """Computing a mapping from parameter index to tangent of probabilities with respect to a parameter. By default,
        use unchanged delta. By default, also compute conditionals derivatives if computed.
        """
        probabilities_mapping: Dict[int, Array] = {}
        conditionals_mapping: Dict[int, Array] = {}
        for p, parameter in enumerate(self.parameters.unfixed):
            probabilities_mapping[p], conditionals_mapping[p] = self.compute_probabilities_by_parameter_tangent(
                parameter, probabilities, conditionals, delta
            )
            if not keep_conditionals:
                conditionals_mapping[p] = None

        return probabilities_mapping, conditionals_mapping

    def update_probabilities_by_parameter_tangent_mapping(
            self, probabilities_tangent_mapping: Dict[int, Array],
            conditionals_tangent_mapping: Dict[int, Optional[Array]], probabilities: Array,
            conditionals: Optional[Array], xi_jacobian: Array) -> None:
        """Update tangents of probabilities with respect to parameters to account for the contribution of xi."""
        probabilities_tensor = conditionals_tensor = None
        for p, parameter in enumerate(self.parameters.unfixed):
            probabilities_tangent = probabilities_tangent_mapping[p]
            conditionals_tangent = conditionals_tangent_mapping[p]

            # total derivatives are zero for beta parameters
            if isinstance(parameter, BetaParameter):
                probabilities_tangent[:] = 0
                if conditionals_tangent is not None:
                    conditionals_tangent[:] = 0
                continue

            # derivatives remain zero for gamma parameters
            if isinstance(parameter, GammaParameter):
                continue

            # otherwise, need to compute the derivatives of probabilities with respect to xi
            if probabilities_tensor is None:
                probabilities_tensor, conditionals_tensor = self.compute_probabilities_by_xi_tensor(
                    probabilities, conditionals,
                    compute_conditionals_tensor=any(t is not None for t in conditionals_tangent_mapping.values())
                )

            # add the contribution of xi
            probabilities_tangent += np.squeeze(
                np.moveaxis(probabilities_tensor, 0, 2) @ xi_jacobian[:, [p]], axis=2
            )
            if conditionals_tangent is not None:
                assert conditionals_tensor is not None
                conditionals_tangent += np.squeeze(
                    np.moveaxis(conditionals_tensor, 0, 2) @ xi_jacobian[:, [p]], axis=2
                )

    def compute_probabilities_by_parameter_tangent(
            self, parameter: Parameter, probabilities: Array, conditionals: Optional[Array],
            delta: Optional[Array] = None) -> Tuple[Array, Optional[Array]]:
        """Compute the tangent of probabilities with respect to a parameter. By default, use unchanged delta."""
        if delta is None:
            assert self.delta is not None
            delta = self.delta

        # without nesting, compute only the tangent of probabilities with respect to the parameter
        if self.H == 0:
            if isinstance(parameter, BetaParameter):
                x = parameter.get_product_characteristic(self)
                probabilities_tangent = probabilities * (x - x.T @ probabilities)
            elif isinstance(parameter, NonlinearCoefficient):
                x = parameter.get_product_characteristic(self)
                v = parameter.get_agent_characteristic(self)
                if parameter.get_rc_type(self) == 'log':
                    v = v * self.compute_single_random_coefficient(parameter.location[0]).T
                probabilities_tangent = probabilities * v.T * (x - x.T @ probabilities)
            else:
                assert isinstance(parameter, GammaParameter)
                probabilities_tangent = np.zeros_like(probabilities)

            if self.epsilon_scale != 1:
                probabilities_tangent /= self.epsilon_scale

            return probabilities_tangent, None

        # marginal probabilities are needed to compute tangents with nesting
        marginals = self.groups.sum(probabilities)

        # compute the tangent of conditional and marginal probabilities with respect to the parameter
        if isinstance(parameter, BetaParameter):
            x = parameter.get_product_characteristic(self)

            # compute the tangent of conditional probabilities with respect to the parameter
            A = conditionals * x
            A_sums = self.groups.sum(A)
            conditionals_tangent = conditionals * (x - self.groups.expand(A_sums)) / (1 - self.rho)

            # compute the tangent of marginal probabilities with respect to the parameter
            B = marginals * A_sums
            marginals_tangent = B - marginals * B.sum(axis=0, keepdims=True)

        elif isinstance(parameter, NonlinearCoefficient):
            x = parameter.get_product_characteristic(self)
            v = parameter.get_agent_characteristic(self)
            if parameter.get_rc_type(self) == 'log':
                v = v * self.compute_single_random_coefficient(parameter.location[0]).T

            # compute the tangent of conditional probabilities with respect to the parameter
            vx = v.T * x
            A = conditionals * vx
            A_sums = self.groups.sum(A)
            conditionals_tangent = conditionals * (vx - self.groups.expand(A_sums)) / (1 - self.rho)

            # compute the tangent of marginal probabilities with respect to the parameter
            B = marginals * A_sums
            marginals_tangent = B - marginals * B.sum(axis=0, keepdims=True)

        elif isinstance(parameter, RhoParameter):
            group_associations = parameter.get_group_associations(self)
            associations = self.groups.expand(group_associations)

            # utilities are needed to compute tangents with respect to rho
            utilities = (delta + self.mu) / (1 - self.rho)

            # compute the tangent of conditional probabilities with respect to the parameter
            A = conditionals * utilities / (1 - self.rho)
            A_sums = self.groups.sum(A)
            conditionals_tangent = associations * (A - conditionals * self.groups.expand(A_sums))

            # compute the tangent of marginal probabilities with respect to the parameter (re-scale for robustness)
            utility_reduction = np.clip(utilities.max(axis=0, keepdims=True), 0, None)
            with np.errstate(divide='ignore', invalid='ignore'):
                B = marginals * (
                    A_sums * (1 - self.group_rho) -
                    (np.log(self.groups.sum(np.exp(utilities - utility_reduction))) + utility_reduction)
                )
                marginals_tangent = group_associations * B - marginals * (group_associations.T @ B)
            marginals_tangent[~np.isfinite(marginals_tangent)] = 0

        else:
            assert isinstance(parameter, GammaParameter)
            conditionals_tangent = np.zeros_like(conditionals)
            marginals_tangent = np.zeros_like(marginals)

        # compute the tangent of probabilities with respect to the parameter
        probabilities_tangent = (
            conditionals_tangent * self.groups.expand(marginals) +
            conditionals * self.groups.expand(marginals_tangent)
        )
        return probabilities_tangent, conditionals_tangent

    def compute_probabilities_by_xi_tensor(
            self, probabilities: Array, conditionals: Optional[Array],
            compute_conditionals_tensor: bool = True) -> Tuple[Array, Optional[Array]]:
        """Use choice probabilities to compute their tensor derivatives (holding beta fixed) with respect to xi
        (equivalently, to delta), indexed with the first axis. By default, also compute the tensor derivatives of
        conditional probabilities with respect to xi when there is nesting.
        """
        probabilities_tensor = -probabilities[None] * probabilities[None].swapaxes(0, 1)
        np.einsum('jji->ji', probabilities_tensor)[...] += probabilities
        conditionals_tensor = None

        if self.epsilon_scale != 1:
            probabilities_tensor /= self.epsilon_scale

        if self.H > 0:
            assert conditionals is not None
            membership = self.get_membership_matrix()
            multiplied_probabilities = self.rho / (1 - self.rho) * probabilities
            probabilities_tensor -= membership[..., None] * (
                conditionals[None] * multiplied_probabilities[None].swapaxes(0, 1)
            )
            np.einsum('jji->ji', probabilities_tensor)[...] += multiplied_probabilities
            if compute_conditionals_tensor:
                multiplied_conditionals = 1 / (1 - self.rho) * conditionals
                conditionals_tensor = -membership[..., None] * (
                    conditionals[None] * multiplied_conditionals[None].swapaxes(0, 1)
                )
                np.einsum('jji->ji', conditionals_tensor)[...] += multiplied_conditionals

        return probabilities_tensor, conditionals_tensor

    def compute_shares_by_variable_jacobian(
            self, utility_derivatives: Array, probabilities: Optional[Array] = None,
            conditionals: Optional[Array] = None) -> Array:
        """Compute the Jacobian of market shares with respect to a variable. By default, compute unchanged choice
        probabilities.
        """
        if probabilities is None:
            probabilities, conditionals = self.compute_probabilities()
        probability_utility_derivatives = probabilities * utility_derivatives
        capital_lamda_diagonal, capital_gamma = self.compute_capital_lamda_gamma(
            probability_utility_derivatives, probabilities, conditionals
        )
        return np.diag(capital_lamda_diagonal) - capital_gamma

    def compute_capital_lamda_gamma_by_variable_tensor(
            self, utility_derivatives: Array, utility_second_derivatives: Array, probabilities: Array,
            conditionals: Array) -> Tuple[Array, Array]:
        """Compute the tensor derivative of the diagonal of the capital lambda matrix and the dense capital gamma matrix
        with respect to a variable, indexed with the last axis.
        """

        # pre-compute components that are common for each product
        probability_utility_derivatives = probabilities * utility_derivatives
        weighted_derivatives = self.agents.weights * probability_utility_derivatives.T

        membership = conditional_utility_derivatives = None
        if self.H > 0:
            membership = self.get_membership_matrix()
            conditional_utility_derivatives = conditionals * utility_derivatives / (1 - self.rho)

        # take derivatives product-by-product
        capital_lamda_tensor = np.zeros((self.J, self.J, self.J), options.dtype)
        capital_gamma_tensor = np.zeros((self.J, self.J, self.J), options.dtype)
        for j in range(self.J):
            # compute the derivatives of probabilities with respect to the product characteristic
            if self.H == 0:
                probability_derivatives = -probabilities * probability_utility_derivatives[j]
                probability_derivatives[j] += probability_utility_derivatives[j]
            else:
                assert membership is not None
                probability_derivatives = -(
                    probabilities * probability_utility_derivatives[j] +
                    self.rho / (1 - self.rho) * membership[:, [j]] * conditionals * probability_utility_derivatives[j]
                )
                probability_derivatives[j] += probability_utility_derivatives[j] / (1 - self.rho[j])

            # compute derivatives of their product with utility derivatives with respect to the product characteristic
            probability_utility_second_derivatives = probability_derivatives * utility_derivatives
            probability_utility_second_derivatives[j] += probabilities[j] * utility_second_derivatives[j]

            # compute derivatives of capital lambda with respect to the product characteristic
            diagonal = probability_utility_second_derivatives @ self.agents.weights
            if self.H > 0:
                diagonal /= 1 - self.rho

            capital_lamda_tensor[..., j] = np.diagflat(diagonal)

            # compute derivatives of capital gamma with respect to the product characteristic
            weighted_second_derivatives = self.agents.weights * probability_utility_second_derivatives.T
            capital_gamma_tensor[..., j] = (
                probability_derivatives @ weighted_derivatives +
                probabilities @ weighted_second_derivatives
            )
            if self.H > 0:
                assert conditionals is not None
                assert membership is not None and conditional_utility_derivatives is not None

                # compute the derivatives of conditional probabilities with respect to the product characteristic
                conditional_derivatives = -conditionals * membership[:, [j]] * conditional_utility_derivatives[j]
                conditional_derivatives[j] += conditional_utility_derivatives[j]

                capital_gamma_tensor[..., j] += self.rho / (1 - self.rho) * membership * (
                    conditional_derivatives @ weighted_derivatives +
                    conditionals @ weighted_second_derivatives
                )

        return capital_lamda_tensor, capital_gamma_tensor

    def compute_shares_by_variable_hessian(
            self, utility_derivatives: Array, utility_second_derivatives: Array, probabilities: Optional[Array] = None,
            conditionals: Optional[Array] = None) -> Array:
        """Compute the Hessian of market shares with respect to a variable. By default, compute unchanged choice
        probabilities.
        """
        if probabilities is None:
            probabilities, conditionals = self.compute_probabilities()
        capital_lamda_tensor, capital_gamma_tensor = self.compute_capital_lamda_gamma_by_variable_tensor(
            utility_derivatives, utility_second_derivatives, probabilities, conditionals
        )
        return capital_lamda_tensor - capital_gamma_tensor

    def compute_profit_jacobian(self, costs: Array, prices: Optional[Array] = None) -> Array:
        """Compute the Jacobian of profits with respect to prices. By default, use unchanged prices."""
        if prices is None:
            prices = self.products.prices
            probabilities, conditionals = self.compute_probabilities()
            shares = self.products.shares
            utility_derivatives = self.compute_utility_derivatives('prices')
        else:
            delta = self.update_delta_with_variable('prices', prices)
            mu = self.update_mu_with_variable('prices', prices)
            probabilities, conditionals = self.compute_probabilities(delta, mu)
            shares = probabilities @ self.agents.weights
            utility_derivatives = self.compute_utility_derivatives('prices', prices)

        # compute derivatives of shares with respect to prices
        shares_jacobian = self.compute_shares_by_variable_jacobian(utility_derivatives, probabilities, conditionals)

        # compute derivatives of profits with respect to prices
        return np.diagflat(shares) + (prices - costs) * shares_jacobian

    def compute_profit_hessian(self, costs: Array, prices: Optional[Array] = None) -> Array:
        """Compute the Hessian of profits with respect to prices. By default, use unchanged prices."""
        if prices is None:
            prices = self.products.prices
            probabilities, conditionals = self.compute_probabilities()
            utility_derivatives = self.compute_utility_derivatives('prices')
            utility_second_derivatives = self.compute_utility_derivatives('prices', order=2)
        else:
            delta = self.update_delta_with_variable('prices', prices)
            mu = self.update_mu_with_variable('prices', prices)
            probabilities, conditionals = self.compute_probabilities(delta, mu)
            utility_derivatives = self.compute_utility_derivatives('prices', prices)
            utility_second_derivatives = self.compute_utility_derivatives('prices', prices, order=2)

        # compute derivatives of shares with respect to prices
        shares_jacobian = self.compute_shares_by_variable_jacobian(utility_derivatives, probabilities, conditionals)
        shares_hessian = self.compute_shares_by_variable_hessian(
            utility_derivatives, utility_second_derivatives, probabilities, conditionals
        )

        # compute derivatives of profits with respect to prices
        profit_hessian = (prices - costs)[:, :, None] * shares_hessian
        profit_hessian[np.arange(self.J), np.arange(self.J), :] += shares_jacobian
        profit_hessian[np.arange(self.J), :, np.arange(self.J)] += shares_jacobian
        return profit_hessian

    def compute_shares_by_xi_jacobian(self, probabilities: Array, conditionals: Optional[Array]) -> Array:
        """Compute the Jacobian (holding beta fixed) of shares with respect to xi (equivalently, to delta)."""
        diagonal_shares = np.diagflat(self.products.shares)
        weighted_probabilities = self.agents.weights * probabilities.T
        jacobian = diagonal_shares - probabilities @ weighted_probabilities

        if self.epsilon_scale != 1:
            jacobian /= self.epsilon_scale

        if self.H > 0:
            membership = self.get_membership_matrix()
            jacobian += self.rho / (1 - self.rho) * (
                diagonal_shares - membership * (conditionals @ weighted_probabilities)
            )

        return jacobian

    def compute_shares_by_theta_jacobian(self, probabilities_tangent_mapping: Dict[int, Array]) -> Array:
        """Compute the Jacobian of shares with respect to theta."""
        jacobian = np.zeros((self.J, self.parameters.P), options.dtype)
        for p, tangent in probabilities_tangent_mapping.items():
            jacobian[:, [p]] = tangent @ self.agents.weights
        return jacobian

    def compute_capital_lamda_gamma_by_parameter_tangent(
            self, parameter: Parameter, probability_utility_derivatives: Array,
            probability_utility_derivatives_tangent: Array, probabilities: Array, probabilities_tangent: Array,
            conditionals: Optional[Array], conditionals_tangent: Optional[Array]) -> Tuple[Array, Array]:
        """Compute the tangent of the diagonal of the capital lambda matrix and the dense capital gamma matrix with
        respect to a parameter.
        """

        # compute the capital lambda tangent
        capital_lamda_diagonal_tangent = probability_utility_derivatives_tangent @ self.agents.weights
        if self.H > 0:
            capital_lamda_diagonal_tangent /= 1 - self.rho
            if isinstance(parameter, RhoParameter):
                associations = self.groups.expand(parameter.get_group_associations(self))
                capital_lamda_diagonal_tangent += associations / (1 - self.rho)**2 * (
                    probability_utility_derivatives @ self.agents.weights
                )

        # compute the capital gamma tangent
        weighted_derivatives = self.agents.weights * probability_utility_derivatives.T
        weighted_derivatives_tangent = self.agents.weights * probability_utility_derivatives_tangent.T
        capital_gamma_tangent = (
            probabilities_tangent @ weighted_derivatives +
            probabilities @ weighted_derivatives_tangent
        )
        if self.H > 0:
            assert conditionals is not None and conditionals_tangent is not None
            membership = self.get_membership_matrix()
            capital_gamma_tangent += membership * self.rho / (1 - self.rho) * (
                conditionals_tangent @ weighted_derivatives +
                conditionals @ weighted_derivatives_tangent
            )
            if isinstance(parameter, RhoParameter):
                associations = self.groups.expand(parameter.get_group_associations(self))
                capital_gamma_tangent += associations * membership / (1 - self.rho)**2 * (
                    conditionals @ weighted_derivatives
                )

        return capital_lamda_diagonal_tangent.flatten(), capital_gamma_tangent

    def compute_eta_by_theta_jacobian(
            self, eta: Array, capital_delta_inverse: Array, probabilities: Array, conditionals: Optional[Array],
            probabilities_tangent_mapping: Dict[int, Array],
            conditionals_tangent_mapping: Dict[int, Optional[Array]]) -> Array:
        """Compute the Jacobian of the markup term in the BLP-markup equation with respect to theta."""
        ownership = self.get_ownership_matrix()

        # compute derivatives of aggregate inclusive values with respect to prices
        utility_derivatives = self.compute_utility_derivatives('prices')
        probability_utility_derivatives = probabilities * utility_derivatives

        # compute derivatives of X1 and X2 with respect to prices
        X1_derivatives = self.compute_X1_derivatives('prices')
        X2_derivatives = self.compute_X2_derivatives('prices')

        # add each parameter's additional contraction
        eta_jacobian = np.zeros((self.J, self.parameters.P), options.dtype)
        for p, parameter in enumerate(self.parameters.unfixed):
            # compute the tangent with respect to the parameter of derivatives of aggregate inclusive values
            utility_derivatives_tangent = self.compute_utility_derivatives_by_parameter_tangent(
                parameter, X1_derivatives, X2_derivatives
            )
            probability_utility_derivatives_tangent = (
                probabilities_tangent_mapping[p] * utility_derivatives +
                probabilities * utility_derivatives_tangent
            )

            # compute the tangent of capital delta with respect to the parameter
            capital_lamda_diagonal_tangent, capital_delta_tangent = (
                self.compute_capital_lamda_gamma_by_parameter_tangent(
                    parameter, probability_utility_derivatives, probability_utility_derivatives_tangent, probabilities,
                    probabilities_tangent_mapping[p], conditionals, conditionals_tangent_mapping[p]
                )
            )
            np.einsum('jj->j', capital_delta_tangent)[...] -= capital_lamda_diagonal_tangent
            capital_delta_tangent *= ownership

            # subtract this parameter's contribution
            eta_jacobian[:, [p]] = -(capital_delta_inverse @ capital_delta_tangent @ eta)

        return eta_jacobian

    def compute_xi_by_theta_jacobian(
            self, probabilities: Array, conditionals: Optional[Array],
            probabilities_tangent_mapping: Dict[int, Array]) -> Tuple[Array, List[Error]]:
        """Use the Implicit Function Theorem to compute the Jacobian (holding beta fixed) of xi (equivalently, of delta)
        with respect to theta.
        """
        errors: List[Error] = []
        shares_by_xi_jacobian = self.compute_shares_by_xi_jacobian(probabilities, conditionals)
        shares_by_theta_jacobian = self.compute_shares_by_theta_jacobian(probabilities_tangent_mapping)
        xi_by_theta_jacobian, replacement = approximately_solve(shares_by_xi_jacobian, -shares_by_theta_jacobian)
        if replacement:
            errors.append(exceptions.SharesByXiJacobianInversionError(shares_by_xi_jacobian, replacement))
        return xi_by_theta_jacobian, errors

    def compute_omega_by_theta_jacobian(
            self, tilde_costs: Array, eta: Array, capital_delta_inverse: Array, probabilities: Array,
            conditionals: Optional[Array], probabilities_tangent_mapping: Dict[int, Array],
            conditionals_tangent_mapping: Dict[int, Optional[Array]]) -> Array:
        """Compute the Jacobian (holding gamma fixed) of omega (equivalently, of transformed marginal costs) with
        respect to theta.
        """

        # compute the Jacobian of the markup term in the BLP-markup equation with respect to theta
        eta_jacobian = self.compute_eta_by_theta_jacobian(
            eta, capital_delta_inverse, probabilities, conditionals, probabilities_tangent_mapping,
            conditionals_tangent_mapping,
        )

        # transform the Jacobian according to the marginal cost specification
        if self.costs_type == 'linear':
            omega_jacobian = -eta_jacobian
        else:
            assert self.costs_type == 'log'
            omega_jacobian = -eta_jacobian / np.exp(tilde_costs)

        # incorporate the contributions from any parameters in gamma that haven't been concentrated out
        for p, parameter in enumerate(self.parameters.unfixed):
            if isinstance(parameter, GammaParameter):
                omega_jacobian[:, [p]] = -parameter.get_product_characteristic(self)

        return omega_jacobian

    def compute_micro_contributions(
            self, moments: Moments, delta: Optional[Array] = None, probabilities: Optional[Array] = None,
            probabilities_tangent_mapping: Optional[Dict[int, Array]] = None, compute_jacobians: bool = False,
            compute_covariances: bool = False) -> Tuple[Array, Array, Array, Array, Array]:
        """Compute contributions to micro moment values, Jacobians, and covariances. By default, use the mean utilities
        with which this market was initialized and do not compute Jacobian and covariance contributions.
        """
        if delta is None:
            assert self.delta is not None
            delta = self.delta

        # pre-compute probabilities if not already computed
        if probabilities is None:
            probabilities, _ = self.compute_probabilities(delta)

        # pre-compute and validate micro dataset weights, multiplying these with probabilities and using these to
        #   compute micro value denominators
        weights_mapping: Dict[MicroDataset, Array] = {}
        denominator_mapping: Dict[MicroDataset, Array] = {}
        outside_probabilities = None
        eliminated_probabilities = outside_eliminated_probabilities = eliminated_outside_probabilities = None
        weights_tangent_mapping: Dict[Tuple[MicroDataset, int], Array] = {}
        denominator_tangent_mapping: Dict[Tuple[MicroDataset, int], Array] = {}
        outside_probabilities_tangent_mapping: Dict[int, Array] = {}
        eliminated_probabilities_tangent_mapping: Dict[int, Array] = {}
        outside_eliminated_probabilities_tangent_mapping: Dict[int, Array] = {}
        eliminated_outside_probabilities_tangent_mapping: Dict[int, Array] = {}
        for moment in moments.micro_moments:
            dataset = moment.dataset
            if dataset in weights_mapping or (dataset.market_ids is not None and self.t not in dataset.market_ids):
                continue

            # compute and validate weights
            try:
                weights = np.asarray(dataset.compute_weights(self.t, self.products, self.agents), options.dtype)
            except Exception as exception:
                message = f"Failed to compute weights for micro dataset '{dataset}' because of the above exception."
                raise RuntimeError(message) from exception
            shapes = [
                (self.I, self.J), (self.I, 1 + self.J), (self.I, self.J, self.J), (self.I, 1 + self.J, self.J),
                (self.I, self.J, 1 + self.J), (self.I, 1 + self.J, 1 + self.J)
            ]
            if weights.shape not in shapes:
                raise ValueError(
                    f"In market {self.t}, micro dataset '{moment.dataset}' returned an array of shape "
                    f"{weights.shape}, which is not one of the acceptable shapes, {shapes}."
                )

            # check whether the weights admit a denominator that does not depend on theta
            constant_denominator = (weights == weights[0]).all()
            if len(weights.shape) == 3 and weights.shape[2] == 1 + self.J:
                constant_denominator &= (weights == weights[..., [0]]).all()

            # if so, we can already compute the contribution to the denominator for micro values based on this dataset
            if constant_denominator:
                shares = self.products.shares.flatten()
                if weights.shape[1] == 1 + self.J:
                    shares = np.r_[1 - shares.sum(), shares]
                if len(weights.shape) == 2:
                    denominator_mapping[dataset] = shares @ weights[0]
                else:
                    assert len(weights.shape) == 3
                    denominator_mapping[dataset] = shares @ weights[0, :, 0]

                if compute_jacobians:
                    for p in range(self.parameters.P):
                        denominator_tangent_mapping[(dataset, p)] = 0

            # pre-compute outside probabilities
            if outside_probabilities is None and weights.shape[1] == 1 + self.J:
                outside_probabilities = 1 - probabilities.sum(axis=0)

                if compute_jacobians:
                    assert probabilities_tangent_mapping is not None
                    for p, probabilities_tangent in probabilities_tangent_mapping.items():
                        outside_probabilities_tangent_mapping[p] = -probabilities_tangent.sum(axis=0)

            # pre-compute second choice probabilities
            if len(weights.shape) == 3 and eliminated_probabilities is None:
                eliminated_probabilities_list = []
                for j in range(self.J):
                    eliminated_probabilities_list.append(self.compute_eliminated_probabilities(
                        probabilities, delta, eliminate_product=j
                    ))

                eliminated_probabilities = np.stack(eliminated_probabilities_list)

                if compute_jacobians:
                    with np.errstate(all='ignore'):
                        eliminated_ratios = eliminated_probabilities / probabilities[None]
                        eliminated_ratios[~np.isfinite(eliminated_ratios)] = 0

                    assert probabilities_tangent_mapping is not None
                    for p, probabilities_tangent in probabilities_tangent_mapping.items():
                        eliminated_probabilities_tangent_mapping[p] = eliminated_ratios * (
                            probabilities_tangent[None] + eliminated_probabilities * probabilities_tangent[:, None]
                        )

            # pre-compute probabilities after the outside option has been removed
            if len(weights.shape) == 3 and outside_eliminated_probabilities is None and weights.shape[1] == 1 + self.J:
                outside_eliminated_probabilities = self.compute_eliminated_probabilities(
                    probabilities, delta, eliminate_outside=True
                )

                if compute_jacobians:
                    with np.errstate(all='ignore'):
                        outside_eliminated_ratio = outside_eliminated_probabilities / probabilities
                        outside_eliminated_ratio[~np.isfinite(outside_eliminated_ratio)] = 0

                    assert probabilities_tangent_mapping is not None
                    for p, probabilities_tangent in probabilities_tangent_mapping.items():
                        outside_eliminated_probabilities_tangent_mapping[p] = outside_eliminated_ratio * (
                            probabilities_tangent -
                            outside_eliminated_probabilities * probabilities_tangent.sum(axis=0, keepdims=True)
                        )

            # pre-compute outside second choice probabilities
            if len(weights.shape) == 3 and eliminated_outside_probabilities is None and weights.shape[2] == 1 + self.J:
                assert eliminated_probabilities is not None
                eliminated_outside_probabilities = 1 - eliminated_probabilities.sum(axis=1)

                if compute_jacobians:
                    for p, eliminated_probabilities_tangent in eliminated_probabilities_tangent_mapping.items():
                        eliminated_outside_probabilities_tangent_mapping[p] = -(
                            eliminated_probabilities_tangent.sum(axis=1)
                        )

            # both weights and their Jacobians will be multiplied by agent integration weights
            if len(weights.shape) == 2:
                weights *= self.agents.weights
            else:
                assert len(weights.shape) == 3
                weights *= self.agents.weights[..., None]

            # multiply weights by choice probabilities
            dataset_weights = weights.copy()
            if len(weights.shape) == 2:
                dataset_weights[:, -self.J:] *= probabilities.T
                if weights.shape[1] == 1 + self.J:
                    assert outside_probabilities is not None
                    dataset_weights[:, 0] *= outside_probabilities
            else:
                assert len(weights.shape) == 3
                assert eliminated_probabilities is not None
                dataset_weights[:, -self.J:] *= probabilities.T[..., None]
                dataset_weights[:, -self.J:, -self.J:] *= np.moveaxis(eliminated_probabilities, (0, 1, 2), (1, 2, 0))
                if weights.shape[1] == 1 + self.J:
                    assert outside_probabilities is not None and outside_eliminated_probabilities is not None
                    dataset_weights[:, 0] *= outside_probabilities[:, None]
                    dataset_weights[:, 0, -self.J:] *= outside_eliminated_probabilities.T
                if weights.shape[2] == 1 + self.J:
                    assert eliminated_outside_probabilities is not None
                    dataset_weights[:, -self.J:, 0] *= eliminated_outside_probabilities.T

            weights_mapping[dataset] = dataset_weights

            if compute_jacobians:
                assert probabilities_tangent_mapping is not None

                for p in range(self.parameters.P):
                    weights_tangent = weights.copy()

                    if len(weights.shape) == 2:
                        weights_tangent[:, -self.J:] *= probabilities_tangent_mapping[p].T
                        if weights.shape[1] == 1 + self.J:
                            weights_tangent[:, 0] *= outside_probabilities_tangent_mapping[p]
                    else:
                        assert len(weights.shape) == 3
                        assert eliminated_probabilities is not None
                        product1 = np.ones_like(weights_tangent)
                        product2 = np.ones_like(weights_tangent)
                        product1[:, -self.J:] = probabilities_tangent_mapping[p].T[..., None]
                        product2[:, -self.J:] = probabilities.T[..., None]
                        product1[:, -self.J:, -self.J:] *= np.moveaxis(eliminated_probabilities, (0, 1, 2), (1, 2, 0))
                        product2[:, -self.J:, -self.J:] *= np.moveaxis(
                            eliminated_probabilities_tangent_mapping[p], (0, 1, 2), (1, 2, 0)
                        )
                        if weights.shape[1] == 1 + self.J:
                            assert outside_probabilities is not None and outside_eliminated_probabilities is not None
                            product1[:, 0] = outside_probabilities_tangent_mapping[p][:, None]
                            product2[:, 0] = outside_probabilities[:, None]
                            product1[:, 0, -self.J:] *= outside_eliminated_probabilities.T
                            product2[:, 0, -self.J:] *= outside_eliminated_probabilities_tangent_mapping[p].T
                        if weights.shape[2] == 1 + self.J:
                            assert eliminated_outside_probabilities is not None
                            product1[:, -self.J:, 0] *= eliminated_outside_probabilities.T
                            product2[:, -self.J:, 0] *= eliminated_outside_probabilities_tangent_mapping[p].T

                        weights_tangent *= product1 + product2

                    weights_tangent_mapping[(dataset, p)] = weights_tangent

            # compute the contribution to the denominator for micro values based on this dataset if not already computed
            if not constant_denominator:
                denominator_mapping[dataset] = dataset_weights.sum()

                if compute_jacobians:
                    for p in range(self.parameters.P):
                        denominator_tangent_mapping[(dataset, p)] = weights_tangent_mapping[(dataset, p)].sum()

        # compute this market's contribution to micro moment values' and covariances' numerators and denominators
        values_mapping: Dict[int, Array] = {}
        micro_numerator = np.zeros((moments.MM, 1), options.dtype)
        micro_denominator = np.zeros((moments.MM, 1), options.dtype)
        micro_numerator_jacobian = np.full(
            (moments.MM, self.parameters.P), 0 if compute_jacobians else np.nan, options.dtype
        )
        micro_denominator_jacobian = np.full(
            (moments.MM, self.parameters.P), 0 if compute_jacobians else np.nan, options.dtype
        )
        micro_covariances_numerator = np.full(
            (moments.MM, moments.MM), 0 if compute_covariances else np.nan, options.dtype
        )
        for m, moment in enumerate(moments.micro_moments):
            dataset = moment.dataset
            if dataset not in weights_mapping:
                continue

            # compute and validate moment values
            weights = weights_mapping[dataset]
            try:
                values = np.asarray(moment.compute_values(self.t, self.products, self.agents), options.dtype)
            except Exception as exception:
                message = f"Failed to compute values for micro moment '{moment}' because of the above exception."
                raise RuntimeError(message) from exception
            if values.shape != weights.shape:
                raise ValueError(
                    f"In market {self.t}, micro moment '{moment}' returned an array of shape {values.shape} from "
                    f"compute_values, which is not {weights.shape}, the shape of the array returned by its "
                    f"dataset's compute_weights."
                )

            # compute the contribution to the numerator and denominator
            weighted_values = weights * values
            micro_numerator[m] = weighted_values.sum()
            micro_denominator[m] = denominator_mapping[dataset]
            if compute_jacobians:
                for p in range(self.parameters.P):
                    micro_numerator_jacobian[m, p] = (weights_tangent_mapping[(dataset, p)] * values).sum()
                    micro_denominator_jacobian[m, p] = denominator_tangent_mapping[(dataset, p)]

            # compute the contribution to the covariance numerator (cache values so they don't need to be re-computed)
            if compute_covariances:
                values_mapping[m] = values
                for m2, moment2 in enumerate(moments.micro_moments):
                    if m2 <= m and moment2.dataset == moment.dataset:
                        micro_covariances_numerator[m2, m] = (weighted_values * values_mapping[m2]).sum()

        return (
            micro_numerator, micro_denominator, micro_numerator_jacobian, micro_denominator_jacobian,
            micro_covariances_numerator
        )
