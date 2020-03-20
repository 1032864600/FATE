#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import time

import numpy as np

from arch.api.utils import log_utils
from federatedml.ftl.data_util.common_data_util import overlapping_samples_converter, load_model_parameters, \
    save_model_parameters, convert_instance_table_to_dict, convert_instance_table_to_array, \
    add_random_mask_for_list_of_values, add_random_mask, remove_random_mask_from_list_of_values, remove_random_mask
from federatedml.ftl.data_util.log_util import create_shape_msg
from federatedml.ftl.eggroll_computation.helper import distribute_decrypt_matrix
from federatedml.ftl.encrypted_ftl import EncryptedFTLGuestModel
from federatedml.ftl.encryption.encryption import generate_encryption_key_pair, decrypt_array
from federatedml.ftl.hetero_ftl.hetero_ftl_base import HeteroFTLParty
from federatedml.ftl.plain_ftl import PlainFTLGuestModel
from federatedml.optim.convergence import DiffConverge
from federatedml.param.ftl_param import FTLModelParam
from federatedml.util import consts
from federatedml.util.transfer_variable.hetero_ftl_transfer_variable import HeteroFTLTransferVariable

LOGGER = log_utils.getLogger()


class HeteroFTLGuest(HeteroFTLParty):

    def __init__(self, guest: PlainFTLGuestModel, model_param: FTLModelParam,
                 transfer_variable: HeteroFTLTransferVariable):
        super(HeteroFTLGuest, self).__init__()
        self.guest_model = guest
        self.model_param = model_param
        self.transfer_variable = transfer_variable
        self.max_iter = model_param.max_iter
        self.local_iterations = model_param.local_iterations
        self.n_iter_ = 0
        self.converge_func = DiffConverge(eps=model_param.eps)

    def set_converge_function(self, converge_func):
        self.converge_func = converge_func

    def prepare_data(self, guest_data):
        LOGGER.info("@ start guest prepare_data")
        guest_features_dict, guest_label_dict, guest_sample_indexes = convert_instance_table_to_dict(guest_data)
        guest_sample_indexes = np.array(guest_sample_indexes)
        LOGGER.debug("@ send guest_sample_indexes shape" + str(guest_sample_indexes.shape))
        self._do_remote(guest_sample_indexes,
                        name=self.transfer_variable.guest_sample_indexes.name,
                        tag=self.transfer_variable.generate_transferid(self.transfer_variable.guest_sample_indexes),
                        role=consts.HOST,
                        idx=-1)
        host_sample_indexes = self._do_get(name=self.transfer_variable.host_sample_indexes.name,
                                           tag=self.transfer_variable.generate_transferid(
                                               self.transfer_variable.host_sample_indexes),
                                           idx=-1)[0]

        LOGGER.debug("@ receive host_sample_indexes len" + str(len(host_sample_indexes)))
        guest_features, overlap_indexes, non_overlap_indexes, guest_label = overlapping_samples_converter(
            guest_features_dict, guest_sample_indexes, host_sample_indexes, guest_label_dict)
        return guest_features, overlap_indexes, non_overlap_indexes, guest_label

    def predict(self, guest_data):
        LOGGER.info("@ start guest predict")
        features, labels, instances_indexes = convert_instance_table_to_array(guest_data)
        guest_x = np.squeeze(features)
        guest_y = np.expand_dims(labels, axis=1)
        LOGGER.debug("guest_x, guest_y: " + str(guest_x.shape) + ", " + str(guest_y.shape))

        host_prob = self._do_get(name=self.transfer_variable.host_prob.name,
                                 tag=self.transfer_variable.generate_transferid(self.transfer_variable.host_prob),
                                 idx=-1)[0]

        self.guest_model.set_batch(guest_x, guest_y)
        pred_prob = self.guest_model.predict(host_prob)
        LOGGER.debug("pred_prob: " + str(pred_prob.shape))

        self._do_remote(pred_prob,
                        name=self.transfer_variable.pred_prob.name,
                        tag=self.transfer_variable.generate_transferid(self.transfer_variable.pred_prob),
                        role=consts.HOST,
                        idx=-1)
        return None

    def load_model(self, model_table_name, model_namespace):
        LOGGER.info("@ load guest model from name/ns" + ", " + str(model_table_name) + ", " + str(model_namespace))
        model_parameters = load_model_parameters(model_table_name, model_namespace)
        self.guest_model.restore_model(model_parameters)

    def save_model(self, model_table_name, model_namespace):
        LOGGER.info("@ save guest model to name/ns" + ", " + str(model_table_name) + ", " + str(model_namespace))
        _ = save_model_parameters(self.guest_model.get_model_parameters(), model_table_name, model_namespace)


