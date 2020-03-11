from abc import ABC
import abc

from federatedml.ensemble.boosting.boosting_core import Boosting

from federatedml.param.boosting_tree_param import HeteroBoostingParam
from federatedml.secureprotol import IterativeAffineEncrypt
from federatedml.secureprotol import PaillierEncrypt
from federatedml.secureprotol.encrypt_mode import EncryptModeCalculator
from federatedml.util import consts

from federatedml.feature.binning.quantile_binning import QuantileBinning

from federatedml.util.classify_label_checker import ClassifyLabelChecker
from federatedml.util.classify_label_checker import RegressionLabelChecker

from arch.api.table.eggroll.table_impl import DTable
from arch.api.utils import log_utils

from fate_flow.entity.metric import Metric
from fate_flow.entity.metric import MetricMeta

from federatedml.transfer_variable.transfer_class.hetero_boosting_transfer_variable import \
    HeteroSecureBoostingTransferVariable

import time

LOGGER = log_utils.getLogger()


class HeteroBoosting(Boosting, ABC):

    def __init__(self):
        super(HeteroBoosting, self).__init__()
        self.encrypter = None
        self.encrypted_calculator = None
        self.early_stopping = -1
        self.binning_class = QuantileBinning
        self.model_param = HeteroBoostingParam()
        self.transfer_variable = HeteroSecureBoostingTransferVariable()

    def _init_model(self, param: HeteroBoostingParam):
        super(HeteroBoosting, self)._init_model(param)
        self.encrypt_param = param.encrypt_param
        self.re_encrypt_rate = param.encrypted_mode_calculator_param
        self.calculated_mode = param.encrypted_mode_calculator_param.mode
        self.re_encrypted_rate = param.encrypted_mode_calculator_param.re_encrypted_rate
        self.early_stopping = param.early_stopping

    def generate_encrypter(self):
        LOGGER.info("generate encrypter")
        if self.encrypt_param.method.lower() == consts.PAILLIER.lower():
            self.encrypter = PaillierEncrypt()
            self.encrypter.generate_key(self.encrypt_param.key_length)
        elif self.encrypt_param.method.lower() == consts.ITERATIVEAFFINE.lower():
            self.encrypter = IterativeAffineEncrypt()
            self.encrypter.generate_key(self.encrypt_param.key_length)
        else:
            raise NotImplementedError("encrypt method not supported yes!!!")

        self.encrypted_calculator = EncryptModeCalculator(self.encrypter, self.calculated_mode, self.re_encrypted_rate)

    def check_label(self):

        LOGGER.info("check label")
        classes_ = []
        num_classes, booster_dim = 1, 1
        if self.task_type == consts.CLASSIFICATION:
            num_classes, classes_ = ClassifyLabelChecker.validate_label(self.data_bin)

            if num_classes > 2:
                booster_dim = num_classes

            range_from_zero = True
            for _class in classes_:
                try:
                    if 0 <= _class < len(classes_) and isinstance(_class, int):
                        continue
                    else:
                        range_from_zero = False
                        break
                except:
                    range_from_zero = False

            classes_ = sorted(classes_)
            if not range_from_zero:
                class_mapping = dict(zip(self.classes_, range(self.num_classes)))
                self.y = self.y.mapValues(lambda _class: class_mapping[_class])

        else:
            RegressionLabelChecker.validate_label(self.data_bin)

        return classes_, num_classes, booster_dim


