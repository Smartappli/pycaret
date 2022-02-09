import logging
import time
import warnings
from collections import defaultdict
from functools import partial
from typing import Any, Dict, Generator, Optional, Tuple, Union

import numpy as np
import pandas as pd
from joblib import Parallel, delayed  # type: ignore
from scipy.stats import rankdata
from sklearn.base import clone  # type: ignore
from sklearn.metrics._scorer import _PredictScorer  # type: ignore
from sklearn.model_selection import (  # type: ignore
    ParameterGrid,
    ParameterSampler,
    check_cv,
)
from sklearn.model_selection._search import _check_param_grid  # type: ignore
from sklearn.model_selection._validation import _aggregate_score_dicts  # type: ignore
from sktime.utils.validation.forecasting import check_y_X  # type: ignore

from pycaret.internal.utils import get_function_params
from pycaret.utils import _get_metrics_dict
from pycaret.utils.time_series.forecasting import (
    get_predictions_with_intervals,
    update_additional_scorer_kwargs,
)


def get_folds(cv, y) -> Generator[Tuple[pd.Series, pd.Series], None, None]:
    """
    Returns the train and test indices for the time series data
    """
    # https://github.com/alan-turing-institute/sktime/blob/main/examples/window_splitters.ipynb
    for train_indices, test_indices in cv.split(y):
        # print(f"Train Indices: {train_indices}, Test Indices: {test_indices}")
        yield train_indices, test_indices


def _fit_and_score(
    forecaster,
    y: pd.Series,
    X: Optional[Union[pd.Series, pd.DataFrame]],
    scoring: Dict[str, Union[str, _PredictScorer]],
    train: np.ndarray,
    test: np.ndarray,
    parameters,
    fit_params,
    return_train_score,
    error_score=0,
    **additional_scorer_kwargs,
):
    """Fits the forecaster on a single train split and scores on the test split
    Similar to _fit_and_score from `sklearn` [1] (and to some extent `sktime` [2]).
    Difference is that [1] operates on a single fold only, whereas [2] operates on all cv folds.
    Ref:
    [1] https://github.com/scikit-learn/scikit-learn/blob/0.24.1/sklearn/model_selection/_validation.py#L449
    [2] https://github.com/alan-turing-institute/sktime/blob/v0.5.3/sktime/forecasting/model_selection/_tune.py#L95

    Parameters
    ----------
    forecaster : [type]
        Time Series Forecaster that is compatible with sktime
    y : pd.Series
        The variable of interest for forecasting
    X : Optional[Union[pd.Series, pd.DataFrame]]
        Exogenous Variables
    scoring : Dict[str, Union[str, _PredictScorer]]
        Scoring Dictionary. Values can be valid strings that can be converted to
        callable metrics or the callable metrics directly
    train : np.ndarray
        Indices of training samples (integer based indexing).
    test : np.ndarray
        Indices of test samples (integer based indexing).
    parameters : [type]
        Parameter to set for the forecaster
    fit_params : [type]
        Fit parameters to be used when training
    return_train_score : [type]
        Should the training scores be returned. Unused for now.
    error_score : int, optional
        Unused for now, by default 0
    **additional_scorer_kwargs: Dict[str, Any]
            Additional scorer kwargs such as {`sp`:12} required by metrics like MASE

    Raises
    ------
    ValueError
        When test indices do not match predicted indices. This is only for
        for internal checks and should not be raised when used by external users
    """
    if parameters is not None:
        forecaster.set_params(**parameters)

    y_train, y_test = y[train], y[test]
    X_train = None if X is None else X.iloc[train]
    X_test = None if X is None else X.iloc[test]

    #### Fit the forecaster ----
    start = time.time()
    try:
        forecaster.fit(y_train, X_train, **fit_params)
    except Exception as error:
        logging.error(f"Fit failed on {forecaster}")
        logging.error(error)

        if error_score == "raise":
            raise

    fit_time = time.time() - start

    #### Determine Cutoff ----
    # NOTE: Cutoff is available irrespective of whether fit passed or failed
    cutoff = forecaster.cutoff

    #### Score the model ----
    lower = pd.Series([])
    upper = pd.Series([])
    if forecaster.is_fitted:
        # TODO: Add alpha here???
        y_pred, lower, upper = get_predictions_with_intervals(
            forecaster=forecaster, X=X_test
        )

        if (y_test.index.values != y_pred.index.values).any():
            print(
                f"\t y_train: {y_train.index.values},"
                f"\n\t y_test: {y_test.index.values}"
            )
            print(f"\t y_pred: {y_pred.index.values}")
            raise ValueError(
                "y_test indices do not match y_pred_indices or split/prediction "
                "length does not match forecast horizon."
            )

    start = time.time()
    fold_scores = {}
    scoring = _get_metrics_dict(scoring)

    # SP should be passed from outside in additional_scorer_kwargs already
    additional_scorer_kwargs = update_additional_scorer_kwargs(
        initial_kwargs=additional_scorer_kwargs,
        y_train=y_train,
        lower=lower,
        upper=upper,
    )
    for scorer_name, scorer in scoring.items():
        if forecaster.is_fitted:
            # get all kwargs in additional_scorer_kwargs
            # that correspond to parameters in function signature
            kwargs = {
                **{
                    k: v
                    for k, v in additional_scorer_kwargs.items()
                    if k in get_function_params(scorer._score_func)
                },
                **scorer._kwargs,
            }
            metric = scorer._score_func(y_true=y_test, y_pred=y_pred, **kwargs)
        else:
            metric = None
        fold_scores[scorer_name] = metric
    score_time = time.time() - start

    return fold_scores, fit_time, score_time, cutoff