class HeteroPlainFTLGuest(HeteroFTLGuest):

    def __init__(self, guest: PlainFTLGuestModel, model_param: FTLModelParam,
                 transfer_variable: HeteroFTLTransferVariable):
        super(HeteroPlainFTLGuest, self).__init__(guest, model_param, transfer_variable)

    def fit(self, guest_data):
        LOGGER.info("@ start guest fit")

        guest_x, overlap_indexes, non_overlap_indexes, guest_y = self.prepare_data(guest_data)

        LOGGER.debug("guest_x： " + str(guest_x.shape))
        LOGGER.debug("guest_y： " + str(guest_y.shape))
        LOGGER.debug("overlap_indexes: " + str(len(overlap_indexes)))
        LOGGER.debug("non_overlap_indexes: " + str(len(non_overlap_indexes)))

        self.guest_model.set_batch(guest_x, guest_y, non_overlap_indexes, overlap_indexes)

        start_time = time.time()
        is_stop = False
        while self.n_iter_ < self.max_iter:
            guest_comp = self.guest_model.send_components()
            LOGGER.debug("send guest_comp: " + str(guest_comp[0].shape) + ", " + str(guest_comp[1].shape) + ", " + str(
                guest_comp[2].shape))
            self._do_remote(guest_comp, name=self.transfer_variable.guest_component_list.name,
                            tag=self.transfer_variable.generate_transferid(self.transfer_variable.guest_component_list,
                                                                           self.n_iter_),
                            role=consts.HOST,
                            idx=-1)

            host_comp = self._do_get(name=self.transfer_variable.host_component_list.name,
                                     tag=self.transfer_variable.generate_transferid(
                                         self.transfer_variable.host_component_list, self.n_iter_),
                                     idx=-1)[0]
            LOGGER.debug("receive host_comp: " + str(host_comp[0].shape) + ", " + str(host_comp[1].shape) + ", " + str(
                host_comp[2].shape))
            self.guest_model.receive_components(host_comp, epoch=self.n_iter_)

            loss = self.guest_model.send_loss()
            if self.converge_func.is_converge(loss):
                is_stop = True

            self._do_remote(is_stop, name=self.transfer_variable.is_stopped.name,
                            tag=self.transfer_variable.generate_transferid(self.transfer_variable.is_stopped,
                                                                           self.n_iter_),
                            role=consts.HOST,
                            idx=-1)
            LOGGER.info("@ time: " + str(time.time()) + ", ep:" + str(self.n_iter_) + ", loss:" + str(loss))
            LOGGER.info("@ converged: " + str(is_stop))
            self.n_iter_ += 1
            if is_stop:
                break

        end_time = time.time()
        LOGGER.info("@ running time: " + str(end_time - start_time))


"""
Centralized encryption scheme with an arbiter in the loop for decryption.
"""


