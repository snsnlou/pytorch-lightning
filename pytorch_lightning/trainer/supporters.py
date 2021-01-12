# Copyright The PyTorch Lightning team.
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

import errno
import os
import os.path as osp
from collections.abc import Iterable, Iterator, Mapping, Sequence
from getpass import getuser
from tempfile import gettempdir
from tempfile import NamedTemporaryFile as TempFile
from typing import Any, Optional, Union

import torch
from torch.utils.data import Dataset

from pytorch_lightning.trainer.connectors.logger_connector.logger_connector import LoggerStages
from pytorch_lightning.utilities.apply_func import apply_to_collection
from pytorch_lightning.utilities.data import get_len
from pytorch_lightning.utilities.exceptions import MisconfigurationException


def makedirs(path):
    try:
        os.makedirs(osp.expanduser(osp.normpath(path)))
    except OSError as e:
        if e.errno != errno.EEXIST and osp.isdir(path):
            raise e


class TensorRunningAccum(object):
    """Tracks a running accumulation values (min, max, mean) without graph
    references.

    Examples:
        >>> accum = TensorRunningAccum(5)
        >>> accum.last(), accum.mean()
        (None, None)
        >>> accum.append(torch.tensor(1.5))
        >>> accum.last(), accum.mean()
        (tensor(1.5000), tensor(1.5000))
        >>> accum.append(torch.tensor(2.5))
        >>> accum.last(), accum.mean()
        (tensor(2.5000), tensor(2.))
        >>> accum.reset()
        >>> _= [accum.append(torch.tensor(i)) for i in range(13)]
        >>> accum.last(), accum.mean(), accum.min(), accum.max()
        (tensor(12.), tensor(10.), tensor(8.), tensor(12.))
    """

    def __init__(self, window_length: int):
        self.window_length = window_length
        self.memory = None
        self.current_idx: int = 0
        self.last_idx: Optional[int] = None
        self.rotated: bool = False

    def reset(self) -> None:
        """Empty the accumulator."""
        self.__init__(self.window_length)

    def last(self):
        """Get the last added element."""
        if self.last_idx is not None:
            return self.memory[self.last_idx]

    def append(self, x):
        """Add an element to the accumulator."""
        if self.memory is None:
            self.memory = torch.zeros(self.window_length, *x.shape)

        # ensure same device and type
        if self.memory.device != x.device or self.memory.type() != x.type():
            x = x.to(self.memory)

        # store without grads
        with torch.no_grad():
            self.memory[self.current_idx] = x
            self.last_idx = self.current_idx

        # increase index
        self.current_idx += 1

        # reset index when hit limit of tensor
        self.current_idx = self.current_idx % self.window_length
        if self.current_idx == 0:
            self.rotated = True

    def mean(self):
        """Get mean value from stored elements."""
        return self._agg_memory('mean')

    def max(self):
        """Get maximal value from stored elements."""
        return self._agg_memory('max')

    def min(self):
        """Get minimal value from stored elements."""
        return self._agg_memory('min')

    def _agg_memory(self, how: str):
        if self.last_idx is not None:
            if self.rotated:
                return getattr(self.memory, how)()
            else:
                return getattr(self.memory[: self.current_idx], how)()


class Accumulator(object):
    def __init__(self):
        self.num_values = 0
        self.total = 0

    def accumulate(self, x):
        with torch.no_grad():
            self.total += x
            self.num_values += 1

    def mean(self):
        return self.total / self.num_values