def cross_validate(
    forecaster,
    y: pd.Series,
    X: Optional[Union[pd.Series, pd.DataFrame]],
    cv,
    scoring: Dict[str, Union[str, _PredictScorer]],
    fit_params,
    n_jobs,
    return_train_score,
    error_score=0,
    verbose: int = 0,
    **additional_scorer_kwargs,
) -> Dict[str, np.array]:
    """Performs Cross Validation on time series data

    Parallelization is based on `sklearn` cross_validate function [1]
    Ref:
    [1] https://github.com/scikit-learn/scikit-learn/blob/0.24.1/sklearn/model_selection/_validation.py#L246


    Parameters
    ----------
    forecaster : [type]
        Time Series Forecaster that is compatible with sktime
    y : pd.Series
        The variable of interest for forecasting
    X : Optional[Union[pd.Series, pd.DataFrame]]
        Exogenous Variables
    cv : [type]
        [description]
    scoring : Dict[str, Union[str, _PredictScorer]]
        Scoring Dictionary. Values can be valid strings that can be converted to
        callable metrics or the callable metrics directly
    fit_params : [type]
        Fit parameters to be used when training
    n_jobs : [type]
        Number of cores to use to parallelize. Refer to sklearn for details
    return_train_score : [type]
        Should the training scores be returned. Unused for now.
    error_score : int, optional
        Unused for now, by default 0
    verbose : int
        Sets the verbosity level. Unused for now
    additional_scorer_kwargs: Dict[str, Any]
        Additional scorer kwargs such as {`sp`:12} required by metrics like MASE

    Returns
    -------
    [type]
        [description]

    Raises
    ------
    Error
        If fit and score raises any exceptions
    """
    try:
        # # For Debug
        # n_jobs = 1
        scoring = _get_metrics_dict(scoring)
        parallel = Parallel(n_jobs=n_jobs)

        out = parallel(
            delayed(_fit_and_score)(
                forecaster=clone(forecaster),
                y=y,
                X=X,
                scoring=scoring,
                train=train,
                test=test,
                parameters=None,
                fit_params=fit_params,
                return_train_score=return_train_score,
                error_score=error_score,
                **additional_scorer_kwargs,
            )
            for train, test in get_folds(cv, y)
        )
    # raise key exceptions
    except Exception:
        raise

    # Similar to parts of _format_results in BaseGridSearch
    (test_scores_dict, fit_time, score_time, cutoffs) = zip(*out)
    test_scores = _aggregate_score_dicts(test_scores_dict)

    return test_scores, cutoffs