class HeteroEncryptFTLGuest(HeteroFTLGuest):

    def __init__(self, guest_model, model_param: FTLModelParam, transfer_variable: HeteroFTLTransferVariable):
        super(HeteroEncryptFTLGuest, self).__init__(guest_model, model_param, transfer_variable)
        self.guest_model: EncryptedFTLGuestModel = guest_model

    def _precompute(self):
        pass

    def fit(self, guest_data):
        LOGGER.info("@ start guest fit")
        public_key = self._do_get(name=self.transfer_variable.paillier_pubkey.name,
                                  tag=self.transfer_variable.generate_transferid(
                                      self.transfer_variable.paillier_pubkey),
                                  idx=-1)[0]

        guest_x, overlap_indexes, non_overlap_indexes, guest_y = self.prepare_data(guest_data)

        LOGGER.debug("guest_x： " + str(guest_x.shape))
        LOGGER.debug("guest_y： " + str(guest_y.shape))
        LOGGER.debug("overlap_indexes: " + str(len(overlap_indexes)))
        LOGGER.debug("non_overlap_indexes: " + str(len(non_overlap_indexes)))

        self.guest_model.set_batch(guest_x, guest_y, non_overlap_indexes, overlap_indexes)
        self.guest_model.set_public_key(public_key)

        start_time = time.time()
        while self.n_iter_ < self.max_iter:

            guest_comp = self.guest_model.send_components()
            LOGGER.debug("send guest_comp: " + create_shape_msg(guest_comp))
            self._do_remote(guest_comp, name=self.transfer_variable.guest_component_list.name,
                            tag=self.transfer_variable.generate_transferid(self.transfer_variable.guest_component_list,
                                                                           self.n_iter_),
                            role=consts.HOST,
                            idx=-1)

            host_comp = self._do_get(name=self.transfer_variable.host_component_list.name,
                                     tag=self.transfer_variable.generate_transferid(
                                         self.transfer_variable.host_component_list, self.n_iter_),
                                     idx=-1)[0]
            LOGGER.debug("receive host_comp: " + create_shape_msg(host_comp))
            self.guest_model.receive_components(host_comp)

            self._precompute()

            encrypt_guest_gradients = self.guest_model.send_gradients()
            LOGGER.debug("send encrypt_guest_gradients: " + create_shape_msg(encrypt_guest_gradients))
            self._do_remote(encrypt_guest_gradients, name=self.transfer_variable.encrypt_guest_gradient.name,
                            tag=self.transfer_variable.generate_transferid(
                                self.transfer_variable.encrypt_guest_gradient, self.n_iter_),
                            role=consts.ARBITER,
                            idx=-1)

            decrypt_guest_gradients = self._do_get(name=self.transfer_variable.decrypt_guest_gradient.name,
                                                   tag=self.transfer_variable.generate_transferid(
                                                       self.transfer_variable.decrypt_guest_gradient, self.n_iter_),
                                                   idx=-1)[0]
            LOGGER.debug("receive decrypt_guest_gradients: " + create_shape_msg(decrypt_guest_gradients))
            self.guest_model.receive_gradients(decrypt_guest_gradients, epoch=self.n_iter_)

            encrypt_loss = self.guest_model.send_loss()
            self._do_remote(encrypt_loss, name=self.transfer_variable.encrypt_loss.name,
                            tag=self.transfer_variable.generate_transferid(self.transfer_variable.encrypt_loss,
                                                                           self.n_iter_),
                            role=consts.ARBITER,
                            idx=-1)

            is_stop = self._do_get(name=self.transfer_variable.is_encrypted_ftl_stopped.name,
                                   tag=self.transfer_variable.generate_transferid(
                                       self.transfer_variable.is_encrypted_ftl_stopped, self.n_iter_),
                                   idx=-1)[0]

            LOGGER.info("@ time: " + str(time.time()) + ", ep: " + str(self.n_iter_) + ", converged：" + str(is_stop))
            self.n_iter_ += 1
            if is_stop:
                break

        end_time = time.time()
        LOGGER.info("@ running time: " + str(end_time - start_time))


"""
Decentralized encryption scheme without arbiter in the loop.
"""