class HeteroBoostingGuest(HeteroBoosting, ABC):

    def __init__(self):
        super(HeteroBoostingGuest, self).__init__()

    def _init_model(self, param):
        super(HeteroBoostingGuest, self)._init_model(param)

    def sync_booster_dim(self):
        LOGGER.info("sync booster_dim to host")

        self.transfer_variable.booster_dim.remote(self.booster_dim,
                                                  role=consts.HOST,
                                                  idx=-1)

    def sync_stop_flag(self, stop_flag, num_round):
        LOGGER.info("sync stop flag to host, boosting_core round is {}".format(num_round))

        self.transfer_variable.stop_flag.remote(stop_flag,
                                                role=consts.HOST,
                                                idx=-1,
                                                suffix=(num_round,))

    def fit(self, data_inst: DTable, validate_data: DTable = None):

        LOGGER.info('begin to fit a hetero boosting model, model is {}'.format(self.model_name))

        self.data_bin, self.bin_split_points, self.bin_sparse_points = self.prepare_data(data_inst)

        self.y = self.get_label(self.data_bin)

        self.classes_, self.num_classes, self.booster_dim = self.check_label()

        self.loss = self.get_loss_function()

        self.sync_booster_dim()

        self.y_hat, self.init_score = self.get_init_score(self.y, self.num_classes)

        self.generate_encrypter()

        self.callback_meta("loss",
                           "train",
                           MetricMeta(name="train",
                                      metric_type="LOSS",
                                      extra_metas={"unit_name": "iters"}))

        self.validation_strategy = self.init_validation_strategy(data_inst, validate_data)

        total_time = 0

        for epoch_idx in range(self.boosting_round):

            LOGGER.debug('cur epoch idx is {}'.format(epoch_idx))

            for class_idx in range(self.booster_dim):

                # fit a booster
                s_time = time.time()
                model = self.fit_a_booster(epoch_idx, class_idx)
                e_time = time.time()
                total_time += e_time - s_time

                booster_meta, booster_param = model.get_model()
                self.booster_meta = booster_meta
                self.boosting_model_list.append(booster_param)

                # update predict score
                cur_sample_weights = model.get_sample_weights()
                self.y_hat = self.get_new_predict_score(self.y_hat, cur_sample_weights, dim=class_idx)

            # compute loss
            loss = self.compute_loss(self.y_hat, self.y)
            self.history_loss.append(loss)
            LOGGER.info("round {} loss is {}".format(epoch_idx, loss))
            self.callback_metric("loss",
                                 "train",
                                 [Metric(epoch_idx, loss)])

            self.run_validation(epoch_idx)
            should_stop = self.check_stop_condition(loss)
            self.sync_stop_flag(should_stop, epoch_idx)
            if should_stop:
                break

        self.callback_meta("loss",
                           "train",
                           MetricMeta(name="train",
                                      metric_type="LOSS",
                                      extra_metas={"Best": min(self.history_loss)}))

        LOGGER.debug('total time is {}'.format(total_time))

    def predict(self, data_inst: DTable):

        LOGGER.info("predicting, there are {} trees".format(len(self.boosting_model_list)))
        data_inst = self.data_alignment(data_inst)
        init_score = self.init_score
        self.predict_y_hat = data_inst.mapValues(lambda v: init_score)
        rounds = len(self.boosting_model_list) // self.booster_dim

        s_time = time.time()

        for idx in range(rounds):
            for booster_idx in range(self.booster_dim):
                model = self.load_booster(self.booster_meta,
                                          self.boosting_model_list[idx * self.booster_dim + booster_idx],
                                          idx, booster_idx)
                score = model.predict(data_inst)
                self.predict_y_hat = self.get_new_predict_score(self.predict_y_hat, score, booster_idx)

        LOGGER.debug('prediction finished')

        e_time = time.time()

        print('predict take time {}'.format(e_time - s_time))

        return self.score_to_predict_result(data_inst, self.predict_y_hat)

    @abc.abstractmethod
    def fit_a_booster(self, epoch_idx: int, booster_dim: int):
        raise NotImplementedError()

    @abc.abstractmethod
    def load_booster(self, model_meta, model_param, epoch_idx, booster_idx):
        raise NotImplementedError()

    @abc.abstractmethod
    def get_model_meta(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def get_model_param(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def set_model_meta(self, model_meta):
        raise NotImplementedError()

    @abc.abstractmethod
    def set_model_param(self, model_param):
        raise NotImplementedError()


class HeteroBoostingHost(HeteroBoosting, ABC):

    def __init__(self):
        super(HeteroBoostingHost, self).__init__()

    def _init_model(self, param):
        super(HeteroBoostingHost, self)._init_model(param)

    def sync_booster_dim(self):
        LOGGER.info("sync booster dim from guest")
        self.booster_dim = self.transfer_variable.booster_dim.get(idx=0)
        LOGGER.info("booster dim is %d" % self.booster_dim)

    def sync_stop_flag(self, num_round):
        LOGGER.info("sync stop flag from guest, boosting_core round is {}".format(num_round))
        stop_flag = self.transfer_variable.stop_flag.get(idx=0,
                                                         suffix=(num_round,))
        return stop_flag

    def fit(self, data_inst: DTable, validate_data: DTable = None):

        LOGGER.info('begin to fit a hetero boosting model, model is {}'.format(self.model_name))

        self.data_bin, self.bin_split_points, self.bin_sparse_points = self.prepare_data(data_inst)
        self.sync_booster_dim()
        self.generate_encrypter()

        self.validation_strategy = self.init_validation_strategy(data_inst, validate_data)

        for epoch_idx in range(self.boosting_round):

            LOGGER.debug('cur epoch idx is {}'.format(epoch_idx))

            for class_idx in range(self.booster_dim):
                # fit a booster
                model = self.fit_a_booster(epoch_idx, class_idx)  # need to implement
                booster_meta, booster_param = model.get_model()
                self.booster_meta = booster_meta
                self.boosting_model_list.append(booster_param)

            self.run_validation(epoch_idx)
            should_stop = self.sync_stop_flag(epoch_idx)
            if should_stop:
                break

    def predict(self, data_inst: DTable):

        LOGGER.info("predicting, there are {} trees".format(len(self.boosting_model_list)))
        data_inst = self.data_alignment(data_inst)
        init_score = self.init_score
        self.predict_y_hat = data_inst.mapValues(lambda v: init_score)
        rounds = len(self.boosting_model_list) // self.booster_dim

        for idx in range(rounds):
            for booster_idx in range(self.booster_dim):
                model = self.load_booster(self.booster_meta,
                                          self.boosting_model_list[idx * self.booster_dim + booster_idx],
                                          idx, booster_idx)
                model.predict(data_inst)

        LOGGER.debug('prediction finished')

    @abc.abstractmethod
    def load_booster(self, model_meta, model_param, epoch_idx, booster_idx):
        raise NotImplementedError()

    @abc.abstractmethod
    def fit_a_booster(self, epoch_idx: int, booster_dim: int):
        raise NotImplementedError()

    @abc.abstractmethod
    def get_model_meta(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def get_model_param(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def set_model_meta(self, model_meta):
        raise NotImplementedError()

    @abc.abstractmethod
    def set_model_param(self, model_param):
        raise NotImplementedError()