class BaseGridSearch:
    """
    Parallelization is based predominantly on [1]. Also similar to [2]

    Ref:
    [1] https://github.com/scikit-learn/scikit-learn/blob/0.24.1/sklearn/model_selection/_search.py#L795
    [2] https://github.com/scikit-optimize/scikit-optimize/blob/v0.8.1/skopt/searchcv.py#L410
    """

    def __init__(
        self,
        forecaster,
        cv,
        n_jobs=None,
        pre_dispatch=None,
        refit: bool = False,
        refit_metric: str = "smape",
        scoring=None,
        verbose=0,
        error_score=None,
        return_train_score=None,
    ):
        self.forecaster = forecaster
        self.cv = cv
        self.n_jobs = n_jobs
        self.pre_dispatch = pre_dispatch
        self.refit = refit
        self.refit_metric = refit_metric
        self.scoring = scoring
        self.verbose = verbose
        self.error_score = error_score
        self.return_train_score = return_train_score

        self.best_params_ = {}
        self.cv_results_ = {}

    def fit(
        self,
        y: pd.Series,
        X: Optional[pd.DataFrame] = None,
        additional_scorer_kwargs: Optional[Dict[str, Any]] = None,
        **fit_params,
    ) -> "BaseGridSearch":
        """Run fit with all sets of parameters.

        Parameters
        ----------
        y : pd.Series
            Target
        X : Optional[pd.DataFrame], optional
            Exogenous Variables, by default None
        additional_scorer_kwargs: Dict[str, Any]
            Additional scorer kwargs such as {`sp`:12} required by metrics like MASE
        **fit_params: Dict[str, Any]
            Parameters passed to the ``fit`` method of the estimator

        Returns
        -------
        BaseGridSearch
            Grid Search Object returned to allow chaining

        Raises
        ------
        ValueError
            When any of the following is True
            (1) Metric can not be found
            (2) No candidate provided in search
            (3) CV Iterator is empty
        """
        if additional_scorer_kwargs is None:
            additional_scorer_kwargs = {}

        y, X = check_y_X(y, X)

        # validate cross-validator
        cv = check_cv(self.cv)
        base_forecaster = clone(self.forecaster)

        # This checker is sktime specific and only support 1 metric
        # Removing for now since we can have multiple metrics
        # TODO: Add back later if it supports multiple metrics
        # scoring = check_scoring(self.scoring)
        # Multiple metrics supported
        scorers = self.scoring  # Dict[str, Union[str, scorer]]  Not metrics container
        scorers = _get_metrics_dict(scorers)
        refit_metric = self.refit_metric
        if refit_metric not in list(scorers.keys()):
            raise ValueError(
                f"Refit Metric: '{refit_metric}' is not available. ",
                f"Available Values are: {list(scorers.keys())}",
            )

        results = {}
        all_candidate_params = []
        all_out = []

        def evaluate_candidates(candidate_params):
            candidate_params = list(candidate_params)
            n_candidates = len(candidate_params)
            n_splits = cv.get_n_splits(y)

            if self.verbose > 0:
                print(  # noqa
                    f"Fitting {n_splits} folds for each of {n_candidates} "
                    f"candidates, totalling {n_candidates * n_splits} fits"
                )

            parallel = Parallel(
                n_jobs=self.n_jobs, verbose=self.verbose, pre_dispatch=self.pre_dispatch
            )
            out = parallel(
                delayed(_fit_and_score)(
                    forecaster=clone(base_forecaster),
                    y=y,
                    X=X,
                    scoring=scorers,
                    train=train,
                    test=test,
                    parameters=parameters,
                    fit_params=fit_params,
                    return_train_score=self.return_train_score,
                    error_score=self.error_score,
                    **additional_scorer_kwargs,
                )
                for parameters in candidate_params
                for train, test in get_folds(cv, y)
            )

            if len(out) < 1:
                raise ValueError(
                    "No fits were performed. "
                    "Was the CV iterator empty? "
                    "Were there no candidates?"
                )

            all_candidate_params.extend(candidate_params)
            all_out.extend(out)

            nonlocal results
            results = self._format_results(
                all_candidate_params, scorers, all_out, n_splits
            )
            return results

        self._run_search(evaluate_candidates)

        self.best_index_ = results["rank_test_%s" % refit_metric].argmin()
        self.best_score_ = results["mean_test_%s" % refit_metric][self.best_index_]
        self.best_params_ = results["params"][self.best_index_]

        self.best_forecaster_ = clone(base_forecaster).set_params(**self.best_params_)

        if self.refit:
            refit_start_time = time.time()
            self.best_forecaster_.fit(y, X, **fit_params)
            self.refit_time_ = time.time() - refit_start_time

        # Store the only scorer not as a dict for single metric evaluation
        self.scorer_ = scorers

        self.cv_results_ = results
        self.n_splits_ = cv.get_n_splits(y)

        self._is_fitted = True
        return self

    @staticmethod
    def _format_results(candidate_params, scorers, out, n_splits):
        """From sklearn and sktime"""
        n_candidates = len(candidate_params)
        (test_scores_dict, fit_time, score_time, cutoffs) = zip(*out)
        test_scores_dict = _aggregate_score_dicts(test_scores_dict)

        results = {}

        # From sklearn (with the addition of greater_is_better from sktime)
        # INFO: For some reason, sklearn func does not work with sktime metrics
        # without passing greater_is_better (as done in sktime) and processing
        # it as such.
        def _store(
            key_name,
            array,
            weights=None,
            splits=False,
            rank=False,
            greater_is_better=False,
        ):
            """A small helper to store the scores/times to the cv_results_"""
            # When iterated first by splits, then by parameters
            # We want `array` to have `n_candidates` rows and `n_splits` cols.
            array = np.array(array, dtype=np.float64).reshape(n_candidates, n_splits)
            if splits:
                for split_idx in range(n_splits):
                    # Uses closure to alter the results
                    results["split%d_%s" % (split_idx, key_name)] = array[:, split_idx]

            array_means = np.average(array, axis=1, weights=weights)
            results["mean_%s" % key_name] = array_means

            if key_name.startswith(("train_", "test_")) and np.any(
                ~np.isfinite(array_means)
            ):
                warnings.warn(
                    f"One or more of the {key_name.split('_')[0]} scores "
                    f"are non-finite: {array_means}",
                    category=UserWarning,
                )

            # Weighted std is not directly available in numpy
            array_stds = np.sqrt(
                np.average(
                    (array - array_means[:, np.newaxis]) ** 2, axis=1, weights=weights
                )
            )
            results["std_%s" % key_name] = array_stds

            if rank:
                # This section is taken from sktime
                array_means = -array_means if greater_is_better else array_means
                results["rank_%s" % key_name] = np.asarray(
                    rankdata(array_means, method="min"), dtype=np.int32
                )

        _store("fit_time", fit_time)
        _store("score_time", score_time)
        # Use one MaskedArray and mask all the places where the param is not
        # applicable for that candidate. Use defaultdict as each candidate may
        # not contain all the params
        param_results = defaultdict(
            partial(
                np.ma.MaskedArray, np.empty(n_candidates,), mask=True, dtype=object,
            )
        )
        for cand_i, params in enumerate(candidate_params):
            for name, value in params.items():
                # An all masked empty array gets created for the key
                # `"param_%s" % name` at the first occurrence of `name`.
                # Setting the value at an index also unmasks that index
                param_results["param_%s" % name][cand_i] = value

        results.update(param_results)
        # Store a list of param dicts at the key "params"
        results["params"] = candidate_params

        for scorer_name, scorer in scorers.items():
            # Computed the (weighted) mean and std for test scores alone
            _store(
                "test_%s" % scorer_name,
                test_scores_dict[scorer_name],
                splits=True,
                rank=True,
                weights=None,
                greater_is_better=True if scorer._sign == 1 else False,
            )

        return results