class HeteroDecentralizedEncryptFTLGuest(HeteroFTLGuest):

    def __init__(self, guest_model, model_param: FTLModelParam, transfer_variable: HeteroFTLTransferVariable):
        super(HeteroDecentralizedEncryptFTLGuest, self).__init__(guest_model, model_param, transfer_variable)
        self.guest_model: EncryptedFTLGuestModel = guest_model
        self.public_key = None
        self.private_key = None
        self.host_public_key = None

    def _precompute(self):
        pass

    def prepare_encryption_key_pair(self):
        LOGGER.info("@ start guest prepare encryption key pair")
        self.public_key, self.private_key = generate_encryption_key_pair()
        # exchange public_key with host
        self._do_remote(self.public_key, name=self.transfer_variable.guest_public_key.name,
                        tag=self.transfer_variable.generate_transferid(self.transfer_variable.guest_public_key,
                                                                       self.n_iter_),
                        role=consts.HOST,
                        idx=-1)

        self.host_public_key = self._do_get(name=self.transfer_variable.host_public_key.name,
                                            tag=self.transfer_variable.generate_transferid(
                                                self.transfer_variable.host_public_key, self.n_iter_),
                                            idx=-1)[0]

    def fit(self, guest_data):
        LOGGER.info("@ start guest fit")
        self.prepare_encryption_key_pair()
        guest_x, overlap_indexes, non_overlap_indexes, guest_y = self.prepare_data(guest_data)

        LOGGER.debug("guest_x： " + str(guest_x.shape))
        LOGGER.debug("guest_y： " + str(guest_y.shape))
        LOGGER.debug("overlap_indexes: " + str(len(overlap_indexes)))
        LOGGER.debug("non_overlap_indexes: " + str(len(non_overlap_indexes)))
        LOGGER.debug("converge eps: " + str(self.converge_func.eps))

        self.guest_model.set_batch(guest_x, guest_y, non_overlap_indexes, overlap_indexes)
        self.guest_model.set_public_key(self.public_key)
        self.guest_model.set_host_public_key(self.host_public_key)
        self.guest_model.set_private_key(self.private_key)

        start_time = time.time()
        is_stop = False

        # TODO: refactor self.n_iter_ since this may introduce bug
        while self.n_iter_ < self.max_iter:
            global_iteration_start = time.time()

            # Stage 1: compute and encrypt components (using guest public key) required by host to
            #          calculate gradients.
            LOGGER.debug("@ Stage 1: ")
            exchange_full_time_start = time.time()

            precompute_start_time = time.time()
            guest_comp = self.guest_model.send_components()
            precompute_end_time = time.time()

            exchange_communication_time_start = time.time()
            LOGGER.debug("send enc guest_comp: " + create_shape_msg(guest_comp))
            self._do_remote(guest_comp, name=self.transfer_variable.guest_component_list.name,
                            tag=self.transfer_variable.generate_transferid(self.transfer_variable.guest_component_list,
                                                                           self.n_iter_),
                            role=consts.HOST,
                            idx=-1)

            # Stage 2: receive host components in encrypted form (encrypted by host public key),
            #          calculate guest gradients and loss in encrypted form (encrypted by host public key),
            #          and send them to host for decryption
            LOGGER.debug("@ Stage 2: ")
            host_comp = self._do_get(name=self.transfer_variable.host_component_list.name,
                                     tag=self.transfer_variable.generate_transferid(
                                         self.transfer_variable.host_component_list, self.n_iter_),
                                     idx=-1)[0]
            LOGGER.debug("receive enc host_comp: " + create_shape_msg(host_comp))
            self.guest_model.receive_components(host_comp)
            exchange_communication_time_end = time.time()

            exchange_full_time_end = time.time()

            precompute_time = precompute_end_time - precompute_start_time
            exchange_communication_time = exchange_communication_time_end - exchange_communication_time_start
            exchange_spending_time = exchange_full_time_end - exchange_full_time_start
            LOGGER.debug("guest precompute time {0}".format(precompute_time))
            LOGGER.debug("guest exchange communication time {0}".format(exchange_communication_time))
            LOGGER.debug("guest exchange spending time {0}".format(exchange_spending_time))

            loss = None
            # TODO comm-eft: start local training
            for local_iter in range(self.local_iterations):
                local_iter_start_time = time.time()
                LOGGER.debug("--> guest local computation: {0}".format(local_iter))

                self.guest_model.compute_gradients()

                self._precompute()

                # calculate guest gradients in encrypted form (encrypted by host public key)
                encrypt_guest_gradients = self.guest_model.send_gradients()
                LOGGER.debug("compute encrypt_guest_gradients: " + create_shape_msg(encrypt_guest_gradients))
                encrypt_loss = self.guest_model.send_loss()

                gradient_decryption_start_time = time.time()
                # add random mask to encrypt_guest_gradients and encrypt_loss, and send them to host for decryption
                masked_enc_guest_gradients, gradients_masks = add_random_mask_for_list_of_values(
                    encrypt_guest_gradients)
                masked_enc_loss, loss_mask = add_random_mask(encrypt_loss)

                LOGGER.debug("send masked_enc_guest_gradients: " + create_shape_msg(masked_enc_guest_gradients))
                self._do_remote(masked_enc_guest_gradients, name=self.transfer_variable.masked_enc_guest_gradients.name,
                                tag=self.transfer_variable.generate_transferid(
                                    self.transfer_variable.masked_enc_guest_gradients, self.n_iter_),
                                role=consts.HOST,
                                idx=-1)

                # TODO comm-eft: send encrypted loss to host for decryption.
                # TODO We want to compute the loss on the first local iteration.
                if local_iter == 0:
                    self._do_remote(masked_enc_loss, name=self.transfer_variable.masked_enc_loss.name,
                                    tag=self.transfer_variable.generate_transferid(
                                        self.transfer_variable.masked_enc_loss,
                                        self.n_iter_),
                                    role=consts.HOST,
                                    idx=-1)

                # Stage 3: receive and then decrypt masked encrypted host gradients and send them to guest
                LOGGER.debug("@ Stage 3: ")
                masked_enc_host_gradients = self._do_get(name=self.transfer_variable.masked_enc_host_gradients.name,
                                                         tag=self.transfer_variable.generate_transferid(
                                                             self.transfer_variable.masked_enc_host_gradients,
                                                             self.n_iter_),
                                                         idx=-1)[0]

                masked_dec_host_gradients = self.__decrypt_gradients(masked_enc_host_gradients)

                LOGGER.debug("send masked_dec_host_gradients: " + create_shape_msg(masked_dec_host_gradients))
                self._do_remote(masked_dec_host_gradients, name=self.transfer_variable.masked_dec_host_gradients.name,
                                tag=self.transfer_variable.generate_transferid(
                                    self.transfer_variable.masked_dec_host_gradients, self.n_iter_),
                                role=consts.HOST,
                                idx=-1)

                # Stage 4: receive masked but decrypted guest gradients and loss from host, remove mask,
                #          and update guest model parameters using these gradients.
                LOGGER.debug("@ Stage 4: ")
                masked_dec_guest_gradients = self._do_get(name=self.transfer_variable.masked_dec_guest_gradients.name,
                                                          tag=self.transfer_variable.generate_transferid(
                                                              self.transfer_variable.masked_dec_guest_gradients,
                                                              self.n_iter_),
                                                          idx=-1)[0]
                LOGGER.debug("receive masked_dec_guest_gradients: " + create_shape_msg(masked_dec_guest_gradients))

                cleared_dec_guest_gradients = remove_random_mask_from_list_of_values(masked_dec_guest_gradients,
                                                                                     gradients_masks)

                gradient_decryption_end_time = time.time()
                gradient_decryption_time = gradient_decryption_end_time - gradient_decryption_start_time
                LOGGER.debug("guest local iteration {0} with gradient decryption time {1}".
                             format(local_iter, gradient_decryption_time))

                # update guest model parameters using these gradients.
                self.guest_model.receive_gradients(cleared_dec_guest_gradients, epoch=self.n_iter_)

                # TODO comm-eft: get decrypted loss from host.
                if local_iter == 0:
                    masked_dec_loss = self._do_get(name=self.transfer_variable.masked_dec_loss.name,
                                                   tag=self.transfer_variable.generate_transferid(
                                                       self.transfer_variable.masked_dec_loss, self.n_iter_),
                                                   idx=-1)[0]
                    LOGGER.debug("receive masked_dec_loss: " + str(masked_dec_loss))

                    loss = remove_random_mask(masked_dec_loss, loss_mask)

                # TODO comm-eft: We would not compute components on the last local iteration.
                # TODO This depends on specific algorithm.
                if local_iter != self.local_iterations - 1:
                    self.guest_model.compute_components()

                local_iter_end_time = time.time()
                local_iter_spending_time = local_iter_end_time - local_iter_start_time
                LOGGER.debug("guest local iteration spending time {0}".format(local_iter_spending_time))

            # Stage 5: determine whether training is terminated based on loss and send stop signal to host.
            LOGGER.debug("@ Stage 5: ")
            if self.converge_func.is_converge(loss):
                is_stop = True

            # send is_stop indicator to host
            self._do_remote(is_stop,
                            name=self.transfer_variable.is_decentralized_enc_ftl_stopped.name,
                            tag=self.transfer_variable.generate_transferid(
                                self.transfer_variable.is_decentralized_enc_ftl_stopped, self.n_iter_),
                            role=consts.HOST,
                            idx=-1)

            global_iteration_end = time.time()
            global_iteration_spending_time = global_iteration_end - global_iteration_start
            LOGGER.info("@ time: " + str(time.time()) + ", ep:" + str(self.n_iter_) +
                        ", spending time:" + str(global_iteration_spending_time) + ", loss:" + str(loss))
            LOGGER.info("@ converged: " + str(is_stop))
            self.n_iter_ += 1
            if is_stop:
                break

        end_time = time.time()
        LOGGER.info("@ running time: " + str(end_time - start_time))

    def __decrypt_gradients(self, encrypt_gradients):
        return distribute_decrypt_matrix(self.private_key, encrypt_gradients[0]), decrypt_array(self.private_key,
                                                                                                encrypt_gradients[1])


