import numpy as np
from typing import Union

from fedot.core.data.data import InputData
from golem.utilities.requirements_notificator import warn_requirement

from fedot.core.operations.evaluation.operation_implementations.implementation_interfaces import ModelImplementation
from fedot.core.operations.operation_parameters import OperationParameters


try:
    import torch
    from torch.nn import GRU, LSTM, RNN, Linear, Dropout, MSELoss, BatchNorm1d, Conv1d, Sequential, ReLU
    from torch.optim import Adam
    from torch.utils.data import TensorDataset, DataLoader
except ModuleNotFoundError:
    warn_requirement('torch', 'fedot[extra]')
    torch = object()


class RNNImplementation(ModelImplementation):
    """__init__(self, params: OperationParameters)

    The implementation class of RNN models. Provides Jordan-RNN, Elman-RNN, LSTM and GRU models.

    **params (dict)**: parameters of model. The following parameters are supported:
        *rnn_type (str)*: type of RNN model implementation. It can take one of the following values:
            'gru', 'lstm', 'rnn_elman' or 'rnn_jordan'. Default: 'rnn_elman'
        *max_step (int)*: maximum steps number for optimization. Default: 300;

        *dropout (float)*: If non-zero, introduces a `Dropout` layer on the outputs of each RNN model layer
        except the last layer, with dropout probability equal to :attr:`dropout`. Default: 0;

        *hidden_size (int)*: The number of features in the hidden stat. Default: round(data.features.shape[1] * 2 / (1 - dropout));

        *num_layers (int)*: number of recurrent layers. Default: 3;

        *seed (int)*: random_seed number.

    """
    def __init__(self, params: OperationParameters):
        super().__init__(params)

        self.device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
        self.model = None
        self.max_step = params.get('max_step') or 500
        self.batch_size = 50
        self.validation_size = 0.2
        self.preprocessing_type = 'normalization'
        self.preprocessing_params = dict()
        self.seed = params.get('seed')

        if self.seed is not None:
            self.generator = torch.Generator()
            self.generator.manual_seed(self.seed)
        else:
            self.generator = None

        self.type_rnn = params.get('rnn_type') or 'rnn_elman'
        if self.type_rnn not in ['rnn_elman', 'rnn_jordan', 'lstm', 'gru']:
            raise ValueError((f"Unknown type of RNN: {self.type_rnn}."
                              f" Allowed types: 'rnn_elman', 'rnn_jordan', 'lstm', 'gru'"))

    def preprocessing(self, x, parameters_defining=False):
        if parameters_defining not in (True, False):
            raise ValueError(
                f"parameters_defining should be one of (True, False) but {parameters_defining} is provided")
        if self.preprocessing_type == 'normalization':
            return (x - self.preprocessing_params['mean']) / (self.preprocessing_params['std'] + 1e-6)
        elif self.preprocessing_type == 'minmax':
            return (x - self.preprocessing_params['min']) / (
                        self.preprocessing_params['max'] - self.preprocessing_params['min'])
        elif self.preprocessing_type == 'window_bias':
            if parameters_defining:
                self.preprocessing_params['window_bias'] = np.mean(x, axis=(2, 1)).reshape((-1, 1, 1))
            return x - self.preprocessing_params['window_bias']
        elif self.preprocessing_type is None:
            return x
        else:
            raise ValueError((f"Unknown type of preprocessing: {self.preprocessing_type}."
                              f" Allowed types: normalization, minmax"))

    def postprocessing(self, y):
        if self.preprocessing_type == 'normalization':
            return y * self.preprocessing_params['std'] + self.preprocessing_params['mean']
        elif self.preprocessing_type == 'minmax':
            return y * (self.preprocessing_params['max'] - self.preprocessing_params['min']) + \
                self.preprocessing_params['min']
        elif self.preprocessing_type == 'window_bias':
            return y + self.preprocessing_params['window_bias'].reshape((-1, 1, 1) if np.ndim(y) == 3 else (-1, 1))
        elif self.preprocessing_type is None:
            return y
        else:
            raise ValueError((f"Unknown type of preprocessing: {self.preprocessing_type}."
                              f" Allowed types: normalization, minmax"))

    def numpy_to_torch(self, x, third_dimension=True):
        # (batch_size, num_timesteps or sequence_length, feature_size)
        x = x.astype(np.float32)
        if third_dimension:
            x = x.reshape((x.shape[0], x.shape[1], 1))
        else:
            x = x.reshape((x.shape[0], x.shape[1]))
        x = torch.from_numpy(x)
        return x

    def initialize_hidden(self):
        return torch.randn(*self.hidden_size, generator=self.generator)

    def fit(self, data: InputData):
        # TODO: choose num_layers and hidden_size accordance to amount of data
        #       if there is a small dataset then big gru cannot be fitted
        dropout = self.params.get('dropout') or 0.0
        hidden_size = (self.params.get('hidden_size') or
                        round(data.features.shape[1] * 2 / (1 - dropout)))
        num_layers = self.params.get('num_layers') or 3
        conv_layers = self.params.get('conv_layers') or None
        conv_params = {'out_channels': self.params.get('conv_out_channels') or None,
                       'kernel_size': self.params.get('conv_kernel_size') or None}

        if self.type_rnn == 'gru':
            self.model = GRUModel(input_data_length=data.features.shape[1],
                                  output_data_length=data.task.task_params.forecast_length,
                                  hidden_size=hidden_size,
                                  num_layers=num_layers,
                                  conv_layers=conv_layers,
                                  conv_params=conv_params,
                                  dropout=dropout)
        elif self.type_rnn == 'lstm':
            self.model = LSTMModel(input_data_length=data.features.shape[1],
                                   output_data_length=data.task.task_params.forecast_length,
                                   hidden_size=hidden_size,
                                   num_layers=num_layers,
                                   conv_layers=conv_layers,
                                   conv_params=conv_params,
                                   dropout=dropout)
        elif self.type_rnn == 'rnn_elman':
            self.model = RNNModel(input_data_length=data.features.shape[1],
                                  output_data_length=data.task.task_params.forecast_length,
                                  hidden_size=hidden_size,
                                  num_layers=num_layers,
                                  conv_layers=conv_layers,
                                  conv_params=conv_params,
                                  dropout=dropout)

        # prepare objects
        torch.manual_seed(self.seed)
        model = self.model.to(self.device)
        model.train()
        loss_fun = MSELoss(reduction="mean")
        opt_fun = Adam(model.parameters(), lr=1e-3)
        self.hidden_size = (model.rnn.input_size * model.rnn.num_layers, self.batch_size, model.rnn.hidden_size)

        # prepare data
        x, y = data.features, data.target
        self.preprocessing_params = {'mean': np.mean(x), 'std': np.std(x), 'max': np.max(x), 'min': np.min(x)}
        x, y = self.preprocessing(x, True), self.preprocessing(y, False)
        x, y = self.numpy_to_torch(x).to(self.device), self.numpy_to_torch(y, False).to(self.device)
        batch_count = int(x.shape[0] / self.batch_size)
        train_count = int(batch_count * (1 - self.validation_size))

        def fit_step(count_range, x=x, y=y, model=model, loss_fun=loss_fun,
                     opt_fun=opt_fun, grad=True):
            _losses = []
            h = self.initialize_hidden().to(self.device)
            for batch_num in count_range:
                x_iter = x[batch_num * self.batch_size:(batch_num + 1) * self.batch_size, :, :]
                y_iter = y[batch_num * self.batch_size:(batch_num + 1) * self.batch_size, :]
                y_pred, h = model(x_iter, h)
                loss = loss_fun(y_iter, y_pred)
                if grad:
                    loss.backward()
                    opt_fun.step()
                    opt_fun.zero_grad()
                _losses.append(loss.item())
            return np.mean(_losses)

        losses, validations = [], []
        for epoch in range(self.max_step):
            losses.append(fit_step(range(train_count)))
            validations.append(fit_step(range(train_count, batch_count), grad=False))

            if epoch > 5:
                # TODO: adaptive early stop
                if np.mean(abs(np.diff(validations[-5:]) / validations[-5:-1]) < 0.05) > 0.5:
                    break

        # fit on validation data
        fit_step(range(train_count, batch_count))
        return self.model

    def predict(self, data: InputData):
        self.model.eval()
        with torch.no_grad():
            x = self.preprocessing(data.features, True)
            x = self.numpy_to_torch(x).to(self.device)
            return self.postprocessing(self.model(x)[0].to('cpu').numpy())