class PredictionCollection(object):
    def __init__(self, trainer):
        self.trainer = trainer
        self.global_rank = self.trainer.global_rank
        self.world_size = self.trainer.world_size
        self._predictions = {stage: {} for stage in LoggerStages}

    @property
    def current_stage(self):
        return self.trainer.logger_connector._current_stage

    @property
    def predictions(self):
        return self._predictions[self.current_stage]    # type: ignore

    @staticmethod
    def convert_to_numpy(value):
        return value.cpu().numpy()

    def _add_prediction(self, predictions):
        model_ref = self.trainer.get_model()
        dl_idx = model_ref._current_dataloader_idx if model_ref._current_dataloader_idx is not None else 0

        internal_predictions = self.predictions

        if dl_idx not in internal_predictions:
            internal_predictions[dl_idx] = {}

        for pred in predictions:
            if isinstance(pred, (list, tuple)):
                key = pred[0]
                pred = {i: v for i, v in enumerate(pred)}
            else:
                if "path" in pred:
                    key = pred["path"]
                elif "id" in pred:
                    key = pred["id"]
                else:
                    raise MisconfigurationException(
                        "When predictions are provided within a dict, we expect either a `path` or `id` key. "
                    )

            if key not in internal_predictions[dl_idx]:
                internal_predictions[dl_idx][key] = []
            else:
                raise MisconfigurationException(
                    "Prediction Collection doesn't support multiple prediction for one sample yet.")

            internal_predictions[dl_idx][key] = apply_to_collection(
                pred, torch.Tensor, PredictionCollection.convert_to_numpy)

    def add(self, predictions):
        if predictions is None:
            return

        assert isinstance(predictions, (list, tuple))
        if not all([isinstance(p, (list, tuple, dict)) for p in predictions]):
            raise MisconfigurationException(
                "predictions objects should be a list or tuple. "
                "Each contained element should be either a dict, list or tuple. "
            )
        if not all([len(p) > 1 for p in predictions]):
            raise MisconfigurationException(
                "predictions objects should be a list or tuple. "
                "Each contained element should contain at minimum an ID and a prediction tensor. "
            )

        self._add_prediction(predictions)

    def attach_predictions(self, results) -> list:
        if len(self) > 0:
            predictions = self.predictions
            for dl_idx, result in enumerate(results):
                if dl_idx in predictions:
                    dl_predictions = predictions[dl_idx]
                    dl_predictions = self.reduce_predictions(dl_predictions)
                    result["predictions"] = [*dl_predictions.values()]
        return results

    def reduce_predictions(self, predictions):
        if torch.distributed.is_available() and torch.distributed.is_initialized():
            path = os.path.join(gettempdir(), f'{getuser()}"_{self.global_rank}"')
            makedirs(path)
            with TempFile(mode='wb', delete=True, dir=path) as fp:
                torch.save(predictions, fp)
                self.trainer.accelerator_backend.barrier("reduce_predictions_start")
                predictions = {}
                if str(self.global_rank) == '0':
                    predictions = {}
                    for rank in range(int(self.world_size)):
                        dir = os.path.join(gettempdir(), f'{getuser()}_{rank}')
                        for path in os.listdir(dir):
                            predictions.update(torch.load(osp.join(dir, path)))
                self.trainer.accelerator_backend.barrier("reduce_predictions_end")
        return predictions

    def __len__(self):
        return len(self.predictions.keys())


class CycleIterator(object):
    """
    Iterator for restarting a dataloader if it runs out of samples
    """
    def __init__(self, loader: Any, length: Optional[int] = None):
        """

        Args:
            loader: the loader to restart for cyclic (and optionally infinite) sampling
            length: the number of batches to sample (with restarted loaders if necessary) before raising StopIteration
                if None: infinite

        """
        if length is None:
            length = float('inf')

        self.length = length
        self.loader = loader
        self._loader_iter = None
        self.counter = 0

    def __iter__(self) -> Any:
        """

        Creates the internal iterator and returns self

        Returns:
            CycleIterator: self

        """
        self.counter = 0
        self._loader_iter = iter(self.loader)
        return self

    def __next__(self) -> Any:
        """
        Fetches the next batch from internal dataloader and restarts
        it if necessary

        Returns:
            Any: the resulting batch

        Raises:
            StopIteration: if more then :attr:`length` batches have been returned

        """
        # Note: if self.length is `inf`, then the iterator will never stop
        if self.counter >= self.__len__():
            raise StopIteration

        try:
            return next(self._loader_iter)

        except StopIteration:
            self._loader_iter = iter(self.loader)
            return next(self._loader_iter)

        finally:
            self.counter += 1

    def __len__(self) -> Union[int, float]:
        return self.length


