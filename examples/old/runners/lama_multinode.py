'''
скрипт для запуска xgboost в режиме multinode:gpu в LightAutoML-gpu
прежде чем запустить скрипт нужно настроить кластер
1. вызвать шедулер в командной строке ``dask-scheduler``
2. далее в терминале должен появиться его адрес (н-р  10.1.16.3:8786)
3. на каждом узле создаем рабочих ``dask-cuda-worker  10.1.16.3:8786``
4. теперь можно запустить скрипт, например:

python ./runners/lama_multinode.py -b ../../data/old_presets -p ../../data/old_presets/data -k airlines -f 2 -n 4 -s 42 -c ../../examples/old/runners/lama_multinode.yml -t 72000 -d 0 -a 10.1.16.3:8786

'''

import argparse

parser = argparse.ArgumentParser()

parser.add_argument('-b', '--bench', type=str)
parser.add_argument('-p', '--path', type=str)

parser.add_argument('-k', '--key', type=str)
parser.add_argument('-f', '--fold', type=int)

parser.add_argument('-n', '--njobs', type=int)
parser.add_argument('-s', '--seed', type=int)
parser.add_argument('-d', '--device', type=str)
parser.add_argument('-c', '--config', type=str)
parser.add_argument('-t', '--timeout', type=int)
parser.add_argument('-a', '--address', type=str)

if __name__ == '__main__':

    import os
    args = parser.parse_args()

    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"] = args.device

    str_nthr = str(args.njobs)

    os.environ["OMP_NUM_THREADS"] = str_nthr
    os.environ["OPENBLAS_NUM_THREADS"] = str_nthr
    os.environ["MKL_NUM_THREADS"] = str_nthr
    os.environ["VECLIB_MAXIMUM_THREADS"] = str_nthr
    os.environ["NUMEXPR_NUM_THREADS"] = str_nthr

    print(args.address)
    from dask.distributed import Client

    client = Client(args.address)

    from lightautoml_gpu.automl.presets.gpu.tabular_gpu_presets import TabularAutoMLGPU
    from lightautoml_gpu.tasks import Task
    from lightautoml_gpu.dataset.roles import TargetRole

    import joblib
    import numpy as np
    import torch
    import pandas as pd
    import cudf

    from time import time
    from sklearn.metrics import roc_auc_score, mean_squared_error

    def cent(y_true, y_pred):

        y_pred = np.clip(y_pred, 1e-7, 1 - 1e-7)
        return - np.log(np.take_along_axis(y_pred, y_true[:, np.newaxis].astype(np.int32), axis=1)).mean()

    torch.set_num_threads(args.njobs)
    np.random.seed(args.seed)

    # paths ..
    data_info = joblib.load(os.path.join(args.bench, 'data_info.pkl'))[args.key]
    folds = joblib.load(os.path.join(args.bench, 'folds', '{0}.pkl'.format(args.key)))

    print('Train dataset {0}, fold {1}'.format(args.key, args.fold))

    # GET DATA AND PREPROCESS
    read_csv_params = {}
    if 'read_csv_params' in data_info:
        read_csv_params = {**read_csv_params, **data_info['read_csv_params']}
        print(read_csv_params)

    data = pd.read_csv(os.path.join(args.path, data_info['path']), **read_csv_params)

    if 'drop' in data_info:
        data.drop(data_info['drop'], axis=1, inplace=True)

    if 'class_map' in data_info:
        data[data_info['target']] = data[data_info['target']].map(data_info['class_map']).values
        assert data[data_info['target']].notnull().all(), 'Class mapping is set unproperly'

    print(data.head())

    results = {}

    # CREATE AUTOML
    client.run(cudf.set_allocator, "managed")
    cudf.set_allocator("managed")

    automl = TabularAutoMLGPU(task=Task(data_info['task_type'],
                                        device="mgpu"),
                              timeout=args.timeout,
                              config_path=args.config,
                              client=client)

    roles = {TargetRole(): data_info['target']}

    # TRAIN
    t = time()
    oof_predictions = automl.fit_predict(data[folds != args.fold].reset_index(drop=True), roles=roles, verbose=4)
    results['train_time'] = time() - t

    # VALID
    t = time()
    test_pred = automl.predict(data[folds == args.fold].reset_index(drop=True)).data
    results['prediction_time'] = time() - t

    # EVALUATE
    if type(test_pred) is not np.ndarray:
        test_pred = test_pred.get()

    if data_info['task_type'] == 'binary':
        results['score'] = roc_auc_score(data[folds == args.fold][data_info['target']].values, test_pred[:, 0])

    if data_info['task_type'] == 'reg':
        results['score'] = mean_squared_error(data[folds == args.fold][data_info['target']].values, test_pred[:, 0])

    if data_info['task_type'] == 'multiclass':
        results['score'] = cent(data[folds == args.fold][data_info['target']].values, test_pred)

    print(results)

    automl.to_cpu()
    cpu_inf = automl.predict(data[folds == args.fold].reset_index().drop(['index'], axis=1)).data
    print("cpu_inf vs test_pred")
    print(cpu_inf)
    print(test_pred)

    from joblib import dump
    import time
    pickle_file = './old_mgpu.joblib'
    start = time.time()
    with open(pickle_file, 'wb') as f:
        dump(automl, f)
    raw_dump_duration = time.time() - start
    print("Raw dump duration: %0.3fs" % raw_dump_duration)

    exit(0)
