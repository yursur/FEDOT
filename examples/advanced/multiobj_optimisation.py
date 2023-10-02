import pandas as pd

from fedot import Fedot
from fedot.core.utils import fedot_project_root
from fedot.core.utils import set_random_seed


def run_classification_multiobj_example(visualization=False, timeout=1, with_tuning=True):
    train_data = pd.read_csv(f'{fedot_project_root()}/examples/data/Hill_Valley_with_noise_Training.data')
    test_data = pd.read_csv(f'{fedot_project_root()}/examples/data/Hill_Valley_with_noise_Testing.data')
    target = test_data['class']
    del test_data['class']
    problem = 'classification'

    metric_names = ['f1', 'node_number']
    auto_model = Fedot(problem=problem, timeout=timeout, preset='best_quality',
                       metric=metric_names,
                       with_tuning=with_tuning)
    auto_model.fit(features=train_data, target='class')
    prediction = auto_model.predict_proba(features=test_data)
    print(auto_model.get_metrics(target))

    if visualization:
        auto_model.plot_prediction()
        auto_model.plot_pareto()

    return prediction


if __name__ == '__main__':
    set_random_seed(42)

    run_classification_multiobj_example(visualization=True)