class CombinedDataset(object):
    """
    Combine multiple datasets and compute their statistics
    """
    COMPUTE_FUNCS = {'min_size': min, 'max_size_cycle': max}

    def __init__(self, datasets: Union[Sequence, Mapping], mode: str = 'min_size'):
        """

        Args:
            datasets: a sequence/mapping datasets. Can be a collections of torch.utils.Dataset,
                Iterable or even None.
            mode: whether to use the minimum number of batches in all samples or the maximum
                number of batches in all samples.

        """
        self.datasets = datasets
        if mode not in self.COMPUTE_FUNCS.keys():
            raise MisconfigurationException(
                f'You have selected unsupported mode "{mode}",'
                f' please select one the: {list(self.COMPUTE_FUNCS.keys())}.'
            )
        self.mode = mode

    @property
    def max_len(self) -> Union[int, float]:
        return self._calc_num_data(self.datasets, 'max_size_cycle')

    @property
    def min_len(self) -> Union[int, float]:
        return self._calc_num_data(self.datasets, 'min_size')

    @staticmethod
    def _calc_num_data(datasets: Union[Sequence, Mapping], mode: str) -> Union[int, float]:
        """
        Compute the length of `CombinedDataset` according to the `mode`.

        Args:
            datasets: a sequence/mapping datasets. Can be a collections of torch.utils.data.Dataset,
                Iterable or even None.
            mode: Determine `CombinedDataset`'s length is the maximum or minimum of
                the datasets.

        Returns:
            length: the length of `CombinedDataset`

        """
        if mode not in CombinedDataset.COMPUTE_FUNCS.keys():
            raise MisconfigurationException(f"Invalid Mode: {mode}")

        # extract the lengths
        all_lengths = apply_to_collection(datasets, (Dataset, Iterable, type(None)), get_len,
                                          wrong_dtype=(Sequence, Mapping))

        compute_func = CombinedDataset.COMPUTE_FUNCS[mode]

        if isinstance(all_lengths, (int, float)):
            length = all_lengths

        elif isinstance(all_lengths, Mapping):
            length = compute_func(all_lengths.values())

        elif isinstance(all_lengths, Sequence):
            length = compute_func(all_lengths)

        return length

    def __len__(self) -> int:
        """Return the minimum length of the datasets."""
        return self._calc_num_data(self.datasets, self.mode)