class GRUModel(torch.nn.Module):
    """__init__(self, input_data_length: int, output_data_length: int, num_layers: int, dropout: float, hidden_size: int)

    **GRU** model implementation class.
    """
    def __init__(self, input_data_length: int, output_data_length: int, num_layers: int, dropout: float,
                 hidden_size: int, conv_layers: Union[None, int], conv_params: Union[None, dict]):
        super().__init__()

        if conv_layers is not None and conv_layers > 0:

            assert conv_params.get['out_channels'] is None, "If convolutional layers are used the parameter 'conv_output_channels' must be determined."
            assert len(conv_params.get['out_channels']) != conv_layers, f"There are {conv_layers} convolutional layers, \
            but only for {len(conv_params.get['out_channels'])} layers the 'conv_out_channels' parameter is specified."
            assert len(conv_params.get['kernel_size']) != conv_layers, f"There are {conv_layers} convolutional layers, \
                        but only for {len(conv_params.get['kernel_size'])} layers the 'conv_kernel_size' parameter is specified."

            self.conv = torch.nn.Sequential()
            self.conv.add_module("conv_1", torch.nn.Conv1d(in_channels=input_data_length,
                                                           out_channels=conv_params.get['out_channels'][0],
                                                           kernel_size=conv_params.get['kernel_size'][0]))
            self.conv.add_module("relu_1", torch.nn.ReLU())
            for l in range(1, conv_layers):
                self.conv.add_module(f"conv_{l+1}", torch.nn.Conv1d(in_channels=conv_params.get['out_channels'][l-1],
                                                                    out_channels=conv_params.get['out_channels'][l],
                                                                    kernel_size=conv_params.get['kernel_size'][l]))
                self.conv.add_module(f"relu_{l+1}", torch.nn.ReLU())

        self.rnn = GRU(input_size=1,
                       hidden_size=hidden_size,
                       num_layers=num_layers,
                       bias=True,
                       batch_first=True,
                       dropout=dropout,
                       bidirectional=False)

        self.linear = Linear(in_features=input_data_length * hidden_size,
                             out_features=output_data_length)

    def forward(self, x, h=None):
        if hasattr(self, 'conv'):
            x = self.conv(x)
        if h is None:
            x, h = self.rnn(x, h)
        else:
            x, h = self.rnn(x)
        x = self.linear(x.flatten(1))
        return x, h


