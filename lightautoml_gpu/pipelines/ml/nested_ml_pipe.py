"""Nested MLPipeline."""

import logging

from copy import copy
from copy import deepcopy
from typing import Any
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union

import numpy as np
import pandas as pd

from pandas import Series

import torch
if torch.cuda.is_available():
    from ...dataset.gpu.gpu_dataset import CupyDataset, CudfDataset, DaskCudfDataset
else:
    print("could not load gpu related libs (pipelines/ml/nested_ml_pipe.py)")

from ...dataset.np_pd_dataset import NumpyDataset
from ...ml_algo.base import PandasDataset
from ...ml_algo.base import TabularDataset
from ...ml_algo.base import TabularMLAlgo
from ...ml_algo.tuning.base import DefaultTuner
from ...ml_algo.tuning.base import ParamsTuner
from ...ml_algo.utils import tune_and_fit_predict
from ...reader.utils import set_sklearn_folds
from ...utils.timer import PipelineTimer
from ...validation.base import TrainValidIterator
from ...validation.utils import create_validation_iterator
from ..features.base import FeaturesPipeline
from ..selection.base import SelectionPipeline
from ..selection.importance_based import ImportanceEstimator
from .base import MLPipeline


logger = logging.getLogger(__name__)


class NestedTabularMLAlgo(TabularMLAlgo, ImportanceEstimator):
    """Wrapper for MLAlgo to make it trainable over nested folds.

    Limitations - only for ``TabularMLAlgo``.

    """

    @property
    def params(self) -> dict:
        """Parameters of ml_algo."""
        if self._ml_algo._params is None:
            self._ml_algo._params = copy(self.default_params)
        return self._ml_algo._params

    @params.setter
    def params(self, new_params: dict):
        assert isinstance(new_params, dict)
        self._ml_algo.params = {**self._ml_algo.params, **new_params}
        self._params = self._ml_algo.params

    def init_params_on_input(self, train_valid_iterator: TrainValidIterator) -> dict:
        """Init params depending on input data.

        Args:
            train_valid_iterator: Iterator over input data.

        Returns:
            dict with model hyperparameters.

        """
        return self._ml_algo.init_params_on_input(train_valid_iterator)

    def __init__(
        self,
        ml_algo: TabularMLAlgo,
        tuner: Optional[ParamsTuner] = None,
        refit_tuner: bool = False,
        cv: int = 5,
        n_folds: Optional[int] = None,
    ):
        self._name = ml_algo.name
        self._default_params = ml_algo.default_params

        super().__init__(default_params=ml_algo.default_params)
        self.default_params = ml_algo.default_params

        self._params_tuner = tuner
        self._refit_tuner = refit_tuner

        # take timer from inner algo and set to outer
        self.timer = ml_algo.timer
        if self.timer.key is not None:
            self.timer.key = "nested_" + self.timer.key
        # reset inner timer
        self._ml_algo = ml_algo.set_timer(PipelineTimer().start().get_task_timer())

        self.nested_cv = cv
        self.n_folds = n_folds

    def to_cpu(self):
        """Base method for pipeline conversion to CPU inference mode.

        Returns:
            instance of pipeline with CPU inference.
        """

        def convert_recursive_cpu(pipeline):
            if hasattr(pipeline, 'transformer_list'):
                for i in range(len(pipeline.transformer_list)):
                    convert_recursive_cpu(pipeline.transformer_list[i])
            else:
                if hasattr(pipeline, 'to_cpu'):
                    pipeline.to_cpu()
                if hasattr(pipeline, 'dataset_type'):
                    if pipeline.dataset_type == DaskCudfDataset or \
                            pipeline.dataset_type == CudfDataset:
                        pipeline.dataset_type = PandasDataset
                    elif pipeline.dataset_type == CupyDataset:
                        pipeline.dataset_type = NumpyDataset

        convert_recursive_cpu(self.features_pipeline._pipeline)
        self.features_pipeline = self.features_pipeline.to_cpu()

        if self.pre_selection.features_pipeline is not None:
            convert_recursive_cpu(self.pre_selection.features_pipeline._pipeline)
            self.pre_selection.features_pipeline = self.pre_selection.features_pipeline.to_cpu()
        if self.pre_selection.ml_algo is not None:
            self.pre_selection.ml_algo = self.pre_selection.ml_algo.to_cpu()
        if self.pre_selection._empty_algo is not None:
            self.pre_selection._empty_algo = None

        for i in range(len(self.ml_algos)):
            self.ml_algos[i] = deepcopy(self.ml_algos[i].to_cpu())

        if self.post_selection.features_pipeline is not None:
            convert_recursive_cpu(self.post_selection.features_pipeline._pipeline)
            self.post_selection.features_pipeline = self.post_selection.features_pipeline.to_cpu()
        if self.post_selection.ml_algo is not None:
            self.post_selection.ml_algo = self.post_selection.ml_algo.to_cpu()
        if self.post_selection._empty_algo is not None:
            self.post_selection._empty_algo = None
        return self

    def fit_predict(self, train_valid_iterator: TrainValidIterator) -> NumpyDataset:  # noqa DAR102
        self.timer.start()
        div = len(train_valid_iterator) if self.n_folds is None else self.n_folds
        self._per_task_timer = self.timer.time_left / div

        return super().fit_predict(train_valid_iterator)

    def fit_predict_single_fold(self, train: TabularDataset, valid: TabularDataset) -> Tuple[Any, np.ndarray]:
        """Implements training and prediction on single fold.

        Args:
            train: TabularDataset to train.
            valid: TabularDataset to validate.

        Returns:
            Tuple (model, predicted_values).

        """
        logger.info3("Start fit_predict for nested model on a single fold ...")
        # TODO: rewrite
        if isinstance(train, PandasDataset):
            train.folds = pd.Series(
                set_sklearn_folds(
                    train.task,
                    train.target,
                    self.nested_cv,
                    random_state=42,
                    group=train.group,
                )
            )
        else:
            train.folds = set_sklearn_folds(
                train.task,
                train.target,
                self.nested_cv,
                random_state=42,
                group=train.group,
            )

        train_valid = create_validation_iterator(train, n_folds=self.n_folds)

        model = deepcopy(self._ml_algo)
        model.set_timer(PipelineTimer(timeout=self._per_task_timer, overhead=0).start().get_task_timer())
        logger.debug(self._ml_algo.params)
        tuner = self._params_tuner
        if self._refit_tuner:
            tuner = deepcopy(tuner)

        if tuner is None:
            logger.debug("Run without tuner")
            model.fit_predict(train_valid)
        else:
            logger.debug("Run with tuner")
            (
                model,
                _,
            ) = tune_and_fit_predict(model, tuner, train_valid, True)

        val_pred = model.predict(valid).data
        logger.debug("Model params", model.params)
        return model, val_pred

    def predict_single_fold(self, model: Any, dataset: TabularDataset) -> np.ndarray:
        """Model prediction on a dataset.

        Args:
            model: Model.
            dataset: Dataset.

        Returns:
            Predictions.

        """
        pred = model.predict(dataset).data

        return pred

    def _get_search_spaces(self, suggested_params: dict, estimated_n_trials: int) -> dict:
        return self._ml_algo._get_search_spaces(suggested_params, estimated_n_trials)

    def get_features_score(self) -> Series:
        """Score of each features."""
        scores = pd.concat([x.get_features_score() for x in self.models], axis=1).mean(axis=1)

        return scores

    def fit(self, train_valid: TrainValidIterator):
        """Just to be compatible with :class:`~lightautoml_gpu.validation.selection.importance_based.ImportanceEstimator`.

        Args:
            train_valid: Classic cv iterator.

        """
        self.fit_predict(train_valid)