class ForecastingGridSearchCV(BaseGridSearch):
    """Exhaustive search over specified parameter values for an estimator.

    TODO: Add detailed docstring similar to the one available here:
    https://github.com/scikit-learn/scikit-learn/blob/c6512929fbee7232949c0f18cfb28cf3b5959df9/sklearn/model_selection/_search.py#L972
    """

    def __init__(
        self,
        forecaster,
        cv,
        param_grid,
        scoring=None,
        n_jobs=None,
        refit=True,
        refit_metric: str = "smape",
        verbose=0,
        pre_dispatch="2*n_jobs",
        error_score=np.nan,
        return_train_score=False,
    ):
        super(ForecastingGridSearchCV, self).__init__(
            forecaster=forecaster,
            cv=cv,
            n_jobs=n_jobs,
            pre_dispatch=pre_dispatch,
            refit=refit,
            refit_metric=refit_metric,
            scoring=scoring,
            verbose=verbose,
            error_score=error_score,
            return_train_score=return_train_score,
        )
        self.param_grid = param_grid
        _check_param_grid(param_grid)

    def _run_search(self, evaluate_candidates):
        """Search all candidates in param_grid"""
        evaluate_candidates(ParameterGrid(self.param_grid))


class ForecastingRandomizedSearchCV(BaseGridSearch):
    """Randomized search on hyper parameters.

    TODO: Add detailed docstring similar to the one available here:
    https://github.com/scikit-learn/scikit-learn/blob/c6512929fbee7232949c0f18cfb28cf3b5959df9/sklearn/model_selection/_search.py#L1292
    """

    def __init__(
        self,
        forecaster,
        cv,
        param_distributions,
        n_iter=10,
        scoring=None,
        n_jobs=None,
        refit=True,
        refit_metric: str = "smape",
        verbose=0,
        random_state=None,
        pre_dispatch="2*n_jobs",
        error_score=np.nan,
        return_train_score=False,
    ):
        super(ForecastingRandomizedSearchCV, self).__init__(
            forecaster=forecaster,
            cv=cv,
            n_jobs=n_jobs,
            pre_dispatch=pre_dispatch,
            refit=refit,
            refit_metric=refit_metric,
            scoring=scoring,
            verbose=verbose,
            error_score=error_score,
            return_train_score=return_train_score,
        )
        self.param_distributions = param_distributions
        self.n_iter = n_iter
        self.random_state = random_state

    def _run_search(self, evaluate_candidates):
        """Search n_iter candidates from param_distributions"""
        return evaluate_candidates(
            ParameterSampler(
                self.param_distributions, self.n_iter, random_state=self.random_state
            )
        )