class GuestFactory(object):

    @classmethod
    def create(cls, ftl_model_param: FTLModelParam, transfer_variable: HeteroFTLTransferVariable, ftl_local_model):
        if ftl_model_param.is_encrypt:
            if ftl_model_param.enc_ftl == "dct_enc_ftl":
                # decentralized encrypted ftl guest
                LOGGER.debug("@ create decentralized encrypted ftl_guest")
                guest_model = EncryptedFTLGuestModel(local_model=ftl_local_model, model_param=ftl_model_param)
                guest = HeteroDecentralizedEncryptFTLGuest(guest_model, ftl_model_param, transfer_variable)
            elif ftl_model_param.enc_ftl == "enc_ftl":
                # encrypted ftl guest
                LOGGER.debug("@ create encrypt ftl_guest")
                guest_model = EncryptedFTLGuestModel(local_model=ftl_local_model, model_param=ftl_model_param)
                guest = HeteroEncryptFTLGuest(guest_model, ftl_model_param, transfer_variable)
            else:
                raise Exception("{0} is not a supported ftl model,".format(ftl_model_param.enc_ftl))
        else:
            # plain ftl guest
            LOGGER.debug("@ create plain ftl_guest")
            guest_model = PlainFTLGuestModel(local_model=ftl_local_model, model_param=ftl_model_param)
            guest = HeteroPlainFTLGuest(guest_model, ftl_model_param, transfer_variable)
        return guest