class LSTMModel(torch.nn.Module):
    """__init__(self, input_data_length: int, output_data_length: int, num_layers: int, dropout: float, hidden_size: int)

    **LSTM** model implementation class.
    """
    def __init__(self, input_data_length: int, output_data_length: int, num_layers: int, dropout: float,
                 hidden_size: int, conv_layers: Union[None, int], conv_params: Union[None, dict]):
        super().__init__()

        if conv_layers is not None and conv_layers > 0:

            assert conv_params.get[
                       'out_channels'] is None, "If convolutional layers are used the parameter 'conv_output_channels' must be determined."
            assert len(conv_params.get['out_channels']) != conv_layers, f"There are {conv_layers} convolutional layers, \
            but only for {len(conv_params.get['out_channels'])} layers the 'conv_out_channels' parameter is specified."
            assert len(conv_params.get['kernel_size']) != conv_layers, f"There are {conv_layers} convolutional layers, \
                        but only for {len(conv_params.get['kernel_size'])} layers the 'conv_kernel_size' parameter is specified."

            self.conv = torch.nn.Sequential()
            self.conv.add_module("conv_1", torch.nn.Conv1d(in_channels=input_data_length,
                                                           out_channels=conv_params.get['out_channels'][0],
                                                           kernel_size=conv_params.get['kernel_size'][0]))
            self.conv.add_module("relu_1", torch.nn.ReLU())
            for l in range(1, conv_layers):
                self.conv.add_module(f"conv_{l + 1}",
                                     torch.nn.Conv1d(in_channels=conv_params.get['out_channels'][l - 1],
                                                     out_channels=conv_params.get['out_channels'][l],
                                                     kernel_size=conv_params.get['kernel_size'][l]))
                self.conv.add_module(f"relu_{l + 1}", torch.nn.ReLU())

        self.rnn = LSTM(input_size=1,
                         hidden_size=hidden_size,
                         num_layers=num_layers,
                         bias=True,
                         batch_first=True,
                         dropout=dropout,
                         bidirectional=False)
        self.linear = Linear(in_features=input_data_length * hidden_size,
                             out_features=output_data_length)

    def forward(self, x, h=None):
        if hasattr(self, 'conv'):
            x = self.conv(x)
        if h is None:
            x, h = self.rnn(x, h)
        else:
            x, h = self.rnn(x)
        x = self.linear(x.flatten(1))
        return x, h