class CombinedLoader(object):
    """
    Combines different dataloaders and allows sampling in parallel.

    Supported modes are 'min_size', which raises StopIteration after the shortest loader
    (the one with the lowest number of batches) is done, and 'max_size_cycle` which raises
    StopIteration after the longest loader (the one with most batches) is done, while cycling
    through the shorter loaders.

    Examples:
        >>> loaders = {'a': torch.utils.data.DataLoader(range(6), batch_size=4),
        ...            'b': torch.utils.data.DataLoader(range(15), batch_size=5)}
        >>> combined_loader = CombinedLoader(loaders, 'max_size_cycle')
        >>> for item in combined_loader:
        ...     print(item)
        {'a': tensor([0, 1, 2, 3]), 'b': tensor([0, 1, 2, 3, 4])}
        {'a': tensor([4, 5]), 'b': tensor([5, 6, 7, 8, 9])}
        {'a': tensor([0, 1, 2, 3]), 'b': tensor([10, 11, 12, 13, 14])}
        >>> combined_loader = CombinedLoader(loaders, 'min_size')
        >>> for item in combined_loader:
        ...     print(item)
        {'a': tensor([0, 1, 2, 3]), 'b': tensor([0, 1, 2, 3, 4])}
        {'a': tensor([4, 5]), 'b': tensor([5, 6, 7, 8, 9])}

    """
    SUPPORTED_MODES = ('min_size', 'max_size_cycle')

    def __init__(self, loaders: Any, mode: str = 'min_size'):
        """

        Args:
            loaders: the loaders to sample from. Can be all kind of collection
            mode: the mode. Supported are 'min_size' which stops if the shortest loader is exhausted and
                'max_size_cycle' which stops if the longest loader is exhausted and cycles through the smaller ones.

        """
        self.loaders = loaders

        datasets = apply_to_collection(self.loaders, Iterable, getattr, 'dataset', None,
                                       wrong_dtype=(Sequence, Mapping))
        # could be multiple datasets, but use self.dataset to follow the name convention in DataLoader
        self.dataset = CombinedDataset(datasets, mode)

        if mode not in self.SUPPORTED_MODES:
            raise MisconfigurationException(f"Invalid Mode: {mode}")

        self.mode = mode

        if self.mode == 'max_size_cycle':
            self._wrap_loaders_max_size_cycle()

    @property
    def sampler(self) -> Union[Iterable, Sequence, Mapping]:
        """Return a collections of samplers extracting from loaders."""
        return apply_to_collection(self.loaders, Iterable, getattr, 'sampler', None,
                                   wrong_dtype=(Sequence, Mapping))

    def _wrap_loaders_max_size_cycle(self) -> Any:
        """
        Wraps all loaders to make sure they are cycled until the longest loader is exhausted

        Returns:
            Any: the wrapped loaders

        """
        all_lengths = apply_to_collection(self.loaders, Iterable, get_len,
                                          wrong_dtype=(Sequence, Mapping))

        if isinstance(all_lengths, (int, float)):
            length = all_lengths

        elif isinstance(all_lengths, Mapping):
            length = max(all_lengths.values())

        elif isinstance(all_lengths, Sequence):
            length = max(all_lengths)

        if isinstance(self.loaders, Mapping):
            self.loaders = type(self.loaders)({k: CycleIterator(v, length=length)
                                               for k, v in self.loaders.items()})

        elif isinstance(self.loaders, Sequence):
            self.loaders = type(self.loaders)([
                CycleIterator(v, length=length) for v in self.loaders
            ])

        # dataloaders are iterable but not sequence
        elif isinstance(self.loaders, Iterable):
            # only one dataloader, just keep it the same.
            pass
        else:
            raise ValueError(f'Invalid Datatype for loaders: {type(self.loaders).__name__}')

    def __iter__(self) -> Any:
        """
        Create and return an iterator, `CombinedLoaderIterator`, for the combined loader.
        """
        return CombinedLoaderIterator(self.loaders)

    @staticmethod
    def _calc_num_batches(loaders: Any) -> Union[int, float]:
        """
        Compute the length (aka the number of batches) of `CombinedLoader`.

        Args:
            loaders: a collections of loaders.

        Returns:
            length: the minimum length of loaders

        """
        all_lengths = apply_to_collection(loaders, Iterable, get_len,
                                          wrong_dtype=(Sequence, Mapping))

        if isinstance(all_lengths, (int, float)):
            return all_lengths

        elif isinstance(all_lengths, Mapping):
            return min(all_lengths.values())

        elif isinstance(all_lengths, Sequence):
            return min(all_lengths)

        raise TypeError(f'Got Type {type(all_lengths).__name__}, but expected one of Sequence, int or Mapping')

    def __len__(self) -> int:
        return self._calc_num_batches(self.loaders)


class CombinedLoaderIterator(object):
    """
    Custom Iterator returning data from multple loaders, and allows sampling in parallel
    """
    def __init__(self, loaders: Any):
        """

        Args:
            loaders: the loaders to sample from. Can be all kind of collection

        """
        self.loaders = loaders
        self._loader_iters = None

    @property
    def loader_iters(self) -> Any:
        """
        Get the `_loader_iters` and create one if it is None.
        """
        if self._loader_iters is None:
            self._loader_iters = self.create_loader_iters(self.loaders)

        return self._loader_iters

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        """
        Fetches the next batch from multiple data loaders

        Returns:
            Any: a collections of batch data

        """
        return self.request_next_batch(self.loader_iters)

    @staticmethod
    def request_next_batch(loader_iters: Union[Iterator, Sequence, Mapping]) -> Any:
        """
        Return the batch of data from multiple iterators.

        Args:
            loader_iters: a collections of iterators

        Returns
            Any: a collections of batch data

        """
        return apply_to_collection(loader_iters, Iterator, next)

    @staticmethod
    def create_loader_iters(
        loaders: Union[Any, Iterator, Sequence, Mapping]
    ) -> Union[Any, Iterator, Sequence, Mapping]:
        """
        Create and return a collection of iterators from loaders.

        Args:
            loaders: a collections of loaders

        Returns
            a collections of iterators

        """
        # dataloaders are Iterable but not Sequences. Need this to specifically exclude sequences
        return apply_to_collection(loaders, Iterable, iter, wrong_dtype=(Sequence, Mapping))