class NestedTabularMLPipeline(MLPipeline):
    """Wrapper for MLPipeline to make it trainable over nested folds.

    Limitations:

        - Only for TabularMLAlgo
        - Nested trained only MLAlgo. FeaturesPipelines and
          SelectionPipelines are trained as usual.

    Args:
        ml_algos: Sequence of MLAlgo's or Pair - (MlAlgo, ParamsTuner).
        force_calc: Flag if single fold of
            :class:`~lightautoml_gpu.ml_algo.base.MlAlgo`
            should be calculated anyway.
        pre_selection: Initial feature selection.
            If ``None`` there is no initial selection.
        features_pipeline: Composition of feature transforms.
        post_selection: Post feature selection.
            If ``None`` there is no post selection.
        cv: Nested folds cv split.
        n_folds: Limit of valid iterations from cv.
        inner_tune: Should we refit tuner each inner
            cv run or tune ones on outer cv.
        refit_tuner: Should we refit tuner each inner
            loop with ``inner_tune==True``.

    """

    def __init__(
        self,
        ml_algos: Sequence[Union[TabularMLAlgo, Tuple[TabularMLAlgo, ParamsTuner]]],
        force_calc: Union[bool, Sequence[bool]] = True,
        pre_selection: Optional[SelectionPipeline] = None,
        features_pipeline: Optional[FeaturesPipeline] = None,
        post_selection: Optional[SelectionPipeline] = None,
        cv: int = 1,
        n_folds: Optional[int] = None,
        inner_tune: bool = False,
        refit_tuner: bool = False,
    ):
        if cv > 1:
            new_ml_algos = []

            for n, mt_pair in enumerate(ml_algos):

                try:
                    mod, tuner = mt_pair
                except (TypeError, ValueError):
                    mod, tuner = mt_pair, DefaultTuner()

                if inner_tune:
                    new_ml_algos.append(NestedTabularMLAlgo(mod, tuner, refit_tuner, cv, n_folds))
                else:
                    new_ml_algos.append((NestedTabularMLAlgo(mod, None, True, cv, n_folds), tuner))

            ml_algos = new_ml_algos

        super().__init__(ml_algos, force_calc, pre_selection, features_pipeline, post_selection)