class RNNModel(torch.nn.Module):
    """__init__(self, input_data_length: int, output_data_length: int, num_layers: int, dropout: float, hidden_size: int)

    **Elman-RNN** model implementation class.
    """
    def __init__(self, input_data_length: int, output_data_length: int, num_layers: int, conv: bool, dropout: float,
                 hidden_size: int, conv_layers: Union[None, int], conv_params: Union[None, dict]):
        super().__init__()

        if conv_layers is not None and conv_layers > 0:

            assert conv_params.get[
                       'out_channels'] is None, "If convolutional layers are used the parameter 'conv_output_channels' must be determined."
            assert len(conv_params.get['out_channels']) != conv_layers, f"There are {conv_layers} convolutional layers, \
            but only for {len(conv_params.get['out_channels'])} layers the 'conv_out_channels' parameter is specified."
            assert len(conv_params.get['kernel_size']) != conv_layers, f"There are {conv_layers} convolutional layers, \
                        but only for {len(conv_params.get['kernel_size'])} layers the 'conv_kernel_size' parameter is specified."

            self.conv = torch.nn.Sequential()
            self.conv.add_module("conv_1", torch.nn.Conv1d(in_channels=input_data_length,
                                                           out_channels=conv_params.get['out_channels'][0],
                                                           kernel_size=conv_params.get['kernel_size'][0]))
            self.conv.add_module("relu_1", torch.nn.ReLU())
            for l in range(1, conv_layers):
                self.conv.add_module(f"conv_{l + 1}",
                                     torch.nn.Conv1d(in_channels=conv_params.get['out_channels'][l - 1],
                                                     out_channels=conv_params.get['out_channels'][l],
                                                     kernel_size=conv_params.get['kernel_size'][l]))
                self.conv.add_module(f"relu_{l + 1}", torch.nn.ReLU())

        self.rnn = RNN(input_size=1,
                       hidden_size=hidden_size,
                       num_layers=num_layers,
                       bias=True,
                       batch_first=True,
                       dropout=dropout,
                       bidirectional=False)
        self.linear = Linear(in_features=input_data_length * hidden_size,
                             out_features=output_data_length)

    def forward(self, x, h=None):
        if hasattr(self, 'conv'):
            x = self.conv(x)
        if h is None:
            x, h = self.rnn(x, h)
        else:
            x, h = self.rnn(x)
        x = self.linear(x.flatten(1))
        return x, h